"""
Vendoring provider for `cffi`: builds `_cffi_backend` from source against a
statically-built libffi, for a target `isolated-build` (see
`smelt.isolated_build`) has no wheel for.

cffi's own `setup.py` builds the same single C file (`src/c/_cffi_backend.c`)
against libffi, deciding a couple of feature macros (`USE__THREAD`,
`HAVE_SYNC_SYNCHRONIZE`) via compiler probes that run natively against the host
-- meaningless under cross-compilation, and both unconditionally true for every
Linux target this project supports (see `ask_supports_thread`/
`ask_supports_sync_synchronize` in cffi's own `setup.py`), so they are
hardcoded here instead of shelling out to `setup.py` (which also knows nothing
about `ZigCompiler`/cross triples).

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Final

from setuptools import Extension

from smelt.isolated_build import vendored_build_cache_dir
from smelt.utils import ImportPath, PathExists, SmeltError, assert_is_valid_import_path, assert_path_exists
from smelt.vendoring._compile import compile_extension_for_target
from smelt.vendoring._libffi_build import build_libffi
from smelt.vendoring.base import VendoredExtension

#: cffi versions this provider has been checked against: `src/c/_cffi_backend.c`'s
#: path and `setup.py`'s own sources/macros for a Linux/CPython build have not
#: moved across them (checked against a local `cffi` checkout at `2.2.0.dev0`).
#: Anything outside this range raises rather than risk silently building the
#: wrong file list for a layout that has since changed.
_SUPPORTED_VERSIONS: Final[tuple[str, ...]] = ("2.1.1", "2.2.0")

_IMPORT_PATH: Final[ImportPath] = assert_is_valid_import_path("_cffi_backend")


class CffiVendoringError(SmeltError):
    """
    Raised when cffi cannot be vendored (built from source) for a target.
    """


def _extract_sdist(sdist_path: PathExists, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if sdist_path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(sdist_path) as archive:
            archive.extractall(dest_dir, filter="data")
    elif sdist_path.suffix == ".zip":
        with zipfile.ZipFile(sdist_path) as archive:
            archive.extractall(dest_dir)
    else:
        raise CffiVendoringError(
            f"Unrecognized cffi source distribution archive format: {sdist_path}"
        )


class CffiProvider:
    """
    `smelt.vendoring.VendoredProvider` for `cffi`.
    """

    def compile(
        self,
        version_requirement: str,
        target: str | None,
        python_version: tuple[int, int],
        *,
        build_dir: Path,
    ) -> VendoredExtension:
        try:
            from unearth import PackageFinder, TargetPython
        except ImportError as exc:
            raise ImportError(
                "unearth is not installed, so smelt cannot fetch cffi's source "
                "distribution to vendor it. Install this package with the "
                "isolated-build extra: `uv pip install 'smelt[isolated-build]'`."
            ) from exc

        finder = PackageFinder(
            target_python=TargetPython(py_ver=python_version),
            # Force a source distribution regardless of whether a wheel exists
            # for this platform -- unlike `fetch_wheel`, which is only ever
            # reached once a provider *isn't* registered for a distribution,
            # this provider is the one deciding cffi is built from source.
            no_binary=[":all:"],
        )
        best_match = finder.find_best_match(f"cffi{version_requirement}")
        package = best_match.best
        if package is None or package.version is None:
            raise CffiVendoringError(
                f"No source distribution satisfies {'cffi' + version_requirement!r} "
                "-- cffi cannot be vendored for it."
            )
        if package.version not in _SUPPORTED_VERSIONS:
            raise CffiVendoringError(
                f"cffi=={package.version} is outside this provider's checked "
                f"version range {_SUPPORTED_VERSIONS} -- its source layout has not "
                "been verified against this vendored build recipe."
            )

        cache_dir = vendored_build_cache_dir("cffi", target) / package.version
        extracted_dir = cache_dir / "extracted"
        if not extracted_dir.is_dir() or not any(extracted_dir.iterdir()):
            sdist_dir = cache_dir / "sdist"
            sdist_dir.mkdir(parents=True, exist_ok=True)
            sdist_path = assert_path_exists(finder.download(package.link, location=sdist_dir))
            _extract_sdist(sdist_path, extracted_dir)

        source_root = assert_path_exists(extracted_dir / f"cffi-{package.version}")
        backend_source = assert_path_exists(source_root / "src" / "c" / "_cffi_backend.c")

        libffi = build_libffi(target, build_dir=cache_dir / "libffi")

        extension = Extension(
            name="_cffi_backend",
            sources=[str(backend_source)],
            include_dirs=[str(libffi.include_dir)],
            define_macros=[
                ("FFI_BUILDING", "1"),
                ("USE__THREAD", None),
                ("HAVE_SYNC_SYNCHRONIZE", None),
            ],
        )
        objects = compile_extension_for_target(extension, target, build_dir)
        return VendoredExtension(
            import_path=_IMPORT_PATH,
            module_name="_cffi_backend",
            objects=[*objects, libffi.archive_path],
        )
