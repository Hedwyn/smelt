"""
Test suite for nuitka operations

@date: 11.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sysconfig
import tempfile

import pytest
from test_compiler import get_module_path

from smelt.nuitkaify import compile_with_nuitka


def fib(n: int) -> int:
    if n <= 1:
        return n
    else:
        return fib(n - 2) + fib(n - 1)


def _python_links_static_libatomic() -> bool:
    """
    python-build-standalone interpreters (what `uv` installs) leak `-l:libatomic.a`
    from their own build into `sysconfig`'s `MODLIBS`, which Nuitka's backend then
    passes to the linker. Zig's linker cannot resolve the `-l:<file>` spelling, even
    with `-L` pointing at the directory holding the archive, so the Nuitka build fails
    at link time with `unable to find library -l:libatomic.a`. Hits distributions
    that keep `libatomic.a` in a GCC-private directory (e.g. Arch).
    """
    return any(
        "-l:libatomic.a" in (sysconfig.get_config_var(name) or "")
        for name in ("MODLIBS", "LOCALMODLIBS", "LIBS")
    )


@pytest.mark.skipif(
    _python_links_static_libatomic(),
    reason=(
        "python-build-standalone interpreter requests `-l:libatomic.a`, which Zig's "
        "linker cannot resolve (astral-sh/python-build-standalone#616, still open); "
        "run the tests with a different CPython build to exercise this one"
    ),
)
def test_nuitkaify() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set the temporary directory as the current working directory
        standalone_app = get_module_path("entrypoint")
        with contextlib.chdir(tmpdir):
            # Compile the standalone app using nuitka
            compile_with_nuitka(str(standalone_app), stdout="stdout")
        expected_exe_path = os.path.join(tmpdir, "entrypoint.bin")
        assert os.path.exists(expected_exe_path), "Nuitka did not produce the expected executable."

        # testing the produced exe - computing fib(10), checking if we get it on stdout
        p = subprocess.run([expected_exe_path, "10"], check=True, stdout=subprocess.PIPE)
        assert p.returncode == 0, "The produced executable did not run successfully."
        assert p.stdout.decode().rstrip() == str(fib(10))
