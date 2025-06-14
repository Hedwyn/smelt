"""
Wrapper on top of nuitka to compile a Python script into a standalone executable.

Currently nuitka is called as a subprocess, as it would be from `python -m nuitka`.
Options are passed as CLI arguments.

This is the simple option as nuitka is not really designed for library use: some of the business logic
is run on import, a few critical components are handled global variables, so there a some major drawbacks to
trying to import the code and call directly.
This might be changed later.

@date: 11.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Final, Iterable, Literal

_logger = logging.getLogger(__name__)

NUITKA_ENTRYPOINT: Final[tuple[str, ...]] = (sys.executable, "-m", "nuitka")

type Stdout = Literal["stdout", "logger"]


def compile_with_nuitka(
    path: str,
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
) -> str:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.
    """
    try:
        import nuitka

        # not using the import - just checking if it is available
        # as following logic would fail otherwise
        _ = nuitka
    except ImportError:
        raise ImportError(
            "Nuitka is not installed. Please install this package with nuitka extra: `pip install smelt[nuitka]`."
        )
    cmd = list(NUITKA_ENTRYPOINT)
    if not no_follow_imports:
        cmd.append("--follow-imports")
    cmd.append("--onefile")
    cmd.append(path)

    # handling special flags
    if include_modules:
        for mod in include_modules:
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    _logger.debug("Running %s", " ".join(cmd))

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(cmd)
    while True:
        assert proc.stdout is not None, "Process not created with stdout in PIPE mode"
        line = proc.stdout.readline()
        if not line:
            break
        decoded_line = line.decode().rstrip()
        if stdout == "logger":
            _logger.info(decoded_line)
        elif stdout == "stdout":
            print(decoded_line)

    _logger.info("[Nuitka]: %d", proc.returncode)
    expected_extension = ".exe" if sys.platform == "Windows" else ".bin"
    bin_path = os.path.basename(path).replace(".py", expected_extension)
    assert os.path.exists(bin_path)
    return bin_path
