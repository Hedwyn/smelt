from __future__ import annotations

from pathlib import Path

import pytest

from smelt.utils import PackageLayout, convert_to_path

EPONYM_LAYOUT_TESTS = {
    "package/subpackage/mod.py": "package.subpackage.mod",
    "package/mod.py": "package.mod",
    "mod.py": "mod",
    "mod.pyx": "mod",
    "mod.c": "mod",
}

SRC_LAYOUT_TESTS = {
    "src/package/subpackage/mod.py": "package.subpackage.mod",
    "src/package/mod.py": "package.mod",
    "src/mod.py": "mod",
    "src/mod.pyx": "mod",
    "src/mod.c": "mod",
}


def convert_to_import_path(path: str | Path, layout: PackageLayout) -> str:
    parts = Path(path).parts
    if layout is PackageLayout.SRC:
        assert parts[0] == "src"
        parts = parts[1:]
    *packages, module = parts
    return ".".join([*packages, Path(module).stem])


def convert_to_fs_path(import_path: str, layout: PackageLayout, extension: str) -> Path:
    path = convert_to_path(import_path, file_extension=extension)
    if layout is PackageLayout.SRC:
        path = Path("src") / path
    return path


def _build_convert_to_import_path_test_cases() -> list[tuple[str, str, PackageLayout, bool]]:
    test_cases: list[tuple[str, str, PackageLayout, bool]] = []
    for path, import_path in EPONYM_LAYOUT_TESTS.items():
        test_cases.append((path, import_path, PackageLayout.EPONYM, True))
        test_cases.append((path, import_path, PackageLayout.EPONYM, False))

    for path, import_path in SRC_LAYOUT_TESTS.items():
        test_cases.append((path, import_path, PackageLayout.SRC, True))
        test_cases.append((path, import_path, PackageLayout.SRC, False))
    return test_cases


@pytest.mark.parametrize(
    "path,expected_import_path,layout,make_path_obj",
    _build_convert_to_import_path_test_cases(),
)
def test_convert_to_import_path(
    path: str, expected_import_path: str, layout: PackageLayout, make_path_obj: bool
) -> None:
    if make_path_obj:
        path = Path(path)
    assert convert_to_import_path(path, layout=layout) == expected_import_path


def _build_convert_to_fs_path_test_cases() -> list[tuple[str, str, PackageLayout, bool]]:
    test_cases: list[tuple[str, str, PackageLayout, bool]] = []
    for path, import_path in EPONYM_LAYOUT_TESTS.items():
        extension = "." + path.split(".")[-1]
        test_cases.append((import_path, path, PackageLayout.EPONYM, extension))

    for path, import_path in SRC_LAYOUT_TESTS.items():
        extension = "." + path.split(".")[-1]
        test_cases.append((import_path, path, PackageLayout.SRC, extension))
    return test_cases


@pytest.mark.parametrize(
    "import_path,expected_path,layout,extension", _build_convert_to_fs_path_test_cases()
)
def test_convert_to_fs_path(
    import_path: str,
    expected_path: str,
    layout: PackageLayout,
    extension: str,
) -> None:
    assert convert_to_fs_path(import_path, layout=layout, extension=extension) == Path(
        expected_path
    )
