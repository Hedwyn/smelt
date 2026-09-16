"""
Compiles a vendored provider's `setuptools.Extension` for an arbitrary target
triple string (`smelt.isolated_build`'s convention -- e.g. `"aarch64-linux-musl"`),
unlike `smelt.compiler.compile_extension_objects`, which only accepts the
narrower, GNU-libc-only `SupportedPlatforms` enum (3 members today, see
`smelt.compiler`), without requiring `target` to already be one of those 3 members.

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import sysconfig
from pathlib import Path

from setuptools import Extension

from smelt.compiler import PYCONFIG_PATH, ZigCompiler, sanity_msvc_flags
from smelt.own_python import TargetPythonHeaders
from smelt.utils import PathExists, assert_path_exists


def compile_extension_for_target(
    extension: Extension,
    target: str | None,
    build_dir: Path,
    *,
    py_headers: TargetPythonHeaders | None = None,
) -> list[PathExists]:
    """
    Compiles `extension`'s sources for `target` (a Zig-triple-shaped string,
    `None` meaning the host's own platform) into `build_dir`, returning the
    produced object files. Stops short of linking, same as
    `smelt.compiler.compile_extension_objects`.

    `py_headers`, when given, *replaces* the host's own `sysconfig` include dirs
    entirely for a cross-target compile, rather than merely being added ahead of
    them: `Python.h` (from `py_headers.include_dir`) and `pyconfig.h` (from
    `py_headers.pyconfig_dir`) have to come from the same, target-consistent
    tree (see `TargetPythonHeaders`'s own doc for why) -- `Python.h`'s own
    `#include "pyconfig.h"` is a quoted include, which C/C++ resolve against the
    *including file's own directory first*, before any `-I` search path at all.
    Since the host's own `sysconfig` include dir has a `Python.h` with its own,
    host-native `pyconfig.h` sitting right next to it, merely adding the
    target's `pyconfig.h` directory to `-I` (regardless of position) can never
    win that lookup as long as the host's `Python.h` is the one actually
    `#include`d -- only *also* replacing which `Python.h` is found (with one
    whose own directory has no competing `pyconfig.h`) lets the target's
    `-I`-supplied one be found at all. Falls back to the host's own dirs plus
    the generic, checked-in `PYCONFIG_PATH` shim only when `target` is set but
    no headers were resolved, so a direct/standalone call does not outright
    fail to compile (this fallback remains as broken as before for a real
    cross-arch target -- it exists only to avoid a harder failure for a caller
    that never resolves `py_headers` at all).
    """
    compiler = ZigCompiler()
    extra_preargs: list[str] = []
    if target is not None and py_headers is not None:
        extra_preargs.append(f"--target={target}")
        include_dirs = [str(py_headers.include_dir), str(py_headers.pyconfig_dir)]
    else:
        include_dirs = [sysconfig.get_path("include"), sysconfig.get_path("platinclude")]
        if target is not None:
            extra_preargs.append(f"--target={target}")
            include_dirs.insert(0, PYCONFIG_PATH)

    objects = compiler.compile(
        sources=extension.sources,
        output_dir=str(build_dir),
        include_dirs=include_dirs + extension.include_dirs,
        extra_preargs=extra_preargs,
        extra_postargs=sanity_msvc_flags(extension.extra_compile_args or []),
        macros=extension.define_macros,
    )
    return [assert_path_exists(obj) for obj in objects]
