"""
Target vocabulary: Zig triples, optionally with wildcards, plus `host`.

Problem: `supported-targets` can be declared at several levels (`[tool.smelt]`,
`[tool.smelt.bundle]`, an entrypoint), as patterns such as `x86_64-linux-*`, while the
commands deal with concrete triples. This module normalizes triples, matches them
against patterns, and picks the default set of targets to build.

A target is allowed only if it matches the patterns of every level that declares them.
"""

from __future__ import annotations

import fnmatch
import glob
import platform
from collections.abc import Iterable, Sequence
from functools import cache
from typing import Final

from smelt.own_python import host_zig_arch
from smelt.resolver import TargetLevel

#: The `-t` value / pattern naming the machine running smelt.
HOST: Final[str] = "host"

#: `platform.system()` (lowercased) -> the OS component of a Zig triple.
_ZIG_OS: Final[dict[str, str]] = {"darwin": "macos"}

_WILDCARD_CHARS: Final[str] = "*?["


@cache
def host_triple() -> str:
    """The Zig triple of this machine, e.g. `x86_64-linux-gnu`."""
    system = platform.system().lower()
    os_name = _ZIG_OS.get(system, system)
    triple = f"{host_zig_arch()}-{os_name}"
    if os_name != "linux":
        return triple
    libc = "gnu" if platform.libc_ver()[0] == "glibc" or not _is_musl_host() else "musl"
    return f"{triple}-{libc}"


def _is_musl_host() -> bool:
    return bool(glob.glob("/lib/ld-musl-*") or glob.glob("/usr/lib/ld-musl-*"))


def is_pattern(value: str) -> bool:
    """Whether `value` contains wildcards."""
    return any(char in value for char in _WILDCARD_CHARS)


def normalize_triple(value: str) -> str:
    """
    `host` -> this machine's triple; a two-part Linux triple (`x86_64-linux`) gets the
    `gnu` libc, as `SupportedPlatforms` does. Patterns are only touched for `host`.
    """
    if value == HOST:
        return host_triple()
    if is_pattern(value):
        return value
    parts = value.split("-")
    if len(parts) == 2 and parts[1] == "linux":
        return f"{value}-gnu"
    return value


def matches(target: str, patterns: Iterable[str]) -> bool:
    """Whether the concrete `target` matches any of `patterns`."""
    normalized = normalize_triple(target)
    return any(fnmatch.fnmatchcase(normalized, normalize_triple(p)) for p in patterns)


def check_target(target: str | None, levels: Iterable[TargetLevel]) -> TargetLevel | None:
    """
    The first level whose patterns exclude `target` (`None` meaning host), or `None`
    when every declared level allows it.
    """
    concrete = host_triple() if target is None else target
    for level in levels:
        if not matches(concrete, level.patterns):
            return level
    return None


def default_build_set(levels: Sequence[TargetLevel]) -> list[str | None]:
    """
    What to build when no target is requested: the concrete triples (no wildcard) of the
    most specific level declaring `supported-targets`, else the host (`None`).
    """
    if not levels:
        return [None]
    concrete = [normalize_triple(p) for p in levels[-1].patterns if not is_pattern(p)]
    return as_build_targets(concrete) or [None]


def as_build_targets(values: Iterable[str]) -> list[str | None]:
    """
    Normalizes requested targets for building, in order and without duplicates: the host
    (by name or by triple) becomes `None`, the "native, no `-target` flag" sentinel.
    """
    result: list[str | None] = []
    for value in values:
        triple = normalize_triple(value)
        target = None if triple == host_triple() else triple
        if target not in result:
            result.append(target)
    return result
