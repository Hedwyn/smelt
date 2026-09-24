"""
Vendoring provider for `pyomq` (a pure-Rust `libzmq`/ZMTP reimplementation, drop-in
`pyzmq` replacement -- see https://github.com/paddor/omq.rs): builds `pyomq._native`
from source via `maturin` (see `smelt.rust.maturin`), for a target `isolated-build`
(see `smelt.isolated_build`) has no wheel for -- e.g. `arm-linux-gnueabihf`, which
neither `pyzmq` nor `pyomq` publish a wheel for at all.

Unlike `smelt.vendoring.cryptography`/`cffi`, no OpenSSL/libffi/target-Python-headers
plumbing is needed here: `pyomq`'s own crate is pure Rust (`pyo3` + `tokio`, no cffi/C
sub-build) and declares a stable-ABI `pyo3` feature (`abi3-py311`), which lets
`pyo3-build-config` fully self-configure a cross build from that feature alone --
verified against its own `default_cross_compile` (no `PYO3_CROSS_LIB_DIR`/target
`Python.h` needed at all), and end to end against `arm-linux-gnueabihf`.

`pyomq`'s upstream repo also needs `smelt.rust.maturin.build_with_maturin`'s
`manifest_path=None`/`cwd` path rather than an explicit `--manifest-path`: its
`pyproject.toml` (and the `python-source` it points at) sit at the repo root, while
`Cargo.toml` is nested under `bindings/pyomq/` -- see that function's own doc for why
an explicit out-of-tree `--manifest-path` silently drops every pure-Python file from
the wheel instead of failing loudly. Harmless here regardless: this provider only
needs the native module itself (see `VendoredExtension`'s own doc) -- the pure-Python
`pyomq` package this crate builds alongside it is irrelevant to what gets returned.

@date: 18.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Final

from smelt.isolated_build import extract_wheel, locate_native_in_wheel, vendored_build_cache_dir
from smelt.own_python import TargetPythonHeaders
from smelt.rust.maturin import build_with_maturin
from smelt.rust.toolchain import ensure_rust_toolchain
from smelt.utils import (
    ImportPath,
    PathExists,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
)
from smelt.vendoring.base import VendoredExtension

_NATIVE_IMPORT_PATH: Final[ImportPath] = assert_is_valid_import_path("pyomq._native")


class PyomqVendoringError(SmeltError):
    """
    Raised when `pyomq` cannot be vendored (built from source) for a target.
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
        raise PyomqVendoringError(
            f"Unrecognized pyomq source distribution archive format: {sdist_path}"
        )


class PyomqProvider:
    """
    `smelt.vendoring.VendoredProvider` for `pyomq` (see this module's own doc).
    """

    def compile(
        self,
        version_requirement: str,
        target: str | None,
        python_version: tuple[int, int],
        *,
        build_dir: Path,
        py_headers: TargetPythonHeaders | None = None,
    ) -> VendoredExtension:
        try:
            from unearth import PackageFinder, TargetPython
        except ImportError as exc:
            raise ImportError(
                "unearth is not installed, so smelt cannot fetch pyomq's source "
                "distribution to vendor it. Install this package with the "
                "isolated-build extra: `uv pip install 'smelt[isolated-build]'`."
            ) from exc

        finder = PackageFinder(
            target_python=TargetPython(py_ver=python_version),
            # Force a source distribution regardless of whether a wheel exists for
            # this platform -- same reasoning as `CffiProvider`/`CryptographyProvider`.
            no_binary=[":all:"],
        )
        best_match = finder.find_best_match(f"pyomq{version_requirement}")
        package = best_match.best
        if package is None or package.version is None:
            raise PyomqVendoringError(
                f"No source distribution satisfies {'pyomq' + version_requirement!r} "
                "-- pyomq cannot be vendored for it."
            )

        cache_dir = vendored_build_cache_dir("pyomq", target) / package.version
        extracted_dir = cache_dir / "extracted"
        if not extracted_dir.is_dir() or not any(extracted_dir.iterdir()):
            sdist_dir = cache_dir / "sdist"
            sdist_dir.mkdir(parents=True, exist_ok=True)
            sdist_path = assert_path_exists(finder.download(package.link, location=sdist_dir))
            _extract_sdist(sdist_path, extracted_dir)
        source_root = assert_path_exists(extracted_dir / f"pyomq-{package.version}")

        toolchain = ensure_rust_toolchain()
        result = build_with_maturin(
            toolchain,
            # `None`/`cwd` rather than an explicit `bindings/pyomq/Cargo.toml` -- see
            # this module's own doc for why that would silently produce a broken build.
            None,
            cache_dir / "wheel",
            zig_target=target,
            cwd=source_root,
            interpreter=sys.executable,
        )

        extracted_wheel = extract_wheel(result.wheel_path, cache_dir / "wheel" / "extracted")
        native = locate_native_in_wheel(extracted_wheel, Path("pyomq", "_native"), "_native")
        if native is None:
            raise PyomqVendoringError(
                f"`maturin build` for pyomq=={package.version} (target "
                f"{target or 'the host'!r}) reported success, but its wheel has no "
                "pyomq/_native.* file."
            )

        return VendoredExtension(
            import_path=_NATIVE_IMPORT_PATH,
            module_name="_native",
            objects=[native],
            already_linked=True,
        )
