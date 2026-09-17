"""
Tests for `smelt.rust`: fetching a standalone Rust toolchain and cross-compiling
through it, directly (`smelt.rust.toolchain`) and via `maturin` (`smelt.rust.maturin`).

Two tiers, the same split `test_isolated_build.py` uses. The Zig-triple -> Rust-triple
mapping is pure string logic and runs anywhere. Everything that actually fetches a
Rust toolchain or invokes `maturin` needs the `rust` extra installed and a reachable
network, and is skipped otherwise.

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from smelt.rust.maturin import build_with_maturin
from smelt.rust.toolchain import (
    RustToolchainError,
    compile_rust_source_for_target,
    ensure_rust_target,
    ensure_rust_toolchain,
    zig_target_to_rust_triple,
)


def _network_reachable() -> bool:
    try:
        with socket.create_connection(("static.rust-lang.org", 443), timeout=2):
            return True
    except OSError:
        return False


try:
    import puccinialin as _puccinialin

    _ = _puccinialin
    _PUCCINIALIN_AVAILABLE = True
except ImportError:
    _PUCCINIALIN_AVAILABLE = False

needs_rust_network = pytest.mark.skipif(
    not _PUCCINIALIN_AVAILABLE or not _network_reachable(),
    reason="needs the rust extra installed and a reachable rust-lang.org",
)


# --- zig_target_to_rust_triple (pure logic) -----------------------------------------


def test_zig_target_to_rust_triple_linux_gnu() -> None:
    assert zig_target_to_rust_triple("x86_64-linux-gnu") == "x86_64-unknown-linux-gnu"
    assert zig_target_to_rust_triple("aarch64-linux-gnu") == "aarch64-unknown-linux-gnu"


def test_zig_target_to_rust_triple_arm() -> None:
    assert zig_target_to_rust_triple("arm-linux-gnueabihf") == "armv7-unknown-linux-gnueabihf"
    assert zig_target_to_rust_triple("arm-linux-musleabihf") == "armv7-unknown-linux-musleabihf"


def test_zig_target_to_rust_triple_windows() -> None:
    assert zig_target_to_rust_triple("x86_64-windows-gnu") == "x86_64-pc-windows-gnu"


def test_zig_target_to_rust_triple_unsupported_arch() -> None:
    with pytest.raises(RustToolchainError):
        zig_target_to_rust_triple("riscv64-linux-gnu")


def test_zig_target_to_rust_triple_unsupported_os() -> None:
    with pytest.raises(RustToolchainError):
        zig_target_to_rust_triple("aarch64-macos-none")


def test_zig_target_to_rust_triple_malformed() -> None:
    with pytest.raises(RustToolchainError):
        zig_target_to_rust_triple("not-a-triple")


# --- toolchain fetch + cross-compile (needs network) --------------------------------


@needs_rust_network
def test_cross_compile_hello_world_for_armv7(tmp_path: Path) -> None:
    toolchain = ensure_rust_toolchain(tmp_path / "toolchain")

    source = tmp_path / "hello.rs"
    source.write_text('fn main() { println!("Hello, world!"); }')
    output = tmp_path / "hello_armv7"

    compile_rust_source_for_target(toolchain, source, output, "arm-linux-gnueabihf")

    assert output.is_file()
    # ELF header: e_machine (offset 18, 2 bytes little-endian) == EM_ARM (40) --
    # confirms this actually cross-compiled for armv7 rather than the host's own arch.
    header = output.read_bytes()[:20]
    assert header[:4] == b"\x7fELF"
    e_machine = int.from_bytes(header[18:20], "little")
    assert e_machine == 40


@needs_rust_network
def test_cross_compile_static_musl_runs_under_qemu(tmp_path: Path) -> None:
    qemu = shutil.which("qemu-arm-static") or shutil.which("qemu-arm")
    if qemu is None:
        pytest.skip("needs qemu-arm(-static) to actually run the produced binary")

    toolchain = ensure_rust_toolchain(tmp_path / "toolchain")
    source = tmp_path / "hello.rs"
    source.write_text('fn main() { println!("Hello, world!"); }')
    output = tmp_path / "hello_armv7_musl"

    compile_rust_source_for_target(toolchain, source, output, "arm-linux-musleabihf", static=True)

    result = subprocess.run([qemu, str(output)], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "Hello, world!"


@needs_rust_network
def test_ensure_rust_target_is_idempotent(tmp_path: Path) -> None:
    toolchain = ensure_rust_toolchain(tmp_path / "toolchain")
    ensure_rust_target(toolchain, "armv7-unknown-linux-gnueabihf")
    # A second call must not fail: rustup itself no-ops on an already-installed target.
    ensure_rust_target(toolchain, "armv7-unknown-linux-gnueabihf")


# --- maturin (needs network) ---------------------------------------------------------

_CARGO_TOML = """\
[package]
name = "smelt_rust_test_crate"
version = "0.1.0"
edition = "2024"

[lib]
name = "smelt_rust_test_crate"
crate-type = ["cdylib"]

[dependencies]
"""

_PYPROJECT_TOML = """\
[build-system]
requires = ["maturin>=1.15,<2.0"]
build-backend = "maturin"

[project]
name = "smelt-rust-test-crate"
requires-python = ">=3.8"
dynamic = ["version"]

[tool.maturin]
bindings = "cffi"
"""

_LIB_RS = """\
#[unsafe(no_mangle)]
pub extern "C" fn add(a: i32, b: i32) -> i32 { a + b }
"""


@pytest.fixture
def maturin_crate(tmp_path: Path) -> Path:
    crate_dir = tmp_path / "smelt_rust_test_crate"
    (crate_dir / "src").mkdir(parents=True)
    (crate_dir / "Cargo.toml").write_text(_CARGO_TOML)
    (crate_dir / "pyproject.toml").write_text(_PYPROJECT_TOML)
    (crate_dir / "src" / "lib.rs").write_text(_LIB_RS)
    return crate_dir / "Cargo.toml"


@needs_rust_network
def test_build_with_maturin_cross_compiles_and_emits_objects(
    maturin_crate: Path, tmp_path: Path
) -> None:
    toolchain = ensure_rust_toolchain(tmp_path / "toolchain")

    result = build_with_maturin(
        toolchain,
        maturin_crate,
        tmp_path / "wheelout",
        zig_target="arm-linux-gnueabihf",
        interpreter="python3.12",
        emit_objects=True,
    )

    assert result.wheel_path.is_file()
    assert "armv7l" in result.wheel_path.name
    assert result.object_files
    for obj in result.object_files:
        assert obj.is_file()
        assert obj.suffix == ".o"
