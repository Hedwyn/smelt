"""
Tests for statically linking smelt's own extension modules into a mode `own`
interpreter (see `smelt.static_python`).

@date: 08.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from setuptools import Extension

from smelt.compiler import compile_extension_objects
from smelt.native_deps import is_supported_platform
from smelt.own_python import INTERPRETER_REL_PATH, own_python_cache_dir
from smelt.static_python import (
    StaticPythonError,
    _check_module_name,
    _find_libpython,
    _unexpected_dependencies,
    build_static_interpreter,
    generate_inittab_shim,
)
from smelt.utils import assert_path_exists

TEST_FOLDER: Final[Path] = Path(__file__).parent
EXTENSION_FOLDER = TEST_FOLDER / "extensions"

#: A real interpreter build takes minutes; reuse whatever is already cached, exactly
#: like `test_own_python.py`'s own `needs_own_python`.
_CACHED_PREFIX = own_python_cache_dir()
_CACHE_AVAILABLE = (_CACHED_PREFIX / INTERPRETER_REL_PATH).exists()

needs_own_python = pytest.mark.skipif(
    not _CACHE_AVAILABLE or not is_supported_platform(),
    reason=f"needs Linux and an interpreter already built at {_CACHED_PREFIX}",
)


def test_generate_inittab_shim_registers_every_module() -> None:
    shim = generate_inittab_shim(["hello", "fib"])
    assert "extern PyObject* PyInit_hello(void);" in shim
    assert "extern PyObject* PyInit_fib(void);" in shim
    assert 'PyImport_AppendInittab("hello", PyInit_hello);' in shim
    assert 'PyImport_AppendInittab("fib", PyInit_fib);' in shim


def test_check_module_name_accepts_a_plain_identifier() -> None:
    _check_module_name("hello")  # must not raise


@pytest.mark.parametrize("name", ["pkg.mod", "1bad", "has-dash", ""])
def test_check_module_name_refuses_anything_not_a_bare_identifier(name: str) -> None:
    with pytest.raises(StaticPythonError):
        _check_module_name(name)


def test_build_static_interpreter_is_a_noop_without_static_modules(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\necho fake\n")
    fake_python.chmod(0o755)

    result = build_static_interpreter(assert_path_exists(prefix), {})
    assert result == fake_python


def test_build_static_interpreter_refuses_a_missing_interpreter(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    with pytest.raises(StaticPythonError):
        build_static_interpreter(assert_path_exists(prefix), {"hello": [tmp_path / "hello.o"]})


def test_unexpected_dependencies_ignores_the_current_interpreter() -> None:
    """
    `sys.executable` links against libc/the loader (host-supplied) and, on most
    distros, libpython itself -- nothing this check should ever flag.
    """
    assert _unexpected_dependencies(assert_path_exists(sys.executable)) == []


@needs_own_python
def test_find_libpython_locates_the_shared_library() -> None:
    libpython = _find_libpython(assert_path_exists(_CACHED_PREFIX))
    assert libpython.name.startswith("libpython")


@needs_own_python
def test_build_static_interpreter_links_a_dependency_free_extension(tmp_path: Path) -> None:
    """
    End-to-end: a mode `own` prefix's `bin/python` gets a Tier-1-eligible module
    (`hello`, no external `libraries`) linked straight in, reports it as built-in
    (`(built-in)`, no backing file), and still runs everything else normally.
    """
    prefix = tmp_path / "own"
    shutil.copytree(_CACHED_PREFIX, prefix, symlinks=True)

    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    hello_source = assert_path_exists(EXTENSION_FOLDER / "hello.c")
    objects = compile_extension_objects(
        Extension(name="hello", sources=[str(hello_source)]), objects_dir
    )

    new_bin = build_static_interpreter(assert_path_exists(prefix), {"hello": objects})
    assert new_bin == prefix / INTERPRETER_REL_PATH

    completed = subprocess.run(
        [
            str(new_bin),
            "-I",
            "-S",
            "-c",
            "import hello, sys; print(hello.hello()); print(sys.modules['hello'].__spec__.origin)",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert lines[0] == "Hello World!"
    assert lines[1] == "built-in"

    # No loose `.so` was ever written for it.
    assert not any(prefix.rglob("hello*.so"))


#: A system library practically guaranteed to be present as a runtime `.so` wherever
#: this suite runs (it is a transitive dependency of CPython itself), used to force a
#: real, non-optimized-away `DT_NEEDED` entry for the Tier 2 tests below. Referencing
#: a symbol from it (rather than just naming it in `libraries=`, which
#: `build_static_interpreter` never even reads -- it only ever links the object files
#: it is handed) is what actually gets it into the trial link: an unreferenced shared
#: object passed straight to the linker is dropped by `--as-needed` before it ever
#: reaches `DT_NEEDED`.
_LIBZ_SONAME: Final = "libz.so.1"


def _find_libz() -> Path | None:
    for candidate in (
        "/usr/lib/libz.so.1",
        "/usr/lib/x86_64-linux-gnu/libz.so.1",
        "/lib/libz.so.1",
    ):
        if Path(candidate).is_file():
            return Path(candidate)
    return None


def _compile_leaky_extension(dest_folder: Path) -> list[Path]:
    """
    A minimal Python extension module (`PyInit_leaky`) that references a zlib symbol,
    so linking it drags in `libz.so.1` for real -- the implicit dependency Tier 1
    (a purely structural check on `Extension.libraries`) cannot see.
    """
    source = dest_folder / "leaky.c"
    source.write_text(
        "#include <Python.h>\n"
        "extern const char *zlibVersion(void);\n"
        "static struct PyModuleDef leakymodule = {\n"
        '    PyModuleDef_HEAD_INIT, "leaky", NULL, -1, NULL,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_leaky(void) {\n"
        "    (void)zlibVersion();\n"
        "    return PyModule_Create(&leakymodule);\n"
        "}\n"
    )
    return compile_extension_objects(Extension(name="leaky", sources=[str(source)]), dest_folder)


def test_unexpected_dependencies_flags_a_library_beyond_libc_and_libpython(
    tmp_path: Path,
) -> None:
    libz = _find_libz()
    if libz is None:
        pytest.skip(f"{_LIBZ_SONAME} not found on this system")

    from smelt.compiler import ZigCompiler

    objects = _compile_leaky_extension(tmp_path)
    compiler = ZigCompiler()
    compiler.link_shared_object(
        [*(str(obj) for obj in objects), str(libz)],
        "leaky.so",
        output_dir=str(tmp_path),
        libraries=[],
        library_dirs=[],
        runtime_library_dirs=[],
        extra_preargs=[],
    )
    unexpected = _unexpected_dependencies(assert_path_exists(tmp_path / "leaky.so"))
    assert any(name.startswith("libz") for name in unexpected)


@needs_own_python
def test_build_static_interpreter_refuses_a_module_pulling_in_an_external_library(
    tmp_path: Path,
) -> None:
    """
    Tier 2, end-to-end: a module with no declared `libraries` (Tier 1 would wave it
    through) but that references a symbol only `libz` provides must still be refused,
    and must leave the existing interpreter untouched.
    """
    libz = _find_libz()
    if libz is None:
        pytest.skip(f"{_LIBZ_SONAME} not found on this system")

    prefix = tmp_path / "own"
    shutil.copytree(_CACHED_PREFIX, prefix, symlinks=True)
    original_bin = (prefix / INTERPRETER_REL_PATH).read_bytes()

    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    leaky_objects = _compile_leaky_extension(objects_dir)

    with pytest.raises(StaticPythonError):
        build_static_interpreter(assert_path_exists(prefix), {"leaky": [*leaky_objects, libz]})

    # A failed Tier 2 check must leave the existing interpreter untouched.
    assert (prefix / INTERPRETER_REL_PATH).read_bytes() == original_bin
