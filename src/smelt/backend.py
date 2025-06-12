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
            parent_mod = importlib.import_module(parent_path)
        except ImportError as exc:
            msg = f"Failed to import {parent_path} while looking for extension {c_extension}"
            raise SmeltMissingModule(msg) from exc
        assert (
            parent_mod.__file__ is not None
        ), f"Cannot locate C extension parent path {parent_mod}"
        parent_folder_path = Path(parent_mod.__file__).parent
        c_extension_path = parent_folder_path / (extension_name + ".c")

        # TODO: we should probably run that logic in temp folder
        built_so_path = compile_extension(c_extension_path)
        so_final_path = parent_folder_path / os.path.basename(built_so_path)
        shutil.move(built_so_path, so_final_path)
