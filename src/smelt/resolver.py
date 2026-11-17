"""
Layered settings resolution: one value per setting out of several declarations.

Problem: a bundler setting can be written at several levels (built-in default,
`[tool.smelt.bundle]`, an entrypoint's own table, a command-line flag), and the build
needs a single answer -- plus, for debugging, where that answer came from.

The rules are the same for every setting: the most specific level wins; scalars
replace, lists append, tables merge key by key. The one exception is
`supported_targets`, which is never merged: it restricts what may be built, so every
level that declares it has to be kept and checked on its own.

Levels are passed from least to most specific. A level is a dataclass of the same
shape as the result, where unset (None / empty) means "not declared here".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from functools import reduce
from typing import TYPE_CHECKING

from smelt.schema import merge_settings, toml_key

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

#: Source label of the built-in defaults.
SOURCE_DEFAULT = "default"
#: Source label of command-line flags.
SOURCE_CLI = "cli"


@dataclass(frozen=True)
class Level[T]:
    """One declaration of the settings, and the label it is reported under."""

    source: str
    settings: T | None


@dataclass(frozen=True)
class TargetLevel:
    """The `supported-targets` patterns of one level."""

    source: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class Resolved[T]:
    """
    The merged settings, with their provenance.

    `sources` maps the dotted kebab-case key of each declared setting
    (`onefile.compression`) to the levels that contributed to it: only the winner for
    a scalar, every level that added something for a list or table.
    `supported_targets` lists every level declaring it, least specific first.
    """

    value: T
    sources: dict[str, tuple[str, ...]]
    supported_targets: tuple[TargetLevel, ...]


def resolve[T: DataclassInstance](result_type: type[T], levels: Sequence[Level[object]]) -> Resolved[T]:
    """Merges `levels` (least specific first) into a `result_type`, recording sources."""
    value = reduce(
        lambda merged, level: merge_settings(result_type, merged, level.settings),
        levels,
        result_type(),
    )
    sources: dict[str, tuple[str, ...]] = {}
    targets: list[TargetLevel] = []
    _collect(result_type, levels, "", sources, targets)
    return Resolved(value=value, sources=sources, supported_targets=tuple(targets))


def _is_set(value: object) -> bool:
    return value is not None and value != [] and value != {}


def _collect[T: DataclassInstance](
    result_type: type[T],
    levels: Sequence[Level[object]],
    prefix: str,
    sources: dict[str, tuple[str, ...]],
    targets: list[TargetLevel],
) -> None:
    template = result_type()
    for f in fields(result_type):
        key = prefix + toml_key(f.name)
        declared = [
            (level.source, None if level.settings is None else getattr(level.settings, f.name))
            for level in levels
        ]
        default = getattr(template, f.name)
        if is_dataclass(default):
            _collect(
                type(default),
                [Level(source, group) for source, group in declared],
                f"{key}.",
                sources,
                targets,
            )
            continue
        setters = [source for source, value in declared if _is_set(value)]
        if f.name == "supported_targets":
            targets.extend(
                TargetLevel(source, tuple(value))
                for source, value in declared
                if value is not None
            )
            setters = setters[-1:]
        elif not isinstance(default, (list, dict)):
            setters = setters[-1:]
        if setters:
            sources[key] = tuple(setters)
