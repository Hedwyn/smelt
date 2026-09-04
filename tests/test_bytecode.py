from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from smelt.bytecode import (
    PYC_SUFFIX,
    BytecodeCompilationError,
    PycTargetTag,
    compile_module,
    compile_to_pyc,
    module_dest_rel_path,
)
from smelt.utils import assert_is_valid_import_path, assert_path_exists

DEST_PATH_TESTS = {
    ("pkg.sub.mod", False): Path("pkg/sub/mod.pyc"),
    ("pkg.sub", True): Path("pkg/sub/__init__.pyc"),
    ("mod", False): Path("mod.pyc"),
    ("pkg", True): Path("pkg/__init__.pyc"),
}


@pytest.mark.parametrize(("spec", "expected"), DEST_PATH_TESTS.items())
def test_module_dest_rel_path(spec: tuple[str, bool], expected: Path) -> None:
    import_path, is_package = spec
    dest = module_dest_rel_path(assert_is_valid_import_path(import_path), is_package=is_package)
    assert dest == expected


def test_target_tag_matches_running_interpreter() -> None:
    tag = PycTargetTag.current()
    assert tag.python_version == (sys.version_info.major, sys.version_info.minor)
    assert tag.magic_number == importlib.util.MAGIC_NUMBER
    assert tag.version_string == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert tag.serialize()["magic_number"] == importlib.util.MAGIC_NUMBER.hex()


def test_target_tag_resolves_default_optimize_level() -> None:
    """
    -1 means "whatever this interpreter runs with" for `py_compile`, and a recorded tag
    has to name an actual level instead of that placeholder.
    """
    assert PycTargetTag.current(-1).optimize == sys.flags.optimize
    assert PycTargetTag.current(2).optimize == 2


def test_compiled_pyc_is_importable_without_its_source(tmp_path: Path) -> None:
    """
    The whole premise of the backend: a `.pyc` tree with no `.py` in it imports.
    """
    source_dir = tmp_path / "source"
    (source_dir / "pkg").mkdir(parents=True)
    (source_dir / "pkg" / "__init__.py").write_text("VALUE = 1\n")
    (source_dir / "pkg" / "mod.py").write_text("def hello() -> str:\n    return 'hi'\n")

    dist = tmp_path / "dist"
    compile_module(
        assert_is_valid_import_path("pkg"),
        assert_path_exists(source_dir / "pkg" / "__init__.py"),
        dist,
        is_package=True,
    )
    artifact = compile_module(
        assert_is_valid_import_path("pkg.mod"),
        assert_path_exists(source_dir / "pkg" / "mod.py"),
        dist,
        is_package=False,
    )
    assert artifact.dest_rel_path == Path("pkg/mod.pyc")
    assert not list(dist.rglob("*.py"))

    # Run the folder itself rather than passing it through PYTHONPATH: `-I` implies
    # `-E`, so PYTHONPATH would be ignored. A directory argument is put on `sys.path`
    # and its `__main__` is run, which is exactly how a distribution folder is run.
    main_source = source_dir / "entry.py"
    main_source.write_text("from pkg.mod import hello\n\nprint(hello())\n")
    compile_to_pyc(assert_path_exists(main_source), dist / f"__main__{PYC_SUFFIX}")
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(dist)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "hi"


def test_compilation_is_deterministic(tmp_path: Path) -> None:
    """
    Unchecked-hash invalidation carries no mtime, so the same source always produces
    the same bytes -- which is what makes a rebuilt distribution comparable.
    """
    source = tmp_path / "mod.py"
    source.write_text("X = 1\n")
    first = compile_to_pyc(assert_path_exists(source), tmp_path / "first.pyc")
    source.touch()
    second = compile_to_pyc(assert_path_exists(source), tmp_path / "second.pyc")
    assert first.read_bytes() == second.read_bytes()


def test_optimize_level_two_strips_docstrings(tmp_path: Path) -> None:
    source = tmp_path / "mod.py"
    source.write_text('"""A docstring."""\n')
    kept = compile_to_pyc(assert_path_exists(source), tmp_path / f"kept{PYC_SUFFIX}", optimize=0)
    stripped = compile_to_pyc(
        assert_path_exists(source), tmp_path / f"stripped{PYC_SUFFIX}", optimize=2
    )
    assert b"A docstring." in kept.read_bytes()
    assert b"A docstring." not in stripped.read_bytes()


def test_syntax_error_is_reported_as_a_smelt_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.py"
    source.write_text("def oops(:\n")
    with pytest.raises(BytecodeCompilationError):
        compile_to_pyc(assert_path_exists(source), tmp_path / "broken.pyc")
