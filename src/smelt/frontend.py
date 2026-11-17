"""
Command-line interface for Smelt

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import platform
import shutil
import sys
import sysconfig
import tomllib
from collections.abc import Callable, Generator, Iterable
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
    parse_config_from_pyproject,
    toml_get_nested_section,
)
from smelt.context import enable_global_context, get_context
from smelt.bundle_flags import add_bundle_options, settings_from_params
from smelt.dist import (
    assert_supported_platforms,
    build_dist,
    package_data_from_cli,
    resolve_bundle,
    resolve_entrypoint_spec,
    resolve_target_platforms,
    run_instructions,
    validate_bundle,
)
from smelt.own_python import target_python_headers_for
from smelt.targets import HOST, as_build_targets
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


class CliCommaSeparated(ParamType):
    """
    Parses a comma-separated list value (e.g. `-t arm-linux-gnueabihf,aarch64-linux-gnu`)
    into a tuple of its items, empty items (a bare `-t ""`, or unset) dropped.
    """

    name = "comma_separated"

    def convert(self, value: str, param: Parameter | None, ctx: Context | None) -> tuple[str, ...]:
        _ = param
        _ = ctx
        return tuple(item.strip() for item in value.split(",") if item.strip())


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


def _host_label() -> str:
    """
    Cosmetic label for the "host" target platform, used only to name an output
    folder -- never fed to a compiler flag, so it does not need to resolve the
    host's actual Zig triple (arch/os/libc), unlike a real cross-compile target.
    """
    return f"{platform.system().lower()}-{platform.machine().lower()}"


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
    "--package-data",
    "package_data",
    type=str,
    multiple=True,
    help="Collect a package's data files into the binary, on top of "
    "[tool.smelt.nuitka-binary] package-data. Syntax: PACKAGE[:PATTERN,...] "
    "(e.g. 'mypkg:*.json,*.yaml'). Repeatable.",
)
@click.option(
    "--embed-file",
    "embed_files",
    type=CliEmbedFile(),
    multiple=True,
    hidden=True,
    help="Deprecated: use --package-data. Syntax: DATA_FILE_PATH=IMPORT_PATH.",
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
    package_data: tuple[str, ...],
    embed_files: tuple[tuple[PathExists, ImportPath], ...],
    no_cache: bool,
) -> None:
    if embed_files:
        click.echo(
            "Warning: --embed-file is deprecated and will be removed in 0.9; "
            "declare the file's package under package-data in [tool.smelt.nuitka-binary].",
            err=True,
        )
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
    if package_data:
        cli_package_data = package_data_from_cli(package_data)
        for options in config.nuitka_binary.values():
            for package, patterns in cli_package_data.items():
                options.package_data.setdefault(package, []).extend(patterns)
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


@smelt.command("bundle")
@click.option(
    "-p",
    "--project",
    default=".",
    type=CliExistingPath(),
    help="Project to bundle: a folder holding a pyproject.toml. Default: the current folder.",
)
@click.option(
    "-e",
    "--entrypoint",
    type=str,
    default=None,
    help="Entrypoint to bundle: its script name from [project.scripts] (e.g. 'afpu'), "
    "or a 'module.path' / 'module.path:func' key declared in "
    "[tool.smelt.bundle.entrypoints]. Can be omitted when the project has a single "
    "entrypoint.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("dist"),
    help="Folder the bundle is assembled in. Default: ./dist",
)
@click.option(
    "--no-build",
    is_flag=True,
    default=False,
    help="Skip the extension build and reuse the artifacts already on disk.",
)
@click.option(
    "-t",
    "--target",
    "targets",
    type=CliCommaSeparated(),
    multiple=True,
    help="Platform(s) to bundle for: a comma-separated list of Zig target triples "
    "(e.g. 'x86_64-linux-musl') and/or 'host' for a native build. Repeatable. One "
    "bundle is built per target, nested under <output-dir>/<target>/ when there is "
    "more than one. Default: the concrete triples of the most specific "
    "`supported-targets` declared, else host.",
)
@add_bundle_options
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(list(logging._nameToLevel), case_sensitive=False),
    help="Logging level to apply. Logs are emitted to stdout",
    default="warning",
)
@click.option("--report", type=str, default=None, help="Write a report to the given path.")
@wrap_smelt_errors()
def bundle(
    *,
    project: PathExists,
    entrypoint: str | None,
    output_dir: Path,
    no_build: bool,
    targets: tuple[tuple[str, ...], ...],
    log_level: str,
    report: str | None,
    **flags: object,
) -> None:
    """
    Assembles a bundle for an entrypoint: smelt-built extensions plus every other
    module it imports, shipped as bytecode.

    With `--python byo` (the default) the result runs on any machine with a matching
    CPython installed. With `--python own` the interpreter is built and shipped in the
    folder too, which then runs on a machine with no Python at all. Every flag except
    the ones above has a [tool.smelt.bundle] key of the same name and overrides it.
    Run instructions are printed at the end and written into the folder.
    """
    logging.basicConfig(level=logging._nameToLevel[log_level])
    try:
        with (project / "pyproject.toml").open("rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        error_exit("No pyproject.toml found in passed folder")
    config = parse_config_from_pyproject(toml_data, project_root=project)
    config.load_env()
    path_solver = config.get_path_solver(project_root=project)

    cli_settings = settings_from_params(flags)
    entrypoint_spec = resolve_entrypoint_spec(config, entrypoint)
    resolved = resolve_bundle(config.entrypoints[entrypoint_spec], cli_settings)
    validate_bundle(resolved)
    requested_targets = tuple(target for group in targets for target in group)
    resolved_targets = resolve_target_platforms(requested_targets, resolved.supported_targets)
    # Checked once for the whole list, before any build starts -- a later target
    # failing the allowlist must not leave earlier ones half-built.
    assert_supported_platforms(resolved.supported_targets, resolved_targets)
    nested = len(resolved_targets) > 1

    for target in resolved_targets:
        target_label = target or _host_label()
        dist_report = build_dist(
            config,
            entrypoint=entrypoint,
            output_dir=output_dir / target_label if nested else output_dir,
            path_solver=path_solver,
            stdout="stdout",
            build_extensions=not no_build,
            target_platform=target,
            overrides=cli_settings,
        )
        click.echo(dist_report.render())
        click.echo("")
        click.echo(run_instructions(dist_report))
        if report is not None:
            report_path = Path(report)
            if nested:
                report_path = report_path.with_stem(f"{report_path.stem}-{target_label}")
            report_path.write_text(dist_report.render())


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
    "-t",
    "--target",
    "target_platforms",
    type=CliCommaSeparated(),
    multiple=True,
    help="Platform(s) to cross-compile the module for: Zig target triples "
    "(e.g. 'aarch64-linux-gnu') and/or 'host', comma-separated. Repeatable. One "
    "compiled extension is built per platform, nested under a <triple>/ folder "
    "whenever this flag is passed explicitly. Defaults to a single native build.",
)
@wrap_smelt_errors()
def compile_module(
    module_import_path: ImportPath,
    backend: Literal["mypyc", "nuitka", "cython"],
    target_platforms: tuple[tuple[str, ...], ...],
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
        modules = [MypycModule(module_import_path)]
        (generic_ext,) = compile_mypyc_extensions(modules, path_solver)

    elif backend == "cython":
        modules = [CythonExtension(module_import_path)]
        (generic_ext,) = compile_cython_extensions(modules, path_solver=path_solver)

    # `generic_ext` (the transpiled/translated source) is target-independent -- built
    # once above regardless of how many platforms are requested; only the actual
    # compile step below runs once per platform.
    requested = [target for group in target_platforms for target in group]
    for build_target in as_build_targets(requested) if requested else [None]:
        target_platform = (
            SupportedPlatforms.from_triple(build_target) if build_target is not None else None
        )
        platform_value = build_target or HOST
        py_headers = (
            target_python_headers_for(target_platform.get_triple_name())
            if target_platform is not None
            else None
        )
        dest_folder = (
            generic_ext.dest_folder / platform_value
            if requested
            else generic_ext.dest_folder
        )
        dest_folder.mkdir(parents=True, exist_ok=True)
        compiled_so = compile_extension(
            generic_ext.extension, crosscompile=target_platform, py_headers=py_headers
        )
        dest_path = dest_folder / compiled_so
        shutil.move(compiled_so, dest_path)
        if runtime := generic_ext.runtime:
            runtime_compiled_so = compile_extension(
                runtime, crosscompile=target_platform, py_headers=py_headers
            )
            shutil.move(runtime_compiled_so, dest_folder / runtime_compiled_so)
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
