"""
Vendoring provider for `cryptography`: builds `cryptography.hazmat.bindings._rust`
from source via `maturin` (see `smelt.rust.maturin`), linked against a
statically-built OpenSSL (see `smelt.vendoring._openssl_build`), for a target
`isolated-build` (see `smelt.isolated_build`) has no wheel for.

Only versions at or beyond `cryptography`'s Rust transition (3.4) are supported --
`cryptography`'s own C/cffi-only build predates this project's Rust support
(`smelt.rust`) and is out of scope here. A resolved version below that declines
instead of failing (see `smelt.vendoring.base.VendoringDeclined`), so the caller
falls back to an ordinary wheel.

Even Rust-era `cryptography` still generates and compiles a cffi C source
(`_openssl.c`, from its own checked-in `src/_cffi_src/build_openssl.py`) -- but as
part of its own `cryptography-cffi` Rust crate's `build.rs`, not as a separate
Python extension module the way pre-3.4 `cryptography` (or `smelt.vendoring.cffi`)
built one: the whole distribution compiles down to a single native module,
`_rust`, which is all this provider needs to produce.

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Final

from smelt.isolated_build import extract_wheel, locate_native_in_wheel, vendored_build_cache_dir
from smelt.own_python import TargetPythonHeaders
from smelt.rust.maturin import build_with_maturin
from smelt.rust.toolchain import (
    ensure_rust_target,
    ensure_rust_toolchain,
    host_rust_triple,
    zig_target_to_rust_triple,
)
from smelt.utils import (
    ImportPath,
    PathExists,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
)
from smelt.vendoring._openssl_build import build_openssl
from smelt.vendoring.base import VendoredExtension, VendoringDeclined

#: First `cryptography` release requiring a Rust toolchain to build from source
#: (see its own 3.4 changelog entry) -- the only line this provider supports.
#: Anything older declines instead of failing (see this module's own doc).
_RUST_TRANSITION_VERSION: Final = "3.4"

_RUST_IMPORT_PATH: Final[ImportPath] = assert_is_valid_import_path(
    "cryptography.hazmat.bindings._rust"
)

#: Rust target arch -> pointer width, for the hand-written `PYO3_CONFIG_FILE` a
#: cross build needs (see `_pyo3_cross_config`). Mirrors the arch set
#: `smelt.rust.toolchain.zig_target_to_rust_triple` itself supports -- nothing
#: to gain from a richer table here, since an unsupported arch already fails
#: one step earlier, at the triple mapping.
_ARCH_POINTER_WIDTH: Final[dict[str, int]] = {
    "x86_64": 64,
    "aarch64": 64,
    "arm": 32,
    "x86": 32,
}


class CryptographyVendoringError(SmeltError):
    """
    Raised when cryptography cannot be vendored (built from source) for a target --
    a real failure, distinct from `VendoringDeclined` (a pre-Rust version: out of
    scope rather than broken).
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
        raise CryptographyVendoringError(
            f"Unrecognized cryptography source distribution archive format: {sdist_path}"
        )


def _pyo3_cross_lib_dir(
    py_headers: TargetPythonHeaders, python_version: tuple[int, int], *, cache_dir: Path
) -> Path:
    """
    A synthetic `<prefix>/lib/pythonX.Y` directory for `PYO3_CROSS_LIB_DIR` --
    `cryptography`'s own `cryptography-cffi` build script (see this module's own
    doc) derives its C-compile include path from it as
    `<prefix>/include/pythonX.Y`, so that directory is populated with *both*
    `Python.h` (and everything it pulls in, from `py_headers.include_dir`) *and*
    the target-correct `pyconfig.h` (from `py_headers.pyconfig_dir`) side by side
    -- same reasoning as `TargetPythonHeaders`'s own doc: `Python.h`'s `#include
    "pyconfig.h"` is a quoted include resolved against its *own* directory first,
    so the two have to actually sit together for the right one to be found at all.

    Cached under `cache_dir` (keyed by the caller on distribution/target), so a
    second build for the same target does not repopulate it.
    """
    py_ver = f"{python_version[0]}.{python_version[1]}"
    include_dir = cache_dir / "include" / f"python{py_ver}"
    lib_dir = cache_dir / "lib" / f"python{py_ver}"
    if not include_dir.is_dir():
        lib_dir.mkdir(parents=True, exist_ok=True)
        include_dir.mkdir(parents=True)
        for entry in py_headers.include_dir.iterdir():
            dest = include_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, dest)
            else:
                shutil.copy(entry, dest)
        shutil.copy(py_headers.pyconfig_dir / "pyconfig.h", include_dir / "pyconfig.h")
    return lib_dir


def _pyo3_config_file(
    zig_target: str, python_version: tuple[int, int], *, cache_dir: Path
) -> Path:
    """
    A hand-written `PYO3_CONFIG_FILE` for a cross build, bypassing `maturin`'s own
    default cross-compile resolution: that path looks for a target `_sysconfigdata*
    .py` module under `PYO3_CROSS_LIB_DIR` (as a real target Python install would
    have), which `smelt.own_python.target_python_headers_for` does not produce (it
    resolves `pyconfig.h` alone, without building a full target interpreter -- see
    its own doc). Since `cryptography`'s crate builds against the stable ABI
    (`abi3`), every field this file needs is already known statically, with no
    need to introspect a target interpreter at all.
    """
    arch = zig_target.partition("-")[0]
    pointer_width = _ARCH_POINTER_WIDTH.get(arch)
    if pointer_width is None:
        raise CryptographyVendoringError(
            f"No known pointer width for Zig arch {arch!r} (from {zig_target!r}) -- "
            f"supported: {sorted(_ARCH_POINTER_WIDTH)}"
        )
    py_ver = f"{python_version[0]}.{python_version[1]}"
    config_path = cache_dir / "pyo3-config.txt"
    config_path.write_text(
        "\n".join(
            [
                "implementation=CPython",
                f"version={py_ver}",
                "shared=true",
                "abi3=true",
                f"lib_name=python{py_ver}",
                f"pointer_width={pointer_width}",
                "build_flags=",
                "suppress_build_script_link_lines=false",
            ]
        )
        + "\n"
    )
    return config_path


class CryptographyProvider:
    """
    `smelt.vendoring.VendoredProvider` for `cryptography` (>=3.4, its Rust era).
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
            from packaging.version import Version
            from unearth import PackageFinder, TargetPython
        except ImportError as exc:
            raise ImportError(
                "unearth/packaging are not installed, so smelt cannot fetch "
                "cryptography's source distribution to vendor it. Install these "
                "packages with the isolated-build extra: "
                "`uv pip install 'smelt[isolated-build]'`."
            ) from exc

        finder = PackageFinder(
            target_python=TargetPython(py_ver=python_version),
            # Force a source distribution regardless of whether a wheel exists for
            # this platform -- same reasoning as `CffiProvider`.
            no_binary=[":all:"],
        )
        best_match = finder.find_best_match(f"cryptography{version_requirement}")
        package = best_match.best
        if package is None or package.version is None:
            raise CryptographyVendoringError(
                f"No source distribution satisfies "
                f"{'cryptography' + version_requirement!r} -- cryptography cannot "
                "be vendored for it."
            )
        if Version(package.version) < Version(_RUST_TRANSITION_VERSION):
            raise VendoringDeclined(
                f"cryptography=={package.version} is below the Rust transition "
                f"({_RUST_TRANSITION_VERSION}) -- out of scope for this provider."
            )

        cache_dir = vendored_build_cache_dir("cryptography", target) / package.version
        extracted_dir = cache_dir / "extracted"
        if not extracted_dir.is_dir() or not any(extracted_dir.iterdir()):
            sdist_dir = cache_dir / "sdist"
            sdist_dir.mkdir(parents=True, exist_ok=True)
            sdist_path = assert_path_exists(finder.download(package.link, location=sdist_dir))
            _extract_sdist(sdist_path, extracted_dir)
        source_root = assert_path_exists(extracted_dir / f"cryptography-{package.version}")
        manifest_path = assert_path_exists(source_root / "src" / "rust" / "Cargo.toml")

        openssl = build_openssl(target, build_dir=cache_dir / "openssl")
        toolchain = ensure_rust_toolchain()

        extra_env = {
            # openssl-sys's own build-time env vars: point it at this provider's
            # vendored, statically-built OpenSSL instead of a system one.
            # `OPENSSL_LIBS` overrides its default `ssl:crypto` library-name search
            # -- `build_openssl`'s own build.zig produces a single combined archive
            # (see its own doc) rather than the usual separate libssl/libcrypto.
            "OPENSSL_DIR": str(cache_dir / "openssl"),
            "OPENSSL_STATIC": "1",
            "OPENSSL_LIBS": "openssl",
            # Runs `_cffi_src/build_openssl.py` (needs `cffi` importable -- see the
            # `isolated-build` extra) to generate `_openssl.c`; this host's own
            # interpreter always works here regardless of `target`/`python_version`,
            # since this is a host-side codegen step, not something that runs on
            # the target (see `smelt.rust.maturin.build_with_maturin`'s own doc).
            "PYO3_PYTHON": sys.executable,
        }
        if target is not None:
            rust_triple = zig_target_to_rust_triple(target)
            ensure_rust_target(toolchain, rust_triple)
            if py_headers is None:
                raise CryptographyVendoringError(
                    "cryptography cannot be cross-compiled without target-correct "
                    "Python headers (see smelt.own_python.target_python_headers_for)"
                )
            extra_env["PYO3_CROSS_LIB_DIR"] = str(
                _pyo3_cross_lib_dir(py_headers, python_version, cache_dir=cache_dir / "pyo3")
            )
            extra_env["PYO3_CONFIG_FILE"] = str(
                _pyo3_config_file(target, python_version, cache_dir=cache_dir / "pyo3")
            )
        else:
            rust_triple = host_rust_triple()

        result = build_with_maturin(
            toolchain,
            manifest_path,
            cache_dir / "wheel",
            zig_target=target,
            interpreter=sys.executable,
            extra_env=extra_env,
        )

        extracted_wheel = extract_wheel(result.wheel_path, cache_dir / "wheel" / "extracted")
        dest_rel_path = Path("cryptography", "hazmat", "bindings", "_rust")
        native = locate_native_in_wheel(extracted_wheel, dest_rel_path, "_rust")
        if native is None:
            raise CryptographyVendoringError(
                f"`maturin build` for cryptography=={package.version} (target "
                f"{rust_triple!r}) reported success, but its wheel has no "
                f"cryptography/hazmat/bindings/_rust.* file."
            )

        return VendoredExtension(
            import_path=_RUST_IMPORT_PATH,
            module_name="_rust",
            objects=[native],
            already_linked=True,
        )
