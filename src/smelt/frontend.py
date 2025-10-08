"""
Command-line interface for Smelt

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tomllib
import warnings
from contextlib import contextmanager
from typing import Callable, Generator, ParamSpec, TypeVar, cast

import click
from mypyc.build import mypycify

from smelt.backend import SmeltConfig, compile_mypyc_extensions, run_backend
from smelt.compiler import SupportedPlatforms, compile_extension
from smelt.mypycify import mypycify_module
from smelt.utils import SmeltError


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

add_logging_option = click.option(
    "-l",
    "--logging-level",
    type=click.Choice(list(logging._nameToLevel), case_sensitive=False),
    help="Logging level to apply. Logs are emitted to stdout",
    default="warning",
)


def _compile_module_with_nuitka(
    module_path: str, crosscompile: str | None, shadow: bool
) -> str:
    from smelt.nuitkaify import nuitkaify_module

    target_platform = SupportedPlatforms(crosscompile) if crosscompile else None
    warnings.warn(
        "This entrypoint is under construction and will not produce functional .so"
    )
    ext = nuitkaify_module(module_path, stdout="stdout")
    so_path = compile_extension(
        ext, use_zig_native_interface=True, crosscompile=target_platform
    )
    if not shadow:
        return so_path

    source_module_folder = os.path.dirname(module_path)
    dest_path = os.path.join(source_module_folder, os.path.basename(so_path))
    shutil.move(so_path, dest_path)
    return dest_path


def _compile_module_with_mypyc(
    module_path: str, crosscompile: str | None, shadow: bool
) -> str:
    from smelt.nuitkaify import nuitkaify_module

    target_platform = SupportedPlatforms(crosscompile) if crosscompile else None
    warnings.warn(
        "This entrypoint is under construction and will not produce functional .so"
    )
    ext = nuitkaify_module(module_path, stdout="stdout")
    so_path = compile_extension(
        ext, use_zig_native_interface=True, crosscompile=target_platform
    )
    if not shadow:
        return so_path

    source_module_folder = os.path.dirname(module_path)
    dest_path = os.path.join(source_module_folder, os.path.basename(so_path))
    shutil.move(so_path, dest_path)
    return dest_path


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
    For the latter, smelt config should be found under [tool.smelt]
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

    cython = cast(dict[str, str], toml_data.get("cython", {}))

    entrypoint = toml_data.get("entrypoint", None)
    if entrypoint is None:
        # for now, raising
        raise SmeltConfigError("Defining an entrypoint for smelt is mandatory")
    assert isinstance(entrypoint, str), entrypoint
    return SmeltConfig(
        c_extensions=c_extensions,
        mypyc=mypyc,
        entrypoint=entrypoint,
        cython=cython,
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
@add_logging_option
@wrap_smelt_errors()
def show_config(path: str, logging_level: str) -> None:
    """
    Shows the smelt config as defined in the passed file
    """

    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)

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
@add_logging_option
@wrap_smelt_errors()
def build_standalone_binary(package_path: str, logging_level: str) -> None:
    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    try:
        with open(os.path.join(package_path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    config = parse_config_from_pyproject(toml_data)
    run_backend(config, stdout="stdout", project_root=package_path)


@smelt.command()
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=str,
)
@add_logging_option
@wrap_smelt_errors()
def compile_all_mypyc_extensions(package_path: str, logging_level: str) -> None:
    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    try:
        with open(os.path.join(package_path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    config = parse_config_from_pyproject(toml_data)
    compile_mypyc_extensions(package_path, mypyc_config=config.mypyc)


@smelt.command()
@click.argument(
    "entrypoint-path",
    type=str,
)
@add_logging_option
@wrap_smelt_errors()
def nuitkaify(entrypoint_path: str, logging_level: str) -> None:
    """
    Standalone command to run the nuitka wrapper in this package.
    This is mainly intended for manual self-testing, if you only need nuitka
    features you should probably just call nuitka directly.
    """
    from smelt.nuitkaify import compile_with_nuitka

    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    compile_with_nuitka(entrypoint_path, stdout="stdout")


@smelt.command()
@click.argument(
    "module-import-path",
    type=str,
)
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=str,
    help="Path to the package root. "
    "If your package uses src layout or similar, "
    "you should give the path to the source code root folder (i.e., src)",
)
@click.option(
    "-b",
    "--backend",
    default="nuitka",
    type=click.Choice(["mypyc", "nuitka"]),
    help="How to compile the module",
)
@click.option(
    "-cp",
    "--crosscompile",
    type=click.Choice([platform.value for platform in SupportedPlatforms]),
    default=None,
)
@click.option(
    "-s",
    "--shadow",
    type=bool,
    help=(
        "If enabled, places the compiled .so next to the source module; "
        "The interpreter will then import it over the original module"
    ),
    is_flag=True,
    default=None,
)
@wrap_smelt_errors()
def compile_module(
    module_import_path: str,
    package_path: str,
    backend: str,
    crosscompile: str | None,
    shadow: bool,
) -> None:
    """
    Standalone command to run the nuitka wrapper in this package.
    This is mainly intended for manual self-testing, if you only need nuitka
    features you should probably just call nuitka directly.
    """
    module_full_path = os.path.join(
        package_path, module_import_path.replace(".", "/") + ".py"
    )
    click.echo(f"Compiling module {module_full_path}")
    if backend == "nuitka":
        so_path = _compile_module_with_nuitka(module_full_path, crosscompile, shadow)
        click.echo(f"Compiled nuitka extension path: {so_path}")

    elif backend == "mypyc":
        target_platform = SupportedPlatforms(crosscompile) if crosscompile else None
        target_triple_name = (
            None if target_platform is None else target_platform.get_triple_name()
        )
        mypyc_ext = mypycify_module(
            module_import_path,
            module_full_path,
        )
        # for ext in mypycify([module_import_path], include_runtime_files=True):
        if (runtime := mypyc_ext.runtime) is not None:
            runtime_so_path = compile_extension(
                runtime, use_zig_native_interface=True, crosscompile=target_platform
            )
            dest_path = mypyc_ext.get_runtime_dest_path(target_triple_name)
            shutil.move(runtime_so_path, dest_path)
            click.echo(f"Compiled mypyc runtime path: {runtime_so_path}")

        module_so_path = compile_extension(
            mypyc_ext.extension,
            use_zig_native_interface=True,
            crosscompile=target_platform,
        )
        dest_path = mypyc_ext.get_dest_path(target_triple_name)
        shutil.move(module_so_path, dest_path)
        click.echo(f"Compiled mypyc module path: {dest_path}")
    else:
        assert False, f"Unknown backend: {backend}"
