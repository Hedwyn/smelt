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
from typing import Any, Iterable

from mypyc.build import mypycify

from smelt.compiler import compile_extension, compile_zig_module
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
    PathSolver,
    SmeltConfigError,
    SmeltError,
    get_module_name,
    locate_module,
    path_exists,
)

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
        module_so_path = compile_extension(mypyc_ext.extension)
        runtime_so_path = compile_extension(mypyc_ext.runtime)
        so_dest_path = str(mypyc_ext.get_dest_path())
        runtime_dest_path = str(mypyc_ext.get_runtime_dest_path())
        shutil.move(runtime_so_path, runtime_dest_path)
        shutil.move(module_so_path, so_dest_path)
        _logger.info("Built extensions %s @ %s", module.import_path, so_dest_path)
        if mypyc_ext.runtime:
            _logger.info("-> %s runtime: %s", module.import_path, runtime_dest_path)
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
    module_so_path = compile_extension(ext.extension)
    shutil.move(module_so_path, str(ext.get_dest_path()))
    if ext.runtime:
        runtime_so_path = compile_extension(ext.runtime)
        shutil.move(runtime_so_path, str(ext.get_runtime_dest_path()))
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
) -> GenericExtension:
    """
    Compiles `import_path` trying each backend in `backend_priority_order` in turn,
    falling back to the next one when a backend fails to produce a working extension.
    """
    auto_context = _get_auto_mode_context()
    last_exc: Exception | None = None
    for backend in backend_priority_order:
        try:
            ext = _compile_and_place(_generate_with_backend(backend, import_path, path_solver))
        except (SmeltError, RuntimeError, ImportError) as exc:
            auto_context.record_attempt(import_path, backend, error=str(exc))
            _logger.warning("Backend %s failed to compile %s: %s", backend.value, import_path, exc)
            last_exc = exc
            continue
        auto_context.record_attempt(import_path, backend, error=None)
        return ext
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

    Both shared runtimes are included, and both must keep the package-relative
    position they have here: mypyc's is imported under the module's own package
    (`pkg.mod__mypyc`), and smelt's shared Nuitka runtime is resolved through an
    `$ORIGIN` rpath, i.e. from the directory of the `.so` that needs it.
    """
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    nuitka_runtime_name = f"lib{RUNTIME_LIB_NAME}.so"
    targets = artifact_target_folders(config, path_solver)

    built: dict[ImportPath, list[Path]] = {}
    for import_path in sorted(targets):
        dest_folder = targets[import_path]
        module_name = get_module_name(import_path)
        candidates = (
            dest_folder / f"{module_name}{suffix}",
            dest_folder / f"{module_name}__mypyc{suffix}",
            dest_folder / nuitka_runtime_name,
        )
        found = [artifact for artifact in candidates if path_exists(artifact)]
        if found:
            built[import_path] = found
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


def create_entrypoint_script(entrypoint: str, dest_dir: str | os.PathLike[str]) -> Path:
    """
    Codegens a standalone script calling `entrypoint` ("module1.module2:func_name"),
    mirroring what installers generate for `[project.scripts]`. Simpler than those,
    since the only consumer here is Nuitka: no pythonw, no CLI argument handling.
    """
    module_path, sep, func_name = entrypoint.partition(":")
    if not sep or not module_path or not func_name:
        raise SmeltConfigError(
            f"Invalid entrypoint {entrypoint!r}, expected 'module.path:func_name'"
        )
    script = (
        "import sys\n"
        f"from {module_path} import {func_name}\n"
        "\n"
        'if __name__ == "__main__":\n'
        f"    sys.exit({func_name}())\n"
    )
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
) -> list[Path]:
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

    Returns the filesystem paths of every compiled artifact placed next to its
    Python source (module + shared runtime .so/.pyd files), for callers (e.g. the
    hatchling build hook) that need to force-include them in packaging.
    """
    local_platform = platform.system().lower()
    if (platforms := config.platforms) is not None and local_platform not in platforms:
        if stdout is None:
            return []
        printer = _logger.info if stdout == "logger" else print
        printer(
            f"Running on {local_platform}, build hook is restricted to {platforms}, skipping extension building"
        )
        return []

    built_artifacts: list[Path] = []
    path_solver = path_solver or config.get_path_solver()
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )
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

    for native_extension in config.c_extensions:
        sources = native_extension.sources
        if len(sources) > 1:
            raise NotImplementedError("Not supported yet")
        c_extension_path = sources[0]
        parent_folder_path = Path(c_extension_path).parent
        # TODO: we should probably run that logic in temp folder
        built_so_path = compile_extension(c_extension_path)
        so_final_path = parent_folder_path / os.path.basename(built_so_path)
        shutil.move(built_so_path, so_final_path)
        built_artifacts.append(so_final_path)

    # Note: mypyc has a runtime shipped as a separate extension
    # this runtime should be named modname__mypy
    # we need to keep track of it to include to nuitka,
    # as it would be invisible otherwise
    shared_runtime_extensions: set[str] = set()
    collected_extensions: list[GenericExtension] = []
    built_mypyc_extensions = compile_mypyc_extensions(config.mypyc_modules, path_solver)
    for ext in built_mypyc_extensions:
        built_artifacts.append(ext.get_dest_path())
        if ext.runtime:
            shared_runtime_extensions.add(ext.runtime.name)
            built_artifacts.append(ext.get_runtime_dest_path())
    # cython extensions
    collected_extensions.extend(
        compile_cython_extensions(config.cython_modules, path_solver=path_solver)
    )
    for nuitka_mod in config.nuitka_modules:
        collected_extensions.append(nuitkaify_module(nuitka_mod, path_solver=path_solver))

    for generic_ext in collected_extensions:
        _compile_and_place(generic_ext)
        built_artifacts.append(generic_ext.get_dest_path())
        if generic_ext.runtime:
            built_artifacts.append(generic_ext.get_runtime_dest_path())

    # auto-discovered modules (see `config.auto_mode`), each compiled by trying
    # `config.backend_priority_order` in turn until one succeeds. Unlike pinned
    # modules, a module that exhausts every backend is skipped rather than
    # aborting the whole build: "all" mode routinely reaches third-party modules
    # that were never meant to be compiled (soft dependencies, unwritable
    # site-packages, etc).
    for import_path in sorted(discover_auto_targets(config, path_solver)):
        try:
            auto_ext = compile_module_with_fallback(
                import_path, config.backend_priority_order, path_solver
            )
        except SmeltError as exc:
            _logger.warning("Skipping auto-discovered module %s: %s", import_path, exc)
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

    return built_artifacts
