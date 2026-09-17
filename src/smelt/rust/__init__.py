"""
Rust support for smelt, gated behind the `rust` extra (`puccinialin`, `maturin`) the
same way `isolated-build` gates `unearth`.

A Rust extension needs its own toolchain -- `rustc`/`cargo` are not something Zig
provides -- but a build still has to reach smelt's "fully standalone" goal: no system
Rust install, and cross-compiling without a system C toolchain either.
`smelt.rust.toolchain` fetches an isolated `rustup`/`cargo` via `puccinialin` and adds
cross targets to it; `smelt.rust.maturin` builds a Rust extension with that toolchain,
cross-linking through smelt's own `ziglang` dependency (via `maturin`'s own `--zig`
flag) instead of a system linker/libc.

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from smelt.rust.maturin import MaturinBuildError, MaturinBuildResult, build_with_maturin
from smelt.rust.toolchain import (
    RustToolchain,
    RustToolchainError,
    compile_rust_source_for_target,
    ensure_rust_target,
    ensure_rust_toolchain,
    host_rust_triple,
    zig_target_to_rust_triple,
)

__all__ = [
    "MaturinBuildError",
    "MaturinBuildResult",
    "RustToolchain",
    "RustToolchainError",
    "build_with_maturin",
    "compile_rust_source_for_target",
    "ensure_rust_target",
    "ensure_rust_toolchain",
    "host_rust_triple",
    "zig_target_to_rust_triple",
]
