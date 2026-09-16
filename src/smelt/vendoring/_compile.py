"""
Compiles a vendored provider's `setuptools.Extension` for an arbitrary target
triple string (`smelt.isolated_build`'s convention -- e.g. `"aarch64-linux-musl"`),
unlike `smelt.compiler.compile_extension_objects`, which only accepts the
narrower, GNU-libc-only `SupportedPlatforms` enum (3 members today, see
`smelt.compiler`). Mirrors `smelt.compiler._compile_extension_sources` one level
down, without requiring `target` to already be one of those 3 members.

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import sysconfig
from pathlib import Path

from setuptools import Extension

from smelt.compiler import PYCONFIG_PATH, ZigCompiler, sanity_msvc_flags
from smelt.utils import PathExists, assert_path_exists


def compile_extension_for_target(
    extension: Extension,
    target: str | None,
    build_dir: Path,
) -> list[PathExists]:
    """
    Compiles `extension`'s sources for `target` (a Zig-triple-shaped string,
    `None` meaning the host's own platform) into `build_dir`, returning the
    produced object files. Stops short of linking, same as
    `smelt.compiler.compile_extension_objects`.
    """
    compiler = ZigCompiler()
    include_dirs = [sysconfig.get_path("include"), sysconfig.get_path("platinclude")]
    extra_preargs: list[str] = []
    if target is not None:
        extra_preargs.append(f"--target={target}")
        # Same cross-compile pyconfig.h shim `_compile_extension_sources` reaches
        # for -- see that function's own TODO about a target-specific one.
        include_dirs.append(PYCONFIG_PATH)

    objects = compiler.compile(
        sources=extension.sources,
        output_dir=str(build_dir),
        include_dirs=include_dirs + extension.include_dirs,
        extra_preargs=extra_preargs,
        extra_postargs=sanity_msvc_flags(extension.extra_compile_args or []),
        macros=extension.define_macros,
    )
    return [assert_path_exists(obj) for obj in objects]
