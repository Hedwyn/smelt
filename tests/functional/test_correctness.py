"""
Verifies some properties from auto-generated packages using Smelt
build hook.
@date: 11.12.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations
import pytest
from pytest import FixtureRequest
import importlib

USAGE_HINT = """\
These tests are primarily meant for nox and should be run by nox directly.
You can still use this utility to test example packages manually by passing
the package name explicitly with --package {package_name} when calling pytest.
"""

PKG_NOT_FOUND_HINT = """\
Failed to import {package_name}.
If running without nox and using --package,
make sure that the target package `{package_name}` is installed in your venv.
If using nox, check the nox session parameters. 
"""


@pytest.fixture
def package_name(request: FixtureRequest) -> str | None:
    return request.config.getoption("--package")


def test_package_import_cli(package_name: str | None) -> None:
    if package_name is None:
        assert False, USAGE_HINT
    try:
        cli_mod = importlib.import_module(f"{package_name}.cli")
    except ImportError:
        assert False, PKG_NOT_FOUND_HINT.format(package_name=package_name)
    # verifying that the entrypoint function `cli` is in there
    assert hasattr(cli_mod, "compute_fib")
