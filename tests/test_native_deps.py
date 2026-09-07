from __future__ import annotations

import shutil
import struct
import sysconfig
from pathlib import Path

import pytest

from smelt.native_deps import (
    LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
    WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES,
    bundle_native_dependencies,
    bundled_patchelf_dir,
    collect_native_dependencies,
    is_supported_platform,
    ldd_dependencies,
    nested_rpath,
    package_rpath_dirs,
    pe_dependencies,
    pe_imported_dll_names,
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


def _build_minimal_pe(dll_names: list[str]) -> bytes:
    """
    A syntactically valid, semantically minimal PE32 file whose import directory
    table names exactly `dll_names` -- enough for `pe_imported_dll_names` to parse,
    nothing else (no real machine code, no section flags, no checksums).
    """
    e_lfanew = 0x80
    header = bytearray(e_lfanew)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, e_lfanew)

    import_dir_rva = 0x2000
    entries = bytearray()
    strings = bytearray()
    string_rva = import_dir_rva + (len(dll_names) + 1) * 20
    for dll_name in dll_names:
        name_rva = string_rva + len(strings)
        entries += struct.pack("<IIIII", 0, 0, 0, name_rva, 0)
        strings += dll_name.encode("ascii") + b"\x00"
    entries += b"\x00" * 20  # terminator entry
    raw = bytes(entries) + bytes(strings)

    opt = bytearray()
    opt += struct.pack("<H", 0x10B)  # PE32
    opt += b"\x00" * (96 - len(opt))
    opt += struct.pack("<II", 0, 0)  # data directory 0: export table, unused
    opt += struct.pack("<II", import_dir_rva, max(len(entries), 1))  # 1: import table

    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(opt), 0x0102)
    rawptr = e_lfanew + 4 + len(coff) + len(opt) + 40
    section = struct.pack(
        "<8sIIIIIIHHI",
        b".idata\x00\x00",
        len(raw),
        import_dir_rva,
        len(raw),
        rawptr,
        0,
        0,
        0,
        0,
        0,
    )
    body = b"PE\x00\x00" + coff + bytes(opt) + section + raw
    return bytes(header) + body


def test_pe_imported_dll_names_parses_a_synthetic_import_table(tmp_path: Path) -> None:
    dll = tmp_path / "mod.pyd"
    dll.write_bytes(_build_minimal_pe(["FOO.dll", "bar.DLL"]))
    assert pe_imported_dll_names(dll) == ["FOO.dll", "bar.DLL"]


def test_pe_imported_dll_names_with_no_imports_reports_none(tmp_path: Path) -> None:
    dll = tmp_path / "mod.pyd"
    dll.write_bytes(_build_minimal_pe([]))
    assert pe_imported_dll_names(dll) == []


def test_pe_imported_dll_names_on_a_non_pe_file_reports_no_dependencies(tmp_path: Path) -> None:
    """
    A distribution holds plenty of files that are not PE objects; this has to fail
    soft exactly like `ldd_dependencies` does for a non-ELF file.
    """
    data_file = tmp_path / "data.json"
    data_file.write_text("{}\n")
    assert pe_imported_dll_names(data_file) == []


def test_pe_dependencies_resolves_against_search_dirs_case_insensitively(tmp_path: Path) -> None:
    subject = tmp_path / "mod.pyd"
    subject.write_bytes(_build_minimal_pe(["FOO.dll", "missing.dll"]))
    found_dir = tmp_path / "found"
    found_dir.mkdir()
    (found_dir / "foo.dll").write_bytes(b"not a real dll")

    deps = pe_dependencies(subject, [found_dir])

    # keyed lowercase (NTFS resolves names case-insensitively), and the DLL this
    # environment cannot vouch for (not present in any search dir) is left out rather
    # than treated as an error -- it is assumed to be provided by the target Windows
    # installation itself.
    assert deps == {"foo.dll": str((found_dir / "foo.dll").resolve())}


def test_pe_is_supported_regardless_of_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Cross-compiling a Windows target from a Linux host still has to walk PE files --
    that is a static parse needing nothing from the running host, unlike `ldd`.
    """
    from smelt.native_deps import is_supported_platform

    monkeypatch.setattr("smelt.native_deps.platform.system", lambda: "Linux")
    assert is_supported_platform("pe") is True
    monkeypatch.setattr("smelt.native_deps.platform.system", lambda: "Darwin")
    assert is_supported_platform("pe") is True
    assert is_supported_platform("elf") is False


def test_windows_ignore_prefixes_exclude_the_python_dll_and_os_dlls() -> None:
    assert any("python312.dll".startswith(prefix) for prefix in WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES)
    assert any("kernel32.dll".startswith(prefix) for prefix in WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES)
    assert not any("liba.dll".startswith(prefix) for prefix in WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES)


def test_collect_native_dependencies_dispatches_to_pe_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Same transitive-walk contract as the Linux/`ldd` test above, but through the
    Windows dispatch branch.
    """
    graph = {
        "/app/mod.pyd": {"liba.dll": "/vendor/liba.dll"},
        "/vendor/liba.dll": {
            "libb.dll": "/vendor/libb.dll",
            "kernel32.dll": "/windows/kernel32.dll",
        },
        "/vendor/libb.dll": {},
    }
    monkeypatch.setattr(
        "smelt.native_deps.pe_dependencies", lambda path, search_dirs: graph.get(str(path), {})
    )
    collected = collect_native_dependencies(
        ["/app/mod.pyd"], WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES, binary_format="pe"
    )
    assert collected == {"liba.dll": "/vendor/liba.dll", "libb.dll": "/vendor/libb.dll"}
    assert "kernel32.dll" not in collected


def test_bundle_native_dependencies_on_windows_copies_into_every_package_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    PE has no RPATH-equivalent lever, so unlike the Linux placement (flat at the
    root, RPATHs rewritten), a dependency has to be copied next to every nested
    package that needs it, in addition to the root.
    """
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    dependency = vendor_dir / "liba.dll"
    dependency.write_bytes(b"not a real dll")

    dist_root = tmp_path / "dist"
    dest_rel_path = Path("pkg") / "mod.pyd"
    (dist_root / "pkg").mkdir(parents=True)
    (dist_root / dest_rel_path).write_bytes(b"not a real pyd either")

    monkeypatch.setattr(
        "smelt.native_deps.collect_native_dependencies",
        lambda seeds, ignore_prefixes, *, binary_format=None: {"liba.dll": str(dependency)},
    )

    bundled = bundle_native_dependencies(
        dist_root, {dest_rel_path: assert_path_exists(dependency)}, binary_format="pe"
    )

    assert bundled.resolved
    assert bundled.dependencies == {"liba.dll": dependency}
    assert bundled.rewritten == []
    assert (dist_root / "liba.dll").is_file()
    assert (dist_root / "pkg" / "liba.dll").is_file()


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
