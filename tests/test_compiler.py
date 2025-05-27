"""
Test suite for the C-extension compile tools.

@date: 27.05.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Generator, Literal, assert_never, get_args

import pytest

from smelt.compiler import compile_extension

TEST_FOLDER: Final[Path] = Path(__file__).parent
EXTENSION_FOLDER = TEST_FOLDER / "extensions"

# All the extensions avaialble in `extensions` folder
TestExtension = Literal["hello"]
AVAILABLE_EXTENSIONS: list[str] = list(get_args(TestExtension))


@contextmanager
def build_temp_extension(ext_name: str) -> Generator[str, None, None]:
    shared_lib_path = compile_extension(get_extension_path(ext_name))
    try:
        yield shared_lib_path
    finally:
        os.remove(shared_lib_path)


def get_extension_path(ext_name: str) -> Path:
    """
    Returns
    -------
    Path
        Full path to the requested extension
    """
    return EXTENSION_FOLDER / (ext_name + ".c")


@pytest.mark.parametrize("ext_name", AVAILABLE_EXTENSIONS)
def test_extensions_sanity(ext_name: TestExtension) -> None:
    """
    Checks if all available extensions have valid paths
    """
    assert os.path.exists(get_extension_path(ext_name))


@pytest.mark.parametrize("ext_name", AVAILABLE_EXTENSIONS)
def test_compiler_builds_so(ext_name: TestExtension) -> None:
    """
    Verifies that the compiler is able to build a shared library
    """
    with build_temp_extension(ext_name) as shared_lib_path:
        assert os.path.exists(shared_lib_path)
        assert shared_lib_path.endswith(".so")
    assert not os.path.exists(
        shared_lib_path
    ), "`build_temp_extension` fixture did not clean-up properly"


@pytest.mark.parametrize("ext_name", AVAILABLE_EXTENSIONS)
def test_built_so(ext_name: TestExtension) -> None:
    """
    Verifies that the built shared library works
    """
    with build_temp_extension(ext_name):
        ext_mod = importlib.import_module(ext_name)

    match ext_name:
        case "hello":
            hello_func = ext_mod.hello
            assert hello_func() == "Hello World!"

        case _ as unreachable:
            assert_never(unreachable)
