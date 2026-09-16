"""
Builds libffi from source via Zig, statically, for a vendored provider's own
extension (currently only `smelt.vendoring.cffi`) to link against -- no
libffi.so dependency on the target at all, matching smelt's own "fully
standalone binary" goal.

`vendoring/libffi/build.zig` is a thin wrapper around the ready-made
`allyourcodebase/libffi` Zig package -- the same dependency
`meta-python/build.zig` already depends on for its own `_ctypes` build (see that
project's `build.zig.zon`) -- that just re-exposes its `ffi` artifact for a
standalone `zig build`. meta-python never exposes that artifact outside its own
build graph (it is folded straight into `libpython`/`_ctypes`), so there is no
path/env var to reuse from there; this is its own copy of the same dependency
edge.

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from smelt.utils import PathExists, SmeltError, assert_path_exists

_LIBFFI_PROJECT_DIR: Path = Path(__file__).parent / "libffi"


class LibffiBuildError(SmeltError):
    """
    Raised when `zig build` fails building the vendored libffi.
    """


@dataclass
class LibffiBuild:
    include_dir: PathExists
    archive_path: PathExists


def build_libffi(target: str | None, *, build_dir: Path) -> LibffiBuild:
    """
    Runs `zig build` for `vendoring/libffi/build.zig` targeting `target` (a
    Zig-triple string, `None` for the host's own platform), installing into
    `build_dir`. A no-op if `build_dir` already holds a previous build for this
    target (see the caller, `smelt.vendoring.cffi`, for how that directory is
    keyed).

    Returns the installed static archive and include directory `zig build`'s own
    `--prefix` produces (`<build_dir>/lib/libffi.a`, `<build_dir>/include`).
    """
    include_dir = build_dir / "include"
    archive_path = build_dir / "lib" / "libffi.a"
    if not archive_path.is_file():
        build_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "ziglang",
            "build",
            "--prefix",
            str(build_dir),
        ]
        if target is not None:
            cmd.append(f"-Dtarget={target}")
        result = subprocess.run(cmd, cwd=_LIBFFI_PROJECT_DIR, capture_output=True, text=True)
        if result.returncode != 0:
            raise LibffiBuildError(
                f"`zig build` failed building the vendored libffi for target "
                f"{target or 'the host'!r}:\n{result.stdout}\n{result.stderr}"
            )
    return LibffiBuild(
        include_dir=assert_path_exists(include_dir),
        archive_path=assert_path_exists(archive_path),
    )
