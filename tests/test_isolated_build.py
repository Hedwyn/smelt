"""
Tests for `isolated-build`: reinstalling a third-party native dependency for
`build_dist`'s actual target instead of copying it from the local environment.

Two tiers, the same split `test_own_python.py` uses. Version resolution and the
Zig-triple -> wheel-tag mapping are pure string logic and run anywhere. Everything
that actually resolves and downloads a wheel needs the `isolated-build` extra
installed and a reachable PyPI index, and is skipped otherwise.

@date: 08.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from smelt.config import EntrypointOptions, SmeltConfig
from smelt.dist import (
    DEFAULT_ISOLATED_BUILD,
    DistError,
    IsolatedBuildError,
    build_dist,
    resolve_isolated_build,
    resolve_isolated_build_target,
    resolve_isolated_build_versions,
)
from smelt.isolated_build import (
    DEFAULT_ISOLATED_BUILD_VERSIONS,
    extract_wheel,
    fetch_wheel,
    isolated_build_cache_dir,
    locate_native_in_wheel,
    locate_sibling_libs_dirs,
    owning_distribution,
    parse_pyproject_dependencies,
    prepare_isolated_natives,
    resolve_isolated_build_version,
    wheel_platform_tags,
)
from smelt.utils import assert_is_valid_import_path


def _network_reachable() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=2):
            return True
    except OSError:
        return False


try:
    import unearth as _unearth

    _ = _unearth
    _UNEARTH_AVAILABLE = True
except ImportError:
    _UNEARTH_AVAILABLE = False

needs_isolated_build_network = pytest.mark.skipif(
    not _UNEARTH_AVAILABLE or not _network_reachable(),
    reason="needs the isolated-build extra installed and a reachable PyPI index",
)


# --- owning_distribution -----------------------------------------------------------


def test_owning_distribution_finds_the_installed_distribution() -> None:
    assert owning_distribution(assert_is_valid_import_path("click")) == "click"


def test_owning_distribution_is_none_for_an_unclaimed_top_level() -> None:
    assert owning_distribution(assert_is_valid_import_path("no_such_top_level_xyz")) is None


# --- resolve_isolated_build_version -------------------------------------------------


def test_resolve_isolated_build_version_local_pins_the_installed_version() -> None:
    import importlib.metadata

    assert resolve_isolated_build_version("click", "local") == (
        f"=={importlib.metadata.version('click')}"
    )


def test_resolve_isolated_build_version_pyproject_returns_the_declared_specifier() -> None:
    assert (
        resolve_isolated_build_version(
            "numpy", "pyproject", pyproject_dependencies={"numpy": ">=1.24,<2"}
        )
        == ">=1.24,<2"
    )


def test_resolve_isolated_build_version_pyproject_is_unconstrained_for_a_transitive_dep() -> None:
    assert resolve_isolated_build_version("requests", "pyproject") == ""


def test_resolve_isolated_build_version_pyproject_canonicalizes_the_name() -> None:
    # PEP 503: casing and `-`/`_`/`.` separators do not have to match between
    # `owning_distribution`'s installed-metadata spelling and the pyproject author's.
    assert (
        resolve_isolated_build_version(
            "Flask-Cors", "pyproject", pyproject_dependencies={"flask_cors": ">=4"}
        )
        == ">=4"
    )


def test_resolve_isolated_build_version_lock_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        resolve_isolated_build_version("numpy", "lock")


# --- wheel_platform_tags -------------------------------------------------------------


def test_wheel_platform_tags_musl_target() -> None:
    assert wheel_platform_tags("x86_64-linux-musl") == ["musllinux_1_2_x86_64"]


def test_wheel_platform_tags_manylinux_ladder_ends_in_the_generic_tag() -> None:
    tags = wheel_platform_tags("aarch64-linux-gnu")
    assert tags[0] == "manylinux_2_28_aarch64"
    assert tags[-1] == "linux_aarch64"
    assert "manylinux2014_aarch64" in tags


def test_wheel_platform_tags_normalizes_the_armv7l_arch_spelling() -> None:
    assert wheel_platform_tags("arm-linux-gnueabihf")[0] == "manylinux_2_28_armv7l"


def test_wheel_platform_tags_rejects_unsupported_targets() -> None:
    with pytest.raises(IsolatedBuildError):
        wheel_platform_tags("x86_64-windows-gnu")


# --- parse_pyproject_dependencies ----------------------------------------------------


def test_parse_pyproject_dependencies_extracts_names_and_specifiers() -> None:
    # `packaging`'s `SpecifierSet` does not preserve clause order, so compare parsed
    # specifiers rather than their (order-sensitive) string spelling.
    from packaging.specifiers import SpecifierSet

    parsed = parse_pyproject_dependencies(["numpy>=1.24,<2", "requests"])
    assert set(parsed) == {"numpy", "requests"}
    assert SpecifierSet(parsed["numpy"]) == SpecifierSet(">=1.24,<2")
    assert parsed["requests"] == ""


def test_parse_pyproject_dependencies_empty_input_short_circuits() -> None:
    # No `packaging` import forced when there is nothing to parse.
    assert parse_pyproject_dependencies([]) == {}


# --- isolated_build_cache_dir --------------------------------------------------------


def test_isolated_build_cache_dir_keys_on_dist_name_version_and_target() -> None:
    with_target = isolated_build_cache_dir("numpy", "1.24.3", "x86_64-linux-musl")
    without_target = isolated_build_cache_dir("numpy", "1.24.3")
    assert with_target != without_target
    assert with_target.name == "1.24.3"
    assert with_target.parent.name == "numpy"
    assert without_target.parent.parent.name == "native"


# --- resolve_isolated_build / _versions / _target (dist.py) -------------------------


def test_resolve_isolated_build_prefers_the_caller_then_the_declaration() -> None:
    assert resolve_isolated_build(EntrypointOptions()) is DEFAULT_ISOLATED_BUILD
    assert resolve_isolated_build(EntrypointOptions({"isolated-build": True})) is True
    assert (
        resolve_isolated_build(EntrypointOptions({"isolated-build": True}), False) is False
    )


def test_resolve_isolated_build_rejects_a_non_boolean() -> None:
    with pytest.raises(DistError):
        resolve_isolated_build(EntrypointOptions({"isolated-build": "yes"}))  # type: ignore[typeddict-item]


def test_resolve_isolated_build_versions_prefers_the_caller_then_the_declaration() -> None:
    assert (
        resolve_isolated_build_versions(EntrypointOptions())
        == DEFAULT_ISOLATED_BUILD_VERSIONS
    )
    assert (
        resolve_isolated_build_versions(EntrypointOptions({"isolated-build-versions": "lock"}))
        == "lock"
    )
    assert (
        resolve_isolated_build_versions(
            EntrypointOptions({"isolated-build-versions": "lock"}), "pyproject"
        )
        == "pyproject"
    )


def test_resolve_isolated_build_versions_rejects_an_unknown_strategy() -> None:
    with pytest.raises(DistError):
        resolve_isolated_build_versions(
            EntrypointOptions({"isolated-build-versions": "bogus"})  # type: ignore[typeddict-item]
        )


def test_resolve_isolated_build_target_prefers_the_caller_then_the_declaration() -> None:
    assert resolve_isolated_build_target(EntrypointOptions()) is None
    assert (
        resolve_isolated_build_target(
            EntrypointOptions({"isolated-build-target": "aarch64-linux-gnu"})
        )
        == "aarch64-linux-gnu"
    )
    assert (
        resolve_isolated_build_target(
            EntrypointOptions({"isolated-build-target": "aarch64-linux-gnu"}),
            "x86_64-linux-musl",
        )
        == "x86_64-linux-musl"
    )


# --- fetch_wheel / extract_wheel / locate_native_in_wheel (needs network) ----------


@needs_isolated_build_network
def test_fetch_extract_and_locate_native_round_trip(tmp_path: Path) -> None:
    wheel_path = fetch_wheel("MarkupSafe", "", "x86_64-linux-musl", cache_dir=tmp_path / "cache")
    assert wheel_path.suffix == ".whl"

    extracted = extract_wheel(wheel_path, tmp_path / "extracted")
    # The companion `_speedups.c` source file must not be mistaken for the compiled
    # extension: only the real `.so` matches.
    native = locate_native_in_wheel(
        extracted, Path("markupsafe", "_speedups.cpython-999-x86_64-linux-gnu.so"), "_speedups"
    )
    assert native is not None
    assert native.name.endswith(".so")
    assert "musl" in native.name


@needs_isolated_build_network
def test_fetch_wheel_reuses_the_cache_for_a_pinned_version(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    first = fetch_wheel("MarkupSafe", "==3.0.3", "x86_64-linux-gnu", cache_dir=cache_dir)
    second = fetch_wheel("MarkupSafe", "==3.0.3", "x86_64-linux-gnu", cache_dir=cache_dir)
    assert first == second


@needs_isolated_build_network
def test_fetch_wheel_raises_when_nothing_satisfies_the_target(tmp_path: Path) -> None:
    with pytest.raises(IsolatedBuildError):
        fetch_wheel(
            "MarkupSafe", "==999.999.999", "x86_64-linux-gnu", cache_dir=tmp_path
        )


@needs_isolated_build_network
def test_locate_sibling_libs_dirs_finds_a_vendored_libs_directory(tmp_path: Path) -> None:
    wheel_path = fetch_wheel("numpy", "", "aarch64-linux-gnu", cache_dir=tmp_path / "cache")
    extracted = extract_wheel(wheel_path, tmp_path / "extracted")
    libs_dirs = locate_sibling_libs_dirs(extracted)
    assert any(entry.name.endswith(".libs") for entry in libs_dirs)


# --- prepare_isolated_natives / build_dist wiring (needs network) ------------------


@needs_isolated_build_network
def test_prepare_isolated_natives_replaces_extension_entries_for_the_target(
    tmp_path: Path,
) -> None:
    pytest.importorskip("markupsafe")
    from smelt.explorer import ModuleKind, resolve_module

    resolved = resolve_module(assert_is_valid_import_path("markupsafe._speedups"))
    assert resolved.kind == ModuleKind.EXTENSION

    replacements = prepare_isolated_natives(
        {resolved.import_path: resolved},
        tmp_path,  # no `.libs` dir to place for MarkupSafe, so unused here
        target="x86_64-linux-musl",
        versions="local",
        dependencies={},
    )
    replacement = replacements.get(resolved.import_path)
    assert replacement is not None
    assert "musl" in replacement.name


@needs_isolated_build_network
def test_build_dist_ships_the_isolated_build_replacement(tmp_path: Path) -> None:
    pytest.importorskip("markupsafe")
    package = tmp_path / "src" / "isopkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import markupsafe\ndef main() -> int:\n    return 0\n"
    )
    config = SmeltConfig(
        packages_location={"isopkg": "src/isopkg"},
        entrypoints={"isopkg.cli:main": {}},
    )
    report = build_dist(
        config,
        output_dir=tmp_path / "out",
        path_solver=config.get_path_solver(project_root=tmp_path),
        build_extensions=False,
        discovery="static",
        isolated_build=True,
        isolated_build_target="x86_64-linux-musl",
    )
    natives = list(report.payload_root.rglob("*.so"))
    assert len(natives) == 1
    assert "musl" in natives[0].name


@needs_isolated_build_network
def test_build_dist_fails_loudly_when_isolated_build_has_no_wheel_for_the_target(
    tmp_path: Path,
) -> None:
    pytest.importorskip("markupsafe")
    package = tmp_path / "src" / "isopkg2"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import markupsafe\ndef main() -> int:\n    return 0\n"
    )
    config = SmeltConfig(
        packages_location={"isopkg2": "src/isopkg2"},
        entrypoints={"isopkg2.cli:main": {}},
    )
    with pytest.raises(IsolatedBuildError):
        build_dist(
            config,
            output_dir=tmp_path / "out",
            path_solver=config.get_path_solver(project_root=tmp_path),
            build_extensions=False,
            discovery="static",
            isolated_build=True,
            isolated_build_target="x86_64-windows-gnu",
        )
