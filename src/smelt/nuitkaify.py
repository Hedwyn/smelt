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
import subprocess
import sys
from typing import Final, Literal

_logger = logging.getLogger(__name__)

NUITKA_ENTRYPOINT: Final[tuple[str, ...]] = (sys.executable, "-m", "nuitka")

type Stdout = Literal["stdout", "logger"]


def compile_with_nuitka(
    path: str, no_follow_imports: bool = False, stdout: Stdout | None = None
) -> None:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.
    """
    cmd = list(NUITKA_ENTRYPOINT)
    if not no_follow_imports:
        cmd.append("--follow-imports")
    cmd.append("--onefile")
    cmd.append(path)
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
