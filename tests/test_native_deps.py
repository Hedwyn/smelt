from __future__ import annotations

import shutil
import sysconfig
from pathlib import Path

import pytest

from smelt.native_deps import (
    LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
    bundle_native_dependencies,
    bundled_patchelf_dir,
    collect_native_dependencies,
    is_supported_platform,
    ldd_dependencies,
    nested_rpath,
    package_rpath_dirs,
)
from smelt.utils import assert_path_exists

NESTED_RPATH_TESTS = {
    "libfoo.so": "$ORIGIN",
    "pkg/mod.so": "$ORIGIN:$ORIGIN/..",
    "pkg/sub/mod.so": "$ORIGIN:$ORIGIN/../..",
    "pkg/a/b/mod.so": "$ORIGIN:$ORIGIN/../../..",
}

patchelf_available = pytest.mark.skipif(
    not is_supported_platform()
    or (bundled_patchelf_dir() is None and not shutil.which("patchelf")),
    reason="needs Linux and patchelf",
)


def _an_extension_module_with_dependencies() -> Path | None:
    """
    The first extension module in this environment that actually pulls in a library
    worth bundling. Which one that is varies (a given CPython build compiles a
    different set of stdlib modules in), so it is discovered rather than named.
    """
    roots = [
        Path(path)
        for key in ("platlib", "purelib")
        if (path := sysconfig.get_path(key)) and Path(path).is_dir()
    ]
    dynload = sysconfig.get_config_var("DESTSHARED")
    if dynload and Path(dynload).is_dir():
        roots.append(Path(dynload))
    for root in roots:
        for candidate in sorted(root.rglob("*.so")):
            if collect_native_dependencies([candidate]):
                return candidate
    return None


@pytest.mark.parametrize(("dest_rel_path", "expected"), NESTED_RPATH_TESTS.items())
def test_nested_rpath_reaches_siblings_and_the_root(dest_rel_path: str, expected: str) -> None:
    assert nested_rpath(Path(dest_rel_path)) == expected


def test_package_rpath_dirs_lists_folders_holding_artifacts() -> None:
    placed = [Path("libfoo.so"), Path("pkg/mod.so"), Path("pkg/other.so"), Path("pkg/sub/mod.so")]
    assert package_rpath_dirs(placed) == {"pkg", "pkg/sub"}


def test_collect_native_dependencies_walks_transitively(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = {
        "/app/mod.so": {"liba.so": "/usr/lib/liba.so"},
        "/usr/lib/liba.so": {"libb.so": "/usr/lib/libb.so", "libc.so.6": "/usr/lib/libc.so.6"},
        "/usr/lib/libb.so": {},
    }
    monkeypatch.setattr("smelt.native_deps.ldd_dependencies", lambda path: graph.get(str(path), {}))
    collected = collect_native_dependencies(["/app/mod.so"])
    # transitive: libb is only reachable through liba
    assert collected == {"liba.so": "/usr/lib/liba.so", "libb.so": "/usr/lib/libb.so"}
    # and libc is left to the host
    assert "libc.so.6" not in collected
    assert "libc.so" in " ".join(LINUX_SYSTEM_DLL_IGNORE_PREFIXES)


def test_collect_native_dependencies_keeps_the_first_of_conflicting_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two libraries with the same file name have only one flat destination to go to, so
    one of them has to be dropped -- deterministically, and never silently.
    """
    graph = {
        "/app/mod.so": {"libdup.so": "/opt/one/libdup.so"},
        "/opt/one/libdup.so": {"libdup.so": "/opt/two/libdup.so"},
    }
    monkeypatch.setattr("smelt.native_deps.ldd_dependencies", lambda path: graph.get(str(path), {}))
    assert collect_native_dependencies(["/app/mod.so"]) == {"libdup.so": "/opt/one/libdup.so"}


@pytest.mark.skipif(not is_supported_platform(), reason="ldd is Linux-only")
def test_ldd_on_a_non_elf_file_reports_no_dependencies(tmp_path: Path) -> None:
    """
    A distribution holds plenty of files that are not ELF objects; `ldd` refusing one
    is not a build failure.
    """
    data_file = tmp_path / "data.json"
    data_file.write_text("{}\n")
    assert ldd_dependencies(data_file) == {}


@patchelf_available
def test_bundle_rewrites_rpaths_to_be_origin_relative(tmp_path: Path) -> None:
    """
    End to end against a real extension module: its dependencies are copied in and
    every RPATH is rewritten so nothing points outside the folder any more.
    """
    subject = _an_extension_module_with_dependencies()
    if subject is None:
        pytest.skip("no extension module with bundleable dependencies in this environment")

    dist_root = tmp_path / "dist"
    dest_rel_path = Path("pkg") / subject.name
    (dist_root / "pkg").mkdir(parents=True)
    shutil.copy2(subject, dist_root / dest_rel_path)

    bundled = bundle_native_dependencies(dist_root, {dest_rel_path: assert_path_exists(subject)})

    assert bundled.resolved
    assert bundled.dependencies
    for basename in bundled.dependencies:
        assert (dist_root / basename).is_file()
    assert dest_rel_path in bundled.rewritten
    # nothing the copy needs points outside the folder any more
    resolved = ldd_dependencies(dist_root / dest_rel_path)
    for basename in bundled.dependencies:
        assert Path(resolved[basename]).resolve().is_relative_to(dist_root.resolve())
