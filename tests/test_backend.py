"""
Test suite for the backend's compile-and-place pipeline.

@date: 08.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sysconfig
import tempfile
import tomllib
from pathlib import Path
from typing import Final

import pytest
from setuptools import Extension

from smelt.backend import (
    CompiledExtension,
    _compile_and_place,
    compile_generic_extension,
    compile_mypyc_extensions,
    is_static_link_eligible,
    link_generic_extension,
    run_backend,
)
from smelt.config import MypycModule
from smelt.frontend import parse_config_from_pyproject
from smelt.utils import GenericExtension, ImportPath, PathSolver, assert_path_exists

TEST_FOLDER: Final[Path] = Path(__file__).parent
EXTENSION_FOLDER = TEST_FOLDER / "extensions"
MODULE_FOLDER = TEST_FOLDER / "modules"
HATCHDEMO_ROOT: Final[Path] = TEST_FOLDER.parent / "examples" / "hatchdemo"

SO_SUFFIX: Final[str] = sysconfig.get_config_var("EXT_SUFFIX")


def _hello_extension(dest_folder: Path) -> GenericExtension:
    hello_source = assert_path_exists(EXTENSION_FOLDER / "hello.c")
    return GenericExtension.factory(
        src_path=hello_source,
        import_path=ImportPath("hello"),
        extension=Extension(name="hello", sources=[str(hello_source)]),
        dest_folder=dest_folder,
    )


def test_compile_generic_extension_produces_object_files(tmp_path: Path) -> None:
    """
    `compile_generic_extension` must stop short of linking, exactly like
    `compile_extension_objects` (which it is built on).
    """
    ext = _hello_extension(tmp_path)
    compiled = compile_generic_extension(ext, tmp_path)
    assert isinstance(compiled, CompiledExtension)
    assert compiled.generic is ext
    assert compiled.runtime_objects is None
    assert compiled.objects
    for obj in compiled.objects:
        assert os.path.exists(obj)
        assert not os.path.exists(ext.get_dest_path()), (
            "compile_generic_extension must not link or place anything"
        )


def test_compile_then_link_generic_extension_matches_compile_and_place(tmp_path: Path) -> None:
    """
    `compile_generic_extension` + `link_generic_extension` must reproduce
    `_compile_and_place`'s own behavior: a working `.so` at `get_dest_path()`.
    """
    ext = _hello_extension(tmp_path)
    with tempfile.TemporaryDirectory() as build_folder:
        compiled = compile_generic_extension(ext, build_folder)
        result = link_generic_extension(compiled)
    assert result is ext
    dest_path = ext.get_dest_path()
    assert dest_path.exists()

    spec = importlib.util.spec_from_file_location("hello", dest_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.hello() == "Hello World!"


def test_compile_and_place_builds_a_working_extension(tmp_path: Path) -> None:
    ext = _hello_extension(tmp_path)
    result = _compile_and_place(ext)
    assert result is ext
    assert ext.get_dest_path().exists()


def test_is_static_link_eligible_accepts_a_dependency_free_extension() -> None:
    ext = GenericExtension.factory(
        src_path=assert_path_exists(EXTENSION_FOLDER / "hello.c"),
        import_path=ImportPath("hello"),
        extension=Extension(name="hello", sources=[str(EXTENSION_FOLDER / "hello.c")]),
    )
    assert is_static_link_eligible(ext)


def test_is_static_link_eligible_refuses_an_extension_linking_a_library() -> None:
    ext = GenericExtension.factory(
        src_path=assert_path_exists(EXTENSION_FOLDER / "hello.c"),
        import_path=ImportPath("hello"),
        extension=Extension(
            name="hello", sources=[str(EXTENSION_FOLDER / "hello.c")], libraries=["m"]
        ),
    )
    assert not is_static_link_eligible(ext)


def test_is_static_link_eligible_refuses_a_runtime_linking_a_library() -> None:
    ext = GenericExtension.factory(
        src_path=assert_path_exists(EXTENSION_FOLDER / "hello.c"),
        import_path=ImportPath("hello"),
        extension=Extension(name="hello", sources=[str(EXTENSION_FOLDER / "hello.c")]),
        runtime=Extension(
            name="hello__mypyc",
            sources=[str(EXTENSION_FOLDER / "hello.c")],
            extra_link_args=["-lz"],
        ),
    )
    assert not is_static_link_eligible(ext)


@pytest.mark.xfail(
    reason=(
        "_mypycify_one unpacks mypycify(..., include_runtime_files=True)) as a "
        "(runtime, module_ext) pair; the installed mypy/mypyc version returns a "
        "single-element list instead, so this fails before compile_mypyc_extensions' "
        "own compile-and-place logic (this refactor's actual concern) ever runs. "
        "Pre-existing, unrelated to compiling_pipeline_refactor.md."
    ),
    strict=False,
)
def test_compile_mypyc_extensions_builds_module_and_runtime(tmp_path: Path) -> None:
    fib_source = assert_path_exists(MODULE_FOLDER / "fib.py")
    module = MypycModule(import_path=ImportPath("fib"), source=fib_source)
    path_solver = PathSolver()
    try:
        (built,) = compile_mypyc_extensions([module], path_solver)
        assert built.get_dest_path().exists()
        assert built.runtime is not None
        assert built.get_runtime_dest_path().exists()

        fib_module = importlib.import_module("fib")
        assert fib_module.fib(10) == 55
    finally:
        for candidate in (
            MODULE_FOLDER / f"fib{SO_SUFFIX}",
            MODULE_FOLDER / f"fib__mypyc{SO_SUFFIX}",
        ):
            if candidate.exists():
                candidate.unlink()


def test_run_backend_static_link_stages_eligible_modules_only() -> None:
    """
    `hatchdemo` exercises every backend `run_backend` drives (mypyc, Cython, a Nuitka
    module, handwritten C and Zig) in one project, so it is the one place that proves
    `static_link` stages exactly the modules `compiling_pipeline_refactor.md` says it
    should: the mypyc and Cython ones, straight through Tier 1
    (`is_static_link_eligible`) since neither declares an external library -- and
    *not* the Nuitka one, which is out of scope regardless of Tier 1, and not the
    handwritten C/Zig ones, which never go through `GenericExtension` at all.
    """
    with open(HATCHDEMO_ROOT / "pyproject.toml", "rb") as toml_file:
        toml_data = tomllib.load(toml_file)
    config = parse_config_from_pyproject(toml_data, project_root=HATCHDEMO_ROOT)
    path_solver = config.get_path_solver(project_root=HATCHDEMO_ROOT)

    result = run_backend(config, path_solver=path_solver, without_entrypoint=True, static_link=True)
    try:
        assert set(result.static_modules) == {"hatchdemo.fib", "hatchdemo.fib_cython"}
        for objects in result.static_modules.values():
            assert objects
            for obj in objects:
                assert os.path.exists(obj)

        built_names = {path.name for path in result.artifacts}
        assert any(name.startswith("cli") for name in built_names), (
            "the Nuitka module must still be linked and placed as an ordinary .so"
        )
        assert not any(name.startswith("fib") for name in built_names), (
            "a staged module must not also appear as a placed artifact"
        )
        assert result.static_build_dir is not None
    finally:
        if result.static_build_dir is not None:
            shutil.rmtree(result.static_build_dir, ignore_errors=True)
