"""
Mypyc wrapper, resolves some logic specific to mypyc extensions.

@date: 01.07.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from mypyc.build import mypycify

from smelt.utils import (
    GenericExtension,
    ModpathType,
    find_module_in_layout,
    get_extension_suffix,
    import_shadowed_module,
    toggle_mod_path,
)


def mypycify_module(
    import_path: str,
    extpath: str,
    strategy: ModpathType = ModpathType.IMPORT,
    package_root: Path = Path("."),
) -> GenericExtension:
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
            expected_module_path = toggle_mod_path(import_path, ModpathType.FS)
            curated_import_path = find_module_in_layout(
                expected_module_path, package_root=package_root
            )
            runtime, module_ext = mypycify(
                [curated_import_path], include_runtime_files=True
            )
            mod_folder = Path(curated_import_path).parent

        case _ as unreachable:
            assert_never(unreachable)

    ext_name = module_ext.name.split(".")[-1]

    return GenericExtension(
        import_path=import_path,
        src_path=extpath,
        extension=module_ext,
        runtime=runtime,
        name=ext_name,
        dest_folder=mod_folder,
    )
