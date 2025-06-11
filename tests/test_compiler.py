"""
Test suite for the C-extension compile tools.

@date: 27.05.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sysconfig
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Generator, Literal, assert_never, cast, get_args

import pytest
from mypyc.build import mypycify
from setuptools import Extension

from smelt.compiler import (
    PYCONFIG_PATH,
    SupportedPlatforms,
    compile_extension,
    get_extension_suffix,
)

TEST_FOLDER: Final[Path] = Path(__file__).parent
EXTENSION_FOLDER = TEST_FOLDER / "extensions"
MODULE_FOLDER = TEST_FOLDER / "modules"


# All the extensions available in `extensions` folder
TestExtension = Literal["hello"]
TestModule = Literal["fib", "entrypoint"]

AVAILABLE_EXTENSIONS: list[str] = list(get_args(TestExtension))
AVAILABLE_MODULES: Final[list[str]] = list(get_args(TestModule))


@pytest.fixture(scope="session")
def local_platform_triple() -> str:
    """
    Returns
    -------
    str
        Platform triple of the device running this test suite
        Will be used to check some details of the output of native compilation
        tests, as their outputs are by definition platform dependant.
    """
    # harc-ocoding gnu
    system = platform.system().lower()
    match system:
        case "darwin":
            return f"{platform.machine()}-apple-darwin"

        case "windows":
            # TODO: one might actually use mingw32 on Windows
            # which would make this triple incorrect...
            # maybe use sysconfig.get_platform() ?
            return f"{platform.machine()}-windows-msvc"

        case "linux":
            return f"{platform.machine()}-linux-gnu"

        case _:
            raise RuntimeError(f"Running tests on unsupported OS: {system}")


def test_so_extension_suffix(local_platform_triple: str) -> None:
    """
    Checks that `get_extension_suffix` produces the same output
    as the sysconfig EXT_SUFFIX when used on the local platform
    """
    so_suffix = get_extension_suffix(local_platform_triple)
    assert so_suffix == sysconfig.get_config_var("EXT_SUFFIX")


@pytest.mark.parametrize(
    "python_version,platform_triple",
    [
        ["python3.12", "aarch64-linux-gnu"],
    ],
)
def test_pyconfig_headers_bundled(python_version: str, platform_triple: str) -> None:
    """
    Verifies that the pyconfig.h headers are properly bundled
    as package data
    """
    base_path = Path(PYCONFIG_PATH)
    pyconfig_path = base_path / platform_triple / python_version / "pyconfig.h"
    assert os.path.exists(pyconfig_path), f"Missing pyconfig.h @{pyconfig_path}"


@contextmanager
def build_temp_extension(
    ext_name: TestModule | TestExtension,
    crosscompile: SupportedPlatforms | None = None,
) -> Generator[str, None, None]:
    if ext_name in AVAILABLE_MODULES:
        cast(TestModule, ext_name)
        (extension,) = mypycify_module(ext_name)
        shared_lib_path = compile_extension(extension, crosscompile=crosscompile)
    else:
        shared_lib_path = compile_extension(
            get_extension_path(ext_name), crosscompile=crosscompile
        )
    try:
        yield shared_lib_path
    finally:
        if extensions_folder := os.environ.get("SAVE_EXTENSIONS", None):
            shutil.copy(shared_lib_path, extensions_folder)
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
@pytest.mark.parametrize(
    "platform", [SupportedPlatforms.AARCH64_LINUX, SupportedPlatforms.ARMV7L_LINUX]
)
def test_compiler_crosscompiled_so(
    ext_name: TestExtension, platform: SupportedPlatforms
) -> None:
    """
    Verifies that the compiler is able to build a shared library for a foreign platform.
    Does not verify that the shared library works - this requires tooling beyond
    what a isolated Python test session can do.
    """
    with build_temp_extension(ext_name, crosscompile=platform) as shared_lib_path:
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

            case "entrypoint":
                import subprocess
                import sys

                p = subprocess.run(
                    [sys.executable, "-c", "import " + mod_name, "10"],
                    check=True,
                )
                # .so modules cannot be run as main,
                # so just checking here that it imports without errors
                assert p.returncode == 0, (
                    "Entrypoint module did not run successfully.: " + shared_lib_path
                )

            case _ as unreachable:
                assert_never(unreachable)
