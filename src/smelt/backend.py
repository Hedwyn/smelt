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

from mypyc.build import mypycify

from smelt.compiler import compile_extension
from smelt.nuitkaify import compile_with_nuitka


class SmeltError(Exception):
    """
    Base exception class for Smelt errors
    """


class SmeltMissingModule(SmeltError): ...


@dataclass
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    mypyc: list[str]
    c_extensions: list[str]
    entrypoint: str

    def __str__(self) -> str:
        """
        A human-friendly stringified version of this config.
        """
        lines: list[str] = []
        for field_name, value in asdict(self).items():
            if isinstance(value, list):
                value = ",".join(value)
            lines.append(f"{field_name:20}: {value}")
        return "\n".join(lines)


def run_backend(config: SmeltConfig) -> None:
    # Starting with C extensions
    warnings.warn(
        "`run_backend` implementation is not fully implemented yet and will only "
        "compile C extensions"
    )
    for c_extension in config.c_extensions:
        *parent_path_chunks, extension_name = c_extension.split(".")
        parent_path = ".".join(parent_path_chunks)
        # TODO: as __init__ is not mandatory anymore we cannot rely on that to locate
        # Also, this only works in editable mode
        # modules
        try:
            mod = importlib.import_module(parent_path)
        except ImportError as exc:
            msg = f"Failed to import {parent_path} while looking for extension {c_extension}"
            raise SmeltMissingModule(msg) from exc
        assert mod.__file__ is not None, f"Cannot locate C extension parent path {mod}"
        parent_folder_path = Path(mod.__file__).parent
        c_extension_path = parent_folder_path / (extension_name + ".c")

        # TODO: we should probably run that logic in temp folder
        built_so_path = compile_extension(c_extension_path)
        so_final_path = parent_folder_path / os.path.basename(built_so_path)
        shutil.move(built_so_path, so_final_path)

    for mypyc_extension in config.mypyc:
        try:
            mod = importlib.import_module(mypyc_extension)
        except ImportError as exc:
            msg = f"Failed to import {mypyc_extension} while trying to mypycify"
            raise SmeltMissingModule(msg) from exc
        assert mod.__file__ is not None, f"Cannot module to mypycify: {mypyc_extension}"
        # TODO: seems that mypy detects the package and names the module package.mod
        # automatically ?
        extensions = mypycify([mod.__file__], include_runtime_files=True)
        mod_folder = Path(mod.__file__).parent
        for ext in extensions:
            ext_name = mypyc_extension.split(".")[-1]
            built_so_path = compile_extension(ext)
            built_so_path.replace(mod.__name__, ext_name)
            # TODO: see above
            so_final_path = mod_folder / os.path.basename(built_so_path).replace(
                mypyc_extension, ext_name
            )
            shutil.move(built_so_path, so_final_path)
