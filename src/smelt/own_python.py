"""
Builds smelt's own CPython, and stages it inside a distribution folder.

This is what turns a "bring your own python" distribution (`smelt.dist`, mode `byo`)
into a self-contained one (mode `own`): the folder gains the interpreter's own `bin/`
and `lib/` trees, so running it needs no Python installed on the target at all.

The interpreter itself comes from the sibling `meta-python` project, which compiles
CPython through `build.zig` rather than `make`. Nothing here recompiles the
application: bytecode magic is per *minor* version and the C ABI is stable within
one, so a distribution built by the host's 3.12.x runs unchanged under a
meta-python-built 3.12.y -- extension modules, shared runtimes and all. That is the
whole reason mode `own` is additive rather than a second build pipeline.

Two properties of the built tree carry the design, and both are load-bearing rather
than incidental:

* **it is already relocatable.** `bin/python` resolves `libpythonX.Y.so` through its
  own `bin/../lib` (an `$ORIGIN`-relative `RUNPATH`), and CPython's prefix detection
  is executable-relative -- so the tree works from any path it is copied to, with no
  `PYTHONHOME` and no rewriting of our own;
* **`lib/pythonX.Y/os.pyc` is the landmark that prefix detection looks for.** Remove
  it and the interpreter cannot find its own standard library
  (`Could not find platform independent libraries <prefix>`), and `PYTHONHOME`
  cannot rescue it because the distribution runs under `-I`, which implies `-E`.
  `stage_interpreter` therefore asserts it, at build time.

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Final, Iterable

from smelt.bytecode import compile_tree
from smelt.native_deps import (
    ALWAYS_HOST_DLL_PREFIXES,
    LINUX_BASE_LIBC_DLL_PREFIXES,
    BundledNatives,
    collect_native_dependencies,
    is_supported_platform,
    set_rpath,
)
from smelt.process import call_command
from smelt.utils import PathExists, SmeltError, assert_path_exists, path_exists

_logger = logging.getLogger(__name__)

#: Where a built interpreter is cached, keyed by target and build mode underneath.
_METAPYTHON_CACHE_DIR: Final = Path.home() / ".cache" / "smelt" / "metapython"

#: Zig target triple `build_own_python` builds for when the caller names none. `None`
#: means "whatever `zig build`'s own default target is", i.e. a native build against
#: the host's own libc. That is deliberately *not* musl, even though musl is the only
#: shape that reaches genuine host-independence (see `build_own_python`'s docstring):
#: a musl interpreter cannot load the host's glibc-linked third-party extension
#: modules (`orjson`, `psutil`, `pydantic_core`, ... -- every non-stdlib `.so` a real
#: entrypoint pulls in from site-packages), and smelt does not build those. Targeting
#: musl is only useful once the *whole* dependency set is musl-built, so it stays an
#: explicit opt-in (`own-python-target`) rather than the default.
DEFAULT_OWN_PYTHON_TARGET: Final[str | None] = None

#: The interpreter executable inside a built (or staged) prefix, prefix-relative.
INTERPRETER_REL_PATH: Final[Path] = Path("bin", "python")

#: The stdlib module whose presence CPython's prefix detection uses to recognise a
#: standard library directory. Sourceless is fine, absent is not -- see this module's
#: docstring.
STDLIB_LANDMARK: Final[str] = "os"

#: Stdlib directory entries pruned from a staged interpreter by default.
#:
#: `test` is CPython's own test suite (and the only source of bytecode-compilation
#: failures in the whole tree -- it ships deliberately-invalid-syntax fixtures),
#: `idlelib` and `tkinter` are the bundled GUI, `lib2to3` a dead 2-to-3 translator,
#: `ensurepip` a vendored pip installer, `config-*` the static-linking bits
#: `./configure` leaves behind for building *other* extension modules, and
#: `__pycache__` the interpreter's own bytecode cache in the layout a sourceless tree
#: cannot use.
#:
#: `_tkinter*` goes with `tkinter` rather than being a second decision: the extension
#: module is unusable without the Python package that wraps it, and it is the one file
#: in `lib-dynload` whose dependencies cannot be bundled at all -- it names
#: `/lib64/libtk8.6.so` and `/lib64/libtcl8.6.so` by *absolute path* in `DT_NEEDED`,
#: which no RPATH can redirect. Keeping it would leave the distribution reaching
#: outside itself for a GUI toolkit it cannot import, and drag ~4 MB of X11 libraries
#: in behind it. Everything else in `lib-dynload` is kept wholesale, on the principle
#: that an unused `.so` costs size while a missing one costs correctness.
#:
#: TODO(step 2): this is a hardcoded list, and `tkinter` in particular is only dead
#: weight for an application that does not import it. Step 2 of mode B drives the
#: interpreter's contents from `smelt.explorer`'s closure instead, and then prunes
#: `tkinter` (with `_tkinter`) only when discovery did not reach it.
DEFAULT_STDLIB_PRUNE: Final[tuple[str, ...]] = (
    "test",
    "idlelib",
    "tkinter",
    "_tkinter*",
    "lib2to3",
    "ensurepip",
    "config-*",
    "__pycache__",
)

#: What a staged interpreter still takes from the target machine: the C library, its
#: loader, and the kernel-provided objects. Deliberately *not* the `libpython` entry
#: `LINUX_SYSTEM_DLL_IGNORE_PREFIXES` also carries -- in mode B the distribution ships
#: its own `libpythonX.Y.so`, and the interpreter's extension modules resolve it
#: through their own `$ORIGIN`-relative RPATH, so it must not be treated as
#: host-supplied here.
#:
#: libc and its loader stay host-supplied on a native target on purpose, and not for
#: want of trying: a build that `ldd`-resolved `ld-linux`/`libc.so` like any other
#: dependency segfaulted immediately *inside the loader itself*, before any Python ran.
#: `ld.so` and `libc.so` are a tightly ABI-coupled matched pair (internal TLS and data
#: layout, not just public symbol versions), and Zig's outputs do not agree on which
#: glibc copy they point at -- so a generic per-file dependency walk silently mixes two
#: incompatible glibc builds. musl is the only bundleable libc, because it ships loader
#: and library as a single combined `ld-musl-<arch>.so.1` with no pair to mismatch.
INTERPRETER_HOST_DLL_PREFIXES: Final[tuple[str, ...]] = (
    *ALWAYS_HOST_DLL_PREFIXES,
    *LINUX_BASE_LIBC_DLL_PREFIXES,
)

#: What the interpreter's own dependency walk does not *copy*: the host-supplied set
#: above, plus `libpython` itself. The interpreter already ships `libpythonX.Y.so` at
#: the very destination a copy would land at, and every extension module names it in
#: `DT_NEEDED`, so leaving it in would only produce a same-basename conflict between
#: the two paths that reach it (`bin/../lib` and `lib-dynload/../..`). Its own
#: dependencies are still collected: it is one of the walk's seeds, and only the
#: copying is suppressed here, not the walking.
_INTERPRETER_WALK_IGNORE_PREFIXES: Final[tuple[str, ...]] = (
    *INTERPRETER_HOST_DLL_PREFIXES,
    "libpython",
)


class OwnPythonError(SmeltError):
    """
    Raised when smelt's own interpreter cannot be built or staged.
    """


def _ensure_metapython_installed() -> None:
    """
    Raises `ImportError` with an actionable message if `metapython` is not installed.
    """
    try:
        import metapython

        _ = metapython
    except ImportError as exc:
        raise ImportError(
            "metapython is not installed, so smelt cannot build its own interpreter. "
            "Install this package with the metapython extra: "
            "`uv pip install 'smelt[metapython]'`."
        ) from exc


# `#define NAME 1` -> `/* #undef NAME */`: autoconf's own "not available" spelling for
# a plain `#ifdef`-tested `HAVE_*` macro (must be genuinely undefined, not just falsy).
_MUSL_UNDEF_OVERRIDES: Final[tuple[str, ...]] = (
    "HAVE_CLOSE_RANGE",
    "HAVE_SEM_CLOCKWAIT",
    "HAVE_SYS_PIDFD_H",
    "HAVE_SCHED_SETAFFINITY",
    # SysV STREAMS -- a glibc/legacy-Unix-only header, musl never provided it.
    "HAVE_STROPTS_H",
)
# `#define NAME 1` -> `#define NAME 0`: autoconf's `AC_CHECK_DECLS`-style macros are
# always defined (0 or 1), tested via plain `#if`, so `0` -- not an `#undef` -- is the
# "not available" spelling here.
_MUSL_ZERO_OVERRIDES: Final[tuple[str, ...]] = ("HAVE_DECL_RTLD_DEEPBIND",)


def _patch_musl_pyconfig(pyconfig_path: Path, dest: Path) -> None:
    """
    Copies `pyconfig_path` to `dest`, flipping the specific `HAVE_*` macros
    `./configure` gets wrong for a musl target (see `build_own_python`'s docstring):
    it feature-detects against whatever libc the *build machine* actually has, and on
    a glibc build machine that means these come back "available" even though musl's
    headers do not provide them -- `Modules/posixmodule.c`,
    `Python/thread_pthread.h` and `Python/fileutils.c` then fail to compile against
    musl's actual `<sched.h>`/`<unistd.h>`/(missing) `<sys/pidfd.h>`. Each override
    was found empirically, one real compiler error at a time -- there is no general
    "musl mode" flag to flip.
    """
    text = pyconfig_path.read_text()
    for macro in _MUSL_UNDEF_OVERRIDES:
        text = re.sub(rf"^#define {macro} 1$", f"/* #undef {macro} */", text, flags=re.MULTILINE)
    for macro in _MUSL_ZERO_OVERRIDES:
        text = re.sub(rf"^#define {macro} 1$", f"#define {macro} 0", text, flags=re.MULTILINE)
    dest.write_text(text)


def _is_musl_target(target: str | None) -> bool:
    """
    Whether `target` (a Zig target triple, `None` for native) names a musl libc.
    """
    return target is not None and "musl" in target


def own_python_cache_dir(target: str | None = None, *, debug: bool = False) -> Path:
    """
    Where `build_own_python` caches its build for `target` and build mode.

    Keyed on both, because a native and a musl build of the same CPython are not
    interchangeable and neither are a stripped and an unstripped one -- sharing one
    directory would make whichever ran first silently satisfy the other's cache check.
    """
    return _METAPYTHON_CACHE_DIR / f"{target or 'native'}{'-debug' if debug else ''}"


def build_own_python(
    dest_dir: str | os.PathLike[str] | None = None,
    *,
    target: str | None = DEFAULT_OWN_PYTHON_TARGET,
    no_cache: bool = False,
    debug: bool = False,
) -> PathExists:
    """
    Builds smelt's own CPython through the sibling `meta-python` project (Zig-driven:
    its `python/cpython` submodule compiled straight through `build.zig`, no `make`)
    into `dest_dir` -- a per-user, per-target cache directory reused across builds if
    omitted -- and returns that prefix.

    A build takes minutes, so the cache is the normal path: it hits on
    `<dest>/bin/python` already existing. Pass `no_cache` to force a rebuild, e.g.
    after meta-python's pinned CPython version changes.

    Linkage: `python-linkage=dynamic` + `libc-linkage=dynamic`, i.e. a real
    `libpythonX.Y.so` that both `bin/python` and the stdlib's extension modules link
    against. `libc-linkage=static` is deliberately not used even though it sounds like
    the more standalone choice: meta-python rejects it combined with any dynamically
    linked Python at all, and that is an ELF constraint rather than a tunable -- a
    fully static executable has no dynamic section, so it cannot depend on any `.so`,
    by build-time link or by `dlopen`. There is no "static libc, dynamic everything
    else" for it to reach. A genuinely static interpreter needs every extension module
    compiled *into* it (`-Dstatic-modules=`), which in turn requires
    `python-linkage=off` -- and that leaves no `libpython` at all.

    `libraries` is left at meta-python's own defaults (`zlib`/`openssl`/`libffi`
    static, the rest dynamic or off) for this step. If a static library build fails,
    the known-good fallback set is `zlib` static, `openssl`/`libffi` dynamic and
    `readline`/`ncurses` off: the `allyourcodebase` `openssl`/`libffi` packages expose
    no PIC toggle the way zlib's does, so their static archives can fail to link into
    the shared extension modules that need them, and there is no musl-targeted
    readline/ncurses to link against at all.

    `debug` keeps debug info *and* switches the interpreter to `--with-pydebug`. Both
    halves matter and neither is the default:

    * `OptimizeMode.DEBUG` maps to `./configure --with-pydebug`, which changes
      `PyObject`'s layout and so gets its own ABI tag (`cpython-312d-...`). A debug
      interpreter cannot load release-ABI extension modules -- i.e. every `.so` a
      distribution ships -- so it breaks exactly the imports mode B exists to keep
      working;
    * `strip` is a separate knob from the optimize mode because CPython's
      `PY_CORE_CFLAGS` come from the `./configure`-generated Makefile and carry `-g`
      unconditionally: a release build still emits full DWARF without it. Stripping
      takes `libpython3.12.so` from 24.5 MB to 7.3 MB and the 62 `lib-dynload`
      modules from 14 MB to 4 MB, measured -- and every one of those files is copied
      into each distribution this interpreter is staged into.

    `target` is a Zig target triple, or `None` (the default) for a native build:

    * **native**: everything above libc -- the interpreter, `libpythonX.Y.so`, the
      stdlib's own extension modules -- is smelt's own build rather than the machine's
      Python install, while libc and its loader still come from the target (see
      `INTERPRETER_HOST_DLL_PREFIXES` for why that is not an oversight). This is what
      mode B delivers today, and it keeps working with the host's third-party
      extension modules, which is what makes it usable at all.
    * **musl** (`"x86_64-linux-musl"` and friends): the genuinely host-independent
      shape, and **untested on this path** -- the bootstrap below is carried over from
      earlier work and no mode B distribution has been built with it. Two real costs.
      First, meta-python never passes `--host=`, so `./configure` runs natively and
      feature-detects against the build machine's glibc, producing a `pyconfig.h` that
      claims glibc-only features; that is handled by a two-phase bootstrap (a first
      `zig_build` *expected* to fail, whose only purpose is making `./configure`
      produce a base `pyconfig.h`, then `_patch_musl_pyconfig`, then the real build
      with `-Dpyconfig-header=`). Second, and not fixable here: every third-party
      extension module the application ships still comes from the host and is
      glibc-linked, so it will not load under a musl interpreter.
    """
    _ensure_metapython_installed()
    from metapython.compile import (
        VENDORED_PROJECT_DIR,
        BuildOptions,
        LibCLinkage,
        Linkage,
        OptimizeMode,
        zig_build,
    )

    dest = Path(dest_dir) if dest_dir is not None else own_python_cache_dir(target, debug=debug)
    bin_path = dest / INTERPRETER_REL_PATH
    if not no_cache and bin_path.exists():
        _logger.info("Reusing the interpreter already built at %s", dest)
        return assert_path_exists(dest)

    options = BuildOptions(
        target=target,
        # Explicit, and load-bearing: `zig build`'s own default optimize mode is
        # `Debug`, which meta-python maps to `--with-pydebug` -- see this function's
        # docstring for why that cannot be the default here.
        optimize=OptimizeMode.DEBUG if debug else OptimizeMode.RELEASE_FAST,
        # Not implied by `optimize`, see this function's docstring.
        strip=not debug,
        libc_linkage=LibCLinkage.DYNAMIC,
        python_linkage=Linkage.DYNAMIC,
    )

    cpython_dir = VENDORED_PROJECT_DIR / "cpython"
    # meta-python's `runConfigure` only ever runs `./configure` when `cpython/Makefile`
    # is *absent* -- and that source tree is shared across every target and option
    # combination, so a leftover Makefile makes it skip straight to compiling with
    # whatever `pyconfig.h` an earlier run left behind. Force a real reconfigure for
    # *this* target.
    for stale in ("Makefile", "pyconfig.h"):
        (cpython_dir / stale).unlink(missing_ok=True)
    # Also clear Zig's own local project cache: verified empirically that when a
    # `zig build` invocation's CLI options are byte-identical to a previous one, Zig
    # can skip re-executing `build.zig`'s `build()` -- and with it the deletion above
    # and `runConfigure`'s `./configure` re-run -- silently reusing a stale step graph.
    # `.zig-cache` is local and rebuildable, unlike the global package fetch cache
    # (left untouched: no need to re-download zlib/openssl/libffi sources).
    shutil.rmtree(VENDORED_PROJECT_DIR / ".zig-cache", ignore_errors=True)

    dest.mkdir(parents=True, exist_ok=True)
    # `-p`: without an explicit install prefix, `zig build install` writes into
    # `zig-out` next to `build.zig` -- i.e. inside site-packages.
    install_prefix = ["-p", str(dest)]
    if _is_musl_target(target):
        try:
            # Expected to fail: this pass exists only to make `./configure` run and
            # produce a base `pyconfig.h` to patch below.
            zig_build(options, cwd=VENDORED_PROJECT_DIR, extra_args=install_prefix)
        except subprocess.CalledProcessError:
            pass
        unpatched_pyconfig = cpython_dir / "pyconfig.h"
        if not unpatched_pyconfig.exists():
            raise OwnPythonError(
                f"{unpatched_pyconfig} is missing after the bootstrap build for target "
                f"{target!r}: ./configure itself failed, rather than just the "
                "musl-specific compile errors this bootstrap expects to see."
            )
        patched_pyconfig = dest / "_smelt_pyconfig_musl.h"
        _patch_musl_pyconfig(unpatched_pyconfig, patched_pyconfig)
        options = replace(options, pyconfig_header=patched_pyconfig)

    try:
        zig_build(options, cwd=VENDORED_PROJECT_DIR, extra_args=install_prefix)
    except subprocess.CalledProcessError as exc:
        # Best-effort: Zig still installs whatever *did* build when some module failed
        # (each stdlib extension module is an independent compile step; `zig build
        # install` reports overall failure if any one errors, but does not withhold the
        # artifacts that succeeded). A handful of modules with their own
        # platform-specific gaps should not block using the interpreter for an
        # application that does not import them -- only the core interpreter itself is
        # load-bearing here, so re-raise only if that did not come out the other end.
        if not bin_path.exists():
            raise
        _logger.warning(
            "Some stdlib extension module(s) failed to build for target %s. The core "
            "interpreter succeeded, but an application importing one of the modules "
            "that did not build will fail on that import: %s",
            target or "native",
            exc,
        )
    if not bin_path.exists():
        raise OwnPythonError(
            f"The interpreter build for target {target or 'native'} produced no {bin_path}."
        )
    return assert_path_exists(dest)


def interpreter_version(prefix: PathExists) -> tuple[int, int]:
    """
    The `(major, minor)` version of the interpreter installed at `prefix`, asked of
    the interpreter itself rather than inferred from its `lib/pythonX.Y` directory
    name.

    Load-bearing rather than cosmetic: it is what the version-skew guard in
    `smelt.dist` compares against the interpreter that compiled the distribution's
    bytecode, and getting that comparison wrong ships a folder that cannot run.
    """
    executable = prefix / INTERPRETER_REL_PATH
    if not path_exists(executable):
        raise OwnPythonError(f"No interpreter at {executable}.")
    # `-I`: the probe must report the interpreter's own version, not be steered by a
    # `PYTHONPATH`/`sitecustomize` inherited from whatever environment smelt runs in.
    cmd_trace = call_command(
        str(executable),
        "-I",
        "-c",
        "import sys; print(sys.version_info[0], sys.version_info[1])",
    )
    if cmd_trace.exit_code != 0 or not cmd_trace.stdout:
        raise OwnPythonError(
            f"Could not read the version of the interpreter at {executable} "
            f"(exit code {cmd_trace.exit_code}): {' '.join(cmd_trace.stderr)}"
        )
    fields = cmd_trace.stdout[-1].split()
    if len(fields) != 2 or not all(field_value.isdigit() for field_value in fields):
        raise OwnPythonError(
            f"The interpreter at {executable} reported an unreadable version: "
            f"{cmd_trace.stdout[-1]!r}"
        )
    major, minor = (int(field_value) for field_value in fields)
    return major, minor


@dataclass
class StagedInterpreter:
    """
    The interpreter tree copied into a distribution folder, and what was done to it.

    `prefix_rel_path` is the interpreter's `sys.prefix`, relative to the distribution
    root -- and it is the root itself (`.`), because that is what makes CPython's
    executable-relative prefix detection land on the shipped standard library:
    `bin/python` derives its prefix from its own location, so `<dist>/bin/python`
    finds `<dist>/lib/pythonX.Y`. Recorded as a path rather than assumed, so the
    manifest states it instead of leaving readers to infer it.
    """

    prefix_rel_path: Path
    version: tuple[int, int]
    pruned: list[str] = field(default_factory=list)
    sourceless: bool = True
    size_bytes: int = 0
    compile_failures: dict[Path, str] = field(default_factory=dict)
    native_deps: BundledNatives = field(default_factory=BundledNatives)

    @property
    def version_string(self) -> str:
        major, minor = self.version
        return f"{major}.{minor}"

    @property
    def executable_rel_path(self) -> Path:
        return self.prefix_rel_path / INTERPRETER_REL_PATH

    def render(self) -> str:
        lines = [
            f"Interpreter:  CPython {self.version_string} at ./{self.executable_rel_path}",
            f"  size:       {self.size_bytes / 1e6:.1f} MB",
            f"  stdlib:     {'sourceless (.pyc only)' if self.sourceless else 'source kept'}",
            f"  pruned:     {', '.join(self.pruned) or 'nothing'}",
        ]
        if self.compile_failures:
            lines.append(f"  stdlib modules that did not compile: {len(self.compile_failures)}")
        if self.native_deps.dependencies:
            lines.append(f"  bundled libraries: {len(self.native_deps.dependencies)}")
        return "\n".join(lines)

    def serialize(self) -> dict[str, object]:
        return {
            "prefix": self.prefix_rel_path.as_posix(),
            "executable": self.executable_rel_path.as_posix(),
            "version": self.version_string,
            "size_bytes": self.size_bytes,
            "sourceless_stdlib": self.sourceless,
            "pruned": self.pruned,
            "bundled_libraries": sorted(self.native_deps.dependencies),
        }


def _stdlib_dir(prefix: Path) -> Path:
    """
    The `lib/pythonX.Y` directory of the interpreter installed at `prefix`.
    """
    candidates = sorted(entry for entry in prefix.glob("lib/python3.*") if entry.is_dir())
    if not candidates:
        raise OwnPythonError(f"No lib/pythonX.Y standard library directory under {prefix}.")
    if len(candidates) > 1:
        raise OwnPythonError(
            f"Several standard library directories under {prefix}: "
            f"{[entry.name for entry in candidates]}. Which one the interpreter would "
            "pick is not something to guess at."
        )
    return candidates[0]


def _prune_stdlib(stdlib_dir: Path, patterns: Iterable[str]) -> list[str]:
    """
    Removes every entry under `stdlib_dir` whose *name* matches one of `patterns`
    (`fnmatch` syntax), and returns the patterns that actually matched something.

    Matched by name at any depth rather than at the top level only: `__pycache__`
    exists in every package, and a nested test directory is exactly as much dead
    weight as the top-level one. The *patterns* are what comes back rather than the
    paths for that same reason -- `__pycache__` alone accounts for hundreds of
    directories, and a list of those tells a reader nothing the pattern does not.
    """
    pattern_list = list(patterns)
    if not pattern_list:
        return []
    matched: set[str] = set()
    removed = 0
    # Bottom-up, so removing a directory cannot invalidate a path already walked into.
    for current_dir, dir_names, file_names in os.walk(stdlib_dir, topdown=False):
        for name in [*dir_names, *file_names]:
            pattern = next((pattern for pattern in pattern_list if fnmatch(name, pattern)), None)
            if pattern is None:
                continue
            entry = Path(current_dir) / name
            if not entry.exists():
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            matched.add(pattern)
            removed += 1
    _logger.debug("Pruned %d stdlib entrie(s) from %s", removed, stdlib_dir)
    return sorted(matched)


def _tree_size(*roots: Path) -> int:
    """
    Total size in bytes of every regular file under `roots`.
    """
    return sum(
        entry.stat().st_size
        for root in roots
        if root.exists()
        for entry in root.rglob("*")
        if entry.is_file() and not entry.is_symlink()
    )


def _interpreter_elf_files(prefix: Path) -> list[Path]:
    """
    Every ELF file a staged interpreter is made of, prefix-relative: the executable,
    `libpythonX.Y.so`, and the stdlib's own extension modules under `lib-dynload`.

    Walked on the copies rather than on what they were copied from, unlike
    `dist.build_dist`'s own native artifacts: a meta-python-built module resolves its
    dependencies by soname through the loader's search path, not through an RPATH
    relative to where it was installed, so the copy reports exactly what the original
    would.
    """
    found: list[Path] = []
    for pattern in (INTERPRETER_REL_PATH.as_posix(), "lib/libpython*.so*", "lib/python3.*/**/*.so"):
        for entry in sorted(prefix.glob(pattern)):
            if entry.is_file() and not entry.is_symlink():
                found.append(entry.relative_to(prefix))
    return found


def _library_rpath(rel_path: Path, library_dir: Path = Path("lib")) -> str:
    """
    The RPATH a prefix-relative ELF file at `rel_path` needs to find both its own
    siblings and the shared libraries bundled flat into `library_dir`.

    `lib/` is the destination rather than the prefix root because that is where the
    interpreter's own `libpythonX.Y.so` already lives and where `bin/python`'s
    build-time `RUNPATH` already points -- so one directory serves both, and nothing
    lands at the distribution root where it could collide with an application module.
    """
    relative = os.path.relpath(library_dir, rel_path.parent)
    entries = ["$ORIGIN"]
    if relative != os.curdir:
        entries.append(f"$ORIGIN/{Path(relative).as_posix()}")
    return ":".join(entries)


def _bundle_interpreter_dependencies(prefix: Path) -> BundledNatives:
    """
    Copies every shared library the interpreter staged at `prefix` needs into its
    `lib/` directory, and rewrites the RPATHs so they resolve there.

    Without this the folder still runs on *this* machine -- `_ssl`, `_sqlite3`,
    `_lzma`, `_ctypes` and friends find the host's `libssl`/`libsqlite3`/`liblzma`/
    `libffi` by soname like any other program -- but the claim mode B makes is that
    nothing needs to be installed on the target, and a minimal one has none of those.
    libc and its loader are the deliberate exception, for the reasons in
    `INTERPRETER_HOST_DLL_PREFIXES`.

    Not `native_deps.bundle_native_dependencies`, which places dependencies flat at
    the distribution root and gives every artifact an RPATH walking up to it: at the
    root they would sit alongside the application's own top-level names, and rewriting
    `bin/python`'s RPATH to point at the root would drop the `$ORIGIN/../lib` entry it
    finds `libpythonX.Y.so` through. Same primitives, different destination.
    """
    if not is_supported_platform():
        reason = (
            "bundling the interpreter's shared libraries is only implemented for "
            f"Linux, not {platform.system()}"
        )
        _logger.warning(
            "Skipping %s. The libraries its standard library extension modules need "
            "will have to be present on the target.",
            reason,
        )
        return BundledNatives(unsupported=reason)

    elf_files = _interpreter_elf_files(prefix)
    library_dir = prefix / "lib"
    dependencies = {
        basename: assert_path_exists(resolved)
        for basename, resolved in collect_native_dependencies(
            [prefix / rel_path for rel_path in elf_files], _INTERPRETER_WALK_IGNORE_PREFIXES
        ).items()
        # `libpythonX.Y.so` is shipped by the interpreter itself, at the very
        # destination a copy would land at.
        if not (library_dir / basename).exists()
    }
    for basename, resolved in dependencies.items():
        shutil.copy2(resolved, library_dir / basename)

    rewritten: list[Path] = []
    for rel_path in elf_files:
        set_rpath(prefix / rel_path, _library_rpath(rel_path))
        rewritten.append(rel_path)
    for basename in dependencies:
        rel_path = Path("lib") / basename
        set_rpath(prefix / rel_path, _library_rpath(rel_path))
        rewritten.append(rel_path)
    return BundledNatives(dependencies=dependencies, rewritten=rewritten)


def stage_interpreter(
    built_prefix: PathExists,
    dist_root: Path,
    *,
    prune: Iterable[str] = DEFAULT_STDLIB_PRUNE,
    sourceless: bool = True,
    bundle_dependencies: bool = True,
) -> StagedInterpreter:
    """
    Copies the interpreter built at `built_prefix` into `dist_root` and returns what
    landed there.

    It goes at the distribution *root*, not into the payload directory: `bin/` and
    `lib/` are the interpreter's own prefix, not application code, and the whole point
    of keeping the application in its own subfolder is that the root stays free for
    exactly this (see `smelt.dist`'s module docstring). What is copied is
    `bin/python`, `lib/libpython*.so` and the whole `lib/pythonX.Y` tree --
    `lib-dynload` included wholesale rather than cross-referenced against what the
    application imports, because an unused `.so` costs size while a missing one costs
    correctness.

    Then, in order:

    * `prune` removes stdlib entries by name (see `DEFAULT_STDLIB_PRUNE`);
    * `sourceless` compiles the remaining stdlib to `.pyc` and deletes the `.py` it
      compiled. Per-file failures are recorded, not raised -- after pruning `test`
      there are none, but a partially built interpreter is not worth aborting a
      distribution over. Only sources that actually produced a `.pyc` are deleted, so
      a module that failed to compile keeps working from its source;
    * the landmark assertion: `lib/pythonX.Y/os.pyc` (or `os.py`) must exist, or the
      staged interpreter cannot find its own standard library at all. This is checked
      here, loudly, rather than discovered by the user on the target machine;
    * `bundle_dependencies` copies the shared libraries the interpreter's own
      extension modules need into `lib/` and rewrites their RPATHs, so `ssl`,
      `sqlite3`, `lzma` and friends resolve inside the folder instead of against
      whatever the target happens to have installed. libc and its loader are
      excluded and stay host-supplied (see `INTERPRETER_HOST_DLL_PREFIXES`).
    """
    built_stdlib = _stdlib_dir(built_prefix)
    built_executable = built_prefix / INTERPRETER_REL_PATH
    if not built_executable.is_file():
        raise OwnPythonError(f"No interpreter at {built_executable}.")
    version = interpreter_version(built_prefix)

    dest_bin = dist_root / INTERPRETER_REL_PATH
    dest_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_executable, dest_bin)

    dest_lib = dist_root / "lib"
    dest_lib.mkdir(parents=True, exist_ok=True)
    for shared_object in sorted((built_prefix / "lib").glob("libpython*.so*")):
        shutil.copy2(shared_object, dest_lib / shared_object.name)

    dest_stdlib = dest_lib / built_stdlib.name
    shutil.copytree(built_stdlib, dest_stdlib, symlinks=True, dirs_exist_ok=True)

    pruned = _prune_stdlib(dest_stdlib, prune)

    compile_failures: dict[Path, str] = {}
    if sourceless:
        compilation = compile_tree(assert_path_exists(dest_stdlib), dest_stdlib)
        compile_failures = dict(compilation.failed)
        for rel_path in compilation.compiled:
            (dest_stdlib / rel_path).unlink(missing_ok=True)
        if compile_failures:
            _logger.warning(
                "%d standard library file(s) did not compile to bytecode and are "
                "shipped as source: %s",
                len(compile_failures),
                ", ".join(sorted(str(rel_path) for rel_path in compile_failures)),
            )

    landmark = f"{STDLIB_LANDMARK}{'.pyc' if sourceless else '.py'}"
    if not (dest_stdlib / landmark).is_file():
        raise OwnPythonError(
            f"{dest_stdlib.relative_to(dist_root)}/{landmark} is missing from the "
            "staged interpreter. It is the landmark CPython's prefix detection looks "
            "for, so without it the interpreter fails to find its own standard "
            "library ('Could not find platform independent libraries <prefix>') and "
            "PYTHONHOME cannot rescue it, since the distribution runs under -I. Check "
            f"that the prune list ({list(prune)}) does not match it."
        )

    native_deps = (
        _bundle_interpreter_dependencies(dist_root) if bundle_dependencies else BundledNatives()
    )

    staged = StagedInterpreter(
        # The distribution root *is* the interpreter's prefix, see `StagedInterpreter`.
        prefix_rel_path=Path("."),
        version=version,
        pruned=pruned,
        sourceless=sourceless,
        size_bytes=_tree_size(dest_bin.parent, dest_lib),
        compile_failures=compile_failures,
        native_deps=native_deps,
    )
    _logger.info("Staged interpreter into %s: %s", dist_root, staged.render())
    return staged
