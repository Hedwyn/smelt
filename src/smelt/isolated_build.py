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
import sys
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from smelt.backend import _compile_place_or_stage, _cythonize_one, _mypycify_one
from smelt.compiler import SupportedPlatforms
from smelt.config import Backend, CythonExtension, MypycModule
from smelt.explorer import ModuleKind, ResolvedModule
from smelt.manifest import read_manifest_module
from smelt.own_python import TargetPythonHeaders
from smelt.utils import (
    GenericExtension,
    ImportPath,
    PathExists,
    PathSolver,
    SmeltError,
    assert_path_exists,
)

if TYPE_CHECKING:
    # `smelt.vendoring` imports back from this module (see `prepare_isolated_natives`'s
    # own local import, below) -- only safe to name its types here, never to import
    # the package itself at module scope.
    from smelt.vendoring.base import VendoredExtension


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


def canonicalize_distribution_name(name: str) -> str:
    """
    PEP 503 canonicalization (lowercase, `-`/`_`/`.` runs collapsed to a single `-`),
    so `Flask`/`flask`/`flask..core` all key the same way -- reimplemented inline
    (matches `packaging.utils.canonicalize_name`'s own one-line regex) rather than
    importing `packaging` just for this, since `owning_distribution` (from installed
    metadata) and `[project.dependencies]` (as the project author spelled it) are not
    guaranteed to agree on casing/separators for the same distribution.

    Also used by `smelt.vendoring`'s provider registry, so the two agree on which
    distribution a name refers to.
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
            canonical = canonicalize_distribution_name(dist_name)
            for name, specifier in pyproject_dependencies.items():
                if canonicalize_distribution_name(name) == canonical:
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


#: Where `smelt.vendoring`'s from-source builds cache their fetched sources and
#: compiled dependencies (see `vendored_build_cache_dir`), sibling to
#: `_ISOLATED_BUILD_CACHE_DIR` -- a vendored distribution resolves its own version
#: from source, so there is no `version` component to key on ahead of time the way
#: `isolated_build_cache_dir` does for a wheel.
_VENDORED_BUILD_CACHE_DIR: Final[Path] = Path.home() / ".cache" / "smelt" / "vendoring"


def vendored_build_cache_dir(dist_name: str, target: str | None = None) -> Path:
    """
    Where `smelt.vendoring`'s provider for `dist_name` caches whatever it fetches
    and builds for `target` (its source, a vendored native dependency like libffi,
    its compiled objects) across builds. Mirrors `isolated_build_cache_dir` without
    the version component.
    """
    return _VENDORED_BUILD_CACHE_DIR / (target or "native") / dist_name


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
    python_version: tuple[int, int] = sys.version_info[:2],
) -> PathExists:
    """
    Resolves and downloads the wheel satisfying `version_requirement` (see
    `resolve_isolated_build_version`) for `target` (a Zig-triple-shaped string, same
    spelling as `own_python_target`; `None` means the host's own platform) via
    `unearth`'s finder API, targeted at that platform's wheel tags (see
    `wheel_platform_tags`) rather than the running interpreter's own.

    `python_version` is the interpreter the wheel has to be *importable by*, and it is
    as load-bearing as the platform: an extension module is named after the interpreter
    it was built for (`.cpython-312-x86_64-linux-musl.so`) and is invisible to any
    other one. Left unset it is the running interpreter's, which a distribution's own
    interpreter has to match anyway (`dist.assert_no_version_skew`). Without it,
    unearth treats the version as unconstrained and picks whatever the index published
    last -- a `cp314` wheel for a 3.12 distribution, silently unimportable.

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
        # `abis`/`impl` deliberately left to unearth: from `py_ver` alone it derives the
        # whole tag set an interpreter accepts, `abi3` wheels (this version's and every
        # older one's) included, which spelling them out by hand would exclude.
        target_python=TargetPython(py_ver=python_version, platforms=platforms),
        only_binary=[":all:"],
    )
    best_match = finder.find_best_match(f"{dist_name}{version_requirement}")
    package = best_match.best
    if package is None or package.version is None:
        raise IsolatedBuildError(
            f"No wheel satisfies {dist_name + version_requirement!r} for target "
            f"{target or 'the host'!r} and Python "
            f"{python_version[0]}.{python_version[1]} -- {dist_name} cannot be "
            "reinstalled for it."
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


@dataclass
class IsolatedNativesResult:
    """
    What `prepare_isolated_natives` produced: `replacements` for the ordinary,
    loose-`.so` case (a fetched wheel, or a vendored provider's build when static
    linking was not requested), and `static_modules` for a vendored provider's
    build staged for static linking instead (see `static_build_dir`) -- meant to be
    merged into `smelt.backend.BackendResult.static_modules` by the caller, the
    same dict shape.
    """

    replacements: dict[ImportPath, PathExists] = field(default_factory=dict)
    static_modules: dict[ImportPath, list[PathExists]] = field(default_factory=dict)


def rebuild_manifested_extensions(
    dist_name: str,
    closure: dict[ImportPath, ResolvedModule],
    *,
    target: str | None,
    python_version: tuple[int, int],
    py_headers: TargetPythonHeaders | None,
    static_build_dir: Path | None,
) -> IsolatedNativesResult:
    """
    Rebuilds `dist_name`'s manifested (see `smelt.manifest`) `ModuleKind.EXTENSION`
    closure entries from their shadowed `.py` source (`ResolvedModule.shadowed_source`)
    instead of requiring a prebuilt wheel -- the manifest-driven counterpart to
    `smelt.vendoring`'s hand-registered from-source providers, generic over any
    distribution smelt itself compiled instead of needing one written per package.

    Declines (returns an empty result, the same "nothing here" shape
    `VendoringDeclined` gives `smelt.vendoring` callers) when `dist_name` has no
    manifest at all. Per closure entry, also declines -- leaving that one import
    path absent from the result -- when it names a `Backend.NUITKA` module (no
    cross-compile pipeline exists for Nuitka anywhere in smelt yet, mirroring
    `run_backend`'s own refusal), an import path the manifest does not know about,
    or a manifested module with no `shadowed_source` left on disk. A declined entry
    falls through to `prepare_isolated_natives`'s existing `smelt.vendoring`/
    `fetch_wheel` path unchanged.

    `python_version` is accepted for symmetry with `smelt.vendoring.
    build_vendored_extension`'s own signature (every other `prepare_isolated_natives`
    branch takes it) but unused here: `_mypycify_one`/`_cythonize_one` compile
    against whichever interpreter smelt itself is currently running under, same as
    `run_backend` compiling a project's own modules.
    """
    empty = IsolatedNativesResult()
    dist_entries = {
        import_path: resolved
        for import_path, resolved in closure.items()
        if resolved.kind == ModuleKind.EXTENSION and owning_distribution(import_path) == dist_name
    }
    if not dist_entries:
        return empty
    # `dist_name` (a PyPI distribution name) and the *import* top-level package name
    # usually coincide, but are not guaranteed to (e.g. "Pillow" installs "PIL") --
    # `read_manifest_module` needs the latter, so it is taken from an actual closure
    # entry rather than assumed equal to `dist_name`.
    top_level_package = next(iter(dist_entries)).partition(".")[0]
    manifest = read_manifest_module(top_level_package)
    if not manifest:
        return empty

    crosscompile = SupportedPlatforms.from_triple(target) if target is not None else None
    target_triple = crosscompile.get_triple_name() if crosscompile is not None else None
    rebuild_dest_folder = (
        isolated_build_cache_dir(dist_name, importlib.metadata.version(dist_name), target)
        / "rebuilt"
    )
    path_solver = PathSolver()

    #: The top-level package's own directory, symlinked into an otherwise-empty
    #: staging directory the first time it is needed, then reused. Compiling
    #: `shadowed_source` straight from its real location (normally inside the
    #: dependency's own `site-packages` install) makes mypyc's own mypy typecheck
    #: walk up from it looking for the nearest ancestor with no `__init__.py`, which
    #: lands on `site-packages` itself -- every unrelated top-level module living
    #: there is then misread as part of *this* build's own first-party code, which
    #: collides with same-named modules mypy separately expects from typeshed.
    #: Confirmed against `sockcan`: mypy refused to typecheck `_protocol.py` at all,
    #: citing "site-packages/typing_extensions.py shadows library module
    #: 'typing_extensions'". Staging just the one package elsewhere keeps that
    #: walk-up from ever reaching `site-packages`.
    staged_package_root: Path | None = None

    def _staged_source(source: PathExists) -> PathExists:
        nonlocal staged_package_root
        package_root = next(
            (parent for parent in source.parents if parent.name == top_level_package), None
        )
        assert package_root is not None, (
            f"{source} does not sit under a {top_level_package!r} package directory -- "
            "every manifested import path has at least one dot, so its top-level "
            "package must be a real directory, not a bare top-level module."
        )
        if staged_package_root is None:
            stage_dir = rebuild_dest_folder.parent / "src-stage"
            stage_dir.mkdir(parents=True, exist_ok=True)
            staged_package_root = stage_dir / top_level_package
            if not staged_package_root.exists():
                staged_package_root.symlink_to(package_root)
        return assert_path_exists(staged_package_root / source.relative_to(package_root))

    #: One rebuild per manifested module, however many closure entries reference it
    #: (the module itself, plus its `__mypyc` runtime companion when the closure
    #: lists that separately -- see below) -- memoized so a distribution with
    #: several manifested modules, or a module referenced both ways, is only
    #: compiled once per import path.
    built: dict[ImportPath, tuple[GenericExtension, list[PathExists] | None]] = {}

    def _build(
        base_import_path: ImportPath, resolved: ResolvedModule
    ) -> tuple[GenericExtension, list[PathExists] | None] | None:
        if base_import_path in built:
            return built[base_import_path]
        backend = manifest.get(base_import_path)
        if backend not in (Backend.MYPYC, Backend.CYTHON) or resolved.shadowed_source is None:
            return None
        rebuild_dest_folder.mkdir(parents=True, exist_ok=True)
        source = _staged_source(resolved.shadowed_source)
        ext = (
            _mypycify_one(
                MypycModule(base_import_path, source=source),
                path_solver,
                dest_folder=rebuild_dest_folder,
            )
            if backend is Backend.MYPYC
            else _cythonize_one(
                CythonExtension(base_import_path, source=source),
                path_solver,
                dest_folder=rebuild_dest_folder,
            )
        )
        outcome = (
            ext,
            _compile_place_or_stage(ext, static_build_dir, crosscompile, py_headers=py_headers),
        )
        built[base_import_path] = outcome
        return outcome

    replacements: dict[ImportPath, PathExists] = {}
    static_modules: dict[ImportPath, list[PathExists]] = {}

    for import_path, resolved in dist_entries.items():
        base_import_path = import_path
        is_runtime_companion = False
        if import_path not in manifest and import_path.endswith("__mypyc"):
            candidate = ImportPath(import_path.removesuffix("__mypyc"))
            if manifest.get(candidate) is Backend.MYPYC:
                base_import_path, is_runtime_companion = candidate, True

        base_resolved = dist_entries.get(base_import_path) or closure.get(base_import_path)
        if base_resolved is None:
            continue
        outcome = _build(base_import_path, base_resolved)
        if outcome is None:
            continue
        ext, staged = outcome

        if is_runtime_companion:
            if staged is not None or ext.runtime is None:
                # A statically-linked runtime's object code is already folded into
                # the base module's own `static_modules` entry below -- mirrors
                # `run_backend`'s own `static_modules[ext.import_path] = objects`,
                # which never adds a second entry for the runtime, since it has no
                # `PyInit_` of its own to register separately with
                # `PyImport_AppendInittab`. Nothing to add for this closure entry:
                # declined, same as any other case this rebuild cannot serve.
                continue
            replacements[import_path] = assert_path_exists(ext.get_runtime_dest_path(target_triple))
            continue

        if staged is not None:
            static_modules[import_path] = staged
        else:
            replacements[import_path] = assert_path_exists(ext.get_dest_path(target_triple))

    return IsolatedNativesResult(replacements, static_modules)


def prepare_isolated_natives(
    closure: dict[ImportPath, ResolvedModule],
    payload_root: Path,
    *,
    target: str | None,
    versions: IsolatedBuildVersions,
    dependencies: Mapping[str, str] = {},
    static_build_dir: Path | None = None,
    python_version: tuple[int, int] = sys.version_info[:2],
    py_headers: TargetPythonHeaders | None = None,
) -> IsolatedNativesResult:
    """
    For every `ModuleKind.EXTENSION` entry in `closure`, obtains a replacement built
    for `target` instead of `resolved.origin` (the local environment's own build,
    wrong for a foreign target).

    `py_headers`, forwarded to a `smelt.vendoring` provider's build, is the
    target-correct `Python.h`/`pyconfig.h` pair the caller already resolved (see
    `smelt.own_python.target_python_headers_for`) -- unused for a `target=None`
    (native) build, and unused for the wheel-fetch path (a prebuilt wheel compiles
    nothing here).

    Checked *first*, before any wheel lookup, in two steps:

    1. Whether `owning_distribution` was itself compiled by smelt (see
       `smelt.manifest`) -- if so, `rebuild_manifested_extensions` recompiles
       whichever of its manifested modules it can from their shadowed source,
       straight from this same closure, and never touches `unearth`/the package
       index for those import paths at all.
    2. For anything that step left unhandled, whether `owning_distribution` has a
       `smelt.vendoring` provider registered for it instead (see
       `smelt.vendoring.get_provider`) -- likewise built from source instead of
       fetched. A distribution with no provider, or whose provider declines this
       resolved version/target (see `smelt.vendoring.VendoringDeclined` -- e.g.
       `cryptography` below its Rust transition), falls through to fetching a
       prebuilt wheel (see `fetch_wheel`) instead, same as before.

    Either a manifest rebuild or a vendored provider's build is linked into a loose
    `.so` (returned via `IsolatedNativesResult.replacements`, same as a
    wheel-derived one) or, when `static_build_dir` is given, staged for static
    linking instead (returned via `IsolatedNativesResult.static_modules`) --
    mirroring `smelt.backend._compile_place_or_stage`'s own branch for every other
    backend. A wheel-derived replacement is always a finished, already-linked `.so`:
    there is no object-file seam for a prebuilt wheel, so it only ever populates
    `replacements`.

    Also places that distribution's whole sibling tree directly into `payload_root`
    (any `*.libs`-shaped directory the wheel vendors alongside its own extension
    modules, see `locate_sibling_libs_dirs`) -- a repaired manylinux/musllinux wheel's
    own vendored shared libraries, without which the returned replacement file alone
    would not load on the target. Vendored builds have no such sibling tree.

    An import path whose owning distribution could not be determined, or whose
    fetched wheel has no matching file, is simply absent from the returned result --
    the caller decides what that means (today: fail the build rather than silently
    falling back to the local, wrong-platform file or silently dropping the module).
    """
    replacements: dict[ImportPath, PathExists] = {}
    static_modules: dict[ImportPath, list[PathExists]] = {}
    extracted_roots: dict[str, Path] = {}
    vendored_builds: dict[str, tuple[VendoredExtension, PathExists | None]] = {}
    #: One `rebuild_manifested_extensions` call per distribution, covering every one
    #: of its manifested modules found anywhere in `closure` at once -- cached so a
    #: second import path from the same distribution reuses it rather than
    #: recompiling. An empty result (no manifest, or nothing in it usable) is cached
    #: the same as a successful one: either way there is nothing more to learn about
    #: this distribution from a second call.
    rebuilt_natives: dict[str, IsolatedNativesResult] = {}
    #: Distributions whose provider declined this resolved version/target (see
    #: `smelt.vendoring.VendoringDeclined`) -- cached the same way `vendored_builds`
    #: caches a success, so a second import path from the same distribution does not
    #: re-attempt (and re-fail the same way) the provider's own fetch/probe.
    declined_dists: set[str] = set()
    placed_libs_dirs: set[str] = set()

    for import_path, resolved in closure.items():
        if resolved.kind != ModuleKind.EXTENSION:
            continue
        dist_name = owning_distribution(import_path)
        if dist_name is None:
            continue

        # Checked before any `smelt.vendoring` provider or wheel lookup: a
        # distribution smelt itself compiled (see `smelt.manifest`) already carries
        # everything needed to rebuild it for `target` from source, so there is
        # nothing left for either of those slower paths to add for it. In practice a
        # distribution never has both a manifest and a registered `vendoring`
        # provider, so the ordering between them should not matter.
        if dist_name not in rebuilt_natives:
            rebuilt_natives[dist_name] = rebuild_manifested_extensions(
                dist_name,
                closure,
                target=target,
                python_version=python_version,
                py_headers=py_headers,
                static_build_dir=static_build_dir,
            )
        rebuilt = rebuilt_natives[dist_name]
        if import_path in rebuilt.replacements:
            replacements[import_path] = rebuilt.replacements[import_path]
            continue
        if import_path in rebuilt.static_modules:
            static_modules[import_path] = rebuilt.static_modules[import_path]
            continue

        # Local import: `smelt.vendoring` imports back from this module (for
        # `canonicalize_distribution_name`/`vendored_build_cache_dir`), so importing
        # it at module scope here would be a circular import. Deferred to this
        # call, by which point `smelt.isolated_build` has already finished loading.
        from smelt import vendoring

        provider = vendoring.get_provider(dist_name)
        if provider is not None and dist_name not in declined_dists:
            if dist_name not in vendored_builds:
                version_requirement = resolve_isolated_build_version(
                    dist_name, versions, pyproject_dependencies=dependencies
                )
                build_dir = static_build_dir or (
                    vendored_build_cache_dir(dist_name, target) / "objects"
                )
                build_dir.mkdir(parents=True, exist_ok=True)
                try:
                    vendored_builds[dist_name] = vendoring.build_vendored_extension(
                        provider,
                        version_requirement,
                        target,
                        python_version,
                        build_dir=build_dir,
                        static_build_dir=static_build_dir,
                        py_headers=py_headers,
                    )
                except vendoring.VendoringDeclined:
                    declined_dists.add(dist_name)

            if dist_name not in declined_dists:
                vext, so_path = vendored_builds[dist_name]
                if vext.import_path != import_path:
                    # Only a single extension per vendored distribution is supported
                    # today (see `smelt.vendoring.VendoredProvider`) -- nothing else
                    # in this distribution's closure is something this provider built.
                    continue
                if so_path is not None:
                    replacements[import_path] = so_path
                else:
                    static_modules[import_path] = vext.objects
                continue
            # Declined: fall through to the ordinary wheel-fetch path below, same as
            # if no provider had been registered for this distribution at all.

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

    return IsolatedNativesResult(replacements, static_modules)
