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
import re
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final, Literal

from smelt.explorer import ModuleKind, ResolvedModule
from smelt.utils import ImportPath, PathExists, SmeltError, assert_path_exists


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


def _canonicalize_distribution_name(name: str) -> str:
    """
    PEP 503 canonicalization (lowercase, `-`/`_`/`.` runs collapsed to a single `-`),
    so `Flask`/`flask`/`flask..core` all key the same way -- reimplemented inline
    (matches `packaging.utils.canonicalize_name`'s own one-line regex) rather than
    importing `packaging` just for this, since `owning_distribution` (from installed
    metadata) and `[project.dependencies]` (as the project author spelled it) are not
    guaranteed to agree on casing/separators for the same distribution.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


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
            canonical = _canonicalize_distribution_name(dist_name)
            for name, specifier in pyproject_dependencies.items():
                if _canonicalize_distribution_name(name) == canonical:
                    return specifier
            return ""
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


def parse_pyproject_dependencies(dependencies: Iterable[str]) -> dict[str, str]:
    """
    Turns raw `[project.dependencies]` specifier strings (PEP 508, e.g.
    `"numpy>=1.24,<2"`, as carried on `SmeltConfig.dependencies`) into
    `{distribution_name: specifier_string}`, for `resolve_isolated_build_version`'s
    `"pyproject"` strategy.

    Lazily imports `packaging.requirements.Requirement` -- guarded the same way
    `unearth` is below, since parsing real PEP 508 grammar (extras, markers, ...) by
    hand is not worth reimplementing for a feature already gated behind the
    `isolated-build` extra.
    """
    dependencies = list(dependencies)
    if not dependencies:
        # No `packaging` import forced on a project that declares no
        # `[project.dependencies]` at all -- most builds enabling `isolated-build`
        # only for its "local" strategy never reach here.
        return {}
    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise ImportError(
            "packaging is not installed, so smelt cannot parse [project.dependencies] "
            "for isolated-build. Install this package with the isolated-build extra: "
            "`uv pip install 'smelt[isolated-build]'`."
        ) from exc
    return {
        requirement.name: str(requirement.specifier)
        for requirement in (Requirement(dependency) for dependency in dependencies)
    }


#: Where a fetched wheel (and its extraction) is cached, mirroring
#: `smelt.own_python.own_python_cache_dir`.
_ISOLATED_BUILD_CACHE_DIR: Final[Path] = Path.home() / ".cache" / "smelt" / "isolated-build"


def isolated_build_cache_dir(dist_name: str, version: str, target: str | None = None) -> Path:
    """
    Where a wheel for `dist_name`==`version` built for `target` is cached, so a second
    build of the same project does not re-hit the index (mirrors
    `smelt.own_python.own_python_cache_dir`). `target` is part of the key because the
    same version can have a different wheel per platform; `None` (the host's own
    platform) is spelled `"native"` for the same reason `own_python_cache_dir` spells
    an unset target that way.
    """
    return _ISOLATED_BUILD_CACHE_DIR / (target or "native") / dist_name / version


def _cached_wheel(cache_dir: Path) -> PathExists | None:
    if not cache_dir.is_dir():
        return None
    wheels = sorted(cache_dir.glob("*.whl"))
    return assert_path_exists(wheels[0]) if wheels else None


def fetch_wheel(
    dist_name: str,
    version_requirement: str,
    target: str | None,
    *,
    cache_dir: Path | None = None,
) -> PathExists:
    """
    Resolves and downloads the wheel satisfying `version_requirement` (see
    `resolve_isolated_build_version`) for `target` (a Zig-triple-shaped string, same
    spelling as `own_python_target`; `None` means the host's own platform) via
    `unearth`'s finder API, targeted at that platform's wheel tags (see
    `wheel_platform_tags`) rather than the running interpreter's own.

    Cached under `isolated_build_cache_dir()` when `version_requirement` already pins
    an exact version (`"==...")`; a range specifier (the `"pyproject"` strategy's
    common case) cannot be looked up in the cache without asking the index what it
    resolves to first, so that case always re-resolves (still downloading into the
    same cache directory once the exact version is known).

    Raises `IsolatedBuildError` when nothing satisfies both `version_requirement` and
    `target`'s platform tags -- there is no dlopen-vs-inittab fallback to a
    differently-built file here: the file that would need loading cannot exist for
    that libc/arch at all, so this fails the build rather than shipping a file that
    cannot be loaded on the target.
    """
    try:
        from unearth import PackageFinder, TargetPython
    except ImportError as exc:
        raise ImportError(
            "unearth is not installed, so smelt cannot reinstall native dependencies "
            "for isolated-build. Install this package with the isolated-build extra: "
            "`uv pip install 'smelt[isolated-build]'`."
        ) from exc

    if version_requirement.startswith("=="):
        cached = _cached_wheel(
            cache_dir
            or isolated_build_cache_dir(dist_name, version_requirement.removeprefix("=="), target)
        )
        if cached is not None:
            return cached

    platforms = wheel_platform_tags(target) if target is not None else None
    finder = PackageFinder(
        target_python=TargetPython(platforms=platforms),
        only_binary=[":all:"],
    )
    best_match = finder.find_best_match(f"{dist_name}{version_requirement}")
    package = best_match.best
    if package is None or package.version is None:
        raise IsolatedBuildError(
            f"No wheel satisfies {dist_name + version_requirement!r} for target "
            f"{target or 'the host'!r} -- {dist_name} cannot be reinstalled for it."
        )
    dest_dir = cache_dir or isolated_build_cache_dir(dist_name, package.version, target)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return assert_path_exists(finder.download(package.link, location=dest_dir))


def extract_wheel(wheel_path: PathExists, dest_dir: Path) -> Path:
    """
    Unpacks `wheel_path` (a plain zip) into `dest_dir`. Pure stdlib `zipfile`. A no-op
    if `dest_dir` already holds a previous extraction of the same cached wheel.
    """
    if dest_dir.is_dir() and any(dest_dir.iterdir()):
        return dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel_path) as archive:
        archive.extractall(dest_dir)
    return dest_dir


#: Real compiled-extension filename endings across platforms -- narrows
#: `locate_native_in_wheel`'s glob away from same-stem companion files a wheel also
#: ships (a `.c`/`.pyx`/`.pyi` source file sitting right next to the module it built,
#: e.g. `MarkupSafe`'s own `_speedups.c`), which share the module's stem but are not
#: what needs loading.
_NATIVE_EXTENSION_SUFFIXES: Final[tuple[str, ...]] = (".so", ".pyd", ".dylib")


def locate_native_in_wheel(
    extracted_root: Path, dest_rel_path: Path, module_stem: str
) -> PathExists | None:
    """
    Finds the file inside `extracted_root` standing in for the local install's own
    `dest_rel_path` (as `dist._native_dest_rel_path` computes it) -- matched by
    directory + `module_stem`, not exact filename: the target's own `EXT_SUFFIX` (ABI
    tag, platform tag) differs from the host's, so the two builds' files never share a
    full name even though they occupy the same package-relative position.
    """
    directory = extracted_root / dest_rel_path.parent
    if not directory.is_dir():
        return None
    matches = sorted(
        candidate
        for candidate in directory.glob(f"{module_stem}.*")
        if candidate.is_file() and candidate.name.endswith(_NATIVE_EXTENSION_SUFFIXES)
    )
    return assert_path_exists(matches[0]) if matches else None


def locate_sibling_libs_dirs(extracted_root: Path) -> list[Path]:
    """
    Any `*.libs`-shaped directory at `extracted_root`'s top level -- the RPATH-relative
    shared libraries a `manylinux`/`musllinux`-repaired wheel vendors alongside its own
    extension modules (`auditwheel`'s convention, e.g. `numpy.libs/`). A wheel vendors
    at most its own such directory, so no name-matching against the owning
    distribution is needed.
    """
    return [
        entry
        for entry in extracted_root.iterdir()
        if entry.is_dir() and entry.name.endswith(".libs")
    ]


def prepare_isolated_natives(
    closure: dict[ImportPath, ResolvedModule],
    payload_root: Path,
    *,
    target: str | None,
    versions: IsolatedBuildVersions,
    dependencies: Mapping[str, str] = {},
) -> dict[ImportPath, PathExists]:
    """
    For every `ModuleKind.EXTENSION` entry in `closure`, fetches (once per distinct
    owning distribution, see `owning_distribution`) a wheel built for `target` and
    returns the replacement file to ship instead of `resolved.origin`.

    Also places that distribution's whole sibling tree directly into `payload_root`
    (any `*.libs`-shaped directory the wheel vendors alongside its own extension
    modules, see `locate_sibling_libs_dirs`) -- a repaired manylinux/musllinux wheel's
    own vendored shared libraries, without which the returned replacement file alone
    would not load on the target.

    An import path whose owning distribution could not be determined, or whose
    fetched wheel has no matching file, is simply absent from the returned mapping --
    the caller decides what that means (today: fail the build rather than silently
    falling back to the local, wrong-platform file or silently dropping the module).
    """
    replacements: dict[ImportPath, PathExists] = {}
    extracted_roots: dict[str, Path] = {}
    placed_libs_dirs: set[str] = set()

    for import_path, resolved in closure.items():
        if resolved.kind != ModuleKind.EXTENSION:
            continue
        dist_name = owning_distribution(import_path)
        if dist_name is None:
            continue

        if dist_name not in extracted_roots:
            version_requirement = resolve_isolated_build_version(
                dist_name, versions, pyproject_dependencies=dependencies
            )
            wheel_path = fetch_wheel(dist_name, version_requirement, target)
            extracted_roots[dist_name] = extract_wheel(wheel_path, wheel_path.parent / "extracted")
        extracted_root = extracted_roots[dist_name]

        if dist_name not in placed_libs_dirs:
            for libs_dir in locate_sibling_libs_dirs(extracted_root):
                dest = payload_root / libs_dir.name
                if not dest.exists():
                    shutil.copytree(libs_dir, dest)
            placed_libs_dirs.add(dist_name)

        assert resolved.origin is not None, "an EXTENSION module always has an origin"
        dest_rel_path = Path(*import_path.split(".")[:-1], resolved.origin.name)
        module_stem = import_path.rpartition(".")[2]
        native = locate_native_in_wheel(extracted_root, dest_rel_path, module_stem)
        if native is not None:
            replacements[import_path] = native

    return replacements
