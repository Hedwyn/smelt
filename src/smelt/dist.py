"""
Assembles a distribution folder for an application: smelt's own bundler.

Two shapes, selected by `DistPython`:

* `"byo"` ("bring your own python", the default): everything the application itself is
  made of (its modules, its dependencies, the native artifacts smelt built for it),
  but not the interpreter. Running it needs a CPython of the same minor version
  already installed on the target;
* `"own"`: the same folder, plus an interpreter smelt built itself at its root (see
  `smelt.own_python`) and a two-line launcher shim. Nothing has to be installed on
  the target at all.

Either way, see `run_instructions`.

The folder is assembled from two sources:

* every native artifact the regular backend produced (`.so`/`.pyd` from mypyc,
  cython, nuitka, handwritten C and Zig), copied at the package-relative position it
  holds in the source tree;
* every *other* module the entrypoint needs at runtime, compiled to bytecode by the
  `bytecode` backend (see `smelt.bytecode`), so no `.py` is shipped at all.

The *closure* (= the set of modules an entrypoint needs at runtime, i.e. the
transitive closure of the "imports" relation starting from it) is what decides the
contents; the *payload* (= the one subfolder placed on `sys.path`, `PAYLOAD_DIR_NAME`)
is where they go.

The application itself goes into a single subfolder of the distribution
(`PAYLOAD_DIR_NAME`), which is a plain `sys.path` entry holding a `__main__` module --
so `python <folder>/app` is all it takes to run it, with no launcher, no bootstrap and
no `sys.path` manipulation of our own.

That subfolder is not cosmetic. The distribution root has to stay free for things that
are *not* application modules -- the manifest, the run instructions, and later a
launcher executable and a bundled interpreter's own `bin/` and `lib/`. Every one of
those would otherwise be able to collide with a top-level module name: an application
with a `lib` package, or (hit in practice) a launcher named after the very package it
launches.

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Final, Iterable, Iterator, Literal

from smelt.backend import (
    collect_built_artifacts,
    create_entrypoint_script,
    isolation_guard,
    python_version_guard,
    run_backend,
)
from smelt.bytecode import (
    PYC_SUFFIX,
    BytecodeCompilationError,
    PycArtifact,
    PycTargetTag,
    compile_module,
    compile_to_pyc,
)
from smelt.config import EntrypointOptions, SmeltConfig
from smelt.explorer import (
    ModuleKind,
    ResolvedModule,
    build_dependency_graph,
    flatten_dependency_graph,
    iter_package_modules,
    package_directories,
    resolve_module,
)
from smelt.native_deps import (
    BundledNatives,
    bundle_native_dependencies,
    describe_command_failure,
)
from smelt.nuitkaify import Stdout, import_path_search_root
from smelt.own_python import (
    DEFAULT_OWN_PYTHON_TARGET,
    InterpreterRequirements,
    StagedInterpreter,
    build_own_python,
    interpreter_version,
    plan_disabled_libraries,
    resolve_requirements,
    stage_interpreter,
)
from smelt.process import call_command
from smelt.utils import (
    ImportPath,
    PathExists,
    PathSolver,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
    is_valid_import_path,
    path_exists,
)

_logger = logging.getLogger(__name__)

#: Name of the manifest written at the distribution root. Records what the
#: distribution is made of, and -- load-bearing -- which interpreter it needs: bytecode
#: is version-locked, so a wrong interpreter otherwise fails with a bare
#: `RuntimeError: Bad magic number in .pyc file`.
MANIFEST_NAME: str = "smelt-dist.json"

#: Name of the human-readable run instructions written next to the manifest.
INSTRUCTIONS_NAME: str = "HOW_TO_RUN.txt"

#: Module name Python looks for when a directory is passed as the script to run.
MAIN_MODULE_NAME: str = "__main__"

#: Subfolder of the distribution holding the application: the one directory that ends
#: up on `sys.path`. Keeping it out of the root is what stops an application module
#: from colliding with the distribution's own files (see this module's docstring).
PAYLOAD_DIR_NAME: Final[str] = "app"

type ArtifactOrigin = Literal["smelt", "environment"]

#: Which interpreter a distribution runs on. `"byo"` ("bring your own") leaves it to
#: the target machine, `"own"` ships one smelt built itself (see `smelt.own_python`),
#: so the folder runs where no Python is installed at all.
type DistPython = Literal["byo", "own"]

DEFAULT_DIST_PYTHON: Final[DistPython] = "byo"

#: Every valid `DistPython` value, spelled out so a string read from `pyproject.toml`
#: or the CLI can be matched against them rather than asserted to be one of them.
DIST_PYTHON_MODES: Final[tuple[DistPython, ...]] = ("byo", "own")

#: Names at the distribution root a mode `own` launcher must not take: the payload
#: directory, the interpreter's own prefix directories, and the two metadata files.
#: Checked rather than assumed -- writing a launcher over an existing directory fails
#: obscurely (a launcher named after the very package it launches once failed with
#: `ld.lld: cannot open output file: Is a directory`), and overwriting `bin` or the
#: manifest would break the distribution silently.
LAUNCHER_RESERVED_NAMES: Final[tuple[str, ...]] = (
    PAYLOAD_DIR_NAME,
    "bin",
    "lib",
    MANIFEST_NAME,
    INSTRUCTIONS_NAME,
)

#: How the modules a distribution must ship are discovered. `"static"` reads the
#: source's own import statements, `"trace"` imports the entrypoint in a subprocess
#: and reports what actually ended up in `sys.modules`, `"both"` unions them.
type DiscoveryMode = Literal["static", "trace", "both"]

DEFAULT_DISCOVERY: Final[DiscoveryMode] = "both"

#: Whether a mode `own` interpreter's contents follow the application's dependency
#: closure (see `own_python.resolve_requirements`) rather than being the whole standard
#: library and every extension module.
#:
#: **On by default**, and the trade-off deserves stating rather than hiding: tailoring
#: is what makes a mode `own` folder a reasonable size, and the alternative default
#: would mean nobody ever gets the win without knowing the flag exists. What it risks
#: is turning a *discovery* gap into a runtime `ImportError` -- a standard library
#: module imported lazily, on a path not taken at import time, under a name absent from
#: the source is invisible to both discovery methods, and an untailored interpreter
#: would have carried it anyway. Three things make that acceptable: the default
#: discovery mode is the union of both methods (a stdlib module imported anywhere in
#: any reachable source is kept, whether that import ever runs or not), `ALWAYS_KEEP`
#: covers the modules that are structurally invisible to a closure, and
#: `--include-module` forces back anything found missing. `--no-tailor-interpreter`
#: restores the untailored interpreter wholesale.
DEFAULT_TAILOR_INTERPRETER: Final[bool] = True

#: How long the tracing subprocess is given before it is given up on. Importing an
#: entrypoint is normally near-instantaneous; a module that blocks on import (opening
#: a socket, waiting on a lock) would otherwise hang the build indefinitely.
TRACE_TIMEOUT: Final[float] = 120.0

#: The metadata files worth shipping out of a `.dist-info` directory. `METADATA`
#: backs `importlib.metadata.version()`, `entry_points.txt` backs `entry_points()`.
#: `RECORD` is deliberately left out: it lists installed files with hashes, none of
#: which describe the distribution folder it would end up in.
DISTRIBUTION_METADATA_FILES: Final[tuple[str, ...]] = (
    "METADATA",
    "PKG-INFO",
    "entry_points.txt",
    "top_level.txt",
)


class DistError(SmeltError):
    """
    Raised when a distribution folder cannot be assembled.
    """


@dataclass(frozen=True)
class NativeArtifact:
    """
    One native file copied into the distribution.

    `import_path` is the module the artifact belongs to; for a shared runtime it is
    the module that needs it, not an importable name of its own. `origin`
    distinguishes an artifact smelt built in this run from one taken from the
    environment (a third-party wheel's extension module).
    """

    import_path: ImportPath
    source: PathExists
    dest_rel_path: Path
    origin: ArtifactOrigin


@dataclass
class DistReport:
    """
    What went into a distribution folder, and what was deliberately left out.

    `skipped` is not a list of failures: most entries are modules that must *not* be
    shipped (the standard library, builtins, frozen modules), and it is kept only so
    that the decision taken for every module the closure reached is inspectable.
    """

    dist_root: Path
    entrypoint: str
    entrypoint_module: ImportPath
    tag: PycTargetTag
    discovery: DiscoveryMode = DEFAULT_DISCOVERY
    entrypoint_file: Path = Path(f"{MAIN_MODULE_NAME}{PYC_SUFFIX}")
    bytecode: list[PycArtifact] = field(default_factory=list)
    natives: list[NativeArtifact] = field(default_factory=list)
    data_files: list[DataFile] = field(default_factory=list)
    metadata_files: list[DataFile] = field(default_factory=list)
    native_deps: BundledNatives = field(default_factory=BundledNatives)
    skipped: dict[ImportPath, str] = field(default_factory=dict)
    #: The interpreter shipped inside the folder, in mode `own` only.
    interpreter: StagedInterpreter | None = None
    #: Distribution-root-relative path of the mode `own` launcher shim, if written.
    launcher: Path | None = None

    @property
    def payload_root(self) -> Path:
        """
        The distribution's `sys.path` entry, i.e. where its modules live.
        """
        return self.dist_root / PAYLOAD_DIR_NAME

    @property
    def python(self) -> DistPython:
        """
        Which interpreter this distribution runs on (see `DistPython`).
        """
        return "own" if self.interpreter is not None else "byo"

    def render(self) -> str:
        lines = [
            f"Distribution: {self.dist_root}",
            f"Payload:      {self.payload_root}",
            f"Entrypoint:   {self.entrypoint}",
            f"Python:       {self.tag.version_string} "
            f"(optimize={self.tag.optimize}, magic={self.tag.magic_number.hex()})",
            f"Discovery:    {self.discovery}",
            f"Bytecode modules:     {len(self.bytecode)}",
            f"Native artifacts:     {len(self.natives)}",
            f"Bundled libraries:    {len(self.native_deps.dependencies)}"
            + (
                ""
                if self.native_deps.resolved
                else f" (not resolved: {self.native_deps.unsupported})"
            ),
            f"Data files:           {len(self.data_files)}",
            f"Distribution metadata: {len(self.metadata_files)} file(s)",
            f"Skipped modules:      {len(self.skipped)}",
        ]
        if self.interpreter is not None:
            lines.append(self.interpreter.render())
        if self.launcher is not None:
            lines.append(f"Launcher:     ./{self.launcher}")
        for artifact in self.natives:
            lines.append(f"  [native:{artifact.origin}] {artifact.dest_rel_path}")
        for basename in sorted(self.native_deps.dependencies):
            lines.append(f"  [library] {basename}")
        return "\n".join(lines)

    def serialize(self) -> dict[str, object]:
        return {
            "name": self.dist_root.name,
            "mode": self.python,
            "interpreter": None if self.interpreter is None else self.interpreter.serialize(),
            "launcher": None if self.launcher is None else self.launcher.as_posix(),
            # Every path below is relative to this, not to the distribution root:
            # it is the directory that goes on `sys.path`.
            "payload": PAYLOAD_DIR_NAME,
            "entrypoint": self.entrypoint,
            "entrypoint_module": self.entrypoint_module,
            "entrypoint_file": str(self.entrypoint_file),
            "discovery": self.discovery,
            "python": {
                **self.tag.serialize(),
                "implementation": platform.python_implementation(),
                "platform": platform.system().lower(),
                "machine": platform.machine(),
                "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
            },
            "bytecode_modules": {
                artifact.import_path: str(artifact.dest_rel_path) for artifact in self.bytecode
            },
            "native_artifacts": [
                {
                    "import_path": artifact.import_path,
                    "path": str(artifact.dest_rel_path),
                    "origin": artifact.origin,
                }
                for artifact in self.natives
            ],
            "bundled_libraries": {
                basename: str(source)
                for basename, source in sorted(self.native_deps.dependencies.items())
            },
            "native_dependencies_resolved": self.native_deps.resolved,
            "data_files": [str(data_file.dest_rel_path) for data_file in self.data_files],
            "metadata_files": [str(entry.dest_rel_path) for entry in self.metadata_files],
            "skipped": self.skipped,
        }


def resolve_entrypoint_spec(config: SmeltConfig, entrypoint: str | None) -> str:
    """
    Resolves `entrypoint` -- a `[project.scripts]` name, a bare module path, or a full
    `module.path:func_name` spec -- into one of `config.entrypoints`' own keys.

    Omitting it is only valid when the project declares exactly one entrypoint: a
    distribution folder holds a single `__main__`, so unlike `run_backend` (which
    happily builds every entrypoint it is given) there is nothing sensible to default
    to when there are several.
    """
    if not config.entrypoints:
        raise DistError(
            "No entrypoint declared. Add one to [project.scripts] or to [tool.smelt.entrypoints]."
        )
    if entrypoint is None:
        if len(config.entrypoints) > 1:
            raise DistError(
                "Several entrypoints are declared, pick one with --entrypoint. "
                f"Available entrypoints: {list(config.entrypoints)}. "
                f"Available script names: {list(config.script_names)}"
            )
        (only_entrypoint,) = config.entrypoints
        return only_entrypoint

    resolved = config.script_names.get(entrypoint, entrypoint)
    if resolved not in config.entrypoints:
        raise DistError(
            f"Unknown entrypoint {entrypoint!r}. "
            f"Available entrypoints: {list(config.entrypoints)}. "
            f"Available script names: {list(config.script_names)}"
        )
    return resolved


def dist_folder_name(config: SmeltConfig, entrypoint_spec: str) -> str:
    """
    Name of the distribution folder for `entrypoint_spec`: the `[project.scripts]`
    name it was declared under where there is one (that is the name users know the
    application by), falling back to the entrypoint function, then to the module.
    """
    for script_name, target in config.script_names.items():
        if target == entrypoint_spec:
            return f"{script_name}.dist"
    module_path, _, func_name = entrypoint_spec.partition(":")
    return f"{func_name or module_path.split('.')[-1]}.dist"


def launcher_name(config: SmeltConfig, entrypoint_spec: str) -> str:
    """
    Name of the mode `own` launcher for `entrypoint_spec`: the same name the
    distribution folder is named after, so `myapp.dist/myapp` is what users run.
    """
    return dist_folder_name(config, entrypoint_spec).removesuffix(".dist")


#: The mode `own` launcher, POSIX flavour. No compiler, no C: everything that has to
#: be enforced (isolation, the version check) already lives in the generated
#: `__main__`, so all this has to do is find the folder it sits in and hand the payload
#: directory to the bundled interpreter.
#:
#: No interpreter flags either: `__main__`'s own isolation guard re-executes with
#: `-I -S -B`, and `sys.executable` is by then the bundled interpreter -- so the
#: re-execution stays inside the folder.
#:
#: Two ways of finding the folder, and both are needed. `readlink -f` is the one that
#: is *correct*: a launcher is routinely reached through a symlink into `~/.local/bin`
#: or named in a systemd `ExecStart=`, and only resolving the real file finds the
#: distribution. But a distribution shipping its own interpreter is exactly the one
#: that gets run on a machine with nothing on it -- `env -i PATH=/nonexistent ./myapp`
#: is a case this is verified against, and there `readlink` and `dirname` are not
#: callable at all. So `readlink` is used when it is there, and stripping `$0`'s last
#: component (shell parameter expansion, no external command) is the fallback. That
#: covers a plain or relative invocation with no PATH, and a symlinked one with one.
#: `command -v` is a shell builtin, so the probe itself costs nothing and cannot fail
#: for the same reason.
_POSIX_LAUNCHER_TEMPLATE: Final[str] = """\
#!/bin/sh
self=$0
if command -v readlink >/dev/null 2>&1; then
    resolved=$(readlink -f "$self" 2>/dev/null) && self=$resolved
fi
case $self in
    */*) here=${{self%/*}} ;;
    *) here=. ;;
esac
exec "$here/{interpreter}" "$here/{payload}" "$@"
"""

#: The same launcher for Windows. `%~dp0` is the batch expansion for "the directory
#: this script lives in" and already carries a trailing separator.
_WINDOWS_LAUNCHER_TEMPLATE: Final[str] = """\
@echo off
"%~dp0{interpreter}" "%~dp0{payload}" %*
"""

#: Suffix of the Windows launcher written next to the POSIX one.
WINDOWS_LAUNCHER_SUFFIX: Final[str] = ".cmd"


def write_launcher_shim(
    name: str,
    dist_root: Path,
    interpreter: StagedInterpreter,
) -> Path:
    """
    Writes the mode `own` launcher `dist_root/name` (plus a `.cmd` sibling for
    Windows) and returns its distribution-relative path.

    Both are generated text, deliberately: a compiled launcher would arch-lock a
    folder for no gain here, since it would have nothing to do that the generated
    `__main__` does not already do. It is a convenience, not a requirement --
    `myapp.dist/bin/python myapp.dist/app` runs the distribution just as well.

    Raises
    ------
    DistError
        If `name` collides with anything the distribution needs at its root
        (`LAUNCHER_RESERVED_NAMES`), or with an entry already there.
    """
    if name in LAUNCHER_RESERVED_NAMES:
        raise DistError(
            f"A launcher cannot be named {name!r}: the distribution root needs that "
            f"name for itself. Reserved names: {list(LAUNCHER_RESERVED_NAMES)}."
        )
    dest = dist_root / name
    if dest.exists():
        raise DistError(
            f"A launcher cannot be named {name!r}: {dest} already exists. That is "
            "usually a launcher named after the very package it launches, whose "
            "directory is already at the distribution root."
        )
    substitutions = {
        "interpreter": interpreter.executable_rel_path.as_posix(),
        "payload": PAYLOAD_DIR_NAME,
    }
    dest.write_text(_POSIX_LAUNCHER_TEMPLATE.format(**substitutions))
    dest.chmod(0o755)
    windows_dest = dist_root / f"{name}{WINDOWS_LAUNCHER_SUFFIX}"
    windows_dest.write_text(
        _WINDOWS_LAUNCHER_TEMPLATE.format(
            **{**substitutions, "interpreter": str(interpreter.executable_rel_path)}
        )
    )
    windows_dest.chmod(0o755)
    return dest.relative_to(dist_root)


def resolve_dist_python(
    entrypoint_options: EntrypointOptions,
    python: DistPython | None = None,
) -> DistPython:
    """
    Which interpreter the distribution runs on: `python` where the caller named one
    (the CLI wins over the declaration), then the entrypoint's own `python` option,
    then `DEFAULT_DIST_PYTHON`.
    """
    declared = python or entrypoint_options.get("python", DEFAULT_DIST_PYTHON)
    for mode in DIST_PYTHON_MODES:
        if declared == mode:
            return mode
    raise DistError(
        f"Invalid python mode {declared!r}, expected one of 'byo' (bring your own "
        "interpreter, the default) or 'own' (ship one smelt builds itself)."
    )


def resolve_tailor_interpreter(
    entrypoint_options: EntrypointOptions,
    tailor_interpreter: bool | None = None,
) -> bool:
    """
    Whether the mode `own` interpreter's contents follow the application's closure:
    `tailor_interpreter` where the caller decided (the CLI wins over the declaration),
    then the entrypoint's own `tailor-interpreter` option, then
    `DEFAULT_TAILOR_INTERPRETER`.
    """
    if tailor_interpreter is not None:
        return tailor_interpreter
    declared = entrypoint_options.get("tailor-interpreter", DEFAULT_TAILOR_INTERPRETER)
    if not isinstance(declared, bool):
        raise DistError(
            f"Invalid tailor-interpreter {declared!r}, expected a boolean: true (the "
            "default, ship only what the application needs) or false (ship the whole "
            "standard library)."
        )
    return declared


def assert_no_version_skew(tag: PycTargetTag, interpreter_version: tuple[int, int]) -> None:
    """
    Refuses a shipped interpreter whose minor version differs from the one that
    compiled the distribution's bytecode and extension modules.

    Both are hard couplings and neither degrades gracefully: the `.pyc` magic number
    is checked before any code runs, and the extension modules are compiled against a
    specific C ABI. A *patch* difference is fine and proven -- 3.12.12-built artifacts
    run under a 3.12.13 interpreter, because the magic number is per minor version and
    the C ABI is stable within one.
    """
    if tag.python_version == interpreter_version:
        return
    major, minor = interpreter_version
    raise DistError(
        f"The interpreter to ship is CPython {major}.{minor}, but the distribution's "
        f"bytecode and extension modules were built by CPython {tag.version_string}. "
        "That combination cannot run: the bytecode magic number is checked before any "
        "code executes, and the extension modules are compiled against a specific C "
        f"ABI. Build the distribution from a CPython {major}.{minor} environment, or "
        "point --own-python-target at a matching build."
    )


def _package_prefixes(import_path: ImportPath) -> Iterable[ImportPath]:
    """
    Every package `import_path` lives under, shallowest first: importing `a.b.c`
    requires `a` and `a.b` to be importable too, so a distribution shipping the former
    must ship the latter.
    """
    parts = import_path.split(".")
    for depth in range(1, len(parts)):
        yield assert_is_valid_import_path(".".join(parts[:depth]))


def project_search_paths(path_solver: PathSolver) -> list[str]:
    """
    The `sys.path` entries needed for the project's own packages to be importable.

    A distribution is routinely built for a project that is not installed in the
    environment running smelt (a `src` layout, a build hook running before the wheel
    exists, a CI job), and module discovery goes through the import machinery -- so the
    package roots `packages_location` declares have to be made importable first, or
    the entrypoint's own package resolves to nothing at all.
    """
    search_paths: list[str] = []
    for root_import_path, root_path in path_solver.known_roots:
        search_paths.append(
            str(import_path_search_root(root_import_path, root_path, is_package=True))
        )
    if not search_paths:
        # No `packages_location` declared: fall back to the two conventional layouts,
        # matching what `find_module_in_layout` already assumes elsewhere.
        project_root = path_solver.project_root
        search_paths.extend(
            str(candidate)
            for candidate in (project_root / "src", project_root)
            if candidate.is_dir()
        )
    return search_paths


@contextmanager
def _search_paths_prepended(paths: Iterable[str]) -> Iterator[None]:
    """
    Temporarily prepends `paths` to `sys.path`, so the import machinery resolves the
    project's own modules while the closure is being explored.

    Import caches are invalidated on the way in: the backend has just written fresh
    artifacts into those very directories, and the finders cache their directory
    listings -- without this, a module built moments ago in this same process can
    still resolve to its source (or to nothing at all).
    """
    added = [path for path in paths if path not in sys.path]
    sys.path[:0] = added
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for path in added:
            sys.path.remove(path)


@contextmanager
def _pythonpath_prepended(paths: Iterable[str]) -> Iterator[None]:
    """
    Temporarily prepends `paths` to `PYTHONPATH`, for a subprocess to inherit.
    """
    added = [path for path in paths]
    previous = os.environ.get("PYTHONPATH")
    combined = os.pathsep.join([*added, *([previous] if previous else [])])
    if combined:
        os.environ["PYTHONPATH"] = combined
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous


def trace_imported_modules(
    entrypoint_module: ImportPath,
    search_paths: Iterable[str] = (),
    timeout: float = TRACE_TIMEOUT,
) -> set[ImportPath]:
    """
    Imports `entrypoint_module` in a fresh interpreter and reports every module that
    ended up in `sys.modules` as a result.

    This is what static analysis structurally cannot do: a module imported through
    `importlib` on a computed name, chosen by a plugin registry, or selected per
    platform at import time is invisible in the source but perfectly visible here.
    `pyzmq`, for instance, picks its backend by name -- so the extension module that
    makes it work is only ever found this way.

    The trade-off is real and worth stating: this *executes* the entrypoint's
    module-level code. It runs in a subprocess, so it cannot disturb the build, and it
    imports the module under its own name rather than as `__main__`, so an
    `if __name__ == "__main__":` guard does not fire -- but a module whose import has
    side effects will have them. A failed trace is not a build failure: it is logged
    and discovery falls back on what the source says.

    Results come back through a temp file rather than stdout, since the imported code
    is free to print whatever it likes.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_file:
        output_path = tmp_file.name
    try:
        script = (
            "import importlib, json, sys\n"
            f"importlib.import_module({entrypoint_module!r})\n"
            "found = sorted(name for name, mod in sys.modules.items() if mod is not None)\n"
            f"with open({output_path!r}, 'w') as f:\n"
            "    json.dump(found, f)\n"
        )
        with _pythonpath_prepended(search_paths):
            cmd_trace = call_command(sys.executable, "-c", script, timeout=timeout)
        if cmd_trace.exit_code != 0:
            _logger.warning(
                "Could not trace the imports of %s, falling back on static discovery "
                "only (modules imported dynamically may be missing): %s",
                entrypoint_module,
                describe_command_failure(cmd_trace, (sys.executable, "-c", "<trace script>")),
            )
            return set()
        with open(output_path) as f:
            names: list[str] = json.load(f)
    finally:
        os.unlink(output_path)
    return {
        assert_is_valid_import_path(name)
        for name in names
        if is_valid_import_path(name) and name != "__main__"
    }


def _is_excluded(import_path: ImportPath, excluded: Iterable[str]) -> bool:
    """
    Whether `import_path` is one of `excluded`, or lives under one of them: excluding
    a package excludes everything it contains.
    """
    return any(
        import_path == exclusion or import_path.startswith(f"{exclusion}.")
        for exclusion in excluded
    )


def collect_closure(
    entrypoint_module: ImportPath,
    search_paths: Iterable[str] = (),
    extra_modules: Iterable[str] = (),
    extra_packages: Iterable[str] = (),
    discovery: DiscoveryMode = DEFAULT_DISCOVERY,
    exclude_modules: Iterable[str] = (),
) -> dict[ImportPath, ResolvedModule]:
    """
    Resolves every module reachable from `entrypoint_module`, itself included, along
    with the packages they live under, with `search_paths` made importable for the
    duration.

    `discovery` selects how modules are found (see `DiscoveryMode`). The two methods
    fail in opposite directions, which is why the default unions them: the static
    graph sees imports that only happen on some platform or in some branch, and misses
    every dynamic one; the trace sees exactly what one real import actually needed, and
    nothing conditional it did not take.

    `extra_modules` and `extra_packages` force modules in regardless -- both are walked
    like the entrypoint itself, and `extra_packages` additionally sweeps in every
    module its package contains. `exclude_modules` cuts names (and everything under
    them) back out, for trees that were reached but are not needed at runtime.
    """
    with _search_paths_prepended(search_paths):
        roots = {entrypoint_module}
        roots.update(assert_is_valid_import_path(module) for module in extra_modules)
        for package in extra_packages:
            roots.update(iter_package_modules(assert_is_valid_import_path(package)))

        reachable: set[ImportPath] = set(roots)
        if discovery in ("static", "both"):
            for root in roots:
                graph = build_dependency_graph(root)
                reachable.update(node.name for node in flatten_dependency_graph(graph))
        if discovery in ("trace", "both"):
            reachable.update(trace_imported_modules(entrypoint_module, search_paths))

        with_parents = set(reachable)
        for import_path in reachable:
            with_parents.update(_package_prefixes(import_path))

        excluded = list(exclude_modules)
        kept = {
            import_path
            for import_path in with_parents
            # the entrypoint is never excluded: without it there is nothing to run
            if import_path == entrypoint_module or not _is_excluded(import_path, excluded)
        }
        return {import_path: resolve_module(import_path) for import_path in sorted(kept)}


def _native_dest_rel_path(import_path: ImportPath, artifact: Path) -> Path:
    """
    Distribution-relative destination of a native `artifact` belonging to
    `import_path`: the same package-relative position it holds in the source tree.

    Keeping that position is a requirement, not tidiness -- mypyc's shared runtime is
    imported under the module's own package (`pkg.mod__mypyc`), and smelt's shared
    Nuitka runtime is resolved through an `$ORIGIN` rpath, i.e. relative to the
    directory of the `.so` that needs it.
    """
    packages = import_path.split(".")[:-1]
    return Path(*packages, artifact.name)


def _copy_native(
    import_path: ImportPath,
    source: PathExists,
    dist_root: Path,
    origin: ArtifactOrigin,
) -> NativeArtifact:
    dest_rel_path = _native_dest_rel_path(import_path, source)
    dest = dist_root / dest_rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return NativeArtifact(
        import_path=import_path,
        source=source,
        dest_rel_path=dest_rel_path,
        origin=origin,
    )


#: Suffixes that are *code*, never data. Extension modules are excluded here on top
#: of that because they are handled as native artifacts, which additionally get their
#: own dependencies resolved and their RPATH rewritten -- a blind copy through this
#: path would place one with neither.
PACKAGE_DATA_CODE_SUFFIXES: Final[tuple[str, ...]] = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyi",
    ".so",
    ".pyd",
    ".dll",
    ".dylib",
)


@dataclass(frozen=True)
class DataFile:
    """
    One non-code file copied into the distribution, at the package-relative position
    the code reading it expects to find it at.
    """

    source: PathExists
    dest_rel_path: Path


def collect_package_data(
    specs: Iterable[str],
    dist_root: Path,
    search_paths: Iterable[str] = (),
) -> list[DataFile]:
    """
    Copies the data files of each `PACKAGE[:PATTERN,...]` spec into `dist_root` (the
    distribution's payload directory), under the package's own directory.

    Data files are collected only when asked for, never by default: a package
    directory can hold anything at all (test fixtures, sample datasets, caches), so
    guessing would silently bloat distributions -- while a missing data file surfaces
    immediately, on whichever code path first reads it.

    Every file under the package's own directory is copied except code
    (`PACKAGE_DATA_CODE_SUFFIXES`) and `__pycache__`. A `:`-suffixed comma-separated
    pattern list narrows that to file *names* matching one of the `fnmatch` patterns.
    """
    collected: list[DataFile] = []
    with _search_paths_prepended(search_paths):
        for spec in specs:
            package, _, patterns_decl = spec.partition(":")
            patterns = [pattern for pattern in patterns_decl.split(",") if pattern]
            directories = package_directories(assert_is_valid_import_path(package))
            if not directories:
                _logger.warning("Skipping data files for %r: not an importable package", package)
                continue
            dest_root = Path(*package.split("."))
            for directory in directories:
                for source in sorted(directory.rglob("*")):
                    if not source.is_file() or "__pycache__" in source.parts:
                        continue
                    if source.suffix in PACKAGE_DATA_CODE_SUFFIXES:
                        continue
                    if patterns and not any(fnmatch(source.name, p) for p in patterns):
                        continue
                    dest_rel_path = dest_root / source.relative_to(directory)
                    dest = dist_root / dest_rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    collected.append(DataFile(assert_path_exists(source), dest_rel_path))
    return collected


def collect_distribution_metadata(
    import_paths: Iterable[ImportPath],
    dist_root: Path,
    extra_distributions: Iterable[str] = (),
    search_paths: Iterable[str] = (),
) -> list[DataFile]:
    """
    Copies the installation metadata of every distribution owning one of
    `import_paths` into `dist_root` (the distribution's payload directory), keeping
    each `*.dist-info` directory's own name.

    Collected by default, unlike data files, because the cost is a few small text
    files and the failure it prevents is common: `importlib.metadata.version(...)` and
    `entry_points()` are ordinary runtime calls (every plugin system, every `--version`
    flag built that way), and they resolve against `*.dist-info` directories found on
    `sys.path`. With the distribution folder itself being that `sys.path` entry,
    copying them in is all it takes for those calls to keep working.

    `extra_distributions` names distributions to include beyond the ones owning a
    shipped module -- for metadata read for a package whose modules were not shipped.
    """
    with _search_paths_prepended(search_paths):
        top_levels = {import_path.partition(".")[0] for import_path in import_paths}
        owners = importlib.metadata.packages_distributions()
        wanted = {name for top_level in top_levels for name in owners.get(top_level, ())} | set(
            extra_distributions
        )

        collected: list[DataFile] = []
        for name in sorted(wanted):
            try:
                distribution = importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError:
                _logger.warning("Skipping metadata for %r: distribution not found", name)
                continue
            files = distribution.files
            if files is None:
                # No RECORD (a legacy or partially installed distribution): there is
                # no reliable way to enumerate its metadata files.
                _logger.debug("Skipping metadata for %r: distribution lists no files", name)
                continue
            for entry in files:
                if not entry.parts or not entry.parts[0].endswith((".dist-info", ".egg-info")):
                    continue
                if entry.name not in DISTRIBUTION_METADATA_FILES:
                    continue
                source = Path(str(distribution.locate_file(entry)))
                if not path_exists(source):
                    continue
                dest_rel_path = Path(*entry.parts)
                dest = dist_root / dest_rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                collected.append(DataFile(source, dest_rel_path))
    return collected


def write_entrypoint_module(
    entrypoint_spec: str,
    dist_root: Path,
    *,
    tag: PycTargetTag,
    guard_version: bool = True,
    isolate: bool = True,
    optimize: int = -1,
) -> Path:
    """
    Writes the distribution's `__main__` module into `dist_root` (the payload
    directory -- `__main__` has to sit in the directory that goes on `sys.path`).

    For a `module.path:func_name` entrypoint this is the same wrapper the Nuitka path
    uses (`create_entrypoint_script`), with the guards `guard_version` and `isolate`
    ask for (`backend.python_version_guard`, `backend.isolation_guard`). An entrypoint
    given as a bare module path has no function to call, so it is run the way
    `python -m` would run it.

    **Shipped as source when guarded, as bytecode otherwise.** A version guard held in
    a `.pyc` could never run: the interpreter checks the bytecode magic before
    executing anything, so a mismatched one rejects the guard along with everything
    else it was meant to explain. Source compiles under any version, which is exactly
    what a guard needs. This one generated wrapper is therefore the only `.py` in the
    distribution -- and it is smelt's own glue, not application code, so it gives
    nothing away.
    """
    guards = []
    if guard_version:
        guards.append(python_version_guard(tag.python_version, tag.magic_number))
    if isolate:
        guards.append(isolation_guard())

    module_path, sep, _ = entrypoint_spec.partition(":")
    if guards:
        if sep:
            return create_entrypoint_script(
                entrypoint_spec, dist_root, guards=guards, script_name=MAIN_MODULE_NAME
            )
        # A bare module path has no function to import, so the guards go in front of a
        # `runpy` call instead of an import.
        imports = sorted({"sys", *(name for guard in guards for name in guard.imports)})
        blocks = [
            "\n".join(f"import {name}" for name in [*imports, "runpy"]),
            *(guard.code for guard in guards),
            'if __name__ == "__main__":\n'
            f'    runpy.run_module("{module_path}", run_name="__main__", alter_sys=True)',
        ]
        dest = dist_root / f"{MAIN_MODULE_NAME}.py"
        dest.write_text("\n\n".join(blocks) + "\n")
        return dest

    with tempfile.TemporaryDirectory() as scratch_dir:
        if sep:
            script = create_entrypoint_script(entrypoint_spec, scratch_dir)
        else:
            script = Path(scratch_dir) / "__main__.py"
            script.write_text(
                "import runpy\n"
                "\n"
                'if __name__ == "__main__":\n'
                f'    runpy.run_module("{module_path}", run_name="__main__", alter_sys=True)\n'
            )
        dest = dist_root / f"{MAIN_MODULE_NAME}{PYC_SUFFIX}"
        compile_to_pyc(assert_path_exists(script), dest, optimize=optimize)
    return dest


def run_instructions(report: DistReport) -> str:
    """
    How to run the assembled distribution: what to install first, how to invoke it,
    and what is deliberately left to the target machine.

    Two genuinely different answers, so two texts rather than one with conditionals
    sprinkled through: mode `byo` needs a matching interpreter installed and explains
    at length why the version has to match, while mode `own` needs nothing installed
    and its interesting caveat is a different one (libc).
    """
    if report.interpreter is not None:
        return _own_python_run_instructions(report, report.interpreter)
    return _byo_run_instructions(report)


def _discovery_note(report: DistReport) -> str:
    """
    The paragraph naming what the chosen discovery mode structurally cannot see, so
    the reader knows which knob to reach for when a module turns out to be missing.
    """
    return {
        "static": "* Modules were found by reading import statements. One imported "
        "dynamically (through\n  `importlib`, a plugin registry, ...) is invisible that "
        "way and may be missing;\n  name it with `include-modules`/`include-package`, or "
        "build with `--discovery both`.\n",
        "trace": "* Modules were found by importing the entrypoint and recording what "
        "it loaded. A\n  module imported lazily *later* (inside a function, on a code "
        "path not taken at\n  import time) is not seen that way and may be missing; "
        "build with `--discovery both`\n  to also read the source's import statements.\n",
        "both": "* Modules were found both by reading import statements and by "
        "importing the\n  entrypoint to record what it loaded. What can still be missing "
        "is a module\n  imported lazily, on a code path not taken at import time, under "
        "a name that\n  does not appear in the source -- name those with "
        "`include-modules`/`include-package`.\n",
    }[report.discovery]


def _interpreter_contents_note(interpreter: StagedInterpreter) -> str:
    """
    The paragraph saying whether the shipped interpreter is the whole standard library
    or only the part this application was found to need -- and, when it is the latter,
    what that means for someone whose code path reaches further than discovery did.

    Two genuinely different answers again, and the tailored one is the answer to a
    question a reader of a *failing* distribution will have.
    """
    if not interpreter.tailored:
        return (
            "This is a *vanilla* build: which parts of the interpreter this particular\n"
            "application needs is not worked out, so it is bigger than it has to be. Build\n"
            "with `--tailor-interpreter` to ship only what the application was found to need."
        )
    disabled = ", ".join(interpreter.disabled_libraries) or "none"
    trees = len(interpreter.pruned_modules)
    extensions = len(interpreter.pruned_extensions)
    before = f"{interpreter.size_before_prune_bytes / 1e6:.0f}"
    return f"""\
Its contents are *tailored* to this application: the standard library trees and
extension modules the build found no import for are not in here, and the interpreter
itself was compiled without the libraries none of them needed ({disabled}).

{trees} standard library tree(s) and {extensions} extension module(s) were left out.
The tree measured {before} MB just before that pass, and the shared libraries those
extension modules would have pulled in behind them are not here either -- which is
usually the larger half of the difference. The full list is in `{MANIFEST_NAME}`,
under `interpreter.pruned_modules` and `interpreter.pruned_extensions`.

If the application turns out to import a standard library module on a code path that
was not taken while it was being analysed, that import fails here where it would have
worked in an untailored build. `--include-module <name>` puts it back, and
`--no-tailor-interpreter` ships the whole standard library again."""


def _own_python_run_instructions(report: DistReport, interpreter: StagedInterpreter) -> str:
    """
    How to run a mode `own` distribution: nothing to install, with the libc caveat.
    """
    folder = report.dist_root.resolve()
    fallback = f"./{interpreter.executable_rel_path} ./{PAYLOAD_DIR_NAME}"
    if report.launcher is None:
        invocation = fallback
        absolute = f"{folder / interpreter.executable_rel_path} {folder / PAYLOAD_DIR_NAME}"
    else:
        invocation = f"./{report.launcher}"
        absolute = str(folder / report.launcher)
    libraries = len(interpreter.native_deps.dependencies) + len(report.native_deps.dependencies)
    size = f"{interpreter.size_bytes / 1e6:.0f}"
    pruned = (
        "Pruned from it: " + ", ".join(interpreter.pruned) + "."
        if interpreter.pruned
        else "Nothing was pruned from it."
    )
    contents = _interpreter_contents_note(interpreter)
    return f"""\
How to run this distribution
============================

This folder holds the application *and* the interpreter it runs on, so nothing has to
be installed on the target machine -- there need be no Python on it at all.

From inside the folder:

    {invocation}

Or, from anywhere:

    {absolute}

Arguments are passed through as usual:

    {invocation} --your --app --args

The launcher is a two-line shell script (plus a `.cmd` for Windows) resolving its own
location, so it works through a symlink into `~/.local/bin` or from a systemd
`ExecStart=`. It is a convenience, not a requirement: the line below runs the
distribution just as well, and is the answer if the launcher is ever in the way.

    {fallback}

The whole folder can be moved anywhere: the interpreter finds its own `libpython` and
standard library relative to where its executable sits, never through an absolute path
and never through an environment variable.

The interpreter
---------------

CPython {interpreter.version_string}, built by smelt (through meta-python) rather than taken from
this machine's Python install: `bin/python`, `lib/libpython*.so` and the standard
library under `lib/python{interpreter.version_string}/`, {size} MB in total.

The standard library ships as bytecode only, with no `.py` alongside. {pruned}

{contents}

What is bundled, and what is not
--------------------------------

The shared libraries the extension modules need -- the application's and the
interpreter's own -- travel with them ({libraries} of them here), found through paths
relative to this folder.

What is still taken from the target machine is **the C library and its dynamic
loader** (`libc`, `libm`, `ld-linux`). That is not an oversight: `ld.so` and `libc.so`
are a tightly ABI-coupled pair, and a build that shipped its own copy of them
segfaulted inside the loader before any Python ran. Any Linux new enough to have a
compatible glibc will do; a genuinely libc-independent build means targeting musl,
which is a separate mode and not what this folder is.

Current limitations
-------------------

* Package data files are only collected when asked for, through
  `include-package-data` -- a package folder can hold anything at all, so nothing is
  guessed. {len(report.data_files)} file(s) were collected here.
{_discovery_note(report)}"""


def _byo_run_instructions(report: DistReport) -> str:
    """
    How to run a mode `byo` distribution, with an already-installed interpreter.
    """
    payload = report.payload_root
    version = report.tag.version_string
    entrypoint_file = report.entrypoint_file
    libraries = len(report.native_deps.dependencies)
    data_files = len(report.data_files)
    discovery_note = _discovery_note(report)
    native_deps_note = (
        ""
        if report.native_deps.resolved
        else "* Native dependencies were NOT resolved on this build machine "
        f"({report.native_deps.unsupported}), so the shared libraries the extension\n"
        "  modules need must already be present on the target.\n"
    )
    return f"""\
How to run this distribution
============================

This folder holds the application, but not the interpreter. Running it needs a
CPython {version} already installed on the machine -- exactly that minor version,
see "Why the version has to match" below.

    python{version} {payload}

Or, from anywhere:

    /path/to/python{version} {payload.resolve()}

Arguments are passed through as usual:

    python{version} {payload} --your --app --args

Note the `{PAYLOAD_DIR_NAME}` at the end: the application lives in that subfolder, which is the
one that goes on `sys.path`. The distribution root holds this file and the manifest
next to it, and keeping them out of `{PAYLOAD_DIR_NAME}` is what stops them colliding with the
application's own module names. Passing a directory to the interpreter puts it on
`sys.path` and runs its `{MAIN_MODULE_NAME}`.

No flags to remember
--------------------

`{entrypoint_file}` checks two things before importing anything, so that running this the
obvious way is also running it correctly:

* **the interpreter version**, refused with a readable message instead of the
  confusing one described below;
* **isolation**: if it was not started with `-I -S`, it re-executes itself with those
  (plus `-B`) and carries on. That is not tidiness -- without isolation the host's own
  `site-packages` stays on `sys.path` behind this folder, so a module this
  distribution happens to be missing would be silently supplied by the host, and the
  application would work here and fail on a clean machine.

Passing `-I -S` yourself is still fine: the entrypoint then has nothing to do and
skips the re-execution. Neither flag affects your application's own environment
variables.

Why the version has to match
----------------------------

Both kinds of file in here are tied to the interpreter that built them:

* the `.pyc` modules carry a magic number checked before any code runs. Nothing
  reports that as a version problem by itself -- the interpreter simply fails to see
  the modules, and the run ends on `can't find '__main__' module` or
  `RuntimeError: Bad magic number`. The check in the entrypoint exists to turn that
  into a sentence you can act on;
* the native extension modules ({len(report.natives)} of them) are compiled against a
  specific CPython ABI.

This distribution was built with CPython {version} on \
{platform.system().lower()}/{platform.machine()}.

What is bundled, and what is not
--------------------------------

The shared libraries the extension modules need travel with them ({libraries} of
them here), and are found through paths relative to this folder -- so it can be
moved anywhere. What is deliberately left to the target machine is the C library
itself (and its loader), plus the interpreter's own `libpython`: those come from the
CPython install you run this with.

Current limitations
-------------------

* Package data files are only collected when asked for, through
  `include-package-data` -- a package folder can hold anything at all, so nothing is
  guessed. {data_files} file(s) were collected here.
{discovery_note}{native_deps_note}"""


def build_dist(
    config: SmeltConfig,
    *,
    entrypoint: str | None = None,
    output_dir: Path = Path("dist"),
    path_solver: PathSolver | None = None,
    optimize: int = -1,
    stdout: Stdout | None = None,
    build_extensions: bool = True,
    discovery: DiscoveryMode | None = None,
    python: DistPython | None = None,
    own_python_target: str | None = None,
    tailor_interpreter: bool | None = None,
    drop_stdlib_groups: Iterable[str] = (),
    guard_version: bool = True,
    isolate: bool = True,
    include_modules: Iterable[str] = (),
    include_packages: Iterable[str] = (),
    include_package_data: Iterable[str] = (),
    include_distribution_metadata: Iterable[str] = (),
    exclude_modules: Iterable[str] = (),
) -> DistReport:
    """
    Assembles the distribution folder for one of `config`'s entrypoints under
    `output_dir`, and returns what went into it.

    Runs the regular backend first (unless `build_extensions` is False, for a rebuild
    over artifacts already on disk), then ships every artifact it produced, fills in
    everything else the entrypoint imports as bytecode, bundles the shared libraries
    those artifacts need, and collects the requested package data files.

    `discovery` selects how modules are found (see `DiscoveryMode`), defaulting to
    what the entrypoint declares, then to `DEFAULT_DISCOVERY`.

    `python` selects which interpreter the distribution runs on (see `DistPython`),
    defaulting the same way. `"own"` builds one through `smelt.own_python` -- for
    `own_python_target`, and the minutes that first build takes, see
    `own_python.build_own_python` -- and stages it at the distribution *root*, so the
    folder runs on a machine with no Python installed. The interpreter shipped must
    agree on `(major, minor)` with the one compiling the bytecode here; a patch
    difference is fine (see `assert_no_version_skew`).

    `tailor_interpreter` makes that interpreter's contents follow the same closure
    (see `own_python.resolve_requirements` and `DEFAULT_TAILOR_INTERPRETER`), which is
    why the closure is now collected *before* the interpreter is built: which libraries
    it need not be compiled against is part of its build options, and changing those
    means a different build.

    `drop_stdlib_groups` names optional groups of the Minimal Viable Stdlib (see
    `own_python.MINIMAL_VIABLE_STDLIB`) the caller is willing to ship without, each
    with a documented consequence. Additive over the entrypoint's own declaration.

    `guard_version` and `isolate` put the corresponding guards in the generated
    `__main__` (see `write_entrypoint_module`). Both default on: they are what make
    `python <folder>/app` correct on a machine other than the one that built it.

    `include_modules`, `include_packages`, `include_package_data`,
    `include_distribution_metadata` and `exclude_modules` are each additive over what
    the entrypoint declares under the same name in its own options.

    An existing distribution folder of the same name is replaced.
    """
    path_solver = path_solver or config.get_path_solver()
    entrypoint_spec = resolve_entrypoint_spec(config, entrypoint)
    entrypoint_module = assert_is_valid_import_path(entrypoint_spec.partition(":")[0])

    if build_extensions:
        # `without_entrypoint`: the native artifacts are what is wanted here, not a
        # Nuitka-compiled binary of the entrypoint -- this pipeline is the alternative
        # to that one, not a step of it.
        run_backend(config, stdout=stdout, path_solver=path_solver, without_entrypoint=True)

    entrypoint_options = config.entrypoints[entrypoint_spec]
    built = collect_built_artifacts(config, path_solver)
    declared_discovery = discovery or entrypoint_options.get("discovery", DEFAULT_DISCOVERY)
    if declared_discovery not in ("static", "trace", "both"):
        raise DistError(
            f"Invalid discovery mode {declared_discovery!r}, expected one of "
            "'static', 'trace', 'both'."
        )
    resolved_discovery: DiscoveryMode = declared_discovery
    resolved_python = resolve_dist_python(entrypoint_options, python)
    tag = PycTargetTag.current(optimize)

    search_paths = project_search_paths(path_solver)
    forced_modules = [*entrypoint_options.get("include-modules", []), *include_modules]
    closure = collect_closure(
        entrypoint_module,
        search_paths,
        extra_modules=forced_modules,
        extra_packages=[*entrypoint_options.get("include-package", []), *include_packages],
        discovery=resolved_discovery,
        exclude_modules=[
            *entrypoint_options.get("exclude-modules", []),
            *exclude_modules,
        ],
    )

    built_interpreter: PathExists | None = None
    interpreter_requirements: InterpreterRequirements | None = None
    if resolved_python == "own":
        target = own_python_target or entrypoint_options.get(
            "own-python-target", DEFAULT_OWN_PYTHON_TARGET
        )
        # The closure's own standard library modules -- what `build_dist` used to
        # discard as "the target interpreter brings its own", and what decides the
        # interpreter's contents now.
        stdlib_modules = sorted(
            import_path for import_path, resolved in closure.items() if resolved.is_stdlib
        )
        tailor = resolve_tailor_interpreter(entrypoint_options, tailor_interpreter)
        # Which libraries to build without has to be settled *before* the build, while
        # the rest of the decision (`resolve_requirements`) needs the built prefix to
        # ask it for its bootstrap module set -- hence the two calls rather than one.
        disabled_libraries = (
            plan_disabled_libraries(stdlib_modules, include_modules=forced_modules)
            if tailor
            else frozenset[str]()
        )
        # Built (or, where the option set has been built before, taken from cache)
        # before anything is assembled: the skew check below can only be made once the
        # interpreter's own version is known, and failing it after the whole folder has
        # been written would be a pointless wait for an answer available now.
        built_interpreter = build_own_python(target=target, disabled_libraries=disabled_libraries)
        assert_no_version_skew(tag, interpreter_version(built_interpreter))
        if tailor:
            interpreter_requirements = resolve_requirements(
                stdlib_modules,
                built_interpreter,
                include_modules=forced_modules,
                drop_stdlib_groups=[
                    *entrypoint_options.get("drop-stdlib-groups", []),
                    *drop_stdlib_groups,
                ],
            )

    dist_root = output_dir / dist_folder_name(config, entrypoint_spec)
    if dist_root.exists():
        shutil.rmtree(dist_root)
    # Everything the application is made of goes under `payload_root`, never at the
    # distribution root -- see this module's docstring for why that separation exists.
    payload_root = dist_root / PAYLOAD_DIR_NAME
    payload_root.mkdir(parents=True)

    report = DistReport(
        dist_root=dist_root,
        entrypoint=entrypoint_spec,
        entrypoint_module=entrypoint_module,
        tag=tag,
        discovery=resolved_discovery,
    )

    # Native artifacts first: whatever smelt built for a module is what that module
    # ships as, so the bytecode pass below must not also emit a `.pyc` shadowing it.
    # Every built artifact is shipped, including for modules the closure did not
    # reach -- a module explicitly pinned in the config was pinned for a reason, and
    # may well be imported in a way static discovery cannot see.
    placed_natives: set[Path] = set()
    for import_path, artifacts in built.items():
        for artifact in artifacts:
            native = _copy_native(import_path, artifact, payload_root, "smelt")
            if native.dest_rel_path in placed_natives:
                # A shared runtime is reported for every module that needs it, and they
                # share one package folder -- so it resolves to the same destination
                # several times over.
                continue
            placed_natives.add(native.dest_rel_path)
            report.natives.append(native)

    for import_path, resolved in closure.items():
        if import_path in built:
            report.skipped[import_path] = "native (built by smelt)"
            continue
        if resolved.is_stdlib:
            # The interpreter brings its own standard library, and a copy of the
            # host's would be both redundant and version-coupled.
            report.skipped[import_path] = "stdlib"
            continue
        match resolved.kind:
            case ModuleKind.SOURCE:
                assert resolved.origin is not None, "a SOURCE module always has an origin"
                try:
                    report.bytecode.append(
                        compile_module(
                            import_path,
                            resolved.origin,
                            payload_root,
                            is_package=resolved.is_package,
                            optimize=optimize,
                        )
                    )
                except BytecodeCompilationError as exc:
                    # Not fatal: discovery follows every import statement it finds,
                    # including ones guarded for another Python version or another
                    # platform, whose source may legitimately not compile here.
                    _logger.warning("Skipping %s: %s", import_path, exc)
                    report.skipped[import_path] = f"bytecode compilation failed: {exc}"
            case ModuleKind.EXTENSION:
                assert resolved.origin is not None, "an EXTENSION module always has an origin"
                native = _copy_native(import_path, resolved.origin, payload_root, "environment")
                if native.dest_rel_path not in placed_natives:
                    placed_natives.add(native.dest_rel_path)
                    report.natives.append(native)
            case ModuleKind.NAMESPACE:
                # PEP 420: the directory itself is the module. Nothing to compile, and
                # explicitly no `__init__` -- adding one would turn it into a regular
                # package and cut off any other portion of the same namespace.
                (payload_root / Path(*import_path.split("."))).mkdir(parents=True, exist_ok=True)
                report.skipped[import_path] = "namespace package (directory only)"
            case ModuleKind.BUILTIN:
                report.skipped[import_path] = "builtin (compiled into the interpreter)"
            case ModuleKind.FROZEN:
                report.skipped[import_path] = "frozen (embedded in the interpreter)"
            case ModuleKind.MISSING:
                _logger.warning(
                    "Could not resolve %s, it will be missing from the distribution",
                    import_path,
                )
                report.skipped[import_path] = "unresolved"

    # Every native artifact is walked from the file it was *copied from*, not from its
    # copy: a wheel-shipped extension module resolves its own vendored libraries
    # through an RPATH relative to where the wheel installed it, which resolves to
    # nothing once the file sits here instead.
    report.native_deps = bundle_native_dependencies(
        payload_root,
        {artifact.dest_rel_path: artifact.source for artifact in report.natives},
    )

    data_specs = [
        *entrypoint_options.get("include-package-data", []),
        *include_package_data,
    ]
    report.data_files = collect_package_data(data_specs, payload_root, search_paths)

    report.metadata_files = collect_distribution_metadata(
        [artifact.import_path for artifact in report.bytecode]
        + [artifact.import_path for artifact in report.natives],
        payload_root,
        extra_distributions=[
            *entrypoint_options.get("include-distribution-metadata", []),
            *include_distribution_metadata,
        ],
        search_paths=search_paths,
    )

    shipped = {artifact.import_path for artifact in report.natives} | {
        artifact.import_path for artifact in report.bytecode
    }
    if entrypoint_module not in shipped:
        raise DistError(
            f"Nothing was shipped for the entrypoint module {entrypoint_module!r} "
            f"({report.skipped.get(entrypoint_module, 'not reached by discovery')}). "
            "The distribution would fail on its first import: check that the module "
            "is importable from this environment, or that `packages_location` points "
            "at the package root."
        )

    entrypoint_file = write_entrypoint_module(
        entrypoint_spec,
        payload_root,
        tag=report.tag,
        guard_version=guard_version,
        isolate=isolate,
        optimize=optimize,
    )
    report.entrypoint_file = entrypoint_file.relative_to(payload_root)

    if built_interpreter is not None:
        report.interpreter = stage_interpreter(
            built_interpreter, dist_root, requirements=interpreter_requirements
        )
        report.launcher = write_launcher_shim(
            launcher_name(config, entrypoint_spec), dist_root, report.interpreter
        )

    (dist_root / MANIFEST_NAME).write_text(json.dumps(report.serialize(), indent=2))
    (dist_root / INSTRUCTIONS_NAME).write_text(run_instructions(report))

    _logger.info("Assembled distribution at %s", dist_root)
    return report
