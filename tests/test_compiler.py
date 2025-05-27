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
from typing import Final, Generator, Literal, assert_never, cast, get_args

import pytest
from mypyc.build import mypycify
from setuptools import Extension

from smelt.compiler import compile_extension

TEST_FOLDER: Final[Path] = Path(__file__).parent
EXTENSION_FOLDER = TEST_FOLDER / "extensions"
MODULE_FOLDER = TEST_FOLDER / "modules"


# All the extensions available in `extensions` folder
TestExtension = Literal["hello"]
TestModule = Literal["fib"]

AVAILABLE_EXTENSIONS: list[str] = list(get_args(TestExtension))
AVAILABLE_MODULES: Final[list[str]] = list(get_args(TestModule))


@contextmanager
def build_temp_extension(
    ext_name: TestModule | TestExtension,
) -> Generator[str, None, None]:
    if ext_name in AVAILABLE_MODULES:
        cast(TestModule, ext_name)
        (extension,) = mypycify_module(ext_name)
        shared_lib_path = compile_extension(extension)
    else:
        shared_lib_path = compile_extension(get_extension_path(ext_name))
    try:
        yield shared_lib_path
    finally:
        os.remove(shared_lib_path)


def mypycify_module(ext_name: TestModule) -> list[Extension]:
    """
    Builds a C extensioon out of a Python module using mypyc.
    """
    # Expecting to get one extension for mypyc runtime and one for the module
    return mypycify([str(get_module_path(ext_name))])


def get_extension_path(ext_name: TestExtension) -> Path:
    """
    Returns
    -------
    Path
        Full path to the requested extension
    """
    return EXTENSION_FOLDER / (ext_name + ".c")


def get_module_path(mod_name: TestModule) -> Path:
    """
    Returns
    -------
    Path
        Full path to the requested module
    """
    return MODULE_FOLDER / (mod_name + ".py")


@pytest.mark.parametrize("ext_name", AVAILABLE_EXTENSIONS)
def test_extensions_sanity(ext_name: TestExtension) -> None:
    """
    Checks if all available extensions have valid paths
    """
    assert os.path.exists(get_extension_path(ext_name))


@pytest.mark.parametrize("mod_name", AVAILABLE_MODULES)
def test_mypycify_sanity(mod_name: TestModule) -> None:
    """
    Checks if all available extensions have valid paths
    """
    assert len(mypycify_module(mod_name)) == 1


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


@pytest.mark.parametrize("mod_name", AVAILABLE_MODULES)
def test_compiler_compliant_with_mypyc(mod_name: TestModule) -> None:
    with build_temp_extension(mod_name) as shared_lib_path:
        assert os.path.exists(shared_lib_path)
        assert shared_lib_path.endswith(".so")
    assert not os.path.exists(shared_lib_path)


@pytest.mark.parametrize("mod_name", AVAILABLE_MODULES)
def test_compiler_built_mypyc(mod_name: TestModule) -> None:
    with build_temp_extension(mod_name) as shared_lib_path:
        assert os.path.exists(shared_lib_path)
        assert shared_lib_path.endswith(".so")
        ext_mod = importlib.import_module(mod_name)
    match mod_name:
        case "fib":
            fib = ext_mod.fib
            assert fib(10) == 55

        case _ as unreachable:
            assert_never(unreachable)
