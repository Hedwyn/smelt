from __future__ import annotations

import sys
from pathlib import Path

import pytest

from smelt.manifest import read_manifest_supported_targets, write_manifest_module
from smelt.dist import resolve_target_platforms
from smelt.resolver import TargetLevel
from smelt.schema import parse_project_config
from smelt.targets import (
    check_target,
    default_build_set,
    host_triple,
    matches,
    normalize_triple,
)


def test_normalize_triple() -> None:
    assert normalize_triple("host") == host_triple()
    assert normalize_triple("x86_64-linux") == "x86_64-linux-gnu"
    assert normalize_triple("x86_64-linux-musl") == "x86_64-linux-musl"
    assert normalize_triple("x86_64-linux-*") == "x86_64-linux-*"


def test_matches_wildcards_and_host() -> None:
    assert matches("x86_64-linux-musl", ["x86_64-linux-*"])
    assert matches("aarch64-linux-gnu", ["x86_64-linux-*", "aarch64-linux-gnu"])
    assert not matches("x86_64-windows-gnu", ["*-linux-*"])
    assert matches("x86_64-linux", ["x86_64-linux-gnu"])
    assert matches("host", ["host"])
    assert matches(host_triple(), ["host"])


def test_check_target_reports_the_first_refusing_level() -> None:
    levels = (
        TargetLevel("[tool.smelt]", ("*-linux-*",)),
        TargetLevel("[tool.smelt.bundle]", ("x86_64-linux-musl",)),
    )
    assert check_target("x86_64-linux-musl", levels) is None
    refusing = check_target("aarch64-linux-gnu", levels)
    assert refusing is not None
    assert refusing.source == "[tool.smelt.bundle]"
    assert check_target("x86_64-windows-gnu", levels) == levels[0]


def test_default_build_set() -> None:
    assert default_build_set(()) == [None]
    patterns_only = (TargetLevel("[tool.smelt]", ("*-linux-*",)),)
    assert default_build_set(patterns_only) == [None]
    levels = (
        TargetLevel("[tool.smelt]", ("*-linux-*",)),
        TargetLevel("[tool.smelt.bundle]", ("x86_64-linux-musl", "aarch64-linux-*")),
    )
    assert default_build_set(levels) == ["x86_64-linux-musl"]


def test_requested_targets_override_the_default_set() -> None:
    levels = (TargetLevel("[tool.smelt.bundle]", ("x86_64-linux-musl",)),)
    assert resolve_target_platforms([], levels) == ["x86_64-linux-musl"]
    assert resolve_target_platforms(["aarch64-linux-gnu", "host", host_triple()], levels) == [
        "aarch64-linux-gnu",
        None,
    ]


def test_old_platforms_and_archs_alias_to_supported_targets() -> None:
    with pytest.warns(DeprecationWarning):
        config = parse_project_config({"platforms": ["linux"]})
    assert config.supported_targets == ["*-linux-*"]
    with pytest.warns(DeprecationWarning):
        config = parse_project_config({"archs": ["x86_64", "aarch64"]})
    assert config.supported_targets == ["x86_64-*", "aarch64-*"]


def _write_manifest(tmp_path: Path, body: str) -> None:
    package = tmp_path / "manifested_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "_smelt_manifest.py").write_text(body)


def test_manifest_reads_supported_targets_and_old_archs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_manifest(tmp_path, "MODULES = {}\nSUPPORTED_TARGETS = ('*-linux-*',)\n")
    assert read_manifest_supported_targets("manifested_pkg") == ("*-linux-*",)

    # a manifest written before `supported-targets` existed
    old = tmp_path / "old_pkg"
    old.mkdir()
    (old / "__init__.py").write_text("")
    (old / "_smelt_manifest.py").write_text("MODULES = {}\nARCHS = ('x86_64', 'aarch64')\n")
    assert read_manifest_supported_targets("old_pkg") == ("x86_64-*", "aarch64-*")
    sys.modules.pop("manifested_pkg", None)
    sys.modules.pop("old_pkg", None)


def test_write_manifest_module_is_exported() -> None:
    assert callable(write_manifest_module)
