"""
Build backend implementation for smelt.

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import warnings
from pathlib import Path
from typing import Any, Iterable


from smelt.compiler import compile_extension, compile_zig_module

from mypyc.build import mypycify
from smelt.explorer import (
    build_dependency_graph,
    find_modules_under_root,
    flatten_dependency_graph,
    has_local_source,
)
from smelt.nuitkaify import Stdout, compile_with_nuitka, nuitkaify_module
from smelt.utils import (
    GenericExtension,
    ImportPath,
    ModpathType,
    PathSolver,
    SmeltConfigError,
    SmeltError,
    locate_module,
)
from smelt.config import (
    Backend,
    CythonExtension,
    MypycModule,
    NuitkaModule,
    SmeltConfig,
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


def compile_module_with_fallback(
    import_path: ImportPath,
    backend_priority_order: Iterable[Backend],
    path_solver: PathSolver,
) -> GenericExtension:
    """
    Compiles `import_path` trying each backend in `backend_priority_order` in turn,
    falling back to the next one when a backend fails to produce a working extension.
    """
    last_exc: Exception | None = None
    for backend in backend_priority_order:
        try:
            return _compile_and_place(
                _generate_with_backend(backend, import_path, path_solver)
            )
        except (SmeltError, RuntimeError, ImportError) as exc:
            _logger.warning(
                "Backend %s failed to compile %s: %s", backend.value, import_path, exc
            )
            last_exc = exc
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
                node.name
                for node in flatten_dependency_graph(build_dependency_graph(module))
            )

    pinned = _pinned_import_paths(config)
    return {name for name in discovered - pinned if has_local_source(name)}


def run_backend(
    config: SmeltConfig,
    stdout: Stdout | None = None,
    path_solver: PathSolver | None = None,
    strategy: ModpathType = ModpathType.FS,
    *,
    without_entrypoint: bool = False,
) -> None:
    """
    Runs the whole backend pipeline:
    * C extensions compilation
    * mypyc extensions
    * Nuitka compilation
    """
    local_platform = platform.system().lower()
    if (platforms := config.platforms) is not None and local_platform not in platforms:
        if stdout is None:
            return
        printer = _logger.info if stdout == "logger" else print
        printer(
            f"Running on {local_platform}, build hook is restricted to {platforms}, skipping extension building"
        )
        return

    path_solver = path_solver or config.get_path_solver()
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )
    for zig_mod in config.zig_modules:
        compile_zig_module(
            zig_mod.name,
            zig_mod.folder,
            zig_mod.import_path,
            flags=zig_mod.flags,
            path_solver=path_solver,
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

    # Note: mypyc has a runtime shipped as a separate extension
    # this runtime should be named modname__mypy
    # we need to keep track of it to include to nuitka,
    # as it would be invisible otherwise
    shared_runtime_extensions: set[str] = set()
    collected_extensions: list[GenericExtension] = []
    built_mypyc_extensions = compile_mypyc_extensions(config.mypyc_modules, path_solver)
    for ext in built_mypyc_extensions:
        if ext.runtime:
            shared_runtime_extensions.add(ext.runtime.name)
    # cython extensions
    collected_extensions.extend(
        compile_cython_extensions(config.cython_modules, path_solver=path_solver)
    )
    for nuitka_mod in config.nuitka_modules:
        collected_extensions.append(
            nuitkaify_module(nuitka_mod, path_solver=path_solver)
        )

    for generic_ext in collected_extensions:
        _compile_and_place(generic_ext)

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
        if auto_ext.runtime:
            shared_runtime_extensions.add(auto_ext.runtime.name)

    # nuitka entrypoint compilation
    without_entrypoint = without_entrypoint or config.entrypoint is None
    if not without_entrypoint:
        entrypoint_file = locate_module(
            config.entrypoint, strategy=strategy, package_root=path_solver.project_root
        )
        compile_with_nuitka(
            entrypoint_file, stdout=stdout, include_modules=shared_runtime_extensions
        )
