"""
Build backend implementation for smelt.

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import os
import shutil
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from smelt.compiler import compile_extension
from smelt.mypycify import mypycify_module
from smelt.nuitkaify import Stdout, compile_with_nuitka
from smelt.utils import SmeltMissingModule

# TODO: replace .so references to a variable that's set to .so
# for Unix-like and .dll for Windows


@dataclass
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    mypyc: dict[str, str]
    c_extensions: dict[str, str]
    entrypoint: str

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


def run_backend(
    config: SmeltConfig, stdout: Stdout | None = None, project_root: Path | str = "."
) -> None:
    """
    Runs the whole backend pipeline:
    * C extensions compilation
    * mypyc extensions
    * Nuitka compilation
    """
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )
    for c_extension, relative_path in config.c_extensions.items():
        c_extension_path = os.path.join(project_root, relative_path)
        parent_folder_path = Path(c_extension_path).parent
        # TODO: we should probably run that logic in temp folder
        built_so_path = compile_extension(c_extension_path)
        so_final_path = parent_folder_path / os.path.basename(built_so_path)
        shutil.move(built_so_path, so_final_path)

    # Note: mypyc has a runtime shipped as a separate extension
    # this runtime should be named modname__mypy
    # we need to keep track of it to include to nuitka,
    # as it would be invisible otherwise
    mypy_runtime_extensions: list[str] = []
    for mypyc_extension, ext_path in config.mypyc.items():
        full_ext_path = os.path.join(project_root, ext_path)
        mypyc_ext = mypycify_module(mypyc_extension, full_ext_path)
        module_so_path = compile_extension(mypyc_ext.extension)
        shutil.move(module_so_path, str(mypyc_ext.get_dest_path()))
        if (runtime := mypyc_ext.runtime) is not None:
            mypy_runtime_extensions.append(runtime.name)
    # nuitka compile
    entrypoint = config.entrypoint
    try:
        entrypoint_mod = importlib.import_module(entrypoint)
    except ImportError as exc:
        msg = f"Failed to import entrypoint: {entrypoint}"
        raise SmeltMissingModule(msg) from exc
    assert (
        entrypoint_mod.__file__ is not None
    ), f"Failed to locate entrypoint: {entrypoint}"
    compile_with_nuitka(
        entrypoint_mod.__file__, stdout=stdout, include_modules=mypy_runtime_extensions
    )
