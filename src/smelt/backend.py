"""
Build backend implementation for smelt.

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import shutil
import warnings
from pathlib import Path
from typing import Any, Iterable


from smelt.compiler import compile_extension, compile_zig_module
from smelt.mypycify import mypycify_module
from smelt.nuitkaify import Stdout, compile_with_nuitka, nuitkaify_module
from smelt.utils import (
    GenericExtension,
    ModpathType,
    locate_module,
    PathSolver,
)
from smelt.config import SmeltConfig, MypycModule, CythonExtension

# TODO: replace .so references to a variable that's set to .so
# for Unix-like and .dll for Windows

_logger = logging.getLogger(__name__)


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
        module_import_path = module.import_path
        ext_path = module.source or str(
            path_solver.resolve_import_path(module_import_path)
        )
        mypyc_ext = mypycify_module(module_import_path, ext_path)
        module_so_path = compile_extension(mypyc_ext.extension)
        runtime_so_path = compile_extension(mypyc_ext.runtime)
        so_dest_path = str(mypyc_ext.get_dest_path())
        runtime_dest_path = str(mypyc_ext.get_runtime_dest_path())
        shutil.move(runtime_so_path, runtime_dest_path)
        shutil.move(module_so_path, so_dest_path)
        _logger.info("Built extensions %s @ %s", module_import_path, so_dest_path)
        if mypyc_ext.runtime:
            _logger.info("-> %s runtime: %s", module_import_path, runtime_dest_path)
        built_extensions.append(mypyc_ext)
    return built_extensions


def compile_cython_extensions(
    modules: list[CythonExtension],
    options: dict[str, Any] | None = None,
    path_solver: PathSolver | None = None,
) -> list[GenericExtension]:
    """
    Compiles all the cython extensions as defined in `cython_config`
    """
    path_solver = path_solver or PathSolver()
    options = options or {}
    try:
        from Cython.Build import cythonize
    except ImportError as exc:
        raise ImportError(
            "Cython is not installed. consider installing smelt with [cython] extra"
        ) from exc
    extensions: list[GenericExtension] = []

    for module in modules:
        source_path = module.source
        import_path = module.import_path
        cython_ext = cythonize(source_path, **options)
        assert len(cython_ext) == 1, (
            "Passed on source file to cython yet it produced more than one extension"
        )
        (base_ext,) = cython_ext
        ext_name = import_path.split(".")[-1]
        base_ext.name = ext_name
        generic_ext = GenericExtension.factory(
            src_path=source_path,
            import_path=import_path,
            extension=base_ext,
            dest_folder=Path(source_path).parent,
        )
        extensions.append(generic_ext)
    return extensions


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
    path_solver = path_solver or config.get_path_solver()
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )
    for zig_mod in config.zig_modules:
        compile_zig_module(zig_mod.name, zig_mod.folder, zig_mod.import_path)

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
        module_so_path = compile_extension(generic_ext.extension)
        shutil.move(module_so_path, str(generic_ext.get_dest_path()))
        if generic_ext.runtime:
            runtime_so_path = compile_extension(generic_ext.extension)
            shutil.move(runtime_so_path, str(generic_ext.get_runtime_dest_path()))
    # nuitka entrypoint compilation
    without_entrypoint = without_entrypoint and config.entrypoint is not None
    if not without_entrypoint:
        entrypoint_file = locate_module(
            config.entrypoint, strategy=strategy, package_root=path_solver
        )
        compile_with_nuitka(
            entrypoint_file, stdout=stdout, include_modules=shared_runtime_extensions
        )
