"""
Tests for `smelt.pyconfig`: the known-good pyconfig.h sources `own_python` uses
instead of trusting `./configure`'s own (not cross-aware) detection.

@date: 08.09.2026
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smelt.pyconfig import resolve_pyconfig_header


def test_native_resolves_to_the_running_interpreter_s_own_pyconfig() -> None:
    """
    Nothing else on this machine can answer "what does ./configure detect here"
    better than the file the interpreter running this test was actually built with.
    """
    resolved = resolve_pyconfig_header(None)
    assert resolved is not None
    assert resolved.name == "pyconfig.h"


def test_native_refuses_a_debug_mode_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `Py_DEBUG` changes `PyObject`'s layout/ABI tag -- copying a host file built the
    other way than requested would silently override own_python's own `debug` choice.
    """
    monkeypatch.setattr("smelt.pyconfig.sysconfig.get_config_var", lambda name: 0)
    assert resolve_pyconfig_header(None, debug=True) is None
    assert resolve_pyconfig_header(None, debug=False) is not None


def test_explicit_target_with_no_checked_in_file_returns_none() -> None:
    assert resolve_pyconfig_header("i-do-not-exist-linux-musl") is None


def test_explicit_target_prefers_the_checked_in_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smelt.pyconfig.PYCONFIG_DIR", tmp_path)
    target_dir = tmp_path / "x86_64-linux-musl"
    target_dir.mkdir()
    checked_in = target_dir / "pyconfig.h"
    checked_in.write_text("#define HAVE_CLOSE_RANGE 1\n")

    resolved = resolve_pyconfig_header("x86_64-linux-musl")

    assert resolved == checked_in
    # never host-derived, whatever libc the venv running smelt happens to use
    assert resolve_pyconfig_header("x86_64-linux-musl", debug=True) == checked_in
