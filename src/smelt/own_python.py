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

*Tailoring* (= choosing the interpreter's contents from what the application needs,
rather than shipping all of it) works off three sets: the *closure* (= the modules an
entrypoint needs at runtime, computed by `smelt.dist`), the *bootstrap set* (= what
the interpreter imports before any application code runs, see `bootstrap_modules`),
and the *Minimal Viable Stdlib* (= what is kept whatever the closure says, see
`MINIMAL_VIABLE_STDLIB`). Their union is the *keep-set* (= everything the staged
interpreter is allowed to contain); anything outside it is pruned.

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

import json
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Final, Iterable

from smelt.bytecode import compile_tree
from smelt.explorer import package_closure, package_directories
from smelt.native_deps import (
    ALWAYS_HOST_DLL_PREFIXES,
    LINUX_BASE_LIBC_DLL_PREFIXES,
    BundledNatives,
    collect_native_dependencies,
    is_supported_platform,
    set_rpath,
)
from smelt.process import call_command
from smelt.utils import (
    PathExists,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
    is_valid_import_path,
    path_exists,
)

if TYPE_CHECKING:
    # `metapython` is an optional extra (see `_ensure_metapython_installed`), so it is
    # imported lazily everywhere it is *used*. `Library` is only ever needed as an
    # annotation, and `from __future__ import annotations` keeps those unevaluated --
    # so this import costs nothing at runtime and still types `LIBRARY_MODULES`'
    # keys against the option names meta-python's `build.zig` actually accepts.
    from metapython.compile import Library

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
#: in behind it. Everything else in `lib-dynload` is kept wholesale *by this list*, on
#: the principle that an unused `.so` costs size while a missing one costs correctness
#: -- an `InterpreterRequirements` passed to `stage_interpreter` is what narrows it
#: down to what the application actually needs (see `resolve_requirements`).
#:
#: `tkinter` stays in here even when requirements *are* given, unlike every other
#: name: the closure-driven pruning below would keep it for an application that
#: imports it, and that is exactly the one case mode B cannot honour -- the absolute
#: `DT_NEEDED` paths above mean such a folder would reach outside itself for Tcl/Tk.
#: An application genuinely needing a GUI toolkit is not a mode `own` distribution,
#: and saying so at build time (the verification in `stage_interpreter` raises on it)
#: beats shipping a folder that only works on machines with Tk installed.
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

#: Which extension modules each toggleable meta-python library exists for, i.e. what
#: `-D<library>-linkage=off` takes away. Measured with `ldd` over a built
#: `lib-dynload` rather than read off documentation, so it describes *this* build.
#:
#: The pure-Python wrappers are listed alongside their extension module on purpose:
#: `sqlite3` without `_sqlite3` is an `ImportError` waiting to happen, so a closure
#: naming only the wrapper must still keep the library. `hashlib` is deliberately
#: *not* listed under `openssl`: `_md5`, `_sha1`, `_sha2`, `_sha3` and `_blake2` are
#: separate extension modules in this build (verified -- none of them are compiled
#: into `libpython`), so `hashlib` keeps working without OpenSSL; what an
#: `openssl=off` build loses is `ssl`, `_hashlib`, and `hashlib.new()` for the
#: OpenSSL-only algorithms.
#:
#: Three libraries meta-python knows about are absent here, each for its own reason:
#:
#: * `readline` and `ncurses` are already off in this build, so there is nothing to
#:   turn off -- and listing them would append `-Dreadline-linkage=off` to every
#:   tailored build, changing the cache key for no change in the output;
#: * `zlib` is not offered at all. It is statically linked by default, `_hashlib`,
#:   `_ssl` and `_tkinter` link it, and `zipimport` needs it for compressed archives
#:   -- so it is in `ALWAYS_KEEP` instead.
LIBRARY_MODULES: Final[dict[Library, tuple[str, ...]]] = {
    "openssl": ("_ssl", "ssl", "_hashlib"),
    "sqlite": ("_sqlite3", "sqlite3"),
    "libffi": ("_ctypes", "ctypes"),
    "bz2": ("_bz2", "bz2"),
    "lzma": ("_lzma", "lzma"),
    "tk": ("_tkinter", "tkinter", "idlelib", "turtle", "turtledemo"),
}


#: Standard library modules a tailored interpreter keeps whatever the closure says.
#:
#: A named set of standard library modules a distribution keeps regardless of what
#: discovery found, together with what disabling it would cost. See
#: `MINIMAL_VIABLE_STDLIB`.
@dataclass(frozen=True)
class StdlibGroup:
    """
    One group of the Minimal Viable Stdlib.

    `optional` is the whole point of grouping: a group is only worth being a group if
    a caller can meaningfully choose to drop it, which means the loss has to be
    statable in a sentence (`consequence`) and worth stating in bytes. Groups that
    nothing could sensibly run without stay `optional=False` -- naming them still
    documents *why* those modules are unprunable, but they are not offered as a knob.
    """

    name: str
    modules: tuple[str, ...]
    rationale: str
    optional: bool = False
    consequence: str = ""
    #: Measured size in the staged interpreter, for the groups where it decides
    #: whether the knob is worth having at all.
    approx_size_kb: int = 0

    def __post_init__(self) -> None:
        if self.optional and not self.consequence:
            raise ValueError(
                f"Stdlib group {self.name!r} is optional but does not say what "
                "dropping it costs. A knob whose consequence cannot be stated is a "
                "trap, not an option."
            )


#: The **Minimal Viable Stdlib**: what a shipped interpreter keeps whatever the
#: application's closure says, because the closure is a *lower* bound on what a
#: running Python needs. A module resolved by name at runtime, or imported only once
#: something has already gone wrong, is never in a closure -- and is needed exactly
#: when it is missing.
#:
#: Split into named groups so the reasoning is legible and so the one boundary worth
#: choosing can be chosen (see `StdlibGroup.optional`). Every group's membership
#: includes its own transitive imports, measured by importing the group's seeds in a
#: freshly built interpreter and reading `sys.modules` -- keeping `traceback` while
#: pruning `textwrap` would leave the safety net raising on its way to reporting an
#: error.
#:
#: Why the split stops here, in measured bytes: dropping `exception_handling` saves
#: ~110 KB and `interpreter_core` cannot be dropped at all, so neither earns a knob.
#: `international_hostnames` is the one that does -- verified separable, with a
#: consequence a caller can reason about -- though see its own note: it frees 28 KB
#: today and 1.1 MB once the explorer stops following deferred stdlib imports.
MINIMAL_VIABLE_STDLIB: Final[tuple[StdlibGroup, ...]] = (
    StdlibGroup(
        name="interpreter_core",
        rationale=(
            "The import system, path handling and their transitive imports: what runs "
            "before any application code exists to be discovered. `zipimport` needs "
            "`zlib` to read a compressed archive on `sys.path`; `runpy` is how a "
            "`dist.write_entrypoint_module` entrypoint given as a bare module path is "
            "launched. Most of these are frozen into the interpreter in this build, so "
            "keeping them costs almost nothing."
        ),
        modules=(
            "_collections_abc",
            "abc",
            "codecs",
            "collections",
            # `collections/abc.py` re-exports `_collections_abc` and is what everything
            # spells (`from collections.abc import Mapping`). It is 3 KB, and nothing
            # inside `collections` imports it -- only code outside does -- so pruning
            # inside the package cannot see it is needed.
            "collections.abc",
            "contextlib",
            "copyreg",
            "enum",
            "functools",
            "genericpath",
            "importlib",
            "io",
            "keyword",
            "operator",
            "os",
            "posixpath",
            "re",
            "reprlib",
            "runpy",
            "stat",
            "types",
            "zipimport",
            "zlib",
        ),
    ),
    StdlibGroup(
        name="exception_handling",
        rationale=(
            "Imported by the interpreter only *after* something has already failed, so "
            "never present in a closure and always needed when they are needed. "
            "Without them an application's first unhandled exception becomes a "
            "secondary ImportError raised while formatting the traceback, which is the "
            "worst possible moment to lose a diagnostic. `linecache` needs `tokenize` "
            "(hence `token`) and `traceback` needs `textwrap`."
        ),
        modules=("linecache", "textwrap", "token", "tokenize", "traceback", "warnings"),
        approx_size_kb=110,
    ),
    StdlibGroup(
        name="text_codecs",
        rationale=(
            "Codecs are looked up by name at runtime -- `encodings.cp1252` for a "
            "locale, never spelled out in any source file -- so a closure cannot "
            "contain them and pruning by closure breaks text handling on someone "
            "else's machine rather than on the build machine. Kept as a whole package. "
            "Note the CJK codecs are *not* covered: those need the `_codecs_cn`/`_hk`/"
            "`_iso2022`/`_jp`/`_kr`/`_tw` extension modules, which are pruned like any "
            "other unused `.so`."
        ),
        modules=("encodings",),
        approx_size_kb=764,
    ),
    StdlibGroup(
        name="international_hostnames",
        rationale=(
            "`encodings.idna` is what lets a non-ASCII hostname be encoded, and it is "
            "the only reason `stringprep` and 1.1 MB of `unicodedata` are kept -- "
            "neither appears in a closure unless the application imports them itself, "
            "in which case this group is redundant and harmless."
        ),
        modules=("stringprep", "unicodedata"),
        optional=True,
        consequence=(
            'Non-ASCII (internationalised) hostnames stop working: `str.encode("idna")` '
            "and anything resolving such a host through `socket`/`urllib` raise "
            "LookupError. Verified that every other codec, and `socket`/`urllib` "
            "themselves, are unaffected."
        ),
        approx_size_kb=1128,
    ),
)

#: Groups a caller may drop, by name.
OPTIONAL_STDLIB_GROUPS: Final[tuple[str, ...]] = tuple(
    group.name for group in MINIMAL_VIABLE_STDLIB if group.optional
)


def minimal_viable_stdlib(drop_groups: Iterable[str] = ()) -> frozenset[str]:
    """
    The Minimal Viable Stdlib, minus the groups named in `drop_groups`.

    Raises
    ------
    OwnPythonError
        If a name is not a group, or names a group that is not optional. Both are
        caller mistakes worth reporting rather than ignoring: silently keeping a group
        the caller asked to drop would misreport the distribution's contents, and
        silently dropping `interpreter_core` would produce a folder that cannot start.
    """
    dropped = list(drop_groups)
    by_name = {group.name: group for group in MINIMAL_VIABLE_STDLIB}
    for name in dropped:
        group = by_name.get(name)
        if group is None:
            raise OwnPythonError(
                f"Unknown standard library group {name!r}. Droppable groups: "
                f"{list(OPTIONAL_STDLIB_GROUPS)}."
            )
        if not group.optional:
            raise OwnPythonError(
                f"The standard library group {name!r} cannot be dropped: {group.rationale} "
                f"Droppable groups: {list(OPTIONAL_STDLIB_GROUPS)}."
            )
    return frozenset(
        module
        for group in MINIMAL_VIABLE_STDLIB
        if group.name not in dropped
        for module in group.modules
    )


#: Every module the Minimal Viable Stdlib keeps when nothing is dropped. Retained as a
#: name of its own because it is the default, and because the verification pass reports
#: against it.
ALWAYS_KEEP: Final[tuple[str, ...]] = tuple(sorted(minimal_viable_stdlib()))

#: Entries of a `lib/pythonX.Y` directory that are not importable modules, so the
#: closure has nothing to say about them and they are never pruned as one.
#: `lib-dynload` is pruned per-`.so` instead, and `site-packages` is where a
#: distribution's own payload would land if it were not kept in `app/`.
#: Standard library packages a tailored interpreter keeps **whole**, never dropping
#: individual submodules out of them, mapped to the reason.
#:
#: The counterpart to `MINIMAL_VIABLE_STDLIB` one level down: that names the modules a
#: closure structurally cannot contain, this names the *packages* whose interior a
#: closure cannot describe. Every entry resolves at least one of its own submodules
#: from a name computed at runtime -- a dotted string in a config file, a registry
#: keyed by codec or backend, a module-level `__getattr__` -- so no import statement
#: anywhere points at it and pruning by closure removes it.
#:
#: Named at whatever depth the dynamic behaviour actually lives at: `xml.sax` picks its
#: parser by name, while `xml.dom` and `xml.etree` are ordinary packages and together
#: are most of `xml`'s 1.5 MB, so making the whole of `xml` atomic would pay for one
#: module's habit with the other two.
ATOMIC_PACKAGES: Final[dict[str, str]] = {
    "encodings": (
        "codecs are looked up by name -- `encodings.cp1252` for a locale, never "
        "spelled out in any source (see the `text_codecs` group of "
        "MINIMAL_VIABLE_STDLIB)"
    ),
    "importlib": (
        "`importlib.readers` is imported by a function of `_bootstrap_external`, which "
        "is *frozen* into the interpreter and so has no source to read the import from "
        "at all -- the one blind spot no AST pass can cover"
    ),
    "logging": (
        "`logging.config.dictConfig`/`fileConfig` resolve handler classes from dotted "
        "strings in a config file (`logging.handlers.RotatingFileHandler`)"
    ),
    "multiprocessing": (
        "`multiprocessing.context` selects a `popen_*` module by start-method name, and "
        "a spawned child imports `spawn` and `resource_tracker` by name in a fresh "
        "interpreter"
    ),
    "xml.sax": "`make_parser()` imports each candidate parser module by name",
    "dbm": "`dbm.open`/`whichdb` import `dbm.gnu`, `dbm.ndbm` or `dbm.dumb` by name",
    "ctypes": "`ctypes.util` resolves its platform helpers dynamically",
    "unittest": "a module-level `__getattr__` loads `unittest.async_case` on first use",
    "concurrent": (
        "`concurrent.futures`' module-level `__getattr__` loads `.process`/`.thread` on first use"
    ),
    "sqlite3": "a module-level `__getattr__` loads `sqlite3.dbapi2` on first use",
    "zoneinfo": (
        "a module-level `__getattr__` picks the C or Python implementation, and the "
        "data comes from a `tzdata` package imported by name"
    ),
}


_NON_MODULE_STDLIB_ENTRIES: Final[tuple[str, ...]] = (
    "lib-dynload",
    "site-packages",
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


def own_python_cache_dir(
    target: str | None = None,
    *,
    debug: bool = False,
    disabled_libraries: Iterable[str] = (),
) -> Path:
    """
    Where `build_own_python` caches its build for `target`, build mode and library
    option set.

    Keyed on all three, because none of them produce interchangeable trees: a native
    and a musl build of the same CPython are not, a stripped and an unstripped one are
    not, and an `openssl=off` build is missing `_ssl.so` outright -- sharing one
    directory would make whichever ran first silently satisfy the others' cache check.

    The all-defaults configuration keeps the bare `native` (or `<target>`) name it has
    always had, rather than growing an "everything on" fingerprint. That is not
    cosmetic: it is what lets an interpreter cached before tailoring existed keep being
    reused, instead of turning every untailored build into a ten-minute rebuild.
    Disabled libraries are spelled out rather than hashed, so the directory says what
    it holds to whoever goes looking in `~/.cache/smelt/metapython`.
    """
    name = f"{target or 'native'}{'-debug' if debug else ''}"
    disabled = sorted(set(disabled_libraries))
    if disabled:
        name = f"{name}-without-{'-'.join(disabled)}"
    return _METAPYTHON_CACHE_DIR / name


def build_own_python(
    dest_dir: str | os.PathLike[str] | None = None,
    *,
    target: str | None = DEFAULT_OWN_PYTHON_TARGET,
    no_cache: bool = False,
    debug: bool = False,
    disabled_libraries: Iterable[str] = (),
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

    `disabled_libraries` names meta-python libraries to compile *without*
    (`-D<library>-linkage=off`), one of the two levers a tailored interpreter pulls
    (see `resolve_requirements`). It has to be a build-time option rather than a
    staging one because pruning `_ssl.so` out of a staged tree does not stop
    `libssl`/`libcrypto` from being bundled behind it -- only never linking them does.
    The cost is that changing the set changes the cache key, i.e. a real CPython build
    (minutes) per distinct option set. Names outside `LIBRARY_MODULES` are refused
    rather than passed through, since `zig build` would reject them anyway and it is
    cheaper to hear about a typo now.

    Every other entry of `libraries` is left at meta-python's own defaults
    (`zlib`/`openssl`/`libffi` static, the rest dynamic or off). If a static library
    build fails, the known-good fallback set is `zlib` static, `openssl`/`libffi`
    dynamic and `readline`/`ncurses` off: the `allyourcodebase` `openssl`/`libffi`
    packages expose no PIC toggle the way zlib's does, so their static archives can
    fail to link into the shared extension modules that need them, and there is no
    musl-targeted readline/ncurses to link against at all.

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

    disabled = set(disabled_libraries)
    unknown = disabled - set(LIBRARY_MODULES)
    if unknown:
        raise OwnPythonError(
            f"Cannot disable {sorted(unknown)} in the interpreter build: not a "
            f"toggleable library. Known ones: {sorted(LIBRARY_MODULES)}."
        )
    dest = (
        Path(dest_dir)
        if dest_dir is not None
        else own_python_cache_dir(target, debug=debug, disabled_libraries=disabled)
    )
    bin_path = dest / INTERPRETER_REL_PATH
    if not no_cache and bin_path.exists():
        _logger.info("Reusing the interpreter already built at %s", dest)
        return assert_path_exists(dest)

    options = BuildOptions(
        target=target,
        # Iterated over `LIBRARY_MODULES` rather than over `disabled` itself, so the
        # keys carry meta-python's own `Library` type instead of a bare `str`.
        libraries={library: Linkage.OFF for library in LIBRARY_MODULES if library in disabled},
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


def _probe_interpreter(prefix: PathExists, script: str, what: str) -> str:
    """
    Runs `script` under the interpreter at `prefix` and returns its last stdout line.

    `-I`: the answer has to be the interpreter's own, not one steered by a
    `PYTHONPATH` or a `sitecustomize` inherited from whatever environment smelt runs
    in. `-S` on top of that, because `site` is exactly one of the things a tailored
    interpreter may end up without.
    """
    executable = prefix / INTERPRETER_REL_PATH
    if not path_exists(executable):
        raise OwnPythonError(f"No interpreter at {executable}.")
    cmd_trace = call_command(str(executable), "-I", "-S", "-c", script)
    if cmd_trace.exit_code != 0 or not cmd_trace.stdout:
        raise OwnPythonError(
            f"Could not read {what} from the interpreter at {executable} "
            f"(exit code {cmd_trace.exit_code}): {' '.join(cmd_trace.stderr)}"
        )
    return cmd_trace.stdout[-1]


def _decode_module_names(answer: str, executable: Path, what: str) -> frozenset[str]:
    """
    Reads back a JSON list of module names one of the probes above printed.
    """
    try:
        names = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise OwnPythonError(
            f"The interpreter at {executable} reported an unreadable {what}: {answer!r}"
        ) from exc
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise OwnPythonError(
            f"The interpreter at {executable} reported an unreadable {what}: {answer!r}"
        )
    return frozenset(name for name in names if isinstance(name, str))


def bootstrap_modules(prefix: PathExists) -> frozenset[str]:
    """
    The modules the interpreter at `prefix` imports before anything else, i.e. what it
    has already loaded by the time application code gets a say.

    Asked of the interpreter rather than hardcoded, because the set is
    version-dependent (CPython keeps moving startup modules in and out of the frozen
    set), and getting it wrong prunes something the interpreter needs to start at all
    -- a failure with no Python-level error message to read.

    Most of what comes back is builtin or frozen and therefore unprunable anyway; the
    file-backed ones on 3.12 are `abc`, `codecs`, `io` and the `encodings` package.

    The probe imports nothing at all and reports whitespace-joined names rather than
    JSON, which is not fussiness: `import json` alone adds `json`, `re`, `enum`,
    `keyword` and a dozen more to `sys.modules`, so a probe using it would report its
    own imports as the interpreter's startup set and quietly pin them forever.
    `__main__` is dropped for the same reason -- it is the probe itself.
    """
    answer = _probe_interpreter(
        prefix,
        "import sys; print(' '.join(sorted(sys.modules)))",
        "its bootstrap module set",
    )
    names = frozenset(answer.split()) - {"__main__"}
    if not names:
        raise OwnPythonError(
            f"The interpreter at {prefix / INTERPRETER_REL_PATH} reported an empty "
            f"bootstrap module set: {answer!r}"
        )
    return names


def _interpreter_provided_modules(prefix: PathExists) -> frozenset[str]:
    """
    The modules the interpreter at `prefix` resolves with no file in its standard
    library directory at all: the builtins compiled into it, and the frozen modules
    embedded in it.

    Needed by the verification in `stage_interpreter`: `sys`, `_thread` and friends
    are perfectly importable in a staged tree that has no file for them, so a check
    looking only at what is on disk would report them missing.
    """
    answer = _probe_interpreter(
        prefix,
        "import json, sys\n"
        "from importlib.machinery import FrozenImporter\n"
        "frozen = [n for n in sys.stdlib_module_names if FrozenImporter.find_spec(n)]\n"
        "print(json.dumps(sorted(set(sys.builtin_module_names) | set(frozen))))",
        "its builtin and frozen modules",
    )
    return _decode_module_names(
        answer, prefix / INTERPRETER_REL_PATH, "builtin and frozen module set"
    )


def _import_prefixes(import_path: str) -> Iterable[str]:
    """
    Every package `import_path` lives under, itself included: importing `a.b.c` also
    imports `a` and `a.b`, so keeping the former means keeping the latter.
    """
    parts = import_path.split(".")
    for depth in range(1, len(parts) + 1):
        yield ".".join(parts[:depth])


@dataclass(frozen=True)
class InterpreterRequirements:
    """
    What an application needs of the interpreter shipped with it: which standard
    library modules to keep, and which libraries the interpreter need not be compiled
    against at all.

    `keep_modules` is a *lower* bound on what the application imports, widened by
    `ALWAYS_KEEP` and by the interpreter's own bootstrap set. `forced` records what the
    caller asked for by name (`include-modules`), so the manifest can distinguish "the
    closure found this" from "someone had to say so".
    """

    keep_modules: frozenset[str]
    disabled_libraries: frozenset[str]
    forced: frozenset[str]
    dropped_stdlib_groups: frozenset[str] = frozenset()

    @property
    def keep_top_levels(self) -> frozenset[str]:
        """
        The top-level names of `keep_modules`, i.e. which standard library trees and
        `lib-dynload` extension modules survive at all.
        """
        return frozenset(name.partition(".")[0] for name in self.keep_modules)

    def needs(self, module: str) -> bool:
        """
        Whether `module` -- a top-level standard library name -- must be kept.
        """
        return module in self.keep_top_levels


def plan_disabled_libraries(
    stdlib_modules: Iterable[str],
    *,
    include_modules: Iterable[str] = (),
) -> frozenset[str]:
    """
    Which of `LIBRARY_MODULES`' libraries no module in the closure needs, i.e. which
    ones the interpreter can be compiled without.

    Split out of `resolve_requirements` for one reason: the build options have to be
    known *before* the interpreter exists, while `resolve_requirements` needs a built
    prefix to ask for its bootstrap set. The two agree by construction -- the
    bootstrap set holds no library-backed module (nothing in `LIBRARY_MODULES` is
    imported at startup), and `resolve_requirements` asserts as much rather than
    trusting it.
    """
    named = {
        prefix
        for module in (*stdlib_modules, *include_modules, *ALWAYS_KEEP)
        for prefix in _import_prefixes(module)
    }
    return frozenset(
        library for library, modules in LIBRARY_MODULES.items() if not named.intersection(modules)
    )


def _widen_kept_packages(keep: frozenset[str]) -> frozenset[str]:
    """
    Adds, for every package `keep` reaches into, the submodules that package can reach
    from the members already kept -- following deferred imports, which the closure
    itself does not (see `explorer.package_closure`).

    Pruning inside a package is where that difference bites. Dropping `asyncio` because
    only a function body imports it saves 600 KB and is the whole point; dropping
    `email.parser` because only `email.message_from_string()` imports it saves 20 KB and
    breaks `email.message_from_string()`. So the closure decides which packages ship,
    and this decides what a shipped package keeps.

    `ATOMIC_PACKAGES` are skipped: nothing is pruned inside them, so there is nothing to
    widen for.
    """
    packages = {
        name
        for name in keep
        if name not in ATOMIC_PACKAGES
        and is_valid_import_path(name)
        and package_directories(assert_is_valid_import_path(name))
    }
    widened = set(keep)
    for package in sorted(packages):
        # Only the outermost packages need walking: `package_closure` is transitive, so
        # walking `xml` already covers `xml.etree`.
        if any(package.startswith(f"{parent}.") for parent in packages if parent != package):
            continue
        seeds = [
            assert_is_valid_import_path(name)
            for name in keep
            if name == package or name.startswith(f"{package}.")
        ]
        widened.update(package_closure(assert_is_valid_import_path(package), seeds))
    return frozenset(widened)


def resolve_requirements(
    stdlib_modules: Iterable[str],
    prefix: PathExists,
    *,
    include_modules: Iterable[str] = (),
    drop_stdlib_groups: Iterable[str] = (),
) -> InterpreterRequirements:
    """
    Turns the standard library modules `smelt.dist`'s closure reached into a decision
    about the interpreter at `prefix`: what to keep in it, and what not to build it
    with.

    `stdlib_modules` is the closure's own `ResolvedModule.is_stdlib` subset, dotted
    names included. Three things are unioned onto it, and each covers a different way
    the closure can be short:

    * the **Minimal Viable Stdlib** (`minimal_viable_stdlib`) -- modules resolved by
      name at runtime, or reached only once something has already failed, so never
      present in any closure. `drop_stdlib_groups` names the optional groups of it the
      caller is willing to do without (`OPTIONAL_STDLIB_GROUPS`);
    * `bootstrap_modules(prefix)` -- what the interpreter loads before application
      code exists to be discovered;
    * `include_modules` -- what the caller had to name by hand, and the escape hatch
      when discovery misses something. They are kept whether or not they are standard
      library names: deciding that here would mean second-guessing the caller.

    A library is disabled only when *no* kept module appears in its `LIBRARY_MODULES`
    entry -- the conservative direction, since keeping an unused library costs bytes
    while dropping a used one costs an `ImportError` on someone else's machine.

    Deliberately does no filesystem work of its own beyond the one probe: the decision
    is what needs to be inspectable and testable, and the pruning it drives lives in
    `stage_interpreter`.
    """
    forced = frozenset(include_modules)
    bootstrap = bootstrap_modules(prefix)
    dropped = tuple(drop_stdlib_groups)
    mandatory = minimal_viable_stdlib(dropped)
    keep = frozenset(
        prefix_name
        for module in (*stdlib_modules, *forced, *mandatory, *bootstrap)
        for prefix_name in _import_prefixes(module)
    )
    keep = _widen_kept_packages(keep)
    disabled = plan_disabled_libraries(stdlib_modules, include_modules=forced)
    # Cannot happen with any CPython this supports -- nothing in `LIBRARY_MODULES` is
    # imported at startup -- but by the time this runs the interpreter is already
    # built, so a silent disagreement would ship a distribution whose `keep_modules`
    # names a module its own interpreter has no `.so` for.
    unexpected = {library for library in disabled if keep.intersection(LIBRARY_MODULES[library])}
    if unexpected:
        raise OwnPythonError(
            f"The interpreter at {prefix} was built without {sorted(unexpected)}, but "
            "its own bootstrap module set needs them. This is a smelt bug: the build "
            "options are decided by `plan_disabled_libraries` before the interpreter "
            "exists, and that decision has to hold once it does."
        )
    return InterpreterRequirements(
        keep_modules=keep,
        disabled_libraries=disabled,
        forced=forced,
        dropped_stdlib_groups=frozenset(dropped),
    )


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
    #: Whether the contents follow the application's closure (`tailored`) or are the
    #: whole standard library and every extension module (`not tailored`).
    tailored: bool = False
    #: Libraries the interpreter was compiled without (`LIBRARY_MODULES` keys).
    disabled_libraries: list[str] = field(default_factory=list)
    #: Standard library trees dropped because the closure did not reach them.
    pruned_modules: list[str] = field(default_factory=list)
    #: Submodules dropped from *inside* a package that was itself kept, dotted. A
    #: finer decision than `pruned_modules` and a more delicate one -- see
    #: `_prune_to_requirements` and `ATOMIC_PACKAGES`.
    pruned_submodules: list[str] = field(default_factory=list)
    #: `lib-dynload` extension modules dropped for the same reason.
    pruned_extensions: list[str] = field(default_factory=list)
    #: Size of the interpreter tree just before closure-driven pruning, i.e. after the
    #: `prune` patterns and the sourceless compilation but before anything was dropped
    #: on the closure's account. Compared against `size_bytes` it is what tailoring
    #: saved -- with the caveat that `size_bytes` additionally includes the shared
    #: libraries bundled afterwards, which is where the larger half of the win is
    #: (a pruned `.so` also stops its libraries from being copied in).
    size_before_prune_bytes: int = 0
    #: Optional groups of the Minimal Viable Stdlib the caller chose to ship without
    #: (see `MINIMAL_VIABLE_STDLIB`). Recorded so the folder states what it gave up,
    #: rather than leaving a reader to infer it from what is missing.
    dropped_stdlib_groups: list[str] = field(default_factory=list)

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
            f"  contents:   {'tailored to the closure' if self.tailored else 'vanilla'}",
            f"  stdlib:     {'sourceless (.pyc only)' if self.sourceless else 'source kept'}",
            f"  pruned:     {', '.join(self.pruned) or 'nothing'}",
        ]
        if self.tailored:
            lines.extend(
                [
                    f"  libraries off:      {', '.join(self.disabled_libraries) or 'none'}",
                    f"  stdlib trees cut:   {len(self.pruned_modules)}"
                    + (f" ({', '.join(self.pruned_modules)})" if self.pruned_modules else ""),
                    f"  submodules cut:     {len(self.pruned_submodules)}"
                    + (f" ({', '.join(self.pruned_submodules)})" if self.pruned_submodules else ""),
                    f"  extensions cut:     {len(self.pruned_extensions)}"
                    + (f" ({', '.join(self.pruned_extensions)})" if self.pruned_extensions else ""),
                    f"  stdlib before cut:  {self.size_before_prune_bytes / 1e6:.1f} MB",
                ]
            )
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
            "size_before_prune_bytes": self.size_before_prune_bytes,
            "sourceless_stdlib": self.sourceless,
            "tailored": self.tailored,
            "pruned": self.pruned,
            "disabled_libraries": self.disabled_libraries,
            "pruned_modules": self.pruned_modules,
            "pruned_submodules": self.pruned_submodules,
            "pruned_extensions": self.pruned_extensions,
            "dropped_stdlib_groups": self.dropped_stdlib_groups,
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


def _dynload_dir(stdlib_dir: Path) -> Path:
    """
    The `lib-dynload` directory of a standard library tree, i.e. where the standard
    library's own extension modules live.
    """
    return stdlib_dir / "lib-dynload"


def _extension_module_name(entry: Path) -> str:
    """
    The module an extension file in `lib-dynload` provides:
    `_json.cpython-312-x86_64-linux-gnu.so` -> `_json`.
    """
    return entry.name.split(".")[0]


def _dynload_modules(stdlib_dir: Path) -> frozenset[str]:
    """
    Every module `lib-dynload` currently provides.
    """
    dynload = _dynload_dir(stdlib_dir)
    if not dynload.is_dir():
        return frozenset()
    return frozenset(
        _extension_module_name(entry) for entry in dynload.iterdir() if entry.is_file()
    )


def _stdlib_entry_module(entry: Path, *, nested: bool = False) -> str | None:
    """
    The module an entry of a standard library directory provides, or None if it is not
    one at all (`lib-dynload`, `config-*`, `LICENSE.txt`, a package's own `__init__`).

    `nested` is for entries *inside* a package, where a directory is only a subpackage
    if it carries an `__init__`: a package may hold data directories, and those belong
    to the package rather than being modules of their own.
    """
    if entry.name in _NON_MODULE_STDLIB_ENTRIES:
        return None
    if entry.is_dir():
        if nested and not _is_package_dir(entry):
            return None
        name = entry.name
    else:
        if entry.suffix not in (".py", ".pyc") or entry.stem == "__init__":
            return None
        name = entry.stem
    return name if name.isidentifier() else None


def _is_package_dir(entry: Path) -> bool:
    """
    Whether `entry` is an importable package, i.e. carries an `__init__` module. Both
    suffixes, since pruning runs after the sourceless pass on some builds and before it
    on others.
    """
    return any((entry / f"__init__{suffix}").is_file() for suffix in (".pyc", ".py"))


def _remove(entry: Path) -> None:
    """
    Deletes a standard library entry, directory or file.
    """
    if entry.is_dir():
        shutil.rmtree(entry)
    else:
        entry.unlink()


def _prune_package(directory: Path, package: str, keep: frozenset[str]) -> list[str]:
    """
    Drops the submodules of the package at `directory` that `keep` does not name, and
    returns their dotted names. Recurses into the subpackages that survive.

    Stops at an `ATOMIC_PACKAGES` entry: those resolve their own submodules from names
    computed at runtime, so no closure can describe their interior and pruning by one
    removes whatever the application had not happened to import yet.
    """
    pruned: list[str] = []
    for entry in sorted(directory.iterdir()):
        module = _stdlib_entry_module(entry, nested=True)
        if module is None:
            continue
        name = f"{package}.{module}"
        if name not in keep:
            _remove(entry)
            pruned.append(name)
        elif entry.is_dir() and name not in ATOMIC_PACKAGES:
            pruned.extend(_prune_package(entry, name, keep))
    return pruned


def _prune_to_requirements(
    stdlib_dir: Path, requirements: InterpreterRequirements
) -> tuple[list[str], list[str], list[str]]:
    """
    Drops everything in `stdlib_dir` the application does not need, and returns
    `(stdlib trees pruned, submodules pruned, extension modules pruned)`.

    Three passes over two differently stored halves of the standard library, decided
    identically: a name is kept when `requirements` names it and dropped otherwise.
    `lib-dynload` holds only top-level extension modules, so the first pass needs no
    recursion; the tree pass drops whole top-level packages and then goes *inside* the
    survivors (`_prune_package`).

    Going inside is only safe because two things stand behind it. `keep_modules` was
    widened for exactly this (`_widen_kept_packages`), so a submodule that a kept one
    imports from a function body -- `email.parser`, reached only through
    `email.message_from_string()` -- is named rather than inferred absent; and
    `ATOMIC_PACKAGES` carries the packages where even that is not enough.
    """
    keep = requirements.keep_modules
    top_levels = requirements.keep_top_levels
    dynload = _dynload_dir(stdlib_dir)
    pruned_extensions: list[str] = []
    if dynload.is_dir():
        for entry in sorted(dynload.iterdir()):
            if not entry.is_file():
                continue
            module = _extension_module_name(entry)
            if module in top_levels:
                continue
            entry.unlink()
            pruned_extensions.append(module)

    pruned_modules: list[str] = []
    pruned_submodules: list[str] = []
    for entry in sorted(stdlib_dir.iterdir()):
        module = _stdlib_entry_module(entry)
        if module is None:
            continue
        if module not in top_levels:
            _remove(entry)
            pruned_modules.append(module)
        elif entry.is_dir() and module not in ATOMIC_PACKAGES:
            pruned_submodules.extend(_prune_package(entry, module, keep))
    _logger.debug(
        "Tailoring dropped %d stdlib tree(s), %d submodule(s) and %d extension module(s) from %s",
        len(pruned_modules),
        len(pruned_submodules),
        len(pruned_extensions),
        stdlib_dir,
    )
    return (
        sorted(set(pruned_modules)),
        sorted(set(pruned_submodules)),
        sorted(set(pruned_extensions)),
    )


def _resolvable_modules(
    stdlib_dir: Path, names: Iterable[str], provided: frozenset[str]
) -> frozenset[str]:
    """
    Of `names`, the ones the interpreter would actually be able to import from a
    standard library tree in the state `stdlib_dir` is in: as a `.pyc` (or a `.py`
    before the sourceless pass), as a package `__init__`, as a `lib-dynload`
    extension module, or as one of `provided` -- the builtins and frozen modules that
    need no file at all.
    """
    dynload = _dynload_modules(stdlib_dir)
    resolvable: set[str] = set()
    for name in names:
        if name in provided:
            resolvable.add(name)
            continue
        parts = name.split(".")
        base = stdlib_dir.joinpath(*parts)
        if any((base / f"__init__{suffix}").is_file() for suffix in (".pyc", ".py")):
            resolvable.add(name)
        elif any((base.parent / f"{parts[-1]}{suffix}").is_file() for suffix in (".pyc", ".py")):
            resolvable.add(name)
        elif len(parts) == 1 and name in dynload:
            resolvable.add(name)
    return frozenset(resolvable)


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
    requirements: InterpreterRequirements | None = None,
) -> StagedInterpreter:
    """
    Copies the interpreter built at `built_prefix` into `dist_root` and returns what
    landed there.

    It goes at the distribution *root*, not into the payload directory: `bin/` and
    `lib/` are the interpreter's own prefix, not application code, and the whole point
    of keeping the application in its own subfolder is that the root stays free for
    exactly this (see `smelt.dist`'s module docstring). What is copied is
    `bin/python`, `lib/libpython*.so` and the whole `lib/pythonX.Y` tree.

    Then, in order:

    * `prune` removes stdlib entries by name (see `DEFAULT_STDLIB_PRUNE`);
    * `sourceless` compiles the remaining stdlib to `.pyc` and deletes the `.py` it
      compiled. Per-file failures are recorded, not raised -- after pruning `test`
      there are none, but a partially built interpreter is not worth aborting a
      distribution over. Only sources that actually produced a `.pyc` are deleted, so
      a module that failed to compile keeps working from its source;
    * `requirements`, where given, drops every standard library tree and every
      `lib-dynload` extension module the application does not need (see
      `resolve_requirements`). Left as None, nothing else is dropped: the whole
      standard library and every extension module ship, which is what a distribution
      built without tailoring gets;
    * the landmark assertion: `lib/pythonX.Y/os.pyc` (or `os.py`) must exist, or the
      staged interpreter cannot find its own standard library at all. This is checked
      here, loudly, rather than discovered by the user on the target machine;
    * the tailoring verification, where `requirements` was given: every module in
      `keep_modules` that the tree *could* resolve before any pruning must still be
      resolvable after it. Anything missing raises rather than warns -- a warning
      here buys a folder that fails on the target machine, on an import, with no way
      back to this decision;
    * `bundle_dependencies` copies the shared libraries the interpreter's own
      extension modules need into `lib/` and rewrites their RPATHs, so `ssl`,
      `sqlite3`, `lzma` and friends resolve inside the folder instead of against
      whatever the target happens to have installed. libc and its loader are
      excluded and stay host-supplied (see `INTERPRETER_HOST_DLL_PREFIXES`). It runs
      *after* pruning on purpose: an extension module that is gone does not get its
      libraries copied in behind it, which is where most of tailoring's win is.
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

    # Measured on the untouched copy, so the verification below covers the `prune`
    # patterns too and not just the closure-driven pass: a pattern that happens to
    # match a module the application needs is exactly as fatal, and rather more likely
    # to go unnoticed.
    expected: frozenset[str] = frozenset()
    provided: frozenset[str] = frozenset()
    if requirements is not None:
        provided = _interpreter_provided_modules(built_prefix)
        expected = _resolvable_modules(dest_stdlib, requirements.keep_modules, provided)

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

    size_before_prune = 0
    pruned_modules: list[str] = []
    pruned_submodules: list[str] = []
    pruned_extensions: list[str] = []
    if requirements is not None:
        # Measured here rather than on the built prefix: this is the tree as it would
        # have shipped untailored (default prunes applied, stdlib already sourceless),
        # so the difference against `size_bytes` is what tailoring itself accounts for
        # -- minus the libraries a dropped `.so` no longer pulls in, which land later.
        size_before_prune = _tree_size(dest_bin.parent, dest_lib)
        pruned_modules, pruned_submodules, pruned_extensions = _prune_to_requirements(
            dest_stdlib, requirements
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

    if requirements is not None:
        still_there = _resolvable_modules(dest_stdlib, expected, provided)
        missing = sorted(expected - still_there)
        if missing:
            raise OwnPythonError(
                f"{len(missing)} module(s) the application needs were removed from the "
                f"staged interpreter: {missing}. They were present in the interpreter "
                "built at "
                f"{built_prefix} and are in this distribution's keep set -- either "
                f"because discovery reached them, because they are in ALWAYS_KEEP, or "
                f"because they were named explicitly ({sorted(requirements.forced)}). "
                "Shipping the folder anyway would produce an ImportError on the target "
                f"machine. Check the prune patterns ({list(prune)}); if one of these "
                "is a module a mode 'own' distribution cannot carry (tkinter is, its "
                "Tcl/Tk libraries being named by absolute path), drop it from the "
                "closure with `--exclude-module`, or build with "
                "`--no-tailor-interpreter` to ship the whole standard library."
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
        tailored=requirements is not None,
        disabled_libraries=sorted(requirements.disabled_libraries) if requirements else [],
        pruned_modules=pruned_modules,
        pruned_submodules=pruned_submodules,
        pruned_extensions=pruned_extensions,
        dropped_stdlib_groups=sorted(
            requirements.dropped_stdlib_groups if requirements is not None else ()
        ),
        size_before_prune_bytes=size_before_prune,
    )
    _logger.info("Staged interpreter into %s: %s", dist_root, staged.render())
    return staged
