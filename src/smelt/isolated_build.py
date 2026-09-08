"""
Reinstalls a third-party native dependency for `build_dist`'s actual target instead of
shipping whatever build happens to be installed in the environment running smelt.
Only `ModuleKind.EXTENSION` closure entries are affected --
every other module kind already ships something platform-independent.

Opt-in via `isolated-build` (see `DEFAULT_ISOLATED_BUILD`): off by default, so a build's
existing behaviour -- copy the file from the local environment -- does not change
unless asked for.

@date: 08.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Final, Literal

from smelt.utils import ImportPath, SmeltError


class IsolatedBuildError(SmeltError):
    """
    Raised when a native dependency cannot be reinstalled for the build's target.
    """


#: The version-resolution strategies `isolated-build` can use for a reinstalled native
#: dependency -- see `resolve_isolated_build_version`.
type IsolatedBuildVersions = Literal["local", "lock", "pyproject"]

#: Every valid `IsolatedBuildVersions` value, spelled out so a string read from
#: `pyproject.toml` or the CLI can be matched against them rather than asserted to be
#: one of them (mirrors `smelt.dist.DIST_PYTHON_MODES`).
ISOLATED_BUILD_VERSIONS: Final[tuple[IsolatedBuildVersions, ...]] = (
    "local",
    "lock",
    "pyproject",
)

#: Whether a `ModuleKind.EXTENSION` closure entry is reinstalled for the actual target
#: instead of copied from the local environment. **Off by default**: copying from the
#: local environment is only wrong when the target's platform/libc differs from the
#: host's, which most builds never hit, and turning this on pulls in `unearth` and a
#: real network fetch for every native dependency in the closure.
DEFAULT_ISOLATED_BUILD: Final[bool] = False

#: Default version-resolution strategy: pin whatever is installed locally (see
#: `IsolatedBuildVersions`) -- whatever a project was tested against locally is what
#: gets fetched for the target, nothing else moves.
DEFAULT_ISOLATED_BUILD_VERSIONS: Final[IsolatedBuildVersions] = "local"


def owning_distribution(import_path: ImportPath) -> str | None:
    """
    The PyPI distribution `import_path` belongs to, via the same
    `importlib.metadata.packages_distributions()` lookup `collect_distribution_metadata`
    already uses. `None` when nothing claims it (a namespace-only top-level, a stale
    `.pth`-installed path, ...) -- `isolated-build` cannot act on those, and the caller
    falls through to today's behaviour for them.
    """
    top_level = import_path.partition(".")[0]
    owners = importlib.metadata.packages_distributions().get(top_level, ())
    return min(owners, default=None)


def resolve_isolated_build_version(
    dist_name: str,
    strategy: IsolatedBuildVersions,
    *,
    pyproject_dependencies: Mapping[str, str] = {},
) -> str:
    """
    The version specifier to combine with `dist_name` (e.g. `dist_name +
    resolve_isolated_build_version(...)`) when asking the wheel finder for a
    replacement build of `dist_name`:

    * `"local"` -- pins the exact version already installed here
      (`importlib.metadata.version(dist_name)`). The obvious default: whatever the
      project was tested against locally is what gets fetched for the target, nothing
      else moves.
    * `"pyproject"` -- the specifier declared in this project's own
      `[project.dependencies]` (`pyproject_dependencies[dist_name]`, e.g.
      `">=1.24,<2"`), letting the index resolve whichever version satisfies it *for
      the target*. Unconstrained (an empty specifier, any version) for a transitive
      dependency that has no entry there -- `[project.dependencies]` only lists direct
      ones.
    * `"lock"` -- not implemented yet: raises `NotImplementedError` unconditionally.
    """
    match strategy:
        case "local":
            return f"=={importlib.metadata.version(dist_name)}"
        case "pyproject":
            return pyproject_dependencies.get(dist_name, "")
        case "lock":
            raise NotImplementedError(
                'isolated-build\'s "lock" version-resolution strategy is not '
                'implemented yet -- use "local" or "pyproject".'
            )


#: Wheel-tag arch spellings that differ from this codebase's own Zig-triple arch
#: spelling (see `SupportedPlatforms` in `compiler.py`, which names the same target
#: `"arm-linux-gnueabihf"`).
_WHEEL_ARCH_ALIASES: Final[dict[str, str]] = {"arm": "armv7l"}

#: Manylinux baselines, newest (most restrictive glibc floor) first: a caller widens
#: down this ladder until a wheel matches, since a newer-baseline wheel is preferred
#: when present but an older one is still installable on the same target.
_MANYLINUX_LADDER: Final[tuple[str, ...]] = (
    "manylinux_2_28",
    "manylinux_2_17",
    "manylinux2014",
    "manylinux2010",
    "manylinux1",
)


def wheel_platform_tags(target: str) -> list[str]:
    """
    Candidate PyPI wheel platform tags for `target` (a Zig-triple-shaped string, same
    spelling as `own_python_target`, e.g. `"x86_64-linux-musl"`, `"aarch64-linux-gnu"`),
    most specific first. A caller widens down this list until one matches an actually
    published wheel, rather than assuming a single exact tag: musllinux versions are
    cumulative/backward-compatible, and the manylinux family forms a similar ladder of
    increasingly old glibc baselines, ending in the unversioned generic tag.

    Only Linux targets are supported today -- `IsolatedBuildError` otherwise.
    """
    arch, _, rest = target.partition("-")
    arch = _WHEEL_ARCH_ALIASES.get(arch, arch)
    if not rest.startswith("linux"):
        raise IsolatedBuildError(
            f"isolated-build has no wheel-platform-tag mapping for target {target!r} "
            "yet -- only Linux targets (glibc or musl) are supported."
        )
    if "musl" in target:
        return [f"musllinux_1_2_{arch}"]
    return [f"{baseline}_{arch}" for baseline in _MANYLINUX_LADDER] + [f"linux_{arch}"]
