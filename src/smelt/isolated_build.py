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

from typing import Final, Literal

from smelt.utils import SmeltError


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
