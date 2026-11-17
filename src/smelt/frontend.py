"""
Command-line interface for Smelt

@date: 12.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
import sysconfig
import tomllib
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
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
)
from smelt.compiler import SupportedPlatforms, compile_extension
from smelt.config import (
    CythonExtension,
    MypycModule,
    NuitkaModule,
    PyprojectConfig,
    SmeltConfig,
    TomlData,
    load_pyproject_config,
    parse_config_from_pyproject,
    toml_get_nested_section,
)
from smelt.context import enable_global_context, get_context
from smelt.bundle_flags import add_bundle_options, settings_from_params
from smelt.schema import toml_key
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

LOG_LEVELS = click.Choice(list(logging._nameToLevel), case_sensitive=False)


@dataclass
class CliState:
    """
    Options shared by every command, set on the `smelt` group and passed down through
    the click context.
    """

    project: Path
    log_level: str
    report: str | None


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
@click.option(
    "-p",
    "--project",
    default=".",
    type=CliExistingPath(),
    help="Project to act on: a folder holding a pyproject.toml. Default: the current folder.",
)
@click.option(
    "-l",
    "--log-level",
    type=LOG_LEVELS,
    default=None,
    help="Logging level to apply. Logs are emitted to stdout. Default: 'warning', or "
    "'debug' when SMELT_DEBUG is set.",
)
@click.option(
    "--report",
    type=str,
    default=None,
    help="Write a report to the given path. Default: the SMELT_REPORT environment variable.",
)
@click.pass_context
def smelt(ctx: Context, project: PathExists, log_level: str | None, report: str | None) -> None:
    """
    Entrypoint for Smelt frontend
    """
    # Command-line flags win over the environment variables.
    if log_level is None:
        log_level = "debug" if os.environ.get("SMELT_DEBUG") else "warning"
    if report is None:
        report = os.environ.get("SMELT_REPORT") or None
    ctx.obj = CliState(project=project, log_level=log_level, report=report)
    enable_global_context()
    logging.basicConfig(level=logging._nameToLevel[log_level.upper()])
    click.echo(SMELT_ASCCI_ART)


def _load_pyproject(state: CliState) -> PyprojectConfig:
    """
    Reads and validates the project's pyproject.toml, exiting with a message when the
    project has no readable one.
    """
    pyproject_path = state.project / "pyproject.toml"
    if not path_exists(pyproject_path):
        error_exit("No pyproject.toml found in project folder")
    with pyproject_path.open("rb") as f:
        try:
            toml_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            error_exit(f"Invalid TOML file [{pyproject_path}]: {exc}")
    return load_pyproject_config(toml_data, project_root=state.project)


def _load_config(state: CliState) -> SmeltConfig:
    """The project's resolved internal config (see `_load_pyproject`)."""
    return _load_pyproject(state).resolve(project_root=state.project)


def _write_global_report(state: CliState) -> None:
    if state.report is None:
        return
    global_context = get_context()
    assert global_context is not None, "enable_global_context() runs in the group callback"
    Path(state.report).write_text(global_context.render())


def _flatten(settings: object, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yields `(dotted kebab-case key, value)` for every leaf of a settings dataclass."""
    assert is_dataclass(settings)
    for f in fields(settings):
        value = getattr(settings, f.name)
        key = prefix + toml_key(f.name)
        if is_dataclass(value):
            yield from _flatten(value, f"{key}.")
        else:
            yield key, value


_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _render_value(value: object, root: Path) -> str:
    """
    TOML-like rendering of a config value, as the user would write it: kebab-case
    keys, paths relative to the project root, unset or empty fields of a table left out.
    """
    if value is None:
        return "(not set)"
    if is_dataclass(value) and not isinstance(value, type):
        entries = {
            toml_key(f.name): getattr(value, f.name)
            for f in fields(value)
            if getattr(value, f.name) not in (None, [], {})
        }
        return _render_value(entries, root)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Path):
        try:
            value = value.resolve().relative_to(root.resolve())
        except ValueError:
            pass
        return json.dumps(str(value))
    if isinstance(value, str):
        return json.dumps(str(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item, root) for item in value) + "]"
    if isinstance(value, dict):
        items = (
            f"{key if _BARE_TOML_KEY.fullmatch(str(key)) else json.dumps(str(key))} = "
            f"{_render_value(item, root)}"
            for key, item in value.items()
        )
        return "{" + ", ".join(items) + "}"
    return repr(value)


@smelt.command("config")
@click.pass_obj
@wrap_smelt_errors()
def config_command(state: CliState) -> None:
    """
    Shows the resolved config: the project-wide settings, then the bundler settings
    of every entrypoint, each with the level it came from (default, pyproject table
    or CLI).
    """
    pyproject = _load_pyproject(state)
    config = pyproject.resolve(project_root=state.project)
    click.echo("[tool.smelt]")
    for key, value in _flatten(pyproject.schema):
        # bundler settings are shown resolved, per entrypoint, below
        if key.startswith("bundle."):
            continue
        click.echo(f"  {key} = {_render_value(value, state.project)}")
    for spec, levels in config.entrypoints.items():
        resolved = resolve_bundle(levels)
        click.echo(f"\nBundler settings of {spec}:")
        for key, value in _flatten(resolved.settings):
            sources = resolved.sources.get(key)
            if key == "supported-targets":
                for level in resolved.supported_targets:
                    rendered = _render_value(list(level.patterns), state.project)
                    click.echo(f"  {key} = {rendered}  [{level.source}]")
            elif sources:
                rendered = _render_value(value, state.project)
                click.echo(f"  {key} = {rendered}  [{', '.join(sources)}]")


@smelt.command("build")
@click.pass_obj
@wrap_smelt_errors()
def build(state: CliState) -> None:
    """
    Builds the extensions declared in the project's Smelt config, in place.
    """
    config = _load_config(state)
    path_solver = config.get_path_solver(project_root=state.project)
    try:
        run_backend(
            config,
            stdout="stdout",
            path_solver=path_solver,
            without_entrypoint=True,
        )
    finally:
        _write_global_report(state)


@smelt.command("nuitka-binary")
@click.option(
    "-e",
    "--entrypoint",
    type=str,
    default=None,
    help="Restrict the build to this entrypoint: either its script name as declared in "
    "[project.scripts] (e.g. 'afpu'), or its 'module.path'/'module.path:func_name' key "
    "as declared in [tool.smelt.nuitka-binary.entrypoints]. Builds all configured "
    "entrypoints if omitted.",
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
@click.pass_obj
@wrap_smelt_errors()
def nuitka_binary(
    state: CliState,
    entrypoint: str | None,
    package_data: tuple[str, ...],
    embed_files: tuple[tuple[PathExists, ImportPath], ...],
    no_cache: bool,
) -> None:
    """
    Builds a Nuitka whole-program executable per entrypoint, configured by
    [tool.smelt.nuitka-binary].
    """
    if embed_files:
        click.echo(
            "Warning: --embed-file is deprecated and will be removed in 0.9; "
            "declare the file's package under package-data in [tool.smelt.nuitka-binary].",
            err=True,
        )
    config = _load_config(state)
    if package_data:
        cli_package_data = package_data_from_cli(package_data)
        for options in config.nuitka_binary.values():
            for package, patterns in cli_package_data.items():
                options.package_data.setdefault(package, []).extend(patterns)
    path_solver = config.get_path_solver(project_root=state.project)
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
        _write_global_report(state)


@smelt.command("bundle")
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
@click.pass_obj
@wrap_smelt_errors()
def bundle(
    state: CliState,
    *,
    entrypoint: str | None,
    output_dir: Path,
    no_build: bool,
    targets: tuple[tuple[str, ...], ...],
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
    config = _load_config(state)
    path_solver = config.get_path_solver(project_root=state.project)

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
        if state.report is not None:
            report_path = Path(state.report)
            if nested:
                report_path = report_path.with_stem(f"{report_path.stem}-{target_label}")
            report_path.write_text(dist_report.render())


@smelt.command("compile")
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
@click.pass_obj
@wrap_smelt_errors()
def compile_command(
    state: CliState,
    module_import_path: ImportPath,
    backend: Literal["mypyc", "nuitka", "cython"],
    target_platforms: tuple[tuple[str, ...], ...],
) -> None:
    """
    One-off compile of a single installed module with the chosen backend, optionally
    for other platforms. Mainly for trying a backend on a module outside a project
    build; project builds go through `smelt build`.
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
    _write_global_report(state)


@smelt.command("clean")
@click.option(
    "--shadowed-only",
    is_flag=True,
    default=False,
    help="Only clean modules with a pure-Python fallback to unshadow, "
    "excluding handwritten C/Zig extensions",
)
@click.pass_obj
@wrap_smelt_errors()
def clean(state: CliState, *, shadowed_only: bool) -> None:
    """
    Deletes built dynlibs (and their mypyc runtime, where applicable) for the
    project, unshadowing their `.py` counterpart back to being the one Python
    imports.
    """
    config = _load_config(state)
    path_solver = config.get_path_solver(project_root=state.project)
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


# --- Deprecated command names ---------------------------------------------------------
# Every released command name keeps working as a hidden alias of its replacement, taking
# the options the old command had (project path and logging/report flags included, now
# group-level). Removed in 0.9.


def _deprecated_alias(
    old_name: str,
    new_command: click.Command,
    *,
    project_flags: tuple[str, ...] = (),
    log_flags: tuple[str, ...] = (),
    report_flags: tuple[str, ...] = (),
) -> click.Command:
    legacy: list[click.Parameter] = []
    if project_flags:
        legacy.append(
            click.Option(
                [*project_flags, "legacy_project"], type=CliExistingPath(), default=None, hidden=True
            )
        )
    if log_flags:
        legacy.append(
            click.Option([*log_flags, "legacy_log_level"], type=LOG_LEVELS, default=None, hidden=True)
        )
    if report_flags:
        legacy.append(click.Option([*report_flags, "legacy_report"], default=None, hidden=True))
    legacy_names = {param.name for param in legacy}
    forwarded = {param.name for param in new_command.params}

    def callback(**kwargs: object) -> None:
        ctx = click.get_current_context()
        state = ctx.obj
        assert isinstance(state, CliState)
        click.echo(
            f"Warning: `smelt {old_name}` is deprecated and will be removed in 0.9; "
            f"use `smelt {new_command.name}`.",
            err=True,
        )
        if (project := kwargs.get("legacy_project")) is not None:
            assert isinstance(project, Path)
            state.project = project
        if (level := kwargs.get("legacy_log_level")) is not None:
            assert isinstance(level, str)
            state.log_level = level
            logging.getLogger().setLevel(logging._nameToLevel[level.upper()])
        if (report := kwargs.get("legacy_report")) is not None:
            assert isinstance(report, str)
            state.report = report
        ctx.invoke(
            new_command,
            **{k: v for k, v in kwargs.items() if k in forwarded and k not in legacy_names},
        )

    alias = click.Command(
        old_name,
        params=[*new_command.params, *legacy],
        callback=callback,
        hidden=True,
        help=new_command.help,
    )
    smelt.add_command(alias)
    return alias


_deprecated_alias("show-config", config_command, project_flags=("-p", "--path"))
_deprecated_alias("build-extensions", build, project_flags=("-p", "--package"))
_deprecated_alias(
    "build-standalone-binary",
    nuitka_binary,
    project_flags=("-p", "--package-path"),
    log_flags=("-l", "--logging-level"),
    report_flags=("-r", "--report"),
)
_deprecated_alias("compile-module", compile_command)
_deprecated_alias("clean-artifacts", clean, project_flags=("-p", "--package"))


@smelt.command("nuitkaify", hidden=True)
@click.argument("entrypoint-path", type=ImportPath)
def nuitkaify(entrypoint_path: ImportPath) -> None:
    """
    Deprecated: run `python -m smelt.nuitkaify ENTRYPOINT_PATH` instead.
    """
    from smelt.nuitkaify import compile_with_nuitka

    click.echo(
        "Warning: `smelt nuitkaify` is deprecated and will be removed in 0.9; "
        "use `python -m smelt.nuitkaify`.",
        err=True,
    )
    compile_with_nuitka(entrypoint_path, stdout="stdout")
