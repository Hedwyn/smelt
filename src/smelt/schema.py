"""
User-facing configuration schema: what may be written under `[tool.smelt]`.

Problem: a `pyproject.toml` is hand-written, so typos, wrong types and stale keys must
be reported as configuration errors naming the table to fix, not surface later as
`TypeError`s from deep inside a build. This module defines one dataclass per user
table, a strict parser building them from TOML data, and the alias layer that keeps
keys from earlier releases working (with a deprecation warning).

The schema is deliberately separate from `smelt.config.SmeltConfig`, which is the
internal, resolved configuration the build code consumes: `SmeltConfig` is derived
from `ProjectConfig` plus the `[project]` table, so internal fields (dependencies,
script names, ...) can never be set from a `pyproject.toml`.
"""

from __future__ import annotations

import difflib
import re
import types
import warnings
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import ClassVar, Literal, Union, get_args, get_origin, get_type_hints

from smelt.utils import (
    ImportPath,
    PathExists,
    SmeltConfigError,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
)

#: Version in which the keys deprecated in this one are dropped.
ALIAS_REMOVAL_VERSION = "0.9"

#: Any value a TOML file can hold. Left as `object` on purpose: it comes from a file the
#: user wrote, and every use narrows it with `isinstance` before reading it.
type TomlValue = object
type TomlTable = dict[str, object]

type Path_ = tuple[str, ...]


class SmeltDeprecationWarning(DeprecationWarning):
    """
    A configuration key that still works but is going away. A subclass of
    `DeprecationWarning` so it can be filtered on its own; shown by default (the
    interpreter hides plain `DeprecationWarning`s outside `__main__`, which is where
    a config reader never runs).
    """


warnings.filterwarnings("default", category=SmeltDeprecationWarning)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class PythonModuleEntry:
    """
    A pure-Python module compiled by one of the Python backends (`mypyc`, `cython`,
    `nuitka`). Written either as a bare import path or as a table.
    """

    shorthand: ClassVar[str] = "import_path"

    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class CExtensionEntry:
    import_path: ImportPath
    sources: list[PathExists]
    extras: list[str] = field(default_factory=list)


@dataclass
class ZigExtensionEntry:
    import_path: ImportPath
    #: Defaults to the last component of `import_path`.
    name: str | None = None
    #: Defaults to the project root.
    folder: PathExists | None = None
    flags: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


@dataclass
class ZigExecutableEntry:
    """
    Executable produced by the project's own `build.zig`, installed as a wheel script.
    """

    name: str
    exe: str | None = None
    folder: PathExists | None = None
    flags: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


@dataclass
class AutoConfig:
    mode: Literal["off", "package", "all"] = "off"
    #: Backends tried in order, per module.
    backends: list[Literal["nuitka", "mypyc", "cython"]] = field(default_factory=lambda: ["nuitka"])


@dataclass
class ExtensionsConfig:
    mypyc: list[PythonModuleEntry] = field(default_factory=list)
    cython: list[PythonModuleEntry] = field(default_factory=list)
    nuitka: list[PythonModuleEntry] = field(default_factory=list)
    c: list[CExtensionEntry] = field(default_factory=list)
    zig: list[ZigExtensionEntry] = field(default_factory=list)
    auto: AutoConfig = field(default_factory=AutoConfig)


@dataclass
class ExecutablesConfig:
    zig: list[ZigExecutableEntry] = field(default_factory=list)


@dataclass
class ContentGroup:
    """What goes into the app folder."""

    discovery: Literal["static", "trace", "both"] | None = None
    include_modules: list[str] = field(default_factory=list)
    include_packages: list[str] = field(default_factory=list)
    exclude_modules: list[str] = field(default_factory=list)
    drop_optional_imports: bool | None = None
    package_data: dict[str, list[str]] = field(default_factory=dict)
    distribution_metadata: list[str] = field(default_factory=list)


@dataclass
class InterpreterGroup:
    """Settings of the `python = "own"` interpreter."""

    version: str | None = None
    tailor: bool | None = None
    drop_stdlib_groups: list[str] = field(default_factory=list)
    static: bool | None = None
    static_modules: list[str] = field(default_factory=list)
    inittab: bool | None = None


@dataclass
class OnefileGroup:
    enabled: bool | None = None
    compression: Literal["xz", "gzip", "none"] | None = None
    #: Validity range depends on the algorithm; checked by the bundler.
    compression_preset: int | None = None
    cache: bool | None = None
    keep_folder: bool | None = None


@dataclass
class IsolatedBuildGroup:
    enabled: bool | None = None
    versions: Literal["local", "pyproject"] | None = None


@dataclass
class BundleSettings:
    """
    Every bundler setting. Unset means "not declared at this level": defaults are
    applied by the resolver, so a lower level can tell "unset" from "set to the default".
    """

    python: Literal["byo", "own"] | None = None
    supported_targets: list[str] | None = None
    optimize: Literal[0, 1, 2] | None = None
    guard_version: bool | None = None
    isolate: bool | None = None
    content: ContentGroup = field(default_factory=ContentGroup)
    interpreter: InterpreterGroup = field(default_factory=InterpreterGroup)
    onefile: OnefileGroup = field(default_factory=OnefileGroup)
    isolated_build: IsolatedBuildGroup = field(default_factory=IsolatedBuildGroup)


@dataclass
class BundleConfig(BundleSettings):
    """
    `[tool.smelt.bundle]`: project-wide bundler defaults, plus per-entrypoint
    overrides keyed by `[project.scripts]` name or `module.path:func` spec.
    """

    entrypoints: dict[str, BundleSettings] = field(default_factory=dict)


@dataclass
class NuitkaBinarySettings:
    extra_flags: list[str] = field(default_factory=list)
    zig: bool | None = None
    include_modules: list[str] = field(default_factory=list)
    include_packages: list[str] = field(default_factory=list)
    package_data: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class NuitkaBinaryConfig(NuitkaBinarySettings):
    entrypoints: dict[str, NuitkaBinarySettings] = field(default_factory=dict)


@dataclass
class ProjectConfig:
    """The `[tool.smelt]` table."""

    package_dirs: dict[str, str] = field(default_factory=dict)
    #: Zig-triple patterns; wired to the builds by the target unification.
    supported_targets: list[str] | None = None
    #: Pre-unification restrictions, still honored until `supported_targets` replaces them.
    platforms: list[str] | None = None
    archs: list[str] | None = None
    extensions: ExtensionsConfig = field(default_factory=ExtensionsConfig)
    executables: ExecutablesConfig = field(default_factory=ExecutablesConfig)
    bundle: BundleConfig = field(default_factory=BundleConfig)
    nuitka_binary: NuitkaBinaryConfig = field(default_factory=NuitkaBinaryConfig)


# ---------------------------------------------------------------------------
# Strict parser
# ---------------------------------------------------------------------------


def toml_key(field_name: str) -> str:
    """kebab-case TOML spelling of a dataclass field name."""
    return field_name.replace("_", "-")


_INDEX = re.compile(r"\[\d+\]")


def _table_label(path: Path_) -> str:
    return f"[{'.'.join(path)}]"


class _Parser:
    """
    One parse run: carries the project root (path fields are resolved against it) and
    the set of deprecated spellings already warned about.
    """

    def __init__(self, project_root: Path | None) -> None:
        self.project_root = project_root
        self.warned: set[str] = set()

    def warn_deprecated(self, key: str, message: str) -> None:
        if key in self.warned:
            return
        self.warned.add(key)
        warnings.warn(
            f"{message} Support is removed in {ALIAS_REMOVAL_VERSION}.",
            SmeltDeprecationWarning,
            stacklevel=2,
        )

    # -- tables -----------------------------------------------------------

    def build[T](self, datacls: type[T], data: object, path: Path_) -> T:
        """
        Builds `datacls` from the TOML table `data` found at `path`.
        """
        assert is_dataclass(datacls)
        shorthand = getattr(datacls, "shorthand", None)
        if isinstance(data, str) and shorthand is not None:
            data = {toml_key(shorthand): data}
        if not isinstance(data, dict):
            names = ", ".join(toml_key(f.name) for f in fields(datacls))
            expected = "an import path or a table" if shorthand else "a table"
            hint = (
                " Modules are declared one array-of-tables entry each, e.g. "
                '`[[tool.smelt.extensions.c]]` with `import-path = "pkg.mod"` and '
                '`sources = ["src/pkg/mod.c"]`, not as a `module = "source"` mapping.'
                if isinstance(data, str)
                else ""
            )
            raise SmeltConfigError(
                f"{_table_label(path)}: expected {expected} (keys: {names}), found {data!r}.{hint}"
            )
        hints = get_type_hints(datacls)
        known = {toml_key(f.name): f for f in fields(datacls)}
        legacy_spellings = {f.name: f for f in fields(datacls) if "_" in f.name}
        kwargs: dict[str, object] = {}
        for key, value in data.items():
            f = known.get(key)
            if f is None:
                f = legacy_spellings.get(key)
                if f is not None:
                    table = _INDEX.sub("", ".".join(path))
                    self.warn_deprecated(
                        f"{table}.{key}",
                        f"[{table}]: `{key}` is deprecated, use `{toml_key(f.name)}`.",
                    )
            if f is None:
                hint = difflib.get_close_matches(key, known, n=1)
                suffix = f" (did you mean '{hint[0]}'?)" if hint else ""
                raise SmeltConfigError(
                    f"{_table_label(path)}: unknown key '{key}'{suffix}. "
                    f"Valid keys: {', '.join(sorted(known))}."
                )
            if f.name in kwargs:
                raise SmeltConfigError(
                    f"{_table_label(path)}: '{toml_key(f.name)}' is declared twice "
                    "(kebab-case and snake_case spellings)."
                )
            kwargs[f.name] = self.convert(value, hints[f.name], (*path, toml_key(f.name)))
        for f in fields(datacls):
            if f.name not in kwargs and f.default is MISSING and f.default_factory is MISSING:
                raise SmeltConfigError(
                    f"{_table_label(path)}: missing mandatory key '{toml_key(f.name)}'"
                )
        return datacls(**kwargs)

    # -- values -----------------------------------------------------------

    def convert(self, value: object, hint: object, path: Path_) -> object:
        """
        Checks `value` against the type `hint` and converts it (paths resolved,
        tables turned into dataclasses). `path` names the value, for error messages.
        """

        def fail(expected: str) -> SmeltConfigError:
            *table, key = path
            return SmeltConfigError(
                f"{_table_label(tuple(table))} {key}: expected {expected}, got {value!r}"
            )

        origin = get_origin(hint)
        if origin in (Union, types.UnionType):
            arms = [a for a in get_args(hint) if a is not type(None)]
            if len(arms) != 1:
                raise NotImplementedError(f"Unsupported union type: {hint}")
            return self.convert(value, arms[0], path)
        if origin is Literal:
            choices = get_args(hint)
            if not any(type(value) is type(c) and value == c for c in choices):
                raise fail("one of " + ", ".join(repr(c) for c in choices))
            return value
        if origin is list:
            (item_hint,) = get_args(hint)
            if not isinstance(value, list):
                if isinstance(value, dict) and is_dataclass(item_hint):
                    raise fail(
                        "an array of tables, one entry per module, e.g. "
                        '`[[tool.smelt.extensions.c]]` with `import-path = "pkg.mod"` '
                        'and `sources = ["src/pkg/mod.c"]` (not a `module = "source"` mapping)'
                    )
                raise fail("an array")
            return [
                self.convert(item, item_hint, (*path[:-1], f"{path[-1]}[{i}]"))
                for i, item in enumerate(value)
            ]
        if origin is dict:
            key_hint, item_hint = get_args(hint)
            assert key_hint is str, "TOML table keys are strings"
            if not isinstance(value, dict):
                raise fail("a table")
            return {k: self.convert(v, item_hint, (*path, k)) for k, v in value.items()}
        if isinstance(hint, type) and is_dataclass(hint):
            return self.build(hint, value, path)
        if hint is bool:
            if not isinstance(value, bool):
                raise fail("a boolean")
            return value
        if hint is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise fail("an integer")
            return value
        if hint is str:
            if not isinstance(value, str):
                raise fail("a string")
            return value
        if hint is ImportPath:
            if not isinstance(value, str):
                raise fail("a string")
            try:
                return assert_is_valid_import_path(value)
            except SmeltError as exc:
                raise fail("a valid Python import path") from exc
        if hint is PathExists:
            if not isinstance(value, str):
                raise fail("a path")
            resolved = self.project_root / value if self.project_root is not None else Path(value)
            try:
                return assert_path_exists(resolved)
            except SmeltError as exc:
                *table, key = path
                raise SmeltConfigError(f"{_table_label(tuple(table))} {key}: {exc}") from exc
        raise NotImplementedError(f"Unsupported type hint: {hint}")


def build_datacls_from_toml[T](
    datacls: type[T],
    toml_data: object,
    context: Path_ | list[str] | None = None,
    project_root: Path | None = None,
) -> T:
    """
    Builds one `datacls` out of the TOML table `toml_data`, strictly: unknown keys,
    missing mandatory keys and wrongly typed values are `SmeltConfigError`s naming the
    table. `context` is the path of that table, for the error messages.
    """
    return _Parser(project_root).build(datacls, toml_data, tuple(context or ()))


# ---------------------------------------------------------------------------
# Alias layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alias:
    """
    A key from an earlier release. `old` and `new` are key paths below `[tool.smelt]`;
    a `*` segment matches any key at that position (an entrypoint name) and is carried
    over to the same position in `new`. `new` is None for a key that is dropped.
    """

    old: Path_
    new: Path_ | None
    transform: Callable[[TomlValue], TomlValue] | None = None
    note: str = ""


def _package_data_from_specs(value: TomlValue) -> TomlValue:
    """`["pkg:*.json,*.yaml"]` (old mini-syntax) -> `{pkg = ["*.json", "*.yaml"]}`."""
    if not isinstance(value, list):
        return value
    table: dict[str, TomlValue] = {}
    for spec in value:
        if not isinstance(spec, str) or ":" not in spec:
            raise SmeltConfigError(
                f"[tool.smelt] package data entry {spec!r}: expected 'PACKAGE:PATTERN,...'"
            )
        package, _, patterns = spec.partition(":")
        existing = table.setdefault(package, [])
        assert isinstance(existing, list)
        existing.extend(p for p in patterns.split(",") if p)
    return table


def _negate(value: TomlValue) -> TomlValue:
    return (not value) if isinstance(value, bool) else value


_REPORT_NOTE = "per-run settings are no longer read from pyproject.toml; use "

ALIASES: tuple[Alias, ...] = (
    Alias(("packages_location",), ("package-dirs",)),
    Alias(("mypyc_modules",), ("extensions", "mypyc")),
    Alias(("cython_modules",), ("extensions", "cython")),
    Alias(("nuitka_modules",), ("extensions", "nuitka")),
    Alias(("c_extensions",), ("extensions", "c")),
    Alias(("zig_modules",), ("extensions", "zig")),
    Alias(("zig_executables",), ("executables", "zig")),
    Alias(("auto_mode",), ("extensions", "auto", "mode")),
    Alias(("backend_priority_order",), ("extensions", "auto", "backends")),
    # Nuitka keys of the shared `entrypoints.<ep>` table.
    Alias(
        ("entrypoints", "*", "extra_flags"), ("nuitka-binary", "entrypoints", "*", "extra-flags")
    ),
    Alias(
        ("entrypoints", "*", "extra-flags"), ("nuitka-binary", "entrypoints", "*", "extra-flags")
    ),
    Alias(("entrypoints", "*", "no-zig"), ("nuitka-binary", "entrypoints", "*", "zig"), _negate),
    Alias(
        ("entrypoints", "*", "include-modules"),
        ("nuitka-binary", "entrypoints", "*", "include-modules"),
    ),
    Alias(
        ("entrypoints", "*", "include-package"),
        ("nuitka-binary", "entrypoints", "*", "include-packages"),
    ),
    Alias(
        ("entrypoints", "*", "include-package-data"),
        ("nuitka-binary", "entrypoints", "*", "package-data"),
        _package_data_from_specs,
    ),
    # Never functional.
    Alias(("mypyc_options",), None, note="it was never read"),
    Alias(("cython_options",), None, note="it was never read"),
    # Per-run concerns.
    Alias(("debug",), None, note=_REPORT_NOTE + "the SMELT_DEBUG environment variable"),
    Alias(("report_path",), None, note=_REPORT_NOTE + "--report or SMELT_REPORT"),
)


def _match(data: TomlTable, old: Path_) -> list[Path_]:
    """Concrete key paths in `data` matching the pattern `old`."""
    matches: list[Path_] = [()]
    for segment in old:
        extended: list[Path_] = []
        for prefix in matches:
            node: TomlValue = data
            for key in prefix:
                assert isinstance(node, dict)
                node = node[key]
            if not isinstance(node, dict):
                continue
            if segment == "*":
                extended.extend((*prefix, key) for key in node)
            elif segment in node:
                extended.append((*prefix, segment))
        matches = extended
    return matches


def _pop(data: TomlTable, path: Path_) -> TomlValue:
    parents: list[TomlTable] = [data]
    for key in path[:-1]:
        child = parents[-1][key]
        assert isinstance(child, dict)
        parents.append(child)
    value = parents[-1].pop(path[-1])
    # drop tables the removal left empty
    for parent, key in zip(reversed(parents[:-1]), reversed(path[:-1])):
        if not parent[key]:
            del parent[key]
    return value


def _insert(data: TomlTable, path: Path_, value: TomlValue, old: str) -> None:
    node = data
    for depth, key in enumerate(path[:-1], start=1):
        child = node.setdefault(key, {})
        if not isinstance(child, dict):
            raise SmeltConfigError(
                f"[tool.smelt]: cannot move '{old}' to '{'.'.join(path)}': "
                f"'{'.'.join(path[:depth])}' is not a table."
            )
        node = child
    if path[-1] in node:
        raise SmeltConfigError(
            f"[tool.smelt]: both '{old}' (deprecated) and '{'.'.join(path)}' are set; "
            "keep only the latter."
        )
    node[path[-1]] = value


def _fill_wildcards(old: Path_, new: Path_, concrete: Path_) -> Path_:
    """Carries the keys matched by `old`'s `*` segments over to `new`'s `*` segments."""
    wild = iter(c for p, c in zip(old, concrete) if p == "*")
    return tuple(next(wild) if seg == "*" else seg for seg in new)


def _deep_copy(value: TomlValue) -> TomlValue:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def merge_settings[T](result_type: type[T], base: object, override: object | None) -> T:
    """
    Merges two declarations of the same settings into a `result_type` (`base` and
    `override` may be subclasses of it, e.g. a table also carrying `entrypoints`):
    scalars of `override` replace those of `base` when set, lists append, tables merge
    key by key (lists inside them append too), and nested groups merge recursively.
    Neither input is modified.
    """
    assert is_dataclass(result_type)
    merged: dict[str, object] = {}
    for f in fields(result_type):
        mine = getattr(base, f.name)
        theirs = None if override is None else getattr(override, f.name)
        if f.name == "supported_targets":
            # never merged: the target check needs what every level declared
            merged[f.name] = theirs if theirs is not None else mine
        elif is_dataclass(mine):
            merged[f.name] = merge_settings(type(mine), mine, theirs)
        elif isinstance(mine, list) and isinstance(theirs, list):
            merged[f.name] = [*mine, *theirs]
        elif isinstance(mine, dict) and isinstance(theirs, dict):
            table = {k: list(v) if isinstance(v, list) else v for k, v in mine.items()}
            for key, value in theirs.items():
                existing = table.get(key)
                table[key] = (
                    [*existing, *value]
                    if isinstance(existing, list) and isinstance(value, list)
                    else value
                )
            merged[f.name] = table
        else:
            merged[f.name] = theirs if theirs is not None else mine
    return result_type(**merged)


def apply_aliases(toml_data: Mapping[str, TomlValue], parser: _Parser | None = None) -> TomlTable:
    """
    Rewrites keys of earlier releases to the current schema, warning once per
    deprecated key. Returns a new table; `toml_data` is untouched.
    """
    parser = parser or _Parser(None)
    data = {k: _deep_copy(v) for k, v in toml_data.items()}
    for alias in ALIASES:
        for concrete in _match(data, alias.old):
            old_name = ".".join(concrete)
            value = _pop(data, concrete)
            if alias.new is None:
                parser.warn_deprecated(
                    old_name,
                    f"[tool.smelt] `{old_name}` is deprecated and ignored: {alias.note}.",
                )
                continue
            new_path = _fill_wildcards(alias.old, alias.new, concrete)
            new_name = ".".join(new_path)
            if alias.transform is not None:
                value = alias.transform(value)
            _insert(data, new_path, value, old_name)
            parser.warn_deprecated(
                old_name, f"[tool.smelt] `{old_name}` is deprecated, use `{new_name}`."
            )
    # What is left under `entrypoints` was bundler configuration (or a bare declaration).
    leftovers = data.pop("entrypoints", None)
    if leftovers is not None:
        if not isinstance(leftovers, dict):
            raise SmeltConfigError(f"[tool.smelt] entrypoints: expected a table, got {leftovers!r}")
        for name, options in leftovers.items():
            if options == {}:
                # a bare declaration of an entrypoint outside `[project.scripts]`
                _insert(data, ("bundle", "entrypoints", name), {}, f"entrypoints.{name}")
                parser.warn_deprecated(
                    f"entrypoints.{name}",
                    f"[tool.smelt] `entrypoints.{name}` is deprecated, "
                    f"use `bundle.entrypoints.{name}`.",
                )
                continue
            raise SmeltConfigError(
                f"[tool.smelt.entrypoints.{name}]: bundler options moved to "
                f"[tool.smelt.bundle.entrypoints.{name}] (grouped as content / interpreter / "
                "onefile / isolated-build); Nuitka options to "
                f"[tool.smelt.nuitka-binary.entrypoints.{name}]. Found keys: {sorted(options)}"
                if isinstance(options, dict)
                else f"[tool.smelt.entrypoints.{name}]: expected a table, got {options!r}"
            )
    return data


def parse_project_config(
    toml_data: Mapping[str, TomlValue],
    project_root: Path | None = None,
    root_path: Path_ = ("tool", "smelt"),
) -> ProjectConfig:
    """
    Validates the `[tool.smelt]` table `toml_data` and builds the `ProjectConfig`.

    Keys from earlier releases are rewritten first (see `ALIASES`). Anything the
    schema does not know is a `SmeltConfigError`.
    """
    parser = _Parser(project_root)
    return parser.build(ProjectConfig, apply_aliases(toml_data, parser), root_path)
