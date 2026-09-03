"""
Command-line interface for Smelt

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import shutil
import sys
import sysconfig
import tomllib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, NoReturn, ParamSpec, TypeVar

import click
from click import Context, Parameter, ParamType

from smelt.backend import (
    clean_all_artifacts,
    compile_cython_extensions,
    compile_mypyc_extensions,
    nuitkaify_module,
    run_backend,
    write_auto_mode_report,
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
from smelt.dist import build_dist, run_instructions
from smelt.utils import (
    ImportPath,
    PathExists,
    PathSolver,
    SmeltError,
    is_valid_import_path,
    path_exists,
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

    def convert(self, value: str, param: Parameter | None, ctx: Context | None) -> ImportPath:
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

    def convert(self, value: str, param: Parameter | None, ctx: Context | None) -> PathExists:
        _ = param
        _ = ctx
        path = Path(value)
        if not path_exists(path):
            self.fail(f"{value} not found")
        return path


class CliEmbedFile(ParamType):
    """
    Parses `--embed-file` values of the form DATA_FILE_PATH=IMPORT_PATH.
    """

    name = "embed_file"

    def convert(
        self, value: str, param: Parameter | None, ctx: Context | None
    ) -> tuple[PathExists, ImportPath]:
        data_file_path, sep, import_path = value.partition("=")
        if not sep:
            self.fail(f"{value!r} is not of the form DATA_FILE_PATH=IMPORT_PATH", param, ctx)
        if not is_valid_import_path(import_path):
            self.fail(f"{import_path!r} is not a valid Python import path", param, ctx)
        path = Path(data_file_path)
        if not path_exists(path):
            self.fail(f"{data_file_path!r} not found", param, ctx)
        return path, import_path


def wrap_smelt_errors(
    should_exist: bool = True,
    exit_code: int = 1,
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
            "Does the TOML data come from a valid pyproject ?",
        )
    smelt_config = toml_get_nested_section(toml_data, *config_path)
    if smelt_config is None:
        raise SmeltConfigError("No smelt config defined in pyproject")

    if not isinstance(smelt_config, dict):
        raise SmeltConfigError(f"`smelt` section should be a dictionary, got {smelt_config}. ")

    project_scripts_decl = toml_get_nested_section(toml_data, "project", "scripts")
    if not isinstance(project_scripts_decl, dict):
        raise SmeltConfigError(
            f"`project.scripts` section should be a dictionary, got {project_scripts_decl}. ",
        )
    project_scripts: dict[str, str] = {}
    for name, target in project_scripts_decl.items():
        if not isinstance(target, str):
            raise SmeltConfigError(f"`project.scripts.{name}` should be a string, got {target}. ")
        project_scripts[name] = target

    return SmeltConfig.from_toml_data(
        smelt_config,
        project_root=project_root,
        project_scripts=project_scripts,
    )


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
    type=CliExistingPath(),
)
@wrap_smelt_errors()
def show_config(*, path: PathExists) -> None:
    """
    Shows the smelt config as defined in the passed file
    """
    from pprint import pprint

    try:
        with (path / "pyproject.toml").open("rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    pprint(parse_config_from_pyproject(toml_data, project_root=path))


@smelt.command()
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=CliExistingPath(),
)
@add_logging_option
@click.option("-r", "--report", type=str, default=None, help="Produces a report at the given path")
@click.option(
    "-e",
    "--entrypoint",
    type=str,
    default=None,
    help="Restrict the build to this entrypoint: either its script name as declared in "
    "[project.scripts] (e.g. 'afpu'), or its 'module.path'/'module.path:func_name' key "
    "as declared in [tool.smelt.entrypoints]. Builds all configured entrypoints if omitted.",
)
@click.option(
    "--embed-file",
    "embed_files",
    type=CliEmbedFile(),
    multiple=True,
    help="Embed a data file into the built binary. Syntax: DATA_FILE_PATH=IMPORT_PATH. "
    "Adds a --include-package-data flag to Nuitka for the given file. Repeatable.",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Disable Nuitka's build cache, forcing a full rebuild.",
)
@wrap_smelt_errors()
def build_standalone_binary(
    package_path: PathExists,
    logging_level: str,
    report: str | None,
    entrypoint: str | None,
    embed_files: tuple[tuple[PathExists, ImportPath], ...],
    no_cache: bool,
) -> None:
    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    try:
        with (package_path / "pyproject.toml").open("rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    config = parse_config_from_pyproject(toml_data, project_root=package_path)
    config.load_env()
    path_solver = config.get_path_solver(project_root=package_path)
    try:
        run_backend(
            config,
            stdout="stdout",
            path_solver=path_solver,
            entrypoint=entrypoint,
            embed_files=embed_files,
            no_cache=no_cache,
        )
    finally:
        if config.report_path is not None:
            write_auto_mode_report(config.report_path)
    if report is not None:
        global_context = get_context()
        assert global_context is not None
        Path(report).write_text(global_context.render())


@smelt.command("build-dist")
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=CliExistingPath(),
    help="Path to the package to build a distribution for, expects to find a pyproject.toml",
)
@click.option(
    "-e",
    "--entrypoint",
    type=str,
    default=None,
    help="Entrypoint to build the distribution for: either its script name as declared "
    "in [project.scripts] (e.g. 'afpu'), or its 'module.path'/'module.path:func_name' "
    "key as declared in [tool.smelt.entrypoints]. Can be omitted when the project "
    "declares a single entrypoint.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("dist"),
    help="Folder the distribution is assembled in. Defaults to ./dist",
)
@click.option(
    "--optimize",
    type=click.IntRange(0, 2),
    default=None,
    help="Bytecode optimization level (0, 1 or 2). Defaults to the level this "
    "interpreter runs with. Note that 2 strips docstrings, which breaks help(), pydoc, "
    "doctests, and any library building its own help text from docstrings.",
)
@click.option(
    "--no-build",
    is_flag=True,
    default=False,
    help="Skip the extension build and reuse whatever artifacts are already on disk.",
)
@add_logging_option
@click.option("-r", "--report", type=str, default=None, help="Produces a report at the given path")
@wrap_smelt_errors()
def build_dist_folder(
    package_path: PathExists,
    entrypoint: str | None,
    output_dir: Path,
    optimize: int | None,
    no_build: bool,
    logging_level: str,
    report: str | None,
) -> None:
    """
    Assembles a distribution folder for an entrypoint: smelt-built extensions plus
    every other module it imports, shipped as bytecode.

    The result runs on any machine with a matching CPython already installed -- the
    interpreter itself is not bundled. Instructions are printed at the end of the run
    and written into the folder.
    """
    levelno = logging._nameToLevel[logging_level]
    logging.basicConfig(level=levelno)
    try:
        with (package_path / "pyproject.toml").open("rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        error_exit("No pyproject.toml found in passed folder")
    config = parse_config_from_pyproject(toml_data, project_root=package_path)
    config.load_env()
    path_solver = config.get_path_solver(project_root=package_path)
    dist_report = build_dist(
        config,
        entrypoint=entrypoint,
        output_dir=output_dir,
        path_solver=path_solver,
        optimize=-1 if optimize is None else optimize,
        stdout="stdout",
        build_extensions=not no_build,
    )
    click.echo(dist_report.render())
    click.echo("")
    click.echo(run_instructions(dist_report))
    if report is not None:
        Path(report).write_text(dist_report.render())


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
        module_source = path_solver.resolve_import_path(module_import_path, should_exist=True)
    except SmeltConfigError as exc:
        error_exit(str(exc))

    if backend == "nuitka":
        config = NuitkaModule(module_import_path, module_source)
        generic_ext = nuitkaify_module(config, path_solver, stdout="stdout")

    elif backend == "mypyc":
        target_platform = SupportedPlatforms(crosscompile) if crosscompile else None
        target_triple_name = None if target_platform is None else target_platform.get_triple_name()
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
        config.load_env()
        path_solver = config.get_path_solver(project_root=package)
        try:
            run_backend(
                config,
                stdout="stdout",
                path_solver=path_solver,
                without_entrypoint=True,
            )
        finally:
            if config.report_path is not None:
                write_auto_mode_report(config.report_path)


@smelt.command()
@click.option(
    "-p",
    "--package",
    type=CliExistingPath(),
    help="Path the the package to clean built artifacts for, expects to find a pyproject.toml",
    default=Path.cwd(),
)
@click.option(
    "--shadowed-only",
    is_flag=True,
    default=False,
    help="Only clean modules with a pure-Python fallback to unshadow, "
    "excluding handwritten C/Zig extensions",
)
@wrap_smelt_errors()
def clean_artifacts(*, package: PathExists, shadowed_only: bool) -> None:
    """
    Deletes built dynlibs (and their mypyc runtime, where applicable) for the
    passed project, unshadowing their `.py` counterpart back to being the one
    Python imports.
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
        deleted = clean_all_artifacts(config, path_solver, shadowed_only=shadowed_only)

    if not deleted:
        click.echo("No built artifacts found.")
        return
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    click.echo("Deleted following artifacts:")
    for import_path, artifacts in deleted.items():
        click.echo(f"{import_path}:")
        for artifact in artifacts:
            if artifact.name.endswith(f"__mypyc{suffix}"):
                click.echo(f"  - {artifact}")
                continue
            pure_python_path = artifact.with_name(f"{artifact.name.removesuffix(suffix)}.py")
            click.echo(f"  - {artifact} (was shadowing {pure_python_path})")
