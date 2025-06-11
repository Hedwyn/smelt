"""
Test suite for nuitka operations

@date: 11.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile

from test_compiler import get_module_path

from smelt.nuitkaify import compile_with_nuitka


def fib(n: int) -> int:
    if n <= 1:
        return n
    else:
        return fib(n - 2) + fib(n - 1)


def test_nuitkaify() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set the temporary directory as the current working directory
        standalone_app = get_module_path("entrypoint")
        with contextlib.chdir(tmpdir):
            # Compile the standalone app using nuitka
            compile_with_nuitka(str(standalone_app), stdout="stdout")
        expected_exe_path = os.path.join(tmpdir, "entrypoint.bin")
        assert os.path.exists(
            expected_exe_path
        ), "Nuitka did not produce the expected executable."

        # testing the produced exe - computing fib(10), checking if we get it on stdout
        p = subprocess.run(
            [expected_exe_path, "10"], check=True, stdout=subprocess.PIPE
        )
        assert p.returncode == 0, "The produced executable did not run successfully."
        assert p.stdout.decode().rstrip() == str(fib(10))
