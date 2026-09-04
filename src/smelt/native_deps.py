"""
Native (ELF) dependency resolution and RPATH rewriting for assembled distributions.

An extension module copied into a distribution is not self-contained: it carries
`DT_NEEDED` entries for the shared libraries it was linked against, and an RPATH that
was computed for wherever it used to live (a wheel typically points at its own
vendored libraries through something like `$ORIGIN/../../pkg.libs`). Once the file
sits in a distribution folder instead, that RPATH resolves to nothing and the library
is looked up in the host's default search path -- if it is there at all.

This module closes that gap: it walks the dependencies of everything placed in the
distribution, copies what should travel with it, and rewrites every RPATH so the
result resolves through `$ORIGIN` alone -- i.e. relative to the distribution folder,
wherever that folder ends up.

Deliberately built on `ldd`/`patchelf` as subprocesses rather than on any compiler's
or bundler's internal API: the interface is stable, and the whole point is not to be
coupled to the tool that produced the artifacts.

ELF/Linux only. On other platforms the equivalent tooling differs entirely
(`otool`/`install_name_tool`, `dumpbin`), so callers get a clear "not resolved here"
answer rather than a silently incomplete folder.

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from smelt.process import CommandContext, call_command
from smelt.utils import PathExists, SmeltError, assert_path_exists

_logger = logging.getLogger(__name__)


#: Libraries that must always come from the running system, whatever the distribution
#: ships: `linux-vdso` has no backing file at all, and `libnss_*` has to match the
#: host's own `/etc/nsswitch.conf` to resolve users and hosts correctly.
ALWAYS_HOST_DLL_PREFIXES: Final[tuple[str, ...]] = ("linux-vdso.so", "libnss_")

#: glibc's base libraries. Not bundled: the loader and libc are a tightly ABI-coupled
#: pair (internal TLS and data layout, not just public symbol versions), so shipping
#: one without exactly the matching other segfaults inside the loader before any code
#: runs. Making a distribution independent of the host's libc is not a bundling
#: problem -- it needs an interpreter built against a libc that ships as a single
#: combined loader (musl), which is a separate feature.
LINUX_BASE_LIBC_DLL_PREFIXES: Final[tuple[str, ...]] = (
    "ld-linux",
    "libc.so",
    "libpthread.so",
    "libm.so",
    "libdl.so",
    "librt.so",
    "libutil.so",
    "libresolv.so",
    "libnsl.so",
    "libanl.so",
    "libBrokenLocale.so",
    "libcidn.so",
    "libcrypt.so",
    "libmemusage.so",
    "libmvec.so",
    "libpcprofile.so",
    "libSegFault.so",
    "libthread_db",
)

#: libpython, excluded on purpose. The interpreter running the distribution already
#: provides it: where it is dynamically linked, its own `libpython` is loaded before
#: any extension module is imported and satisfies that module's `DT_NEEDED` entry by
#: soname. Bundling the build machine's copy instead would risk loading two different
#: libpythons into one process, which is far worse than the case it would fix (a
#: statically linked host interpreter, where an extension module needing `libpython`
#: cannot be satisfied by anything we could ship either).
HOST_PYTHON_DLL_PREFIXES: Final[tuple[str, ...]] = ("libpython",)

#: What a "bring your own python" distribution does not bundle.
LINUX_SYSTEM_DLL_IGNORE_PREFIXES: Final[tuple[str, ...]] = (
    *ALWAYS_HOST_DLL_PREFIXES,
    *LINUX_BASE_LIBC_DLL_PREFIXES,
    *HOST_PYTHON_DLL_PREFIXES,
)


class NativeDepsError(SmeltError):
    """
    Raised when native dependencies cannot be resolved or rewritten.
    """


def describe_command_failure(cmd_trace: CommandContext, cmd: Iterable[str]) -> str:
    """
    Renders a failed command's exit code alongside its captured stdout/stderr,
    so callers get the actual compiler/tool output rather than just an exit code.
    """
    lines = [f"exitcode {cmd_trace.exit_code}: {' '.join(cmd)}"]
    if cmd_trace.stdout:
        lines.append("stdout:\n" + "\n".join(cmd_trace.stdout))
    if cmd_trace.stderr:
        lines.append("stderr:\n" + "\n".join(cmd_trace.stderr))
    return "\n".join(lines)


def is_supported_platform() -> bool:
    """
    Whether native dependency resolution is implemented for the running platform.
    """
    return platform.system() == "Linux"


def bundled_patchelf_dir() -> Path | None:
    """
    Returns the directory holding the PyPI-provided `patchelf` binary, or None.

    `patchelf` is needed to rewrite RPATHs on Linux, and is located by bare name via
    PATH. Distributions ship varied (sometimes known-buggy, e.g. 0.18.0) versions, so
    smelt bundles a known-good one via the `patchelf` PyPI package to stay
    self-contained. This locates that binary independently of PATH ordering so it can
    be put first.
    """
    import importlib.metadata as md

    try:
        dist = md.distribution("patchelf")
    except md.PackageNotFoundError:
        return None
    for entry in dist.files or []:
        if entry.name == "patchelf" or entry.name.startswith("patchelf."):
            located = Path(entry.locate()).resolve()
            if located.exists():
                return located.parent
    return None


@contextmanager
def patchelf_on_path() -> Iterator[None]:
    """
    Prepends the bundled `patchelf` directory to PATH, if that package is installed.
    """
    bindir = bundled_patchelf_dir()
    if bindir is None:
        yield
        return
    previous = os.environ.get("PATH")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{previous}" if previous else str(bindir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


def ldd_dependencies(binary_path: str | os.PathLike[str]) -> dict[str, str]:
    """
    Runs `ldd` on `binary_path`, returning `{basename: resolved_path}` for every
    dependency it reports as resolved (skipping `not found` entries and the vDSO,
    which has no backing file).
    """
    cmd_trace = call_command("ldd", str(binary_path))
    if cmd_trace.exit_code != 0:
        # A file `ldd` refuses (a data file mistaken for an ELF, a statically linked
        # object) is not a build failure: it simply has no dependencies to walk.
        _logger.debug(
            "ldd could not read %s, treating it as having no dependencies: %s",
            binary_path,
            describe_command_failure(cmd_trace, ("ldd", str(binary_path))),
        )
        return {}
    deps: dict[str, str] = {}
    for line in cmd_trace.stdout:
        _name, sep, rest = line.strip().partition("=>")
        if not sep:
            continue
        rest = rest.strip()
        if not rest or rest.startswith("not found"):
            continue
        resolved = rest.split(" ", 1)[0]
        if resolved and os.path.isfile(resolved):
            deps[os.path.basename(resolved)] = resolved
    return deps


def collect_native_dependencies(
    seed_paths: Iterable[str | os.PathLike[str]],
    ignore_prefixes: tuple[str, ...] = LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
) -> dict[str, str]:
    """
    Recursively `ldd`-walks every path in `seed_paths` (and every dependency found
    along the way), returning `{basename: resolved_path}` for everything worth
    bundling -- i.e. excluding `ignore_prefixes`.

    On a same-basename conflict (two different resolved paths reporting the same file
    name) the first one found is kept, and the rest are logged and dropped: there is
    only one flat destination per name, and picking silently would hide a genuine
    version conflict between two dependencies.
    """
    collected: dict[str, str] = {}
    pending = [str(seed) for seed in seed_paths]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for basename, resolved in ldd_dependencies(current).items():
            if basename.startswith(ignore_prefixes):
                continue
            existing = collected.get(basename)
            if existing is not None:
                if existing != resolved:
                    _logger.warning(
                        "Conflicting native dependency %r: keeping %r, ignoring %r",
                        basename,
                        existing,
                        resolved,
                    )
                continue
            collected[basename] = resolved
            pending.append(resolved)
    return collected


def run_patchelf(*args: str) -> None:
    """
    Runs `patchelf` (preferring smelt's bundled copy) and raises on failure.
    """
    with patchelf_on_path():
        cmd_trace = call_command("patchelf", *args)
    if cmd_trace.exit_code != 0:
        raise NativeDepsError(
            f"patchelf failed: {describe_command_failure(cmd_trace, ('patchelf', *args))}"
        )


def set_rpath(binary_path: Path, rpath: str) -> None:
    """
    Sets `binary_path`'s ELF RPATH to `rpath` (`:`-joined entries).

    Uses `--force-rpath`, i.e. the legacy `DT_RPATH` tag rather than `DT_RUNPATH`:
    unlike `DT_RUNPATH`, `DT_RPATH` is searched before `LD_LIBRARY_PATH` and is
    inherited by the binary's own transitive dependencies. Both matter for a
    distribution that must resolve its own libraries rather than fall back to
    whatever same-named library the host happens to have installed.
    """
    run_patchelf("--force-rpath", "--set-rpath", rpath, str(binary_path))


def set_interpreter(binary_path: Path, interpreter: Path) -> None:
    """
    Sets `binary_path`'s ELF `PT_INTERP` -- the dynamic loader the kernel runs before
    any of the binary's own code.

    Unlike `DT_NEEDED` lookups, this is resolved directly by the kernel and is *not*
    subject to RPATH/`$ORIGIN` expansion, so `interpreter` must be an absolute path
    and the folder cannot be moved afterwards. Only relevant once a distribution
    bundles its own libc.
    """
    run_patchelf("--set-interpreter", str(interpreter), str(binary_path))


def nested_rpath(dest_rel_path: Path) -> str:
    """
    The RPATH an artifact placed at `dest_rel_path` needs to find both its siblings
    and the dependencies bundled flat at the distribution root.

    `$ORIGIN` covers the former (a shared runtime sitting in the same package folder),
    and the walk back up to the root covers the latter.
    """
    depth = len(dest_rel_path.parts) - 1
    if depth == 0:
        return "$ORIGIN"
    return "$ORIGIN:" + "/".join(["$ORIGIN", *([".."] * depth)])


def package_rpath_dirs(dest_rel_paths: Iterable[Path]) -> set[str]:
    """
    The distinct subdirectories (relative to the distribution root, POSIX-style)
    holding at least one native artifact -- i.e. the folders anything at the root has
    to be able to look into.
    """
    return {
        dest_rel_path.parent.as_posix()
        for dest_rel_path in dest_rel_paths
        if dest_rel_path.parent != Path()
    }


@dataclass
class BundledNatives:
    """
    The outcome of resolving a distribution's native dependencies.

    `dependencies` maps each bundled library's file name to where it came from.
    `unsupported` is set when the running platform has no implementation here, in
    which case nothing was resolved and nothing was rewritten.
    """

    dependencies: dict[str, PathExists] = field(default_factory=dict)
    rewritten: list[Path] = field(default_factory=list)
    unsupported: str | None = None

    @property
    def resolved(self) -> bool:
        return self.unsupported is None


def bundle_native_dependencies(
    dist_root: Path,
    placed: Mapping[Path, PathExists],
    *,
    extra_seeds: Iterable[PathExists] = (),
    ignore_prefixes: tuple[str, ...] = LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
) -> BundledNatives:
    """
    Bundles every shared library the distribution's own native artifacts need, and
    rewrites all RPATHs so they resolve relative to `dist_root`.

    `placed` maps each artifact's distribution-relative destination to the *source*
    file it was copied from, and the walk deliberately follows those sources rather
    than the copies: a wheel-shipped extension module usually finds its own vendored
    libraries through an RPATH relative to where the wheel installed it, which
    resolves to nothing once the file sits in the distribution instead. Walking the
    original location is what lets those libraries be found at all -- from the copy,
    `ldd` would report them as `not found` and they would be silently dropped.

    Dependencies are copied flat into `dist_root`, since that is a `sys.path` entry
    already and needs no per-package duplication.
    """
    if not is_supported_platform():
        reason = (
            f"native dependency resolution is only implemented for Linux, not {platform.system()}"
        )
        _logger.warning(
            "Skipping native dependency resolution: %s. Shared libraries the bundled "
            "extension modules need will have to be present on the target.",
            reason,
        )
        return BundledNatives(unsupported=reason)

    seeds = [*placed.values(), *extra_seeds]
    # A library that is already one of the distribution's own artifacts must not be
    # bundled a second time at the root: smelt's shared runtimes are `DT_NEEDED` by
    # the modules that use them and would otherwise be copied twice, once at the
    # package-relative position they are resolved from and once flat.
    already_placed = {dest_rel_path.name for dest_rel_path in placed}
    dependencies = {
        basename: assert_path_exists(resolved)
        for basename, resolved in collect_native_dependencies(seeds, ignore_prefixes).items()
        if basename not in already_placed
    }
    for basename, resolved in dependencies.items():
        shutil.copy2(resolved, dist_root / basename)

    rewritten: list[Path] = []
    for dest_rel_path in placed:
        set_rpath(dist_root / dest_rel_path, nested_rpath(dest_rel_path))
        rewritten.append(dest_rel_path)
    # A bundled dependency may itself need another one, and they all sit flat at the
    # root -- plus the package folders, for a library vendored next to its own
    # extension module.
    dependency_rpath = ":".join(
        ["$ORIGIN", *(f"$ORIGIN/{folder}" for folder in sorted(package_rpath_dirs(placed)))]
    )
    for basename in dependencies:
        set_rpath(dist_root / basename, dependency_rpath)
        rewritten.append(Path(basename))

    return BundledNatives(dependencies=dependencies, rewritten=rewritten)
