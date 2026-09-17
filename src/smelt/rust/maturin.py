"""
Drives `maturin` (the Rust/Python build frontend for a Rust extension module -- PyO3,
`pyo3-ffi`, or plain `cffi`/C-ABI bindings) against a `smelt.rust.toolchain`-fetched
Rust toolchain, cross-compiling for a Zig-triple-shaped `zig_target` the same way the
rest of this codebase does: through `maturin`'s own `--zig` flag, which drives smelt's
`ziglang` dependency as `cargo`'s external linker/libc instead of a system C toolchain
(verified against `armv7-unknown-linux-gnueabihf`, producing a `manylinux2014_armv7l`
wheel with no system Rust or C cross-toolchain installed at all).

@date: 17.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from smelt.rust.toolchain import (
    RustToolchain,
    ensure_rust_target,
    host_rust_triple,
    zig_target_to_rust_triple,
)
from smelt.utils import PathExists, SmeltError, assert_path_exists


class MaturinBuildError(SmeltError):
    """
    Raised when `maturin build` itself fails.
    """


def _maturin_executable() -> str:
    """
    Path to the `maturin` console-script installed alongside this interpreter (the
    `rust` extra installs it into the same environment as smelt itself) -- checked
    there first rather than trusting `PATH` alone, since `RustToolchain.env`'s own
    `PATH` override (cargo's `bin/` prepended ahead of it) must not shadow it.
    """
    candidate = Path(sys.executable).parent / "maturin"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("maturin")
    if found is not None:
        return found
    raise ImportError(
        "maturin is not installed, so smelt cannot build a Rust extension with it. "
        "Install this package with the rust extra: `uv pip install 'smelt[rust]'`."
    )


def _crate_lib_name(manifest_path: PathExists) -> str:
    """
    The compiled artifact stem `cargo` names this crate's `cdylib` after: `[lib].name`
    if the manifest overrides it, else `[package].name` with `-` normalized to `_` --
    `cargo`'s own rule for turning a package name into a valid Rust identifier/filename.
    """
    manifest = tomllib.loads(manifest_path.read_text())
    lib_name = manifest.get("lib", {}).get("name") or manifest["package"]["name"]
    return str(lib_name).replace("-", "_")


@dataclass
class MaturinBuildResult:
    """
    What `build_with_maturin` produced: the built wheel always, plus `object_files` --
    this crate's own per-codegen-unit `.o` files (see `build_with_maturin`'s
    `emit_objects`) -- when the caller asked for them, matching
    `smelt.vendoring.base.VendoredExtension`'s own object-file-first shape instead of
    only ever handing back a finished, dlopen-only `.so`.

    `object_files` never includes this crate's Rust *dependencies* (pyo3, std, ...):
    those stay archived inside their own `.rlib`s under `cargo`'s target directory,
    exactly as opaque a link input as `objects` already treats a pre-built static
    archive elsewhere in this codebase (e.g.
    `smelt.vendoring._libffi_build.LibffiBuild.archive_path`) -- a caller statically
    linking this crate needs those `.rlib`s alongside `object_files`, not instead of them.
    """

    wheel_path: PathExists
    object_files: list[PathExists] = field(default_factory=list)


def build_with_maturin(
    toolchain: RustToolchain,
    manifest_path: Path,
    out_dir: Path,
    *,
    zig_target: str | None,
    interpreter: str | None = None,
    release: bool = True,
    emit_objects: bool = False,
) -> MaturinBuildResult:
    """
    Builds `manifest_path`'s crate (a `Cargo.toml`) into a wheel via `maturin build`,
    using `toolchain` instead of any system Rust install.

    `zig_target` is this codebase's own Zig-triple-shaped target string (`None` for a
    native build, resolved to this host's own Rust triple via
    `smelt.rust.toolchain.host_rust_triple` so the produced layout -- see
    `emit_objects` -- is the same shape regardless of whether this is a cross build).
    For a Linux target, `maturin`'s own `--zig` flag is passed too, cross-linking
    through smelt's `ziglang` dependency (see this module's own doc) instead of a
    system C toolchain -- darwin/windows targets are out of scope here, the same way
    `smelt.own_python._validate_windows_zig_target` restricts Windows to mingw already.

    `emit_objects`, when set, additionally passes `-C save-temps` to `rustc` (as
    trailing `maturin build` rustc args) and returns this crate's own compiled object
    files (see `MaturinBuildResult.object_files`) -- smelt's static-link path
    (`smelt.static_python.build_static_interpreter`) needs these, not the finished,
    dlopen-only `.so` a wheel alone provides.

    Raises `MaturinBuildError` if `maturin build` itself fails; `ImportError` (not
    `MaturinBuildError`) if `maturin`'s own executable cannot be found, matching every
    other extra-gated import in this codebase (e.g. `smelt.isolated_build.fetch_wheel`).
    """
    maturin = _maturin_executable()
    manifest_path = assert_path_exists(manifest_path)

    rust_triple = (
        zig_target_to_rust_triple(zig_target) if zig_target is not None else host_rust_triple()
    )
    ensure_rust_target(toolchain, rust_triple)

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        maturin,
        "build",
        "--manifest-path",
        str(manifest_path),
        "--target",
        rust_triple,
        "-o",
        str(out_dir),
    ]
    if release:
        cmd.append("--release")
    if interpreter is not None:
        cmd.extend(["-i", interpreter])
    if "linux" in rust_triple:
        # `--zig` is what drives cross-/manylinux-compliant linking through smelt's own
        # `ziglang` dependency (see this module's own doc) -- only meaningful, and only
        # accepted by maturin, for a Linux target.
        cmd.append("--zig")
    if emit_objects:
        cmd += ["--", "-C", "save-temps"]

    result = subprocess.run(cmd, env=toolchain.subprocess_env(), capture_output=True, text=True)
    if result.returncode != 0:
        raise MaturinBuildError(
            f"`maturin build` failed for {manifest_path} (target {rust_triple!r}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        raise MaturinBuildError(
            f"`maturin build` reported success but no wheel was found in {out_dir}"
        )
    wheel_path = assert_path_exists(wheels[-1])

    object_files: list[PathExists] = []
    if emit_objects:
        crate_name = _crate_lib_name(manifest_path)
        profile_dir = "release" if release else "debug"
        deps_dir = manifest_path.parent / "target" / rust_triple / profile_dir / "deps"
        object_files = [
            assert_path_exists(obj) for obj in sorted(deps_dir.glob(f"{crate_name}.*.rcgu.o"))
        ]
        if not object_files:
            raise MaturinBuildError(
                f"emit_objects=True but no `.rcgu.o` files were found under {deps_dir} "
                "-- maturin/cargo may have changed its `-C save-temps` output layout."
            )

    return MaturinBuildResult(wheel_path=wheel_path, object_files=object_files)
