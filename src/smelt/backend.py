"""
Build backend implementation for smelt.

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sysconfig
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from mypyc.build import mypycify

from smelt.compiler import (
    compile_extension_objects,
    compile_zig_module,
    link_extension_objects,
)
from smelt.config import (
    Backend,
    CythonExtension,
    MypycModule,
    NuitkaModule,
    SmeltConfig,
)
from smelt.context import create_context_if_enabled, get_context
from smelt.explorer import (
    build_dependency_graph,
    find_modules_under_root,
    flatten_dependency_graph,
    has_local_source,
)
from smelt.nuitkaify import (
    RUNTIME_LIB_NAME,
    Stdout,
    compile_with_nuitka,
    import_path_search_root,
    nuitkaify_module,
)
from smelt.utils import (
    GenericExtension,
    ImportPath,
    ModpathType,
    PathExists,
    PathSolver,
    SmeltConfigError,
    SmeltError,
    get_module_name,
    locate_module,
    path_exists,
)

if TYPE_CHECKING:
    from os import PathLike

# TODO: replace .so references to a variable that's set to .so
# for Unix-like and .dll for Windows

_logger = logging.getLogger(__name__)


def _mypycify_one(module: MypycModule, path_solver: PathSolver) -> GenericExtension:
    """
    Runs mypyc's codegen step for a single module, without compiling the result.
    """
    ext_path = module.source or path_solver.resolve_import_path(module.import_path)
    runtime, module_ext = mypycify([str(ext_path)], include_runtime_files=True)
    return GenericExtension.factory(
        src_path=ext_path,
        import_path=module.import_path,
        extension=module_ext,
        runtime=runtime,
        dest_folder=ext_path.parent,
    )


@dataclass
class CompiledExtension:
    """
    A `GenericExtension`'s object files, compiled but not yet linked (see
    `compiling_pipeline_refactor.md`).

    `runtime_objects` is mypyc's shared runtime's own object files, present exactly
    when `generic.runtime` is -- a runtime needs no `PyInit_` of its own, just its
    code merged in alongside whatever consumes `objects`.
    """

    generic: GenericExtension
    objects: list[PathExists]
    runtime_objects: list[PathExists] | None = None


def compile_generic_extension(
    ext: GenericExtension, dest_folder: PathLike[str]
) -> CompiledExtension:
    """
    Compiles `ext`'s extension (and its runtime, if any) into `dest_folder`, stopping
    short of linking either into a `.so`.

    This is the object-file stage `link_generic_extension` (today's default: ship a
    loose `.so`) and static linking (`smelt.static_python.build_static_interpreter`)
    both build on -- the seam `compiling_pipeline_refactor.md` opens between codegen
    and "compile-and-place".
    """
    objects = compile_extension_objects(ext.extension, dest_folder)
    runtime_objects = compile_extension_objects(ext.runtime, dest_folder) if ext.runtime else None
    return CompiledExtension(ext, objects, runtime_objects)


def link_generic_extension(compiled: CompiledExtension) -> GenericExtension:
    """
    Links a `CompiledExtension`'s object files into `.so`s and moves them to their
    final destination next to the source module.

    The default consumer of `compile_generic_extension`'s output -- what
    `compile_mypyc_extensions` and `_compile_and_place` used to do directly through
    `compile_extension`, now composed from the observable object-file stage instead.
    """
    ext = compiled.generic
    so_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    module_so_path = link_extension_objects(compiled.objects, ext.extension.name + so_suffix)
    shutil.move(module_so_path, str(ext.get_dest_path()))
    if compiled.runtime_objects is not None:
        assert ext.runtime is not None, "runtime_objects is only ever set alongside a runtime"
        runtime_so_path = link_extension_objects(
            compiled.runtime_objects, ext.runtime.name + so_suffix
        )
        shutil.move(runtime_so_path, str(ext.get_runtime_dest_path()))
    return ext


def is_static_link_eligible(ext: GenericExtension) -> bool:
    """
    Tier 1 (structural, before compiling anything) of `compiling_pipeline_refactor.md`'s
    static-linking eligibility check: whether `ext` could safely be folded straight
    into `bin/python` instead of shipped as a loose `.so`.

    An extension (or its mypyc runtime) that names an external library via
    `Extension.libraries`/`extra_link_args` links against something outside its own
    object code. Folding that into the interpreter turns a missing dependency from a
    soft, per-import failure (today's `.so` + `dlopen()`) into a hard, whole-process
    startup failure -- an unresolved `DT_NEEDED`, refused before `main()` even runs.

    This is the cheap, free-before-compiling half of the check; anything pulled in
    implicitly slips past it, which is what `smelt.static_python.build_static_interpreter`'s
    own `ldd` pass over the trial link exists to catch.
    """
    candidates = (ext.extension, *((ext.runtime,) if ext.runtime else ()))
    return all(
        not candidate.libraries and not candidate.extra_link_args for candidate in candidates
    )


def _compile_place_or_stage(
    ext: GenericExtension, static_build_dir: PathLike[str] | None
) -> list[PathExists] | None:
    """
    Compiles `ext` and either links+places it (the default), or -- when
    `static_build_dir` is given and `ext` passes Tier 1 (`is_static_link_eligible`) --
    compiles it into `static_build_dir` instead and returns its (module + runtime)
    object files, unlinked.

    Returns `None` when `ext` was linked and placed normally -- the shared decision
    point every `run_backend` compile loop (pinned or auto-discovered) goes through.
    """
    if static_build_dir is not None and is_static_link_eligible(ext):
        compiled = compile_generic_extension(ext, static_build_dir)
        return [*compiled.objects, *(compiled.runtime_objects or [])]
    with tempfile.TemporaryDirectory() as build_folder:
        link_generic_extension(compile_generic_extension(ext, build_folder))
    return None


def compile_mypyc_extensions(
    modules: Iterable[MypycModule],
    path_solver: PathSolver | None = None,
) -> list[GenericExtension]:
    """
    Compiles all mypy extensions defined in `mypyc_config` for the project found at `project_root`
    """
    path_solver = path_solver or PathSolver()
    built_extensions: list[GenericExtension] = []
    for module in modules:
        mypyc_ext = _mypycify_one(module, path_solver)
        with tempfile.TemporaryDirectory() as build_folder:
            link_generic_extension(compile_generic_extension(mypyc_ext, build_folder))
        so_dest_path = str(mypyc_ext.get_dest_path())
        _logger.info("Built extensions %s @ %s", module.import_path, so_dest_path)
        if mypyc_ext.runtime:
            _logger.info(
                "-> %s runtime: %s", module.import_path, str(mypyc_ext.get_runtime_dest_path())
            )
        built_extensions.append(mypyc_ext)
    return built_extensions


def _cythonize_one(
    module: CythonExtension,
    path_solver: PathSolver,
    options: dict[str, Any] | None = None,
) -> GenericExtension:
    """
    Runs cythonize for a single module, without compiling the result.
    """
    options = options or {}
    try:
        from Cython.Build import cythonize
    except ImportError as exc:
        raise ImportError(
            "Cython is not installed. consider installing smelt with [cython] extra"
        ) from exc
    import_path = module.import_path
    source_path = str(module.source or path_solver.resolve_import_path(import_path))
    cython_ext = cythonize(source_path, **options)
    assert len(cython_ext) == 1, (
        "Passed on source file to cython yet it produced more than one extension"
    )
    (base_ext,) = cython_ext
    base_ext.name = import_path.split(".")[-1]
    return GenericExtension.factory(
        src_path=source_path,
        import_path=import_path,
        extension=base_ext,
        dest_folder=Path(source_path).parent,
    )


def compile_cython_extensions(
    modules: list[CythonExtension],
    options: dict[str, Any] | None = None,
    path_solver: PathSolver | None = None,
) -> list[GenericExtension]:
    """
    Compiles all the cython extensions as defined in `cython_config`
    """
    path_solver = path_solver or PathSolver()
    return [_cythonize_one(module, path_solver, options) for module in modules]


def _compile_and_place(ext: GenericExtension) -> GenericExtension:
    """
    Compiles `ext` and moves its resulting `.so` (and runtime `.so`, if any)
    to their final destination next to the source module.
    """
    with tempfile.TemporaryDirectory() as build_folder:
        link_generic_extension(compile_generic_extension(ext, build_folder))
    return ext


def _generate_with_backend(
    backend: Backend, import_path: ImportPath, path_solver: PathSolver
) -> GenericExtension:
    match backend:
        case Backend.NUITKA:
            return nuitkaify_module(NuitkaModule(import_path), path_solver=path_solver)
        case Backend.MYPYC:
            return _mypycify_one(MypycModule(import_path), path_solver)
        case Backend.CYTHON:
            return _cythonize_one(CythonExtension(import_path), path_solver)


@dataclass
class BackendAttempt:
    """
    The outcome of trying a single backend to compile one auto-discovered module.
    """

    backend: Backend
    succeeded: bool
    error: str | None = None

    def serialize(self) -> dict[str, Any]:
        return {
            "backend": self.backend.value,
            "succeeded": self.succeeded,
            "error": self.error,
        }


@dataclass
class ModuleAutoCompileReport:
    """
    Every backend attempted for a single auto-discovered module, in order,
    and whichever one was ultimately selected (None if all of them failed).
    """

    import_path: ImportPath
    attempts: list[BackendAttempt] = field(default_factory=list)
    selected_backend: Backend | None = None

    def serialize(self) -> dict[str, Any]:
        return {
            "import_path": self.import_path,
            "attempts": [attempt.serialize() for attempt in self.attempts],
            "selected_backend": (self.selected_backend.value if self.selected_backend else None),
        }


@dataclass
class AutoModeContext:
    """
    Persistent context (registered as "auto_mode" in the `GlobalContext`) tracking,
    for every module discovered by `auto_mode`, which backends from
    `backend_priority_order` were tried, their errors on failure, and which backend
    was ultimately selected.
    """

    modules: dict[ImportPath, ModuleAutoCompileReport] = field(default_factory=dict)

    def record_attempt(self, import_path: ImportPath, backend: Backend, error: str | None) -> None:
        report = self.modules.setdefault(import_path, ModuleAutoCompileReport(import_path))
        report.attempts.append(BackendAttempt(backend, succeeded=error is None, error=error))
        if error is None:
            report.selected_backend = backend

    def render(self) -> str:
        lines = ["Auto-mode backend attempts:"]
        for report in self.modules.values():
            lines.append(f"  {report.import_path}:")
            for attempt in report.attempts:
                status = "OK" if attempt.succeeded else f"FAILED ({attempt.error})"
                lines.append(f"    - {attempt.backend.value}: {status}")
        return "\n".join(lines)

    def serialize(self) -> dict[str, Any]:
        return {
            "modules": {
                import_path: report.serialize() for import_path, report in self.modules.items()
            }
        }


def _get_auto_mode_context() -> AutoModeContext:
    """
    Fetches the persistent "auto_mode" context, creating it if context tracking is
    enabled, or a throwaway local instance otherwise (mirrors `NuitkaBuildContext`'s
    lookup in `nuitkaify_module`).
    """
    existing = get_context("auto_mode")
    if existing is not None:
        assert isinstance(existing, AutoModeContext)
        return existing
    return create_context_if_enabled("auto_mode", AutoModeContext) or AutoModeContext()


def write_auto_mode_report(path: str | os.PathLike[str]) -> None:
    """
    Serializes the "auto_mode" context (if any) as JSON to `path`.
    """
    context = get_context("auto_mode")
    report = (
        context.serialize()
        if isinstance(context, AutoModeContext)
        else AutoModeContext().serialize()
    )
    Path(path).write_text(json.dumps(report, indent=2))


def compile_module_with_fallback(
    import_path: ImportPath,
    backend_priority_order: Iterable[Backend],
    path_solver: PathSolver,
    *,
    static_build_dir: PathLike[str] | None = None,
) -> tuple[GenericExtension, list[PathExists] | None]:
    """
    Compiles `import_path` trying each backend in `backend_priority_order` in turn,
    falling back to the next one when a backend fails to produce a working extension.

    `static_build_dir` is threaded straight through to `_compile_place_or_stage`: a
    successful attempt is staged for static linking instead of linked+placed when it
    passes Tier 1 (`is_static_link_eligible`) -- whichever backend actually produced
    it, Nuitka included (it only ever fails Tier 1 on the merits, see `run_backend`'s
    own doc, not because it is auto-discovered). The second element of the returned
    tuple is that staging's own object files, or `None` when linked+placed normally.
    """
    auto_context = _get_auto_mode_context()
    last_exc: Exception | None = None
    for backend in backend_priority_order:
        try:
            ext = _generate_with_backend(backend, import_path, path_solver)
            objects = _compile_place_or_stage(ext, static_build_dir)
        except (SmeltError, RuntimeError, ImportError) as exc:
            auto_context.record_attempt(import_path, backend, error=str(exc))
            _logger.warning("Backend %s failed to compile %s: %s", backend.value, import_path, exc)
            last_exc = exc
            continue
        auto_context.record_attempt(import_path, backend, error=None)
        return ext, objects
    raise SmeltError(
        f"All backends {[b.value for b in backend_priority_order]} failed to compile {import_path}"
    ) from last_exc


def _pinned_import_paths(config: SmeltConfig) -> set[ImportPath]:
    """
    Import paths already explicitly assigned to a backend in the config,
    to exclude from auto-discovery.
    """
    return {
        module.import_path
        for module in (
            *config.mypyc_modules,
            *config.cython_modules,
            *config.nuitka_modules,
        )
    }


def discover_auto_targets(config: SmeltConfig, path_solver: PathSolver) -> set[ImportPath]:
    """
    Resolves `config.auto_mode` into the set of import paths to auto-compile,
    on top of whatever was explicitly pinned in `*_modules`.

    * "off": nothing.
    * "package": every module belonging to a package listed in `packages_location`.
    * "all": the above, plus every module they transitively import (their dependencies).
    """
    if config.auto_mode == "off":
        return set()

    known_roots = path_solver.known_roots
    if not known_roots:
        raise SmeltConfigError("auto_mode requires at least one entry in packages_location")

    package_modules: set[ImportPath] = set()
    for root_import_path, root_path in known_roots:
        package_modules.update(find_modules_under_root(root_import_path, root_path))

    if config.auto_mode == "package":
        discovered = package_modules
    else:  # "all"
        discovered = set(package_modules)
        for module in package_modules:
            discovered.update(
                node.name for node in flatten_dependency_graph(build_dependency_graph(module))
            )

    pinned = _pinned_import_paths(config)
    return {name for name in discovered - pinned if has_local_source(name)}


def discover_external_mypyc_runtimes() -> set[str]:
    """
    Finds mypyc shared-runtime libraries belonging to already-installed dependencies
    that this smelt run never compiled itself (e.g. a third-party package built
    against mypyc upstream, like `charset-normalizer`).

    mypyc's shared runtime is dlopen'd by its compiled modules at C level, never
    through a literal `import` statement, so Nuitka's import-follower cannot discover
    it on its own -- the same problem `shared_runtime_extensions` solves for runtimes
    built in this run, except here smelt has no config entry to know about the module
    in the first place, so the whole environment has to be scanned instead.

    TODO: this scans the whole venv indiscriminately, so it also picks up mypyc
    runtimes belonging to build-time-only tools (e.g. mypy itself) that are never
    reachable from the entrypoint at runtime -- harmless bloat today, but this should
    be scoped down to the project's actual runtime-dependency closure (resolve
    `[project.dependencies]` transitively via `importlib.metadata`, then only scan
    `__mypyc` files belonging to distributions in that closure) before being wired
    back into `run_backend`.
    """
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    site_roots = {
        Path(path)
        for key in ("purelib", "platlib")
        if (path := sysconfig.get_path(key)) and Path(path).is_dir()
    }
    found: set[str] = set()
    for root in site_roots:
        for so_path in root.rglob(f"*__mypyc{suffix}"):
            rel_stem = so_path.relative_to(root).with_name(so_path.name.removesuffix(suffix))
            found.add(".".join(rel_stem.parts))
    return found


def artifact_target_folders(
    config: SmeltConfig,
    path_solver: PathSolver,
    *,
    shadowed_only: bool = False,
) -> dict[ImportPath, Path]:
    """
    The folder each of `config`'s modules has (or would have) its built dynlib placed
    in, i.e. the folder holding that module's own source.

    `shadowed_only` restricts the result to modules with a pure-Python fallback
    (mypyc/cython/nuitka-backed, pinned or auto-discovered), excluding handwritten
    C/Zig extensions that have no `.py` counterpart.
    """
    targets: dict[ImportPath, Path] = {}
    for module in (*config.mypyc_modules, *config.cython_modules, *config.nuitka_modules):
        source = module.source or path_solver.resolve_import_path(
            module.import_path, should_exist=False
        )
        targets[module.import_path] = source.parent

    for import_path in discover_auto_targets(config, path_solver):
        targets[import_path] = path_solver.resolve_import_path(
            import_path, should_exist=False
        ).parent

    if not shadowed_only:
        for native_ext in config.c_extensions:
            targets[native_ext.import_path] = native_ext.sources[0].parent
        for zig_mod in config.zig_modules:
            targets[zig_mod.import_path] = path_solver.resolve_import_path(
                zig_mod.import_path, should_exist=False
            ).parent
    return targets


def collect_built_artifacts(
    config: SmeltConfig,
    path_solver: PathSolver,
) -> dict[ImportPath, list[Path]]:
    """
    Finds, for every module `config` declares (or `auto_mode` discovers), the built
    artifacts currently sitting next to its source: the module's own dynlib, plus the
    shared runtimes it needs at load time where applicable.

    Only artifacts that actually exist on disk are reported, so this describes what a
    build produced rather than what it was asked to produce -- `auto_mode` in
    particular is allowed to give up on individual modules.

    **A module counts as built only when its own dynlib is there.** The two shared
    runtimes are collected *alongside* it and are never evidence for it, which is a
    distinction with teeth: `mod<EXT_SUFFIX>` and `mod__mypyc<EXT_SUFFIX>` carry the
    interpreter's ABI tag, while smelt's shared Nuitka runtime (`lib<name>.so`) carries
    no version at all. Counting the runtime as evidence let a *stale* one -- left next
    to the source by a build under another interpreter -- claim the module was built:
    the bundler then skipped emitting a `.pyc` for it ("native (built by smelt)"),
    its own "nothing was shipped for the entrypoint module" guard saw the import path
    attached to that runtime and passed, and the distribution shipped without the
    module, failing on the target at first import. Every backend produces the module's
    own dynlib (mypyc, cython, Nuitka module mode, handwritten C, Zig), so requiring it
    costs nothing.

    Both shared runtimes must keep the package-relative position they have here:
    mypyc's is imported under the module's own package (`pkg.mod__mypyc`), and smelt's
    shared Nuitka runtime is resolved through an `$ORIGIN` rpath, i.e. from the
    directory of the `.so` that needs it.
    """
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    nuitka_runtime_name = f"lib{RUNTIME_LIB_NAME}.so"
    targets = artifact_target_folders(config, path_solver)

    built: dict[ImportPath, list[Path]] = {}
    for import_path in sorted(targets):
        dest_folder = targets[import_path]
        module_name = get_module_name(import_path)
        module_artifact = dest_folder / f"{module_name}{suffix}"
        if not path_exists(module_artifact):
            continue
        runtimes = (
            dest_folder / f"{module_name}__mypyc{suffix}",
            dest_folder / nuitka_runtime_name,
        )
        built[import_path] = [
            module_artifact,
            *(runtime for runtime in runtimes if path_exists(runtime)),
        ]
    return built


def clean_all_artifacts(
    config: SmeltConfig,
    path_solver: PathSolver,
    *,
    shadowed_only: bool = False,
) -> dict[ImportPath, list[Path]]:
    """
    Deletes every built dynlib for `config`'s modules (including mypyc's shared
    runtime, where applicable), "unshadowing" their `.py` counterpart back to
    being the one Python imports.

    `shadowed_only` restricts cleaning to modules with a pure-Python fallback
    to unshadow (mypyc/cython/nuitka-backed modules, pinned or auto-discovered),
    excluding handwritten C/Zig extensions that have no `.py` counterpart.
    """
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    targets = artifact_target_folders(config, path_solver, shadowed_only=shadowed_only)

    deleted: dict[ImportPath, list[Path]] = {}
    for import_path in sorted(targets):
        dest_folder = targets[import_path]
        module_name = get_module_name(import_path)
        candidates = (
            dest_folder / f"{module_name}{suffix}",
            dest_folder / f"{module_name}__mypyc{suffix}",
        )
        removed = [artifact for artifact in candidates if path_exists(artifact)]
        for artifact in removed:
            artifact.unlink()
        if removed:
            deleted[import_path] = removed
    return deleted


@dataclass(frozen=True)
class EntrypointGuard:
    """
    A block of code `create_entrypoint_script` puts *before* the entrypoint's own
    import, along with the imports that block needs.

    Running before that import is the whole point: a guard that checked something
    after the application's modules were already imported would be checking it too
    late.
    """

    code: str
    imports: tuple[str, ...] = ()


def python_version_guard(version: tuple[int, int], magic: bytes) -> EntrypointGuard:
    """
    A guard refusing to run under an interpreter the distribution was not built for,
    with a message that names the version needed.

    Worth having because the failure it replaces is unreadable: bytecode carries a
    magic number checked before any code runs, so a mismatched interpreter does not
    report a version problem at all -- it fails to see the modules and reports
    `can't find '__main__' module`, or `RuntimeError: Bad magic number`.

    This guard can only do its job from *source*: a `.pyc` holding it would itself be
    rejected by the very check it is meant to explain. That is why the generated
    entrypoint is the one file a distribution ships as `.py` (see
    `smelt.dist.write_entrypoint_module`).
    """
    major, minor = version
    return EntrypointGuard(
        imports=("importlib.util", "sys"),
        code=(
            f"_REQUIRED_VERSION = ({major}, {minor})\n"
            f'_REQUIRED_MAGIC = bytes.fromhex("{magic.hex()}")\n'
            "\n"
            "if (\n"
            "    sys.version_info[:2] != _REQUIRED_VERSION\n"
            "    or importlib.util.MAGIC_NUMBER != _REQUIRED_MAGIC\n"
            "):\n"
            "    sys.exit(\n"
            '        "This application was built for CPython %d.%d, but is running '
            'under %d.%d. "\n'
            '        "Its compiled modules cannot be loaded by this interpreter; '
            'run it with a "\n'
            '        "CPython %d.%d instead."\n'
            "        % (_REQUIRED_VERSION + sys.version_info[:2] + _REQUIRED_VERSION)\n"
            "    )"
        ),
    )


def isolation_guard() -> EntrypointGuard:
    """
    A guard that re-executes the interpreter with `-I -S -B` when it is not already
    isolated, so the distribution runs hermetically however it was invoked.

    Without isolation the host's `site-packages` stays on `sys.path` behind the
    distribution's own directory. A module the distribution is *missing* is then
    silently satisfied by the host's copy, which is the worst possible failure shape:
    it works on the machine that built it and fails on a clean target. Enforcing the
    flags from inside the entrypoint means there is no invocation to get wrong -- and
    no launcher, shell wrapper or compiled stub needed to enforce them.

    `-B` keeps the isolated run from writing `__pycache__` into the distribution.
    (The first, non-isolated run still caches this one generated file where the
    folder is writable; Python skips the write silently where it is not.)
    """
    return EntrypointGuard(
        imports=("os", "sys"),
        code=(
            "if not (sys.flags.isolated and sys.flags.no_site):\n"
            "    os.execv(\n"
            "        sys.executable,\n"
            "        [\n"
            "            sys.executable,\n"
            '            "-I",\n'
            '            "-S",\n'
            '            "-B",\n'
            "            os.path.dirname(os.path.abspath(__file__)),\n"
            "            *sys.argv[1:],\n"
            "        ],\n"
            "    )"
        ),
    )


def create_entrypoint_script(
    entrypoint: str,
    dest_dir: str | os.PathLike[str],
    *,
    guards: Iterable[EntrypointGuard] = (),
    script_name: str | None = None,
) -> Path:
    """
    Codegens a standalone script calling `entrypoint` ("module1.module2:func_name"),
    mirroring what installers generate for `[project.scripts]`. Simpler than those:
    no pythonw, no CLI argument handling.

    `guards` are emitted before the entrypoint's own import (see `EntrypointGuard`).
    `script_name` overrides the generated file's name, for a caller that needs a
    specific one (a distribution's `__main__`).
    """
    module_path, sep, func_name = entrypoint.partition(":")
    if not sep or not module_path or not func_name:
        raise SmeltConfigError(
            f"Invalid entrypoint {entrypoint!r}, expected 'module.path:func_name'"
        )
    guards = list(guards)
    imports = sorted({"sys", *(name for guard in guards for name in guard.imports)})
    blocks = [
        "\n".join(f"import {name}" for name in imports),
        *(guard.code for guard in guards),
        f"from {module_path} import {func_name}",
        f'if __name__ == "__main__":\n    sys.exit({func_name}())',
    ]
    script = "\n\n".join(blocks) + "\n"
    if script_name is None:
        # Nuitka names the compiled entry script after its own filename; if that name
        # collides with a top-level component of `module_path` (e.g. a CLI function
        # named after its own package, as in `pkg = "pkg.cli:pkg"`), it shadows the
        # real package at runtime instead of importing it. Disambiguate in that case.
        script_name = func_name
        if script_name in module_path.split("."):
            script_name = f"_{script_name}_entrypoint"
    dest_path = Path(dest_dir) / f"{script_name}.py"
    dest_path.write_text(script)
    return dest_path


@dataclass
class BackendResult:
    """
    What `run_backend` produced.

    `artifacts` is what it always returned: the filesystem paths of every compiled
    artifact placed next to its Python source. `static_modules` is new: modules
    `static_link` found Tier-1-eligible (see `is_static_link_eligible`), compiled but
    deliberately left unlinked -- their object files, ready to hand to
    `smelt.static_python.build_static_interpreter` instead of a loose `.so`. Empty
    unless `static_link=True`. `static_build_dir` is where those objects live; the
    caller owns cleaning it up once it is done consuming `static_modules` (`None`
    when `static_modules` is empty, since nothing was staged there).
    """

    artifacts: list[Path]
    static_modules: dict[ImportPath, list[PathExists]] = field(default_factory=dict)
    static_build_dir: Path | None = None


def run_backend(
    config: SmeltConfig,
    stdout: Stdout | None = None,
    path_solver: PathSolver | None = None,
    strategy: ModpathType = ModpathType.FS,
    *,
    without_entrypoint: bool = False,
    entrypoint: str | None = None,
    embed_files: Iterable[tuple[Path, ImportPath]] | None = None,
    no_cache: bool = False,
    static_link: bool = False,
) -> BackendResult:
    """
    Runs the whole backend pipeline:
    * C extensions compilation
    * mypyc extensions
    * Nuitka compilation

    `entrypoint` restricts Nuitka compilation to a single one of
    `config.entrypoints` (by import path). If omitted, all of them are built.

    `embed_files` are (data_file_path, import_path) pairs, each turned into a
    Nuitka `--include-data-files=data_file_path=dest` flag (`dest` being
    `import_path`, dotted-to-slash, joined with `data_file_path`'s filename),
    applied to every entrypoint built in this run.

    `static_link` opts every pinned mypyc, Cython, Nuitka and handwritten C/Zig
    (`config.c_extensions`) module into Tier 1 eligibility checking (see
    `is_static_link_eligible` and `compiling_pipeline_refactor.md`): one that passes
    is compiled but left unlinked, its object files reported in the result's
    `static_modules` instead of shipped as a `.so`. A Nuitka module genuinely goes
    through the same `GenericExtension` + compile step as the others (`nuitkaify_module`
    only transpiles to C), so it is checked the same way -- it just never actually
    passes: linking against its own shared runtime or not, it always declares
    `libraries=["m", ...]` (see `nuitkaify._runtime_link_libraries`), so Tier 1 refuses
    it structurally, the same as any other module naming a real external library.

    Auto-discovered modules (`config.auto_mode`, via `compile_module_with_fallback`)
    are checked the same way too, whichever backend (mypyc/Cython/Nuitka)
    `backend_priority_order` lands on for a given module -- Tier 1 needs no
    per-backend special-casing since it only ever looks at the produced `Extension`'s
    own `libraries`/`extra_link_args`.

    Only `config.zig_modules` is hard-excluded rather than routed through Tier 1:
    a project's own `build.zig` (as opposed to a single `.zig` source under
    `c_extensions`, which *is* checked) drives `zig build` end-to-end and hands back
    only a finished `.so`, with no object-file seam smelt controls to stage in the
    first place.

    Returns a `BackendResult` (see its own doc) rather than a bare list, so a caller
    that opted into `static_link` has somewhere to receive `static_modules` from --
    `.artifacts` is what every existing caller (e.g. the hatchling build hook, which
    force-includes them in packaging) already expected from this function.
    """
    local_platform = platform.system().lower()
    if (platforms := config.platforms) is not None and local_platform not in platforms:
        if stdout is None:
            return BackendResult([])
        printer = _logger.info if stdout == "logger" else print
        printer(
            f"Running on {local_platform}, build hook is restricted to {platforms}, skipping extension building"
        )
        return BackendResult([])

    built_artifacts: list[Path] = []
    static_modules: dict[ImportPath, list[PathExists]] = {}
    static_build_dir = Path(tempfile.mkdtemp(prefix="smelt-static-")) if static_link else None
    path_solver = path_solver or config.get_path_solver()
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )

    def _place_or_stage(ext: GenericExtension) -> bool:
        """
        `_compile_place_or_stage` plus this run's bookkeeping: records staged objects
        in `static_modules` and returns whether `ext` was staged (vs. linked+placed).
        """
        objects = _compile_place_or_stage(ext, static_build_dir)
        if objects is None:
            return False
        static_modules[ext.import_path] = objects
        _logger.info("Staged %s for static linking (no .so written)", ext.import_path)
        return True

    # Zig modules drive their own `zig build` end-to-end (a project-supplied
    # `build.zig`, not smelt's own compile step), so -- like Nuitka -- there is no
    # object-file seam here to stage for static linking; always a loose `.so`.
    for zig_mod in config.zig_modules:
        built_artifacts.append(
            compile_zig_module(
                zig_mod.name,
                zig_mod.folder,
                zig_mod.import_path,
                flags=zig_mod.flags,
                path_solver=path_solver,
            )
        )

    # Handwritten C extensions -- eligible for static linking, same as mypyc/Cython:
    # they go through the same `compile_extension`/`GenericExtension` seam.
    for native_extension in config.c_extensions:
        sources = native_extension.sources
        if len(sources) > 1:
            raise NotImplementedError("Not supported yet")
        c_extension_path = sources[0]
        native_ext = GenericExtension.factory(
            src_path=c_extension_path,
            import_path=native_extension.import_path,
            dest_folder=c_extension_path.parent,
        )
        if _place_or_stage(native_ext):
            continue
        built_artifacts.append(native_ext.get_dest_path())

    # Note: mypyc has a runtime shipped as a separate extension
    # this runtime should be named modname__mypy
    # we need to keep track of it to include to nuitka,
    # as it would be invisible otherwise
    shared_runtime_extensions: set[str] = set()

    for module in config.mypyc_modules:
        mypyc_ext = _mypycify_one(module, path_solver)
        if _place_or_stage(mypyc_ext):
            continue
        built_artifacts.append(mypyc_ext.get_dest_path())
        if mypyc_ext.runtime:
            shared_runtime_extensions.add(mypyc_ext.runtime.name)
            built_artifacts.append(mypyc_ext.get_runtime_dest_path())

    # cython extensions -- eligible for static linking, unlike Nuitka below.
    for cython_ext in compile_cython_extensions(config.cython_modules, path_solver=path_solver):
        if _place_or_stage(cython_ext):
            continue
        built_artifacts.append(cython_ext.get_dest_path())
        if cython_ext.runtime:
            built_artifacts.append(cython_ext.get_runtime_dest_path())

    # A Nuitka module *does* go through smelt's own compile step (`nuitkaify_module`
    # only transpiles to C; `_place_or_stage`/`compile_generic_extension` is what
    # actually compiles it) -- so, unlike `zig_modules` above, it is routed through
    # Tier 1 rather than hard-excluded. In practice it never passes: linking against
    # its own shared runtime (`use_runtime=True`) or not, `nuitkaify_module` always
    # declares `libraries=["m", ...]` (see `_runtime_link_libraries`), so Tier 1
    # refuses it the same way it would any other module linking a real external
    # library -- correctly, and without needing a name-the-backend special case.
    for nuitka_mod in config.nuitka_modules:
        nuitka_ext = nuitkaify_module(nuitka_mod, path_solver=path_solver)
        if _place_or_stage(nuitka_ext):
            continue
        built_artifacts.append(nuitka_ext.get_dest_path())
        if nuitka_ext.runtime:
            built_artifacts.append(nuitka_ext.get_runtime_dest_path())

    # auto-discovered modules (see `config.auto_mode`), each compiled by trying
    # `config.backend_priority_order` in turn until one succeeds. Unlike pinned
    # modules, a module that exhausts every backend is skipped rather than
    # aborting the whole build: "all" mode routinely reaches third-party modules
    # that were never meant to be compiled (soft dependencies, unwritable
    # site-packages, etc).
    for import_path in sorted(discover_auto_targets(config, path_solver)):
        try:
            auto_ext, auto_objects = compile_module_with_fallback(
                import_path,
                config.backend_priority_order,
                path_solver,
                static_build_dir=static_build_dir,
            )
        except SmeltError as exc:
            _logger.warning("Skipping auto-discovered module %s: %s", import_path, exc)
            continue
        if auto_objects is not None:
            static_modules[auto_ext.import_path] = auto_objects
            _logger.info("Staged %s for static linking (no .so written)", auto_ext.import_path)
            continue
        built_artifacts.append(auto_ext.get_dest_path())
        if auto_ext.runtime:
            shared_runtime_extensions.add(auto_ext.runtime.name)
            built_artifacts.append(auto_ext.get_runtime_dest_path())

    # nuitka entrypoint(s) compilation
    embed_data_files = [
        f"{data_file_path}={import_path.replace('.', '/')}/{data_file_path.name}"
        for data_file_path, import_path in (embed_files or ())
    ]
    without_entrypoint = without_entrypoint or not config.entrypoints
    if not without_entrypoint:
        entrypoints_to_build = list(config.entrypoints)
        if entrypoint is not None:
            # accepts either the entrypoint's own key ("module.path[:func_name]") or,
            # for one picked up from `[project.scripts]`, the script name as the user
            # invokes it (e.g. "afpu" for `afpu = "advantics.afpu.cli:afpu"`).
            resolved_entrypoint = config.script_names.get(entrypoint, entrypoint)
            if resolved_entrypoint not in config.entrypoints:
                raise SmeltError(
                    f"Unknown entrypoint {entrypoint!r}. "
                    f"Available entrypoints: {list(config.entrypoints)}. "
                    f"Available script names: {list(config.script_names)}"
                )
            entrypoints_to_build = [resolved_entrypoint]
        for entrypoint_spec in entrypoints_to_build:
            module_path, sep, func_name = entrypoint_spec.partition(":")
            module_file = locate_module(
                module_path, strategy=strategy, package_root=path_solver.project_root
            )
            entrypoint_options = config.entrypoints[entrypoint_spec]
            # a codegen'd wrapper script must live outside the package tree it imports
            # (colliding on name with that package -- a common case, e.g. a `main`
            # function in the package's own top-level module -- would otherwise shadow
            # it), so its real package root is passed along explicitly via PYTHONPATH.
            # `output_name` decouples the produced binary's name from the wrapper
            # script's own (possibly disambiguated) filename.
            with tempfile.TemporaryDirectory() as scratch_dir:
                entrypoint_file = (
                    str(create_entrypoint_script(entrypoint_spec, scratch_dir))
                    if sep
                    else module_file
                )
                if sep:
                    module_is_package = Path(module_file).name == "__init__.py"
                    extra_search_paths = [
                        str(
                            import_path_search_root(
                                module_path, module_file, is_package=module_is_package
                            )
                        )
                    ]
                else:
                    extra_search_paths = []
                compile_with_nuitka(
                    entrypoint_file,
                    stdout=stdout,
                    include_modules=shared_runtime_extensions
                    | set(entrypoint_options.get("include-modules", [])),
                    include_packages=entrypoint_options.get("include-package", []),
                    include_package_data=entrypoint_options.get("include-package-data", []),
                    include_data_files=embed_data_files,
                    extra_flags=entrypoint_options.get("extra_flags", []),
                    extra_search_paths=extra_search_paths,
                    output_name=func_name if sep else None,
                    no_zig=entrypoint_options.get("no-zig", False),
                    no_cache=no_cache,
                )

    return BackendResult(built_artifacts, static_modules, static_build_dir)
