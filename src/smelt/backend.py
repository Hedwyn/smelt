"""
Build backend implementation for smelt.

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Self

from smelt.compiler import compile_extension, compile_zig_module
from smelt.mypycify import mypycify_module
from smelt.nuitkaify import Stdout, compile_with_nuitka
from smelt.utils import (
    GenericExtension,
    ModpathType,
    locate_module,
    toggle_mod_path,
    PathSolver,
    PackageRootPath,
)

# TODO: replace .so references to a variable that's set to .so
# for Unix-like and .dll for Windows

_logger = logging.getLogger(__name__)


@dataclass
class NativeExtension:
    import_path: str
    sources: list[str]


@dataclass
class CythonExtension:
    import_path: str
    source: str


@dataclass
class MypycModule:
    import_path: str
    source: str | None = None


@dataclass
class ZigModule:
    name: str
    import_path: str
    folder: str = "."


@dataclass
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    packages_location: dict[str, str] = field(default_factory=dict)
    mypyc_options: dict[str, Any] = field(default_factory=dict)
    mypyc_modules: list[MypycModule] = field(default_factory=list)
    cython_options: dict[str, Any] = field(default_factory=dict)
    cython_modules: list[CythonExtension] = field(default_factory=list)
    c_extensions: list[NativeExtension] = field(default_factory=list)
    zig_modules: list[ZigModule] = field(default_factory=list)
    entrypoint: str | None = None
    debug: bool = False

    @classmethod
    def from_toml_data(cls, toml_data: dict[str, Any]) -> Self:
        # native code
        native_extensions_decl = toml_data.pop("c_extensions", [])
        native_extensions = [NativeExtension(**decl) for decl in native_extensions_decl]
        # zig modules
        zig_modules_decl = toml_data.pop("zig_modules", [])
        zig_modules = [ZigModule(**decl) for decl in zig_modules_decl]
        # mypyc modules
        mypyc_modules_decl = toml_data.pop("mypyc_modules", [])
        mypyc_modules = [MypycModule(**decl) for decl in mypyc_modules_decl]
        # cython
        cython_modules_decl = toml_data.pop("cython_modules", [])
        cython_modules = [CythonExtension(**decl) for decl in cython_modules_decl]
        return cls(
            mypyc_modules=mypyc_modules,
            c_extensions=native_extensions,
            zig_modules=zig_modules,
            cython_modules=cython_modules,
            **toml_data,
        )

    def get_path_solver(self) -> PathSolver:
        """
        Builds a PathSolver based on the package configuration.
        """
        return PathSolver(
            known_roots=[
                PackageRootPath(alias, Path(path))
                for alias, path in self.packages_location.items()
            ]
        )

    def __str__(self) -> str:
        """
        A human-friendly stringified version of this config.
        """
        lines: list[str] = []
        for field_name, value in asdict(self).items():
            if isinstance(value, list):
                value = ",".join(value)
            if isinstance(value, dict):
                value = "".join(
                    ("\n * " + f"{key} -> {val}" for key, val in value.items())
                )
            lines.append(f"{field_name:20}: {value}")
        return "\n".join(lines)

    def load_env(self) -> None:
        """
        Updates internal values based on set environement variables.
        """
        if os.environ.get("SMELT_DEBUG"):
            self.debug = True


def compile_mypyc_extensions(
    modules: Iterable[MypycModule],
    path_solver: PathSolver | None = None,
) -> list[GenericExtension]:
    """
    Compiles all mypy extensions defined in `mypyc_config` for the project found at `project_root`
    """
    path_solver = path_solver or PathSolver()
    built_extensions: list[GenericExtension] = []
    # for mypyc_extension, ext_path in mypyc_config.items():
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
        generic_ext = GenericExtension(
            name=ext_name,
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
    for generic_ext in collected_extensions:
        module_so_path = compile_extension(generic_ext.extension)
        shutil.move(module_so_path, str(generic_ext.get_dest_path()))
        if generic_ext.runtime:
            runtime_so_path = compile_extension(generic_ext.extension)
            shutil.move(runtime_so_path, str(generic_ext.get_runtime_dest_path()))
    # nuitka compile
    without_entrypoint = without_entrypoint and config.entrypoint is not None
    if not without_entrypoint:
        entrypoint_file = locate_module(
            config.entrypoint, strategy=strategy, package_root=path_solver
        )
        compile_with_nuitka(
            entrypoint_file, stdout=stdout, include_modules=shared_runtime_extensions
        )
