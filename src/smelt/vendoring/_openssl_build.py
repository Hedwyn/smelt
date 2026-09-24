"""
Builds OpenSSL from source via Zig, statically, for a vendored provider's own
extension (`smelt.vendoring.cryptography`) to link against -- no
libssl.so/libcrypto.so dependency on the target at all, matching smelt's own
"fully standalone binary" goal.

Unlike `smelt.vendoring._libffi_build` (a build.zig smelt owns and ships itself,
since meta-python never exposes its own libffi build outside its own build
graph), OpenSSL is *not* re-vendored here: meta-python already ships a
standalone, reusable `vendoring/openssl/build.zig` sub-project of its own
(built `no-asm`, arch-agnostic by construction -- see that file's own doc
comment for the full rationale) for its own CPython `_ssl`/`_hashlib` build.
This module just runs *that* project directly (`metapython`'s own installed
copy, located via `metapython.__file__`) instead of hand-maintaining a second,
easily-drifting copy of the same sources/generated headers.

Needs meta-python's own pinned Zig toolchain (a `0.17.0-dev` snapshot, fetched
by `metapython.fetch`) rather than the `ziglang` PyPI package
`_libffi_build` runs via `python -m ziglang`: that build.zig was authored
against the newer, API-incompatible pin and fails to parse under the older one
-- same reasoning `smelt.own_python` already relies on
`metapython.fetch.zig_toolchain` for.

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from smelt.utils import PathExists, SmeltError, assert_path_exists


class OpensslBuildError(SmeltError):
    """
    Raised when `zig build` fails building the vendored OpenSSL.
    """


@dataclass
class OpensslBuild:
    include_dir: PathExists
    archive_path: PathExists


def build_openssl(target: str | None, *, build_dir: Path) -> OpensslBuild:
    """
    Runs `zig build` for meta-python's own `vendoring/openssl/build.zig`
    targeting `target` (a Zig-triple string, `None` for the host's own
    platform), installing into `build_dir`. A no-op if `build_dir` already
    holds a previous build for this target (see the caller,
    `smelt.vendoring.cryptography`, for how that directory is keyed).

    Returns the installed static archive and include directory `zig build`'s
    own `--prefix` produces (`<build_dir>/lib/libopenssl.a`,
    `<build_dir>/include`) -- a single archive satisfying both `libssl` and
    `libcrypto` (see that build.zig's single `openssl` artifact).
    """
    include_dir = build_dir / "include"
    archive_path = build_dir / "lib" / "libopenssl.a"
    if not archive_path.is_file():
        try:
            import metapython
            from metapython import fetch
        except ImportError as exc:
            raise ImportError(
                "metapython is not installed, so smelt cannot build the vendored "
                "OpenSSL its own `vendoring/openssl/build.zig` sub-project needs. "
                "Install this package with the metapython extra: "
                "`uv pip install 'smelt[metapython]'`."
            ) from exc

        openssl_project_dir = Path(metapython.__file__).parent / "_vendor" / "vendoring" / "openssl"
        build_dir.mkdir(parents=True, exist_ok=True)
        zig = fetch.zig_toolchain()
        cmd = [str(zig), "build", "--prefix", str(build_dir)]
        if target is not None:
            cmd.append(f"-Dtarget={target}")
        result = subprocess.run(cmd, cwd=openssl_project_dir, capture_output=True, text=True)
        if result.returncode != 0:
            raise OpensslBuildError(
                f"`zig build` failed building the vendored OpenSSL for target "
                f"{target or 'the host'!r}:\n{result.stdout}\n{result.stderr}"
            )
    return OpensslBuild(
        include_dir=assert_path_exists(include_dir),
        archive_path=assert_path_exists(archive_path),
    )
