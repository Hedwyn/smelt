"""
Command-line flags of `smelt bundle`, generated from the bundler schema.

Problem: every bundler setting lives in `[tool.smelt.bundle]` and has to be
overridable from the command line. Writing the flags by hand lets the two drift; here
the flag's type (choice, switch, repeatable value, ...) is read off the schema field,
and only the flag spelling and help text are declared, one entry per schema key.

`add_bundle_options` decorates a click command with every flag; `settings_from_params`
turns the parsed values back into a `BundleSettings` to layer over the pyproject
declarations. Flags that make no sense to persist (`--project`, `--entrypoint`,
`--output-dir`, `--no-build`, `--target`, ...) are declared on the command itself.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Final, Literal, get_args, get_origin

import click

from smelt.dist import package_data_from_cli
from smelt.schema import BundleSettings, toml_key

#: Schema keys with no flag of their own here: `supported-targets` is overridden by the
#: command's `-t/--target`, which also selects what gets built.
NO_GENERATED_FLAG: Final[frozenset[str]] = frozenset({"supported-targets"})


@dataclass(frozen=True)
class FlagSpec:
    """Spelling and help of the flag for one schema key."""

    #: click option declaration(s), e.g. `("--tailor/--no-tailor",)`.
    decls: tuple[str, ...]
    help: str


#: Dotted kebab-case schema key -> flag. Names follow `<group>-<key>`, the group
#: prefix being dropped for `content` and `interpreter`, where it adds nothing.
FLAGS: Final[Mapping[str, FlagSpec]] = {
    "python": FlagSpec(
        ("--python",),
        "Interpreter the bundle runs on. 'byo' (bring your own) ships the application "
        "only and needs a matching CPython on the target. 'own' also builds an "
        "interpreter and ships it in the folder, so it runs where no Python is "
        "installed; the first such build takes several minutes and is then cached per "
        "target. Default: byo.",
    ),
    "optimize": FlagSpec(
        ("--optimize",),
        "Bytecode optimization level (0, 1 or 2). Defaults to the level this "
        "interpreter runs with. Level 2 strips docstrings, which breaks help(), pydoc, "
        "doctests and libraries building their help text from docstrings.",
    ),
    "guard-version": FlagSpec(
        ("--guard-version/--no-guard-version",),
        "Check the interpreter version in the generated entrypoint. Without it, a "
        "mismatched interpreter fails with `can't find '__main__' module` instead of a "
        "readable message. Default: on.",
    ),
    "isolate": FlagSpec(
        ("--isolate/--no-isolate",),
        "Add an isolation guard to the generated entrypoint. Without it, running the "
        "bundle without -I -S leaves the host's site-packages on sys.path, where it can "
        "silently supply a missing module. Default: on.",
    ),
    "content.discovery": FlagSpec(
        ("--discovery",),
        "How to find the modules to ship. 'static' reads import statements, 'trace' "
        "imports the entrypoint in a subprocess and reports what it loaded (this runs "
        "its module-level code), 'both' unions them. Default: both.",
    ),
    "content.include-modules": FlagSpec(
        ("--include-module",),
        "Force a module into the bundle, on top of what discovery finds. For modules "
        "imported dynamically. Repeatable.",
    ),
    "content.include-packages": FlagSpec(
        ("--include-package",),
        "Force a whole package (every module it contains, recursively) into the "
        "bundle. For a package that loads its own internals dynamically. Repeatable.",
    ),
    "content.exclude-modules": FlagSpec(
        ("--exclude-module",),
        "Drop a module, and everything under it, from the bundle. For trees discovery "
        "reached but that are not needed at runtime. Repeatable.",
    ),
    "content.drop-optional-imports": FlagSpec(
        ("--drop-optional-imports/--keep-optional-imports",),
        "Leave out the modules only reachable through an import whose importer handles "
        "the failure itself (`try: import x` / `except ImportError`). None of them can "
        "raise an ImportError by being absent, but what the fallback costs varies: "
        "dropping `_hashlib` saves 5 MB with no visible change, dropping `ssl` leaves "
        "urllib unable to speak HTTPS. Default: keep; the build output lists them "
        "either way.",
    ),
    "content.package-data": FlagSpec(
        ("--package-data",),
        "Collect a package's data files into the bundle. Syntax: "
        "PACKAGE[:PATTERN,...] (e.g. 'mypkg' or 'mypkg:*.json,*.yaml'). Repeatable.",
    ),
    "content.distribution-metadata": FlagSpec(
        ("--distribution-metadata",),
        "Collect a distribution's *.dist-info metadata beyond those owning a shipped "
        "module. Needed when importlib.metadata is queried for a package whose modules "
        "are not themselves shipped. Repeatable.",
    ),
    "interpreter.version": FlagSpec(
        ("--python-version",),
        "CPython release to build the 'own' interpreter from (e.g. '3.12.3'). Only the "
        "minor version of the Python running smelt can be shipped, since the bytecode "
        "is compiled by it: run smelt under the minor version you want to ship. "
        "Default: the supported release matching the running minor version.",
    ),
    "interpreter.tailor": FlagSpec(
        ("--tailor/--no-tailor",),
        "Ship only the standard library modules, extension modules and libraries the "
        "application was found to need, instead of all of them. Keeps an 'own' bundle a "
        "reasonable size, but a module reached only through a name absent from the "
        "source is invisible to discovery and becomes a runtime ImportError: put it "
        "back with --include-module. Each distinct set of disabled libraries needs its "
        "own interpreter build (minutes), cached separately. Default: tailored.",
    ),
    "interpreter.drop-stdlib-groups": FlagSpec(
        ("--drop-stdlib-group",),
        "Ship the 'own' interpreter without an optional group of the minimal standard "
        "library (currently: international_hostnames, 1.1 MB, which costs support for "
        "non-ASCII hostnames). Refused for groups nothing can run without. Repeatable.",
    ),
    "interpreter.static": FlagSpec(
        ("--static/--no-static",),
        "Build one monolithic, static-PIE 'own' interpreter (no libpythonX.Y.so) "
        "instead of the dynamic shape. A static interpreter has no working dlopen(), so "
        "every native module has to be compiled into it: --static-module for standard "
        "library modules, --inittab for smelt's own extensions. Any .so left in the "
        "bundle fails the build. Verified only for x86_64-linux-musl. Default: off.",
    ),
    "interpreter.static-modules": FlagSpec(
        ("--static-module",),
        "With --static, compile a standard library module (e.g. '_socket', 'zlib') into "
        "the interpreter as a builtin. Repeatable.",
    ),
    "interpreter.inittab": FlagSpec(
        ("--inittab/--no-inittab",),
        "With the 'own' interpreter, link smelt's own compiled extensions into it "
        "instead of shipping .so files, which skips dlopen() and RPATH for them. This "
        "replaces the interpreter's own entrypoint, and a bad build has no .so fallback. "
        "Experimental. Default: off.",
    ),
    "onefile.enabled": FlagSpec(
        ("--onefile/--no-onefile",),
        "Also pack the finished folder into a single file. With 'byo' that is an "
        "executable zip application (a /bin/sh preamble locating a CPython, followed by "
        "a zip). With 'own' it is a compiled launcher carrying the whole folder, "
        "interpreter included, as a compressed payload it unpacks into a cache "
        "directory on first run. Default: off.",
    ),
    "onefile.compression": FlagSpec(
        ("--onefile-compression",),
        "Payload compression. 'xz' (default) is smallest and slowest to inflate on the "
        "first run, 'gzip' trades size for speed, 'none' stores it as is. A 'byo' "
        "payload is imported straight out of its zip, so it is deflated for 'xz' and "
        "'gzip' alike.",
    ),
    "onefile.compression-preset": FlagSpec(
        ("--onefile-compression-preset",),
        "Compression preset: 0-9 for xz, 1-9 for gzip. Default: 6. Higher presets "
        "shrink the file but cost build time and first-run inflation time.",
    ),
    "onefile.cache": FlagSpec(
        ("--onefile-cache/--no-onefile-cache",),
        "Reuse a previous extraction found in the cache directory instead of always "
        "re-extracting. Set SMELT_ONEFILE_VERBOSE=1 on the built artifact to see the "
        "decision at runtime. Default: on.",
    ),
    "onefile.keep-folder": FlagSpec(
        ("--onefile-keep-folder/--onefile-only",),
        "Keep the distribution folder once it is packed (default). --onefile-only "
        "deletes it and leaves only the single file, for a job that publishes one "
        "artifact. Requires --onefile.",
    ),
    "isolated-build.enabled": FlagSpec(
        ("--isolated-build/--no-isolated-build",),
        "Experimental. Reinstall every third-party native dependency for the target "
        "instead of copying the build installed here. Needs the 'isolated-build' extra "
        "and a network fetch per native dependency. Default: off.",
    ),
    "isolated-build.versions": FlagSpec(
        ("--isolated-build-versions",),
        "Which version of each dependency --isolated-build fetches: 'local' (the one "
        "installed here, default) or 'pyproject' (as declared in [project.dependencies]).",
    ),
}


def _leaves(
    settings_type: type, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], object]]:
    """Every leaf field of `settings_type` as (path, resolved type hint)."""
    hints = typing.get_type_hints(settings_type)
    leaves: list[tuple[tuple[str, ...], object]] = []
    for f in fields(settings_type):
        if f.name == "entrypoints":
            continue
        hint = hints[f.name]
        if isinstance(hint, type) and is_dataclass(hint):
            leaves.extend(_leaves(hint, (*prefix, f.name)))
        else:
            leaves.append(((*prefix, f.name), hint))
    return leaves


def dotted_key(path: tuple[str, ...]) -> str:
    return ".".join(toml_key(part) for part in path)


def schema_keys() -> list[str]:
    """Dotted kebab-case key of every bundler setting."""
    return [dotted_key(path) for path, _ in _leaves(BundleSettings)]


def _option(
    path: tuple[str, ...], hint: object, spec: FlagSpec
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    name = "_".join(path)
    args = [t for t in get_args(hint) if t is not type(None)]
    inner = args[0] if args else hint
    if inner is bool:
        return click.option(*spec.decls, name, default=None, help=spec.help)
    if get_origin(inner) is Literal:
        values = get_args(inner)
        if all(isinstance(v, int) for v in values):
            return click.option(
                *spec.decls,
                name,
                type=click.IntRange(min(values), max(values)),
                default=None,
                help=spec.help,
            )
        return click.option(
            *spec.decls, name, type=click.Choice(list(values)), default=None, help=spec.help
        )
    if inner is int:
        return click.option(*spec.decls, name, type=int, default=None, help=spec.help)
    if inner is str:
        return click.option(*spec.decls, name, type=str, default=None, help=spec.help)
    # list[str] and dict[str, list[str]] (package data, given as PACKAGE[:PATTERN,...])
    return click.option(*spec.decls, name, multiple=True, help=spec.help)


def add_bundle_options(command: Callable[..., object]) -> Callable[..., object]:
    """Decorates `command` with one flag per bundler schema key."""
    for path, hint in reversed(_leaves(BundleSettings)):
        key = dotted_key(path)
        if key in NO_GENERATED_FLAG:
            continue
        command = _option(path, hint, FLAGS[key])(command)
    return command


def settings_from_params(params: Mapping[str, object]) -> BundleSettings:
    """Builds the command-line level of the bundler settings out of parsed click values."""
    settings = BundleSettings()
    for path, _ in _leaves(BundleSettings):
        if dotted_key(path) in NO_GENERATED_FLAG:
            continue
        value = params.get("_".join(path))
        if value is None or value == ():
            continue
        if path == ("content", "package_data"):
            assert isinstance(value, tuple), "multiple flag yields a tuple"
            value = package_data_from_cli(str(v) for v in value)
        elif isinstance(value, tuple):
            value = list(value)
        target: object = settings
        for part in path[:-1]:
            target = getattr(target, part)
        setattr(target, path[-1], value)
    return settings
