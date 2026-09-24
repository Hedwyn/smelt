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
from smelt.own_python import TargetPythonHeaders, target_sysconfigdata_for
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
    target: str,
    py_headers: TargetPythonHeaders,
    python_version: tuple[int, int],
    *,
    cache_dir: Path,
) -> Path:
    """
    A synthetic `<prefix>/lib/pythonX.Y` directory for `PYO3_CROSS_LIB_DIR` --
    both `cryptography`'s own `cryptography-cffi` build script (see this module's
    own doc) and `maturin`/`pyo3-build-config`'s own cross-compile resolution read
    from it:

    * `cryptography-cffi`'s `build.rs` derives its C-compile include path from it
      as `<prefix>/include/pythonX.Y`, populated here with *both* `Python.h` (and
      everything it pulls in, from `py_headers.include_dir`) *and* the
      target-correct `pyconfig.h` (from `py_headers.pyconfig_dir`) side by side --
      same reasoning as `TargetPythonHeaders`'s own doc: `Python.h`'s `#include
      "pyconfig.h"` is a quoted include resolved against its *own* directory
      first, so the two have to actually sit together for the right one to be
      found at all.
    * `maturin`/`pyo3-build-config` glob `PYO3_CROSS_LIB_DIR` itself for a
      `_sysconfigdata*.py` module, the way a real target Python install would
      have one -- populated here from `smelt.own_python.target_sysconfigdata_for`.

    `target_sysconfigdata_for` is called with its own default `python_version`
    (a full pinned CPython version, e.g. `"3.12.13"`) rather than deriving one
    from this function's own `python_version: tuple[int, int]` (major.minor
    only, the ABI-relevant granularity `isolated-build` itself works in): that
    default is exactly what `py_headers` was already resolved against (its
    caller, `smelt.isolated_build.prepare_isolated_natives`, calls
    `smelt.own_python.target_python_headers_for` the same way), and the two have
    to come from the *same* `./configure` run -- see `TargetPythonHeaders`'s own
    doc for why mismatched pairs are silently wrong rather than an error.
    `python_version` (major.minor) is only used here to name the `pythonX.Y`
    directories themselves, matching a real Python install's own convention.

    Cached under `cache_dir` (keyed by the caller on distribution/target) -- keyed
    on the `_sysconfigdata*.py` file itself (the last artifact written) rather than
    `include_dir`'s own existence, so a run that failed partway through (e.g. before
    `target_sysconfigdata_for` itself existed, or on a genuine failure) does not
    leave a permanently-incomplete cache behind: the next call redoes the whole
    thing rather than finding `include_dir` already there and stopping short of
    ever (re)writing the sysconfigdata file.
    """
    py_ver = f"{python_version[0]}.{python_version[1]}"
    include_dir = cache_dir / "include" / f"python{py_ver}"
    lib_dir = cache_dir / "lib" / f"python{py_ver}"
    sysconfigdata_dest = lib_dir / "_sysconfigdata__smelt.py"
    if not sysconfigdata_dest.is_file():
        lib_dir.mkdir(parents=True, exist_ok=True)
        include_dir.mkdir(parents=True, exist_ok=True)
        for entry in py_headers.include_dir.iterdir():
            dest = include_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, dest, dirs_exist_ok=True)
            else:
                shutil.copy(entry, dest)
        shutil.copy(py_headers.pyconfig_dir / "pyconfig.h", include_dir / "pyconfig.h")
        sysconfigdata = target_sysconfigdata_for(target)
        shutil.copy(sysconfigdata, sysconfigdata_dest)
    return lib_dir


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
                _pyo3_cross_lib_dir(
                    target, py_headers, python_version, cache_dir=cache_dir / "pyo3"
                )
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
