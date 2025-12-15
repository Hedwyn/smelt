"""
Verifies some properties from auto-generated packages using Smelt
build hook.
@date: 11.12.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations
import sys
import pytest
from types import ModuleType
from pytest import FixtureRequest
import importlib

from smelt.process import call_command

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


def try_import_cli_module(package_name: str | None) -> ModuleType:
    if package_name is None:
        assert False, USAGE_HINT
    try:
        return importlib.import_module(f"{package_name}.cli")
    except ImportError:
        assert False, PKG_NOT_FOUND_HINT.format(package_name=package_name)


def test_package_import_cli(package_name: str | None) -> None:
    cli_mod = try_import_cli_module(package_name)
    # verifying that the entrypoint function `cli` is in there
    assert hasattr(cli_mod, "compute_fib")


def test_cli_mod_returns_correct_value(package_name: str | None) -> None:
    cli_mod = try_import_cli_module(package_name)
    # verifying that the entrypoint function `cli` is in there
    ctx = call_command("compute-fib", "10", timeout=1.0, printer=print)
    assert ctx.exit_code == 0
    assert len(ctx.stdout) == 1
    assert ctx.stdout[0].strip() == "55"
