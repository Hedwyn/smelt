"""
Command-line interface for Smelt

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
import sys
import warnings
from contextlib import contextmanager
from typing import Callable, Generator, ParamSpec, TypeVar, cast

import click
import tomllib

from smelt.backend import SmeltConfig, SmeltError, run_backend
from smelt.compiler import compile_extension


class SmeltConfigError(SmeltError): ...


type TomlData = dict[str, str | list[str] | TomlData]

P = ParamSpec("P")
R = TypeVar("R")

SMELT_ASCCI_ART: str = r"""
 ____                 _ _
/ ___| _ __ ___   ___| | |_
\___ \| '_ ` _ \ / _ \ | __|
 ___) | | | | | |  __/ | |_
|____/|_| |_| |_|\___|_|\__|

"""


def wrap_smelt_errors(
    should_exist: bool = True, exit_code: int = 1
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Captures `SmeltError` exceptions and displays them to the user in a nicer way.
    """

    @contextmanager
    def wrapper() -> Generator[None, None, None]:
        try:
            yield
        except SmeltError as exc:
            click.echo("/!\\  [Smelt] An error occured:")
            click.echo(exc)
            if should_exist:
                sys.exit(exit_code)

    return wrapper()


def parse_config(toml_data: TomlData) -> SmeltConfig:
    """
    Parses a TOML smelt config and returns the dataclass representation.
    TOML data might come from a dedicated smelt config file or from pyproject.toml.
    For the latter, smelt config should be found under [tool.smelt].
    Use `parse_config_from_pyproject` to get a standalone implementation.
    """
    _c_extensions = toml_data.get("c_extensions", [])
    assert isinstance(_c_extensions, dict)
    assert all(
        (
            isinstance(key, str) and isinstance(val, str)
            for key, val in _c_extensions.items()
        )
    ), _c_extensions
    c_extensions = cast(dict[str, str], _c_extensions)
    _mypyc = toml_data.get("mypyc", {})
    assert isinstance(_mypyc, dict)
    assert all(
        (isinstance(key, str) and isinstance(val, str) for key, val in _mypyc.items())
    ), _mypyc
    mypyc = cast(dict[str, str], _mypyc)

    entrypoint = toml_data.get("entrypoint", None)
    if entrypoint is None:
        # for now, raising
        raise SmeltConfigError("Defining an entrypoint for smelt is mandatory")
    assert isinstance(entrypoint, str), entrypoint
    return SmeltConfig(
        c_extensions=c_extensions,
        mypyc=mypyc,
        entrypoint=entrypoint,
    )


def parse_config_from_pyproject(toml_data: TomlData) -> SmeltConfig:
    """
    Extracts Smelt config from TOML data coming out of a pyproject.toml
    If parsing a smelt config file directly, use `parse_config` instead.
    """
    tool_config = toml_data.get("tool", {})
    if not isinstance(tool_config, dict):
        raise SmeltConfigError(
            f"`tool` section in toml data is not a dictionary, got {tool_config}. "
            "Does the TOML data come from a valid pyproject ?"
        )
    smelt_config = tool_config.get("smelt", None)
    if smelt_config is None:
        raise SmeltConfigError("No smelt config defined in pyproject")

    if not isinstance(smelt_config, dict):
        raise SmeltConfigError(
            f"`smelt` section should be a dictionary, got {smelt_config}. "
        )
    return parse_config(smelt_config)


@click.group()
def smelt() -> None:
    """
    Entrypoint for Smelt frontend
    """
    click.echo(SMELT_ASCCI_ART)


@smelt.command()
@click.option(
    "-p",
    "--path",
    default=".",
    type=str,
)
@wrap_smelt_errors()
def show_config(path: str) -> None:
    """
    Shows the smelt config as defined in the passed file
    """
    try:
        with open(os.path.join(path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    print(parse_config_from_pyproject(toml_data))


@smelt.command()
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=str,
)
@wrap_smelt_errors()
def build_standalone_binary(package_path: str) -> None:
    try:
        with open(os.path.join(package_path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    config = parse_config_from_pyproject(toml_data)
    run_backend(config, stdout="stdout", project_root=package_path)


@smelt.command()
@click.argument(
    "entrypoint-path",
    type=str,
)
@wrap_smelt_errors()
def nuitkaify(entrypoint_path: str) -> None:
    """
    Standalone command to run the nuitka wrapper in this package.
    This is mainly intended for manual self-testing, if you only need nuitka
    features you should probably just call nuitka directly.
    """
    from smelt.nuitkaify import compile_with_nuitka

    compile_with_nuitka(entrypoint_path, stdout="stdout")


@smelt.command()
@click.argument(
    "module-path",
    type=str,
)
@wrap_smelt_errors()
def compile_module(module_path: str) -> None:
    """
    Standalone command to run the nuitka wrapper in this package.
    This is mainly intended for manual self-testing, if you only need nuitka
    features you should probably just call nuitka directly.
    """
    from smelt.nuitkaify import nuitkaify_module

    warnings.warn(
        "This entrypoint is under constrution and will not produce functional .so"
    )
    ext = nuitkaify_module(module_path, stdout="stdout")
    so_path = compile_extension(ext)
    click.echo(f".so path {so_path}")
