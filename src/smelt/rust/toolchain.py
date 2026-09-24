"""
Fetches a standalone Rust toolchain (`rustup` + `cargo`, no system Rust needed) via
`puccinialin`, and adds cross-compilation targets to it.

This is the Rust-side counterpart to `smelt.compiler.ZigCompiler`: `rustc` is already a
cross-compiler by design (it only needs a target's standard library, added via `rustup
target add`), but linking a cross target's final binary still needs a linker and libc
for it. `compile_rust_source_for_target` supplies both from smelt's own `ziglang`
dependency, the same way every other backend in this codebase does -- verified end to
end against `armv7-unknown-linux-gnueabihf` (`arm-linux-gnueabihf` and
`arm-linux-musleabihf`), the latter producing a fully static binary that runs under
`qemu-arm-static` with no target sysroot at all.

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from smelt.compiler import ZigCompiler
from smelt.utils import PathExists, SmeltError, assert_path_exists


class RustToolchainError(SmeltError):
    """
    Raised when the Rust toolchain cannot be fetched, or a target cannot be added to it.
    """


#: Where a fetched Rust toolchain (one host-native `rustup`/`cargo` install, regardless
#: of how many cross targets are later added to it) is cached across builds -- mirrors
#: `smelt.own_python.own_python_cache_dir`/`smelt.isolated_build.vendored_build_cache_dir`.
_RUST_TOOLCHAIN_CACHE_DIR: Final[Path] = Path.home() / ".cache" / "smelt" / "rust-toolchain"

#: Where a target's zig-cc linker wrapper script (see `_zig_linker_wrapper`) is cached,
#: sibling to the toolchain install itself rather than under it -- it does not belong
#: to any one `rustup`/`cargo` install, only to the (target, host-python) pair that
#: produced it.
_ZIG_LINKER_CACHE_DIR: Final[Path] = Path.home() / ".cache" / "smelt" / "rust-zig-linkers"

#: Zig arch component -> Rust arch component, for the arches this project's own targets
#: (`own_python_target`, `isolated-build`'s `target`, ...) actually use. `arm` maps to
#: `armv7` (not the older `armv6`-baseline `arm-unknown-linux-gnueabihf`): this
#: codebase's own `arm-linux-gnueabihf` (`smelt.compiler.SupportedPlatforms.ARMV7L_LINUX`)
#: already names itself after the armv7l hardware it targets.
_ZIG_ARCH_TO_RUST_ARCH: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "arm": "armv7",
    "x86": "i686",
}

#: Zig os component -> (Rust vendor, Rust os) components. Windows is restricted to
#: `-gnu` (mingw) ABI the same way `own_python._validate_windows_zig_target` already
#: restricts it -- smelt drives no MSVC toolchain. macOS is not listed: Rust cross-links
#: it through its own SDK/linker story, not a plain C linker `--zig` can stand in for.
_ZIG_OS_TO_RUST_VENDOR_OS: Final[dict[str, tuple[str, str]]] = {
    "linux": ("unknown", "linux"),
    "windows": ("pc", "windows"),
}


def zig_target_to_rust_triple(zig_target: str) -> str:
    """
    The Rust target triple (e.g. `armv7-unknown-linux-gnueabihf`) for `zig_target`
    (this codebase's own Zig-triple-shaped target string, e.g. `arm-linux-gnueabihf` --
    same 3-component `<arch>-<os>-<abi>` shape `smelt.own_python`/`smelt.isolated_build`
    already use). The ABI component (`gnu`, `musl`, `gnueabihf`, `musleabihf`, ...)
    passes through unchanged: Rust and Zig spell it the same way, only the arch and
    (for Rust) an extra vendor component differ.

    Raises `RustToolchainError` for a target this mapping does not recognize yet
    (an unlisted arch or OS -- see `_ZIG_ARCH_TO_RUST_ARCH`/`_ZIG_OS_TO_RUST_VENDOR_OS`),
    mirroring `smelt.compiler.SupportedPlatforms.from_triple`'s own explicit-failure style.
    """
    parts = zig_target.split("-")
    if len(parts) != 3:
        raise RustToolchainError(
            f"{zig_target!r} is not a <arch>-<os>-<abi> Zig target triple -- cannot "
            "derive a Rust target triple from it."
        )
    arch, os_, abi = parts
    rust_arch = _ZIG_ARCH_TO_RUST_ARCH.get(arch)
    if rust_arch is None:
        raise RustToolchainError(
            f"No Rust target arch known for Zig arch {arch!r} (from {zig_target!r}) -- "
            f"supported: {sorted(_ZIG_ARCH_TO_RUST_ARCH)}"
        )
    vendor_os = _ZIG_OS_TO_RUST_VENDOR_OS.get(os_)
    if vendor_os is None:
        raise RustToolchainError(
            f"No Rust target OS known for Zig os {os_!r} (from {zig_target!r}) -- "
            f"supported: {sorted(_ZIG_OS_TO_RUST_VENDOR_OS)}"
        )
    vendor, rust_os = vendor_os
    return f"{rust_arch}-{vendor}-{rust_os}-{abi}"


@dataclass
class RustToolchain:
    """
    An isolated `rustup`/`cargo` install `ensure_rust_toolchain` produced. `env` holds
    the variables (`RUSTUP_HOME`, `CARGO_HOME`, `PATH`) that redirect every
    `cargo`/`rustc`/`rustup` invocation to it instead of any system Rust install --
    forward it (via `subprocess_env`) to every subprocess call driving this toolchain.
    """

    cargo_home: PathExists
    rustup_home: PathExists
    env: dict[str, str]

    def subprocess_env(self) -> dict[str, str]:
        """
        `os.environ`, overridden with this toolchain's own `env` -- what every
        `subprocess.run` call driving `cargo`/`rustc`/`rustup` should pass as `env=`.
        """
        return {**os.environ, **self.env}


def _cached_toolchain_env(install_dir: Path) -> dict[str, str] | None:
    """
    The `extra_env` `puccinialin.setup_rust(installation_dir=install_dir)` would
    return, without invoking it -- `None` if no working `cargo` is there yet (first
    call for this `install_dir`, or a partial/corrupted previous one).

    `puccinialin.setup_rust` has no such short-circuit itself: it re-runs
    `rustup-init -y --no-modify-path ...` unconditionally on every call. Harmless --
    `rustup-init` is itself idempotent, and `-y`/`--no-modify-path` make its own
    "Rust is already installed" PATH scan (routinely tripped by a distro-packaged
    `rustc` on `$PATH`; irrelevant to this isolated, non-PATH-modifying install) a
    non-issue -- but it still means every single smelt build spawns `rustup-init` and
    prints its full warning block for what should be a no-op once the toolchain
    already exists here. Mirrors `puccinialin`'s own paths (`<install_dir>/rustup`,
    `<install_dir>/cargo`) and final health check (`cargo --version`) exactly, so a
    skip here is indistinguishable from one it would have performed itself.
    """
    rustup_home = install_dir / "rustup"
    cargo_home = install_dir / "cargo"
    cargo = cargo_home / "bin" / "cargo"
    if not cargo.is_file() or not os.access(cargo, os.X_OK):
        return None
    extra_env = {
        "RUSTUP_HOME": str(rustup_home),
        "CARGO_HOME": str(cargo_home),
        "PATH": f"{cargo_home / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    try:
        subprocess.run(
            [str(cargo), "--version"],
            env={**os.environ, **extra_env},
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return extra_env


def ensure_rust_toolchain(install_dir: Path | str | None = None) -> RustToolchain:
    """
    Fetches (or reuses an already-fetched) standalone Rust toolchain via `puccinialin`
    -- no system `rustup`/`cargo` needed, the same "fully standalone" goal
    `smelt.vendoring`/`smelt.own_python` already get from smelt's own pinned `ziglang`
    instead of a system C compiler.

    `install_dir` defaults to a single cache directory (`_RUST_TOOLCHAIN_CACHE_DIR`)
    reused across builds -- mirrors `smelt.own_python.own_python_cache_dir`/
    `smelt.isolated_build.vendored_build_cache_dir`. Checked via `_cached_toolchain_env`
    first (see its own doc for why `puccinialin.setup_rust` itself is not enough of a
    no-op to skip calling): only a `_cached_toolchain_env` miss falls through to it
    (and, with it, the `rust` extra's own import requirement -- an already-cached
    toolchain needs `puccinialin` installed no more than it needs to run again).
    """
    install_dir = Path(install_dir) if install_dir is not None else _RUST_TOOLCHAIN_CACHE_DIR
    install_dir.mkdir(parents=True, exist_ok=True)

    extra_env = _cached_toolchain_env(install_dir)
    if extra_env is None:
        try:
            import puccinialin
        except ImportError as exc:
            raise ImportError(
                "puccinialin is not installed, so smelt cannot fetch a standalone "
                "Rust toolchain. Install this package with the rust extra: "
                "`uv pip install 'smelt[rust]'`."
            ) from exc
        extra_env = puccinialin.setup_rust(installation_dir=install_dir)

    return RustToolchain(
        cargo_home=assert_path_exists(extra_env["CARGO_HOME"]),
        rustup_home=assert_path_exists(extra_env["RUSTUP_HOME"]),
        env=extra_env,
    )


def host_rust_triple() -> str:
    """
    This host's own Rust target triple (e.g. `x86_64-unknown-linux-gnu`), as
    `puccinialin` itself derives it from `sysconfig` -- what a native (non-cross)
    `rustc`/`cargo`/`maturin` invocation targets by default.
    """
    try:
        from puccinialin import get_triple
    except ImportError as exc:
        raise ImportError(
            "puccinialin is not installed, so smelt cannot determine this host's own "
            "Rust target triple. Install this package with the rust extra: "
            "`uv pip install 'smelt[rust]'`."
        ) from exc
    return get_triple(sys.stderr)


def ensure_rust_target(toolchain: RustToolchain, rust_triple: str) -> None:
    """
    Adds `rust_triple`'s standard library to `toolchain` via `rustup target add` --
    needed before `rustc`/`cargo` can build anything for a target other than the
    toolchain's own host triple. A no-op (`rustup` checks first, no network round trip)
    if `rust_triple` is already installed.
    """
    result = subprocess.run(
        ["rustup", "target", "add", rust_triple],
        env=toolchain.subprocess_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RustToolchainError(
            f"`rustup target add {rust_triple}` failed:\n{result.stdout}\n{result.stderr}"
        )


def _zig_linker_wrapper(zig_target: str) -> PathExists:
    """
    A small shell script wrapping `zig cc -target {zig_target}` as a single executable
    -- what `rustc -C linker=` needs, since it only accepts one program path, not
    `rustc`'s own multi-argument `python -m ziglang cc` invocation.

    Cached under `_ZIG_LINKER_CACHE_DIR`, keyed by `zig_target`: a no-op past the first
    call for a given target, mirroring `smelt.vendoring._libffi_build.build_libffi`'s
    own build-dir cache.
    """
    _ZIG_LINKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wrapper_path = _ZIG_LINKER_CACHE_DIR / f"zigcc-{zig_target}.sh"
    if not wrapper_path.is_file():
        zig_cc = " ".join(f'"{part}"' for part in (*ZigCompiler.zig_base_path, "cc"))
        wrapper_path.write_text(f'#!/bin/sh\nexec {zig_cc} -target {zig_target} "$@"\n')
        wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return assert_path_exists(wrapper_path)


def compile_rust_source_for_target(
    toolchain: RustToolchain,
    source: Path,
    output: Path,
    zig_target: str,
    *,
    crate_type: str = "bin",
    static: bool = False,
) -> PathExists:
    """
    Compiles a single Rust source file for `zig_target` (a Zig-triple-shaped target
    string, e.g. `arm-linux-gnueabihf`), linking through smelt's own `ziglang`
    dependency (see `_zig_linker_wrapper`) instead of a system linker -- adding
    `zig_target`'s Rust target (see `ensure_rust_target`) first if needed.

    `static`, only meaningful for a `musl` ABI target, statically links the produced
    binary against a musl libc supplied by `zig cc` itself (`-C
    target-feature=+crt-static -C link-self-contained=off`) rather than rustc's own
    bundled musl libc/CRT objects, which duplicate symbols with zig's when both are
    linked in (`ld.lld: error: duplicate symbol: _start`). Verified runnable under
    `qemu-arm-static` (no target sysroot at all) for `arm-linux-musleabihf`.

    This is the standalone verification path for `smelt.rust`'s own toolchain-fetching
    layer -- `smelt.rust.maturin.build_with_maturin` cross-links a real extension
    module the same way, but through `maturin`'s own built-in `--zig` support rather
    than this function.
    """
    rust_triple = zig_target_to_rust_triple(zig_target)
    ensure_rust_target(toolchain, rust_triple)
    linker = _zig_linker_wrapper(zig_target)

    cmd = [
        "rustc",
        "--target",
        rust_triple,
        "--crate-type",
        crate_type,
        "-C",
        f"linker={linker}",
    ]
    if static:
        cmd += ["-C", "target-feature=+crt-static", "-C", "link-self-contained=off"]
    cmd += ["-o", str(output), str(source)]

    result = subprocess.run(cmd, env=toolchain.subprocess_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise RustToolchainError(
            f"rustc failed cross-compiling {source} for {zig_target!r} "
            f"(Rust triple {rust_triple!r}):\n{result.stdout}\n{result.stderr}"
        )
    return assert_path_exists(output)
