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
from contextlib import contextmanager, chdir
from pathlib import Path
from typing import Callable, Generator, Literal, NoReturn, ParamSpec, TypeVar

import click
from click import Context, Parameter, ParamType

from smelt.backend import (
    compile_cython_extensions,
    compile_mypyc_extensions,
    nuitkaify_module,
    run_backend,
)
from smelt.compiler import SupportedPlatforms, compile_extension
from smelt.config import (
    CythonExtension,
    MypycModule,
    NuitkaModule,
    SmeltConfig,
    TomlData,
    auto_detect_is_build_hook,
    toml_get_nested_section,
)
from smelt.context import enable_global_context, get_context
from smelt.utils import (
    ImportPath,
    PathExists,
    PathSolver,
    SmeltError,
    is_valid_import_path,
    path_exists,
    toggle_mod_path,
)


class SmeltConfigError(SmeltError): ...


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


class CliImportPath(ParamType):
    """
    A tiny wrapper for click to verify import paths validity automatically.
    """

    name = "import_path"

    def convert(
        self, value: str, param: Parameter | None, ctx: Context | None
    ) -> ImportPath:
        _ = param
        _ = ctx
        if not is_valid_import_path(value):
            self.fail(f"{value} is not a valid Python import path")
        return value


class CliExistingPath(ParamType):
    """
    A tiny wrapper for click to verify import paths validity automatically.
    """

    name = "existing_path"

    def convert(
        self, value: str, param: Parameter | None, ctx: Context | None
    ) -> PathExists:
        _ = param
        _ = ctx
        path = Path(value)
        if not path_exists(path):
            self.fail(f"{value} not found")
        return path


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


def parse_config_from_pyproject(
    toml_data: TomlData,
    is_configured_as_build_hook: bool | None = None,
    project_root: Path | None = None,
) -> SmeltConfig:
    """
    Extracts Smelt config from TOML data coming out of a pyproject.toml
    """
    is_configured_as_build_hook = (
        is_configured_as_build_hook
        if is_configured_as_build_hook is not None
        else auto_detect_is_build_hook(toml_data)
    )
    tool_config = toml_data.get("tool", {})
    config_path = (
        ("tool", "hatch", "build", "hooks", "smelt")
        if is_configured_as_build_hook
        else ("tool", "smelt")
    )
    if not isinstance(tool_config, dict):
        raise SmeltConfigError(
            f"`tool` section in toml data is not a dictionary, got {tool_config}. "
            "Does the TOML data come from a valid pyproject ?"
        )
    smelt_config = toml_get_nested_section(toml_data, *config_path)
    if smelt_config is None:
        raise SmeltConfigError("No smelt config defined in pyproject")

    if not isinstance(smelt_config, dict):
        raise SmeltConfigError(
            f"`smelt` section should be a dictionary, got {smelt_config}. "
        )
    return SmeltConfig.from_toml_data(smelt_config, project_root=project_root)


def error_exit(msg: str, code: int = 1) -> NoReturn:
    """
    A helper exiting the program with the given `msg` on error.
    """
    click.echo(msg)
    sys.exit(code)


@click.group()
def smelt() -> None:
    """
    Entrypoint for Smelt frontend
    """
    enable_global_context()
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
@click.option(
    "-r", "--report", type=str, default=None, help="Produces a report at the given path"
)
@wrap_smelt_errors()
def build_standalone_binary(
    package_path: str, logging_level: str, report: str | None
) -> None:
    with chdir(package_path):
        path_solver = PathSolver()
        levelno = logging._nameToLevel[logging_level]
        logging.basicConfig(level=levelno)
        try:
            with open("pyproject.toml", "rb") as f:
                toml_data = tomllib.load(f)
        except FileNotFoundError:
            click.echo("No pyproject.toml not found.")
            return
        config = parse_config_from_pyproject(toml_data)
        config.load_env()
        try:
            run_backend(config, stdout="stdout", path_solver=path_solver)
        except Exception as e:
            click.echo(f"Error during build: {e}")
        if report is not None:
            global_context = get_context()
            assert global_context is not None
            Path(report).write_text(global_context.render())


@smelt.command()
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=CliImportPath(),
)
@add_logging_option
@wrap_smelt_errors()
def compile_all_mypyc_extensions(package_path: ImportPath, logging_level: str) -> None:
    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    try:
        with open(os.path.join(package_path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    config = parse_config_from_pyproject(toml_data)
    compile_mypyc_extensions(package_path, mypyc_config=config.mypyc_modules)


@smelt.command()
@click.argument(
    "entrypoint-path",
    type=ImportPath,
)
@add_logging_option
@wrap_smelt_errors()
def nuitkaify(entrypoint_path: ImportPath, logging_level: str) -> None:
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
    type=CliImportPath(),
)
@click.option(
    "-b",
    "--backend",
    default="nuitka",
    type=click.Choice(["mypyc", "nuitka", "cython"]),
    help="How to compile the module",
)
@click.option(
    "-cp",
    "--crosscompile",
    type=click.Choice([platform.value for platform in SupportedPlatforms]),
    default=None,
)
@wrap_smelt_errors()
def compile_module(
    module_import_path: ImportPath,
    backend: Literal["mypyc", "nuitka", "cython"],
    crosscompile: str | None,
) -> None:
    """
    Standalone command to run the nuitka wrapper in this package.
    This is mainly intended for manual self-testing, if you only need nuitka
    features you should probably just call nuitka directly.
    """
    path_solver = PathSolver.from_installed_import_paths(module_import_path)
    click.echo(f"Compiling module {module_import_path}")
    try:
        module_source = path_solver.resolve_import_path(module_import_path)
    except SmeltConfigError as exc:
        error_exit(str(exc))

    if backend == "nuitka":
        config = NuitkaModule(module_import_path, module_source)
        generic_ext = nuitkaify_module(config, path_solver, stdout="stdout")

    elif backend == "mypyc":
        target_platform = SupportedPlatforms(crosscompile) if crosscompile else None
        target_triple_name = (
            None if target_platform is None else target_platform.get_triple_name()
        )
        modules = [MypycModule(module_import_path)]
        (generic_ext,) = compile_mypyc_extensions(modules, path_solver)

    elif backend == "cython":
        modules = [CythonExtension(module_import_path)]
        (generic_ext,) = compile_cython_extensions(modules, path_solver=path_solver)
    compiled_so = compile_extension(generic_ext.extension)
    dest_path = generic_ext.dest_folder / compiled_so
    shutil.move(compiled_so, dest_path)
    if runtime := generic_ext.runtime:
        runtime_compiled_so = compile_extension(runtime)
        shutil.move(runtime_compiled_so, generic_ext.dest_folder / runtime_compiled_so)
    click.echo(f"Compiled so path: {dest_path}")


@smelt.command
@click.option(
    "-p",
    "--package",
    type=CliExistingPath(),
    help="Path the the package to build extensions for, expects to find a pyproject.toml",
    default=Path.cwd(),
)
def build_extensions(*, package: PathExists) -> None:
    """
    Runs the smelt backend on the passed project and builds all extensions
    defined by smelt.
    """
    pyproject_path = package / "pyproject.toml"
    if not path_exists(pyproject_path):
        error_exit("No pyproject.toml found in passed folder")
    with pyproject_path.open("rb") as f:
        try:
            toml_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            error_exit(f"Invalid TOML file [{pyproject_path}]: {exc}")
        config = parse_config_from_pyproject(toml_data, project_root=package)
        path_solver = config.get_path_solver(project_root=package)
        run_backend(
            config, stdout="stdout", path_solver=path_solver, without_entrypoint=True
        )
