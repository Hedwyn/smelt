"""
Mypyc wrapper, resolves some logic specific to mypyc extensions.

@date: 01.07.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations
from typing import assert_never
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from mypyc.build import mypycify
from setuptools._distutils.extension import Extension

from smelt.utils import (
    get_extension_suffix,
    import_shadowed_module,
    ModpathType,
    toggle_mod_path,
    find_module_in_layout,
)


@dataclass
class MypycExtension:
    """
    Extensions provided by mypyc.
    Tracks some additional data on top of the actual `Extension` object.
    Mypyc ships a runtime module on top of the compiled module which is imported
    at runtime.
    It may be shared between extensions, therefore multiple `MypycExtension` might
    point to the same runtime.
    """

    import_path: str
    src_path: str
    name: str
    dest_folder: Path
    extension: Extension
    runtime: Extension | None = None

    def get_dest_path(self, target_triple: str | None = None) -> Path:
        """
        Returns
        -------
        Path
            Full path for the final compiled .so file.
        """
        suffix = (
            sysconfig.get_config_var("EXT_SUFFIX")
            if target_triple is None
            else get_extension_suffix(target_triple)
        )
        ext_so_name = f"{self.name}{suffix}"
        return self.dest_folder / ext_so_name

    def get_runtime_dest_path(self, target_triple: str | None = None) -> Path:
        """
        Returns
        -------
        Path
            Full path for the final runtime .so file.
        """
        suffix = (
            sysconfig.get_config_var("EXT_SUFFIX")
            if target_triple is None
            else get_extension_suffix(target_triple)
        )
        ext_so_name = f"{self.name}__mypyc{suffix}"
        return self.dest_folder / ext_so_name


def mypycify_module(
    import_path: str,
    extpath: str,
    strategy: ModpathType = ModpathType.IMPORT,
) -> MypycExtension:
    match strategy:
        case ModpathType.IMPORT:
            curated_import_path = toggle_mod_path(import_path, ModpathType.IMPORT)
            with import_shadowed_module(curated_import_path) as mod:
                # TODO: seems that mypy detects the package and names the module package.mod
                # automatically ?
                assert mod.__file__ is not None
                runtime, module_ext = mypycify([extpath], include_runtime_files=True)
                mod_folder = Path(mod.__file__).parent
                ext_name = module_ext.name.split(".")[-1]

        case ModpathType.FS:
            curated_import_path = find_module_in_layout(
                toggle_mod_path(import_path, ModpathType.FS)
            )
            runtime, module_ext = mypycify(
                [curated_import_path], include_runtime_files=True
            )
            mod_folder = Path(curated_import_path).parent

        case _ as unreachable:
            assert_never(unreachable)

    ext_name = module_ext.name.split(".")[-1]

    return MypycExtension(
        import_path=import_path,
        src_path=extpath,
        extension=module_ext,
        runtime=runtime,
        name=ext_name,
        dest_folder=mod_folder,
    )
