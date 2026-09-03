"""
Wrapper on top of nuitka to compile a Python script into a standalone executable.

Currently nuitka is called as a subprocess, as it would be from `python -m nuitka`.
Options are passed as CLI arguments.

This is the simple option as nuitka is not really designed for library use: some of the business logic
is run on import, a few critical components are handled global variables, so there a some major drawbacks to
trying to import the code and call directly.
This might be changed later.

Sharing the Nuitka runtime across modules
------------------------------------------
Unlike mypyc/cython, Nuitka's main mode of operation is *whole-program*: it turns an
entrypoint into a single artifact that embeds Nuitka's large C "static runtime"
(compiled functions, generators, calling conventions, the constants system, ...).
Smelt also drives Nuitka per *module* (one native `.so` per Python module, like the
other backends). Done naively that re-embeds the whole static runtime into every
module `.so` — several megabytes of identical C code duplicated N times. So Smelt
builds that runtime **once** as `lib{RUNTIME_LIB_NAME}.so` and links every module
against it (resolved at load via an `$ORIGIN` rpath).

The one thing that makes the runtime not trivially shareable is Nuitka's **constants
blob**. Python constants (strings, numbers, tuples, interned names...) are live
`PyObject`s that cannot be emitted as C statics, so Nuitka serializes them into a
compact binary blob embedded in the artifact and materializes them at import time. The
blob is a whole-program structure: named sections, one universal section shared by all
modules plus one per module; a startup loader (`loadConstantsBlob`) fetches "the" blob
via `getConstantBlobData()` and unpacks a module's section by name.

That single-blob assumption breaks once modules are independent import units: each
module `.so` must keep its **own** blob (its literals + a copy of the universal
constants), and merging blobs would fuse the modules back into one unit. What is shared
is only *code*, not *data*. So Smelt splits the build:
  * the shared runtime carries the generic static runtime and the (identical) universal
    constants;
  * each module keeps its own blob and *passes it* into the runtime's loader, which is
    made blob-agnostic — `loadConstantsBlob` takes the blob as an argument instead of
    fetching a single global one, and each module's generated call sites are rewritten
    to pass their own `getConstantBlobData()`.
This keeps every module reading its own constants while the multi-megabyte machinery is
compiled once. See `build_nuitka_runtime` and `patch_module_constants_calls`.

@date: 11.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Final, Literal

from setuptools import Extension

from smelt.compiler import (
    PythonToolchain,
    ZigCompiler,
    build_mode_compile_flags,
    build_mode_link_flags,
    compile_executable,
    python_import_library_link_args,
)
from smelt.config import NuitkaModule
from smelt.context import create_context_if_enabled, get_context
from smelt.utils import GenericExtension, PathSolver, SmeltError, assert_path_exists

from .process import CommandContext, call_command

_logger = logging.getLogger(__name__)


def _describe_command_failure(cmd_trace: CommandContext, cmd: Iterable[str]) -> str:
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


NUITKA_ENTRYPOINT: Final[tuple[str, ...]] = (sys.executable, "-m", "nuitka")


def _is_importable(module_name: str) -> bool:
    """Checks whether `module_name` resolves via import machinery, without importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _find_module_location(module_name: str) -> tuple[Path, bool] | None:
    """
    Resolves `module_name` via import machinery to `(location, is_package)`, or None if
    unresolvable.

    `location` is the package's own directory when `is_package`, otherwise the module's
    file. Handles PEP 420 namespace packages: those have `spec.origin is None` and give
    their directory via `spec.submodule_search_locations` instead.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None:
        return None
    if spec.origin is not None:
        origin = Path(spec.origin)
        return (origin.parent, True) if origin.name == "__init__.py" else (origin, False)
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations))), True
    return None


def _runtime_link_libraries(*extra: str) -> list[str]:
    """
    System libraries needed to link the Nuitka runtime/module shared objects.
    `dl` (dlopen) and `z` (zlib) are POSIX system libraries with no Windows
    equivalent under that name, so only request them off Windows.
    """
    libraries = list(extra) + ["m"]
    if platform.system() != "Windows":
        libraries += ["dl", "z"]
    return libraries


type Stdout = Literal["stdout", "logger"]

# TODO: this should be built dynamically, obviously
NUITKA_MACROS = [
    ("_XOPEN_SOURCE", None),
    ("__NUITKA_NO_ASSERT__", None),
    # We embed the constants blob via INCBIN (see write_constants_incbin): it only
    # needs a small generated wrapper + the blob file, unlike _NUITKA_CONSTANTS_FROM_CODE
    # which expects a full C-array source that generate-c-only does not produce.
    ("_NUITKA_CONSTANTS_FROM_INCBIN", None),
    ("_NUITKA_FROZEN", 0),
    # TODO:
    # Note: seems that that one was NUITKA_MODULE_MODE
    # and was renamed around Nuitka 2.7.9 to _NUIKA_MODULE
    ("_NUITKA_MODULE_MODE", 1),
]

# Same as `NUITKA_MACROS`, but for an executable artifact (`compile_nuitka_entrypoint`)
# rather than a module `.so`. `_NUITKA_MODULE_MODE` gates real behavioral differences
# across Nuitka's runtime headers (GIL/threadstate access, allocator paths, which
# builtins.h definitions are even compiled in) between "loaded as an extension module by
# an already-running interpreter" and "starts its own interpreter" -- not a naming
# convention, so an executable's sources must never be compiled with the module value.
NUITKA_EXE_MACROS: list[tuple[str, str | int | None]] = [
    ("_XOPEN_SOURCE", None),
    ("__NUITKA_NO_ASSERT__", None),
    ("_NUITKA_CONSTANTS_FROM_INCBIN", None),
    ("_NUITKA_FROZEN", 0),
    ("_NUITKA_MODULE_MODE", 0),
    # Mirrors Nuitka's own Scons `exe_mode` flag (`SconsUtils.setupSconsEnvironment`):
    # bare `-D`, like Scons' own `CPPDEFINES=["_NUITKA_EXE_MODE"]`, so it defaults to `1`.
    # Several static-runtime sources (`CompiledCodeHelpers.c`'s
    # `getPythonProgramDirectoryObject`, `MetaPathBasedLoader.c`, `HelpersConstantsBlob.c`,
    # ...) branch on this specifically -- `_NUITKA_MODULE_MODE=0` alone leaves them in an
    # in-between "neither module nor exe" state that doesn't compile.
    ("_NUITKA_EXE_MODE", None),
]

# Note: no `-fvisibility=hidden`. Nuitka's static runtime helpers are declared as
# plain `extern` (e.g. nuitka/calling.h) with no per-symbol export attribute, so a
# blanket hidden visibility is the *only* thing that would hide them. Once the runtime
# is factored into a shared `.so` (upcoming), the module `.so`s must resolve those
# symbols at load, which requires them exported. `PyInit_*` is force-exported by
# `PyMODINIT_FUNC` regardless, so module loading is unaffected either way.
NUITKA_MINIMAL_FLAGS: Final[tuple[str, ...]] = (
    "-std=c11",
    "-fwrapv",
    "-pipe",
    "-w",
    "-Wno-unused-but-set-variable",
    "-O3",
    "-fPIC",
)


# Base name of the shared Nuitka runtime library, i.e. `lib{RUNTIME_LIB_NAME}.so`.
# Modules link against it via `-l{RUNTIME_LIB_NAME}`.
RUNTIME_LIB_NAME: Final[str] = "__nuitka_runtime"


@dataclass
class NuitkaBuildContext:
    """
    State shared across the multiple `nuitkaify_module` calls of a single build.

    Compiling several modules independently would otherwise re-embed the identical
    Nuitka static runtime into each `.so`. To share it, we must defer the runtime
    compilation and do it once for all modules; this object is the persistence that
    lets `nuitkaify_module` accumulate what that single runtime build needs.

    `runtime_sources` is a set (so contributions from different modules deduplicate
    safely) of the canonical Nuitka static runtime C sources required so far.

    Registered under the name "nuitka" in the generic `GlobalContext`, but kept
    distinct from it: the `GlobalContext` tracks run-command traces globally, whereas
    this only accumulates the state the shared runtime build needs.
    """

    runtime_sources: set[str] = field(default_factory=set)

    def render(self) -> str:
        lines = [f"Nuitka runtime sources ({len(self.runtime_sources)}):"]
        lines.extend(f"  {src}" for src in sorted(self.runtime_sources))
        return "\n".join(lines)


def locate_nuitka_headers() -> list[Path]:
    header_folders: list[Path] = []
    import nuitka

    nuitka_root = Path(nuitka.__file__).parent
    header_folders.append(nuitka_root / "build" / "static_src")
    header_folders.append(nuitka_root / "build" / "inline_copy" / "libbacktrace")
    header_folders.append(nuitka_root / "build" / "inline_copy" / "zlib")
    header_folders.append(nuitka_root / "build" / "include")

    return header_folders


def iterate_nuitka_module_sources(build_folder: str) -> Iterator[Path]:
    """
    Iterates over the per-module generated C sources in `build_folder`.

    These are specific to the compiled module (module code, constants blob wrapper,
    loader, helpers) and must be compiled into the module's own `.so`.
    """
    root = Path(build_folder)
    for f in os.listdir(build_folder):
        if f.endswith(".c"):
            yield root / f


# Static runtime C sources a full Nuitka module build copies into the build folder but
# `--generate-c-only` does not (mirrors `provideStaticSourceFilesBackend` for module
# mode; exe/dll additionally pull `MainProgram.c`, which we never build). Compiling
# `CompiledFunctionType.c` pulls in the whole static runtime via its `#include` tree.
# Used by the standalone path (`use_runtime=False`) to embed the runtime into the module;
# the shared-runtime path builds the runtime separately (see `build_nuitka_runtime`).
NUITKA_MODULE_STATIC_SOURCES: Final[tuple[str, ...]] = ("CompiledFunctionType.c",)


def provide_nuitka_static_sources(
    build_folder: str,
    sources: Iterable[str] = NUITKA_MODULE_STATIC_SOURCES,
) -> None:
    """
    Copies the static runtime C sources that `--generate-c-only` omits into the build
    folder's `static_src`. Used by the standalone build path, which compiles the runtime
    straight into the module (or, for other artifact kinds, executable) `.so`.

    `sources` defaults to the module-mode list; other artifact kinds (e.g. an
    executable, which also needs `MainProgram.c`) pass their own.
    """
    src_dir = _nuitka_root() / "build" / "static_src"
    dst_dir = Path(build_folder) / "static_src"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for filename in sources:
        shutil.copyfile(src_dir / filename, dst_dir / filename)


# `MainProgram.c` (the `main()` entry, driving interpreter startup and running the
# `__main__` module) is specific to an executable artifact, on top of whatever the
# static runtime is provided by (embedded here via `NUITKA_MODULE_STATIC_SOURCES`, or a
# separately-linked shared runtime -- either way `MainProgram.c` itself is never part of
# that shared runtime, since it's the one piece unique to each executable).
NUITKA_EXE_STATIC_SOURCES: Final[tuple[str, ...]] = (*NUITKA_MODULE_STATIC_SOURCES, "MainProgram.c")

# `MainProgram.c`'s `#ifndef __IDE_ONLY__` branch expects these from a Scons-generated
# `build_definitions.h` (see `SconsUtils.createDefinitionsFile`); module builds never
# compile `MainProgram.c` so `nuitkaify_module` blanks the file instead. All defaulted to
# "no special flag" (plain `python script.py` startup) and the entry module named
# `__main__`, non-package -- matches what `--generate-c-only` on a plain script produces.
_EXE_BUILD_DEFINITIONS: Final[str] = """\
#define SYSFLAG_PY3K_WARNING 0
#define SYSFLAG_DIVISION_WARNING 0
#define SYSFLAG_UNICODE 0
#define SYSFLAG_OPTIMIZE 0
#define SYSFLAG_NO_SITE 0
#define SYSFLAG_VERBOSE 0
#define SYSFLAG_BYTES_WARNING 0
#define SYSFLAG_UTF8 0
#define SYSFLAG_UNBUFFERED 0
#define SYSFLAG_DONTWRITEBYTECODE 0
#define NUITKA_MAIN_MODULE_NAME "__main__"
#define NUITKA_MAIN_IS_PACKAGE_BOOL 0
"""


def _c_string_literal(value: str) -> str:
    """Renders `value` as a C string literal (mirrors Nuitka's own `SconsUtils.makeCLiteral`)."""
    return '"' + value.replace("\\", r"\\").replace('"', r"\"") + '"'


def write_exe_build_definitions(build_folder: str, *, standalone: bool = False) -> None:
    """
    Writes the `build_definitions.h` an executable build's `MainProgram.c` needs.

    When not `standalone`, also defines `PYTHON_HOME_PATH` to the real interpreter
    prefix if compiling against a relocatable Python install (a venv on top of a
    `python-build-standalone` / Anaconda / WinPython interpreter, as
    `nuitka.PythonFlavors.isUninstalledPython` detects -- exactly what Nuitka's own Scons
    build does in `Backend.scons:createBuildDefinitionsFile`). Those installs have no
    fixed system-wide location for their stdlib to be found at by CPython's default
    runtime search, so skipping this leaves the produced binary unable to import even
    `encodings` at startup.

    `standalone` skips it: `MainProgram.c`'s `restoreStandaloneEnvironment` applies
    `PYTHON_HOME_PATH` unconditionally, late, *after* the `_NUITKA_STANDALONE_MODE` block
    already pointed `sys.prefix`/`sys.path` at the executable's own directory -- baking in
    the original install's absolute path there would silently reintroduce exactly the
    system dependency standalone mode exists to remove (verified empirically: with it
    set, a standalone build's `import ssl` "worked" only because the original install
    was still sitting at that exact path, not because `_ssl.so` was actually bundled).
    """
    contents = _EXE_BUILD_DEFINITIONS
    if not standalone:
        from nuitka.PythonFlavors import isUninstalledPython
        from nuitka.PythonVersions import getSystemPrefixPath

        if isUninstalledPython():
            contents += f"#define PYTHON_HOME_PATH {_c_string_literal(getSystemPrefixPath())}\n"
    (Path(build_folder) / "build_definitions.h").write_text(contents)


def iterate_nuitka_static_sources(build_folder: str) -> Iterator[Path]:
    """Iterates the static runtime C sources provided into `build_folder`/static_src."""
    static_src = Path(build_folder) / "static_src"
    if not static_src.exists():
        return
    for f in os.listdir(static_src):
        if f.endswith(".c"):
            yield static_src / f


# Compiling this single Nuitka static-runtime translation unit pulls in the whole
# static runtime via its `#include` tree, so it is all we compile into the shared lib.
NUITKA_RUNTIME_AGGREGATION: Final[str] = "CompiledFunctionType.c"

# Files in that `#include` tree we must ship *our own* copies of so the aggregation
# resolves ours (quoted includes look in the including file's directory first):
# the aggregation root and the intermediate that pulls in the blob loader are copied
# verbatim, while the blob loader itself is rewritten (see `_patched_blob_loader`).
_RUNTIME_VERBATIM_SOURCES: Final[tuple[str, ...]] = (
    NUITKA_RUNTIME_AGGREGATION,
    "CompiledCodeHelpers.c",
)
_BLOB_LOADER_SOURCE: Final[str] = "HelpersConstantsBlob.c"


def _nuitka_root() -> Path:
    import nuitka

    return Path(nuitka.__file__).parent


def _runtime_source_paths() -> list[str]:
    """Logical source(s) the shared runtime is built from (the aggregation root)."""
    return [str(_nuitka_root() / "build" / "static_src" / NUITKA_RUNTIME_AGGREGATION)]


def _apply_replacements(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    """Applies each (old, new) replacement, asserting every one actually matched.

    The patched C is coupled to Nuitka internals; a failed replacement means a Nuitka
    version drift we must notice rather than silently produce a broken runtime.
    """
    for old, new in replacements:
        assert old in text, f"Expected Nuitka source fragment not found (version drift?): {old!r}"
        text = text.replace(old, new)
    return text


def _patched_blob_loader(nuitka_static_src: Path) -> str:
    """
    Nuitka's `HelpersConstantsBlob.c`, rewritten so `loadConstantsBlob` takes the blob
    as a parameter instead of fetching the single per-artifact `getConstantBlobData()`.

    This is what lets the loader live in the *shared* runtime while each module reads
    its *own* blob (passed in by the rewritten call sites, see
    `patch_module_constants_calls`).
    """
    src = (nuitka_static_src / _BLOB_LOADER_SOURCE).read_text()
    return _apply_replacements(
        src,
        [
            (
                "int loadConstantsBlob(PyThreadState *tstate, PyObject **output, char const *name) {",
                "int loadConstantsBlob(PyThreadState *tstate, PyObject **output, "
                "char const *name, unsigned char const *blob_data) {",
            ),
            (
                "        constant_bin = getConstantBlobData();",
                "        constant_bin = blob_data;",
            ),
            (
                "    unsigned char const *w = constant_bin;",
                "    unsigned char const *w = blob_data;",
            ),
        ],
    )


def _shadow_constants_header(nuitka_include: Path) -> str:
    """
    Nuitka's `nuitka/constants_blob.h` with the `loadConstantsBlob` prototype updated to
    the blob-parameter signature and a `getConstantBlobData` declaration added, so both
    the shared runtime and the modules compile against the patched contract.
    """
    src = (nuitka_include / "nuitka" / "constants_blob.h").read_text()
    return _apply_replacements(
        src,
        [
            (
                "extern int loadConstantsBlob(PyThreadState *tstate, PyObject **, char const *name);",
                "extern int loadConstantsBlob(PyThreadState *tstate, PyObject **, "
                "char const *name, unsigned char const *blob_data);\n"
                "extern unsigned char const *getConstantBlobData(void);",
            ),
        ],
    )


def _write_shadow_include(dest_dir: Path) -> Path:
    """
    Writes the shadowing `nuitka/constants_blob.h` into `dest_dir` and returns it, for
    use as a priority include dir (before Nuitka's own include) so the patched
    `loadConstantsBlob` prototype is the one seen at compile time.
    """
    shadow_nuitka = dest_dir / "nuitka"
    shadow_nuitka.mkdir(parents=True, exist_ok=True)
    header = _shadow_constants_header(_nuitka_root() / "build" / "include")
    (shadow_nuitka / "constants_blob.h").write_text(header)
    return dest_dir


_LOAD_CONSTANTS_CALL: Final[re.Pattern[str]] = re.compile(r"loadConstantsBlob\(tstate,.*?\);")


def patch_module_constants_calls(build_folder: str) -> None:
    """
    Rewrites the module's generated `loadConstantsBlob(tstate, out, name)` calls to
    `loadConstantsBlob(tstate, out, name, getConstantBlobData())`.

    Each module thus passes its *own* blob into the shared runtime's blob-agnostic
    loader (see `_patched_blob_loader`), so it reads its own constants while the loader
    itself is shared. Idempotent.
    """

    def _add_blob(match: re.Match[str]) -> str:
        call = match.group(0)
        if "getConstantBlobData()" in call:
            return call
        return call[:-2] + ", getConstantBlobData());"

    for entry in os.listdir(build_folder):
        if not entry.endswith(".c"):
            continue
        path = Path(build_folder) / entry
        text = path.read_text()
        patched = _LOAD_CONSTANTS_CALL.sub(_add_blob, text)
        if patched != text:
            path.write_text(patched)


def build_nuitka_runtime(
    include_dirs: Iterable[str],
    dest_folder: Path,
    debug: bool = False,
) -> Path:
    """
    Compiles the shared Nuitka static runtime into a single `lib{RUNTIME_LIB_NAME}.so`
    placed in `dest_folder`, built once and linked by every module.

    The runtime is the full `CompiledFunctionType.c` aggregation, but with the constants
    blob loader replaced by a blob-agnostic version (`_patched_blob_loader`) so it holds
    no per-module blob reference — only the (universal) `global_constants`. We compile
    *our* copies of the aggregation files (quoted includes resolve to the including
    file's directory first) plus a shadow `constants_blob.h`, falling back to Nuitka's
    own sources/headers via `include_dirs` for everything else.

    Built with default symbol visibility (see NUITKA_MINIMAL_FLAGS) so its helpers stay
    resolvable from the module `.so`s, and with an explicit SONAME so those modules
    record a clean `NEEDED` entry resolvable via their `$ORIGIN` rpath.
    """
    compiler = ZigCompiler()
    nuitka_static_src = _nuitka_root() / "build" / "static_src"
    python_includes = [
        sysconfig.get_path("include"),
        sysconfig.get_path("platinclude"),
    ]
    soname = f"lib{RUNTIME_LIB_NAME}.so"
    with tempfile.TemporaryDirectory() as tmp:
        patch_dir = Path(tmp)
        for name in _RUNTIME_VERBATIM_SOURCES:
            shutil.copyfile(nuitka_static_src / name, patch_dir / name)
        (patch_dir / _BLOB_LOADER_SOURCE).write_text(_patched_blob_loader(nuitka_static_src))
        shadow_inc = _write_shadow_include(patch_dir / "shadow_inc")

        objects = compiler.compile(
            sources=[str(patch_dir / NUITKA_RUNTIME_AGGREGATION)],
            output_dir=str(patch_dir / "obj"),
            include_dirs=[str(shadow_inc), str(patch_dir)] + python_includes + list(include_dirs),
            extra_postargs=[*NUITKA_MINIMAL_FLAGS, *build_mode_compile_flags(debug)],
            macros=list(NUITKA_MACROS),
        )
        win_library_dirs, win_libraries = python_import_library_link_args()
        compiler.link_shared_object(
            objects,
            soname,
            output_dir=str(dest_folder),
            libraries=_runtime_link_libraries() + win_libraries,
            library_dirs=win_library_dirs,
            extra_preargs=[f"-Wl,-soname,{soname}"],
            extra_postargs=build_mode_link_flags(debug),
        )
    runtime_path = dest_folder / soname
    assert runtime_path.exists(), f"Runtime library not produced at {runtime_path}"
    return runtime_path


@contextmanager
def _environ_overridden(**overrides: str) -> Iterator[None]:
    """Temporarily set environment variables, restoring the previous state on exit."""
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _bundled_patchelf_dir() -> Path | None:
    """
    Returns the directory holding the PyPI-provided `patchelf` binary, or None.

    Nuitka needs `patchelf` to rewrite RPATHs in standalone/onefile mode on Linux,
    and locates it by bare name via PATH. Distributions ship varied (sometimes
    Nuitka-blacklisted, e.g. 0.18.0) versions, so smelt bundles a known-good one via
    the `patchelf` PyPI package to stay self-contained. This locates that binary
    independently of PATH ordering so we can put it first for Nuitka.
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
def _bundled_patchelf_on_path() -> Iterator[None]:
    """Prepends the bundled `patchelf` directory to PATH, if that package is installed."""
    bindir = _bundled_patchelf_dir()
    if bindir is None:
        yield
        return
    path = os.environ.get("PATH", "")
    with _environ_overridden(PATH=f"{bindir}{os.pathsep}{path}" if path else str(bindir)):
        yield


def _bundled_zig_path() -> Path | None:
    """Returns the `ziglang` PyPI package's bundled `zig` binary, or None if not installed."""
    try:
        import ziglang
    except ImportError:
        return None
    zig_dir = Path(ziglang.__file__).resolve().parent
    zig_exe = zig_dir / ("zig.exe" if platform.system() == "Windows" else "zig")
    return zig_exe if zig_exe.exists() else None


def run_nuitka_data_composer(build_folder: str) -> Path:
    """
    Runs Nuitka's data composer over `build_folder` to produce the constants blob.

    `--generate-c-only` skips this step (see MainControl.shallNotDoExecCCompilerCall),
    which is why the `constant_bin_data` symbol is otherwise undefined at link time.
    Returns the path to the generated blob.
    """
    root = _nuitka_root()
    data_composer = root / "tools" / "data_composer"
    # Absolute so the INCBIN directive in __constants_data.c resolves regardless of the
    # working directory the later setuptools/zig compile runs from.
    blobs_dir = Path(build_folder).resolve() / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    blob_path = blobs_dir / "__constant.bin"
    stats_path = blobs_dir / "__constant.txt"

    # NUITKA_PACKAGE_HOME is required by the data composer entrypoint (tools/data_composer
    # /__main__.py) to locate the nuitka package, mirroring runDataComposer's behaviour.
    package_home = str(root.parent)
    cmd = (
        sys.executable,
        str(data_composer),
        build_folder,
        str(blob_path),
        str(stats_path),
    )
    _logger.debug("Running %s", " ".join(cmd))
    with _environ_overridden(NUITKA_PACKAGE_HOME=package_home):
        cmd_trace = call_command(*cmd)
    context = get_context()
    if context:
        context.add_trace(cmd_trace)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(
            f"Nuitka data composer failed: {_describe_command_failure(cmd_trace, cmd)}",
        )
    assert blob_path.exists(), f"Data composer did not produce a blob at {blob_path}"
    return blob_path


def write_constants_incbin(build_folder: str, blob_path: Path) -> None:
    """
    Writes `__constants_data.c` embedding the constants blob via INCBIN.

    Reproduces `nuitka.build.SconsCompilerSettings._addConstantBlobFileIncbin`, which a
    full build runs from Scons. The blob is referenced by its absolute path so the
    `.incbin` directive resolves regardless of the compiler's working directory during
    the later setuptools build. Pairs with the `_NUITKA_CONSTANTS_FROM_INCBIN` macro.

    The blob's content hash is written into the source as a comment. That looks like
    decoration and is not: `zig cc` caches whole compilations, and its cache key covers
    the source text and the headers the preprocessor reports -- *not* files pulled in by
    an assembler `.incbin` directive, which no `-MD`-style dependency scan can see. Both
    the build folder and the blob's filename are stable across rebuilds, so without
    something content-derived in the source, a second build of the same entrypoint
    re-uses the first build's object file and silently ships the *previous* run's
    constants (verified empirically: a rebuild whose only change was one added constant
    produced a binary whose embedded blob still declared the old constant count, leaving
    the last `global_constants` entry `NULL` and segfaulting at startup -- with no build
    error anywhere). Hashing the blob into the text makes the cache key track it.
    """
    # Symbols per Nuitka's contract for the "__constant.bin" blob:
    # INCBIN(constant_bin, ...) defines `constant_bin_data`; getConstantBlobData()
    # is declared by NUITKA_DECLARE_CONSTANT_BLOB(constant_bin, ConstantBlob, ...).
    blob_digest = hashlib.sha256(blob_path.read_bytes()).hexdigest()
    contents = f"""\
// smelt: constants blob sha256 {blob_digest} -- see write_constants_incbin's docstring:
// this is what makes `zig cc`'s compilation cache notice the blob changed.
#define INCBIN_PREFIX
#define INCBIN_STYLE INCBIN_STYLE_SNAKE
#define INCBIN_LOCAL
#define CONST_CONSTANT const

#include "nuitka/incbin.h"

INCBIN(constant_bin, "{blob_path.as_posix()}");

#ifdef __cplusplus
extern "C" {{
#endif
unsigned CONST_CONSTANT char *getConstantBlobData(void) {{
    return (unsigned CONST_CONSTANT char *)constant_bin_data;
}}
#ifdef __cplusplus
}}
#endif
"""
    (Path(build_folder) / "__constants_data.c").write_text(contents)


def import_path_search_root(import_path: str, location: str | Path, *, is_package: bool) -> Path:
    """
    Returns the ancestor directory that must be on `sys.path` for `location` to be
    importable as the dotted `import_path`.

    That directory has to be added to the subprocess environment for an explicit
    `--include-module=<dotted name>` (e.g. a mypyc runtime extension, never actually
    `import`-ed anywhere) to resolve: Nuitka infers this itself while following imports
    from the entry script, but resolves such names via plain `sys.path`/`PYTHONPATH`
    lookup instead.

    Derived purely from the number of dotted segments in `import_path` (climbing that
    many parents from `location`, one fewer when `location` is a plain module file
    rather than a package directory) -- never by walking parents looking for
    `__init__.py`. That used to be how this was computed, and breaks for PEP 420
    namespace packages: a namespace package has no `__init__.py`, so the walk stopped
    climbing at the first namespace-package ancestor, computing a root nested one or
    more levels too deep inside the real one (or, if the whole hierarchy above
    `location` is a namespace package, not climbing at all).
    """
    resolved = Path(location).resolve()
    if is_package and resolved.name == "__init__.py":
        # Accept either the package's own directory or its `__init__.py` file.
        resolved = resolved.parent
    root = resolved if is_package else resolved.parent
    ascend = len(import_path.split(".")) - (0 if is_package else 1)
    for _ in range(ascend):
        root = root.parent
    return root


def _ensure_nuitka_installed() -> None:
    """Raises `ImportError` with an actionable message if Nuitka isn't installed."""
    try:
        import nuitka

        # not using the import - just checking if it is available
        # as following logic would fail otherwise
        _ = nuitka
    except ImportError:
        raise ImportError(
            "Nuitka is not installed. Please install this package with nuitka extra: `pip install smelt[nuitka]`.",
        )


def _nuitka_pythonpath_overrides(
    extra_search_paths: Iterable[str] | None,
    include_modules: Iterable[str] | None,
    include_packages: Iterable[str] | None,
) -> dict[str, str]:
    """
    Computes the `PYTHONPATH` override a Nuitka subprocess needs to resolve
    `extra_search_paths` (e.g. a codegen'd entrypoint script's real package root, needed
    so `--follow-imports` can resolve its import of the real module) and/or
    `include_modules`/`include_packages`.

    `--include-module`/`--include-package` name modules that are never actually
    `import`-ed anywhere (e.g. a mypyc runtime extension), so Nuitka can't infer their
    location the way it does for followed imports; it resolves them via plain
    `sys.path` lookup instead. Each name's own root is resolved independently via import
    machinery (see `import_path_search_root`), rather than assumed to share a common root.

    Shared by `compile_with_nuitka` and `compile_nuitka_entrypoint`.
    """
    if extra_search_paths:
        search_roots = list(extra_search_paths)
    elif include_modules or include_packages:
        search_roots = []
        for mod in {*(include_modules or ()), *(include_packages or ())}:
            location = _find_module_location(mod)
            if location is None:
                continue
            resolved_path, is_package = location
            root = str(import_path_search_root(mod, resolved_path, is_package=is_package))
            if root not in search_roots:
                search_roots.append(root)
    else:
        search_roots = []
    if not search_roots:
        return {}
    existing = os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": os.pathsep.join([*search_roots, existing] if existing else search_roots)}


def compile_with_nuitka(
    path: str,
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
    include_package_data: Iterable[str] | None = None,
    include_data_files: Iterable[str] | None = None,
    extra_flags: Iterable[str] | None = None,
    extra_search_paths: Iterable[str] | None = None,
    output_name: str | None = None,
    no_zig: bool = False,
    no_cache: bool = False,
) -> str:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.

    `extra_search_paths` is added to the subprocess's `PYTHONPATH` verbatim, for callers
    whose `path` isn't itself located inside the package it imports (e.g. a codegen'd
    entrypoint script sitting in a scratch directory) -- needed so Nuitka's own
    `--follow-imports` can resolve `path`'s import of the real entrypoint module. This is
    unrelated to `include_modules`/`include_packages`, which (see below) resolve their
    own search root independently via import machinery.

    `output_name` names the produced binary, overriding the default of reusing `path`'s
    basename -- useful when `path` was itself renamed to something other than the
    entrypoint's name (e.g. to dodge a collision with a package of the same name).
    """
    context = get_context()
    _ensure_nuitka_installed()
    expected_extension = ".exe" if platform.system() == "Windows" else ".bin"
    bin_path = (
        f"{output_name}{expected_extension}"
        if output_name
        else os.path.basename(path).replace(".py", expected_extension)
    )

    cmd = list(NUITKA_ENTRYPOINT)
    if not no_follow_imports:
        cmd.append("--follow-imports")
    cmd.append("--onefile")
    cmd.append(path)
    if output_name is not None:
        cmd.append(f"--output-filename={bin_path}")
    if no_cache:
        cmd.append("--disable-cache=all")

    # handling special flags
    if include_modules:
        for mod in include_modules:
            if not _is_importable(mod):
                _logger.warning("Skipping --include-module=%s: module is not importable", mod)
                continue
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    if include_package_data:
        for package in include_package_data:
            cmd.append(f"--include-package-data={package}")

    if include_data_files:
        for entry in include_data_files:
            cmd.append(f"--include-data-files={entry}")

    if extra_flags:
        cmd.extend(extra_flags)

    _logger.debug("Running %s", " ".join(cmd))

    env_overrides: dict[str, str] = dict(
        _nuitka_pythonpath_overrides(extra_search_paths, include_modules, include_packages)
    )

    if not no_zig and "CC" not in os.environ:
        # This is the only step that invokes an actual C backend compiler. On Windows
        # there is none by default, and Nuitka's fallback is to download its own
        # MinGW64 gcc; on Linux Nuitka falls back to the system gcc/clang, which may
        # not be present or may link against system dynlibs. smelt uses Zig for every
        # other compile step, so point it at our already-installed Zig instead on
        # every platform: Nuitka's Scons backend recognizes a `CC` binary named "zig"
        # and drives it correctly (`zig cc ...`), no Nuitka-version-specific flag
        # (like the newer `--zig`) required.
        zig_path = _bundled_zig_path()
        if zig_path is not None:
            env_overrides["CC"] = str(zig_path)
            env_overrides["CXX"] = str(zig_path)

    # Standalone/onefile builds shell out to `patchelf`; prefer our bundled, known-good
    # copy over whatever the system ships (some distro releases are Nuitka-blacklisted).
    with _bundled_patchelf_on_path(), _environ_overridden(**env_overrides):
        cmd_trace = call_command(*cmd, printer=print)
    if context:
        context.add_trace(cmd_trace)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(f"Nuitka failed: {_describe_command_failure(cmd_trace, cmd)}")

    absolute_bin_path = os.path.join(os.getcwd(), bin_path)
    assert os.path.exists(absolute_bin_path), f"Nuitka binary not found at {absolute_bin_path}"
    return absolute_bin_path


def nuitkaify_module(
    module: NuitkaModule,
    path_solver: PathSolver | None = None,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
    use_runtime: bool = platform.system() != "Windows",
    nuitka_context: NuitkaBuildContext | None = None,
    debug: bool = False,
) -> GenericExtension:
    """
    Compiles the module given by `path` into a native `.so`.

    `use_runtime` selects how the Nuitka static runtime is provided:
    - True (default off Windows): shared runtime. The static runtime is compiled once
      into `lib{RUNTIME_LIB_NAME}.so` and linked by the module `.so`, which then holds
      only its own generated code. The module keeps its own constants blob and its
      `loadConstantsBlob` call sites are rewritten to pass it into the runtime's
      blob-agnostic loader (`patch_module_constants_calls`), so each module reads its own
      constants while the multi-megabyte runtime is compiled once and shared.
      Relies on cross-shared-object symbols staying unresolved at link time and being
      satisfied by whichever module loads the runtime, which ELF/Mach-O shared objects
      allow but Windows DLLs do not — so this mode is unavailable there.
    - False (default on Windows): standalone. The static runtime is embedded straight
      into the module `.so` (via `provide_nuitka_static_sources`), producing a fully
      self-contained module with no external runtime dependency — larger, but with
      nothing shared between modules.

    `nuitka_context` (used only when `use_runtime=True`) is the persistence a future
    build-once-for-all-modules step reads from. When not provided it is resolved from the
    "nuitka" persistent context, and created if that is absent too, so the accumulated
    sources are never lost. It is distinct from the generic `GlobalContext` (which tracks
    run-command traces).
    """
    if nuitka_context is None:
        existing = get_context("nuitka")
        if existing is not None:
            assert isinstance(existing, NuitkaBuildContext)
            nuitka_context = existing
    if nuitka_context is None:
        nuitka_context = (
            create_context_if_enabled("nuitka", NuitkaBuildContext) or NuitkaBuildContext()
        )
    path_solver = path_solver or PathSolver()
    context = get_context()
    src_path = module.source or path_solver.resolve_import_path(module.import_path)
    mod_filename = os.path.basename(src_path)
    cmd = list(NUITKA_ENTRYPOINT)
    cmd.append("--module")
    cmd.append(str(src_path))
    # We compile ourselves, so ask Nuitka for the C sources only. `--generate-c-only`
    # returns early (MainControl.shallNotDoExecCCompilerCall) and therefore skips the
    # data composer (constants blob) and the constants-blob C wrapper, which we reproduce
    # below. This avoids the previous behaviour of letting Nuitka compile fully just to
    # recompile.
    cmd.append("--generate-c-only")

    # handling special flags
    if include_modules:
        for mod in include_modules:
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    _logger.debug("Running %s", " ".join(cmd))

    cmd_trace = call_command(*cmd, printer=print if stdout == "stdout" else None)
    if context:
        context.add_trace(cmd_trace)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(f"Nuitka failed: {_describe_command_failure(cmd_trace, cmd)}")

    build_folder = mod_filename.replace(".py", ".build")
    assert os.path.exists(build_folder)

    # Reproduce the build steps `--generate-c-only` skips (see comment above).
    blob_path = run_nuitka_data_composer(build_folder)
    write_constants_incbin(build_folder, blob_path)

    module_sources = [str(src) for src in iterate_nuitka_module_sources(build_folder)]
    assert module_sources, (
        "Nuitka did not produce any C file or build folder path logic is incorrect"
    )
    # patching build_definitions.h, as we don't need extensions
    open(os.path.join(build_folder, "build_definitions.h"), "w+").close()

    dest_folder = Path(src_path).parent
    header_sources = [str(f) for f in locate_nuitka_headers()]
    header_sources.append(build_folder)

    if use_runtime:
        # Shared runtime: only the per-module generated code goes into the module `.so`;
        # the static runtime lives in `lib{RUNTIME_LIB_NAME}.so`. Rewrite the module's
        # constants-loader calls to pass its own blob into the runtime's blob-agnostic
        # loader, and shadow `constants_blob.h` (patched prototype) ahead of Nuitka's.
        patch_module_constants_calls(build_folder)
        shadow_inc = _write_shadow_include(Path(build_folder) / "__smelt_shadow")
        header_sources.insert(0, str(shadow_inc))
        nuitka_context.runtime_sources.update(_runtime_source_paths())
        build_nuitka_runtime(header_sources, dest_folder, debug=debug)
        # `$ORIGIN` lets the module `.so` find the runtime sitting next to it in the package.
        libraries = _runtime_link_libraries(RUNTIME_LIB_NAME)
        library_dirs = [str(dest_folder)]
        runtime_library_dirs = ["$ORIGIN"]
    else:
        # Standalone: compile the static runtime straight into the module `.so`.
        provide_nuitka_static_sources(build_folder)
        module_sources.extend(str(src) for src in iterate_nuitka_static_sources(build_folder))
        libraries = _runtime_link_libraries()
        library_dirs = []
        runtime_library_dirs = []

    setup_tools_ext = Extension(
        name=mod_filename.replace(".py", ""),
        sources=module_sources,
        include_dirs=header_sources,
        define_macros=NUITKA_MACROS,
        libraries=libraries,
        library_dirs=library_dirs,
        runtime_library_dirs=runtime_library_dirs,
        extra_compile_args=list(NUITKA_MINIMAL_FLAGS),
    )
    return GenericExtension(
        import_path=module.import_path,
        src_path=str(src_path),
        extension=setup_tools_ext,
        dest_folder=dest_folder,
    )


_FROZEN_MODULES_TABLE: Final[re.Pattern[str]] = re.compile(
    r"static struct frozen_desc _frozen_modules\[\] = \{(.*?)\n\};", re.DOTALL
)
_FROZEN_MODULE_ENTRY: Final[re.Pattern[str]] = re.compile(r'^\{"', re.MULTILINE)


def _count_frozen_modules(build_folder: str) -> int:
    """
    Counts the entries Nuitka generated into `__loader.c`'s `_frozen_modules[]` table (in
    standalone mode: every stdlib module reachable by static analysis, registered as a
    real CPython frozen module -- see `compile_nuitka_entrypoint`'s standalone docstring
    section). Must match `_NUITKA_FROZEN` exactly: `MainProgram.c`'s `prepareFrozenModules`
    mallocs its merged table from that macro's value, so an undercount truncates the copy
    and an overcount reads past the generated array.
    """
    loader_text = (Path(build_folder) / "__loader.c").read_text()
    match = _FROZEN_MODULES_TABLE.search(loader_text)
    assert match, (
        "Expected `_frozen_modules[]` table not found in __loader.c (Nuitka version drift?)"
    )
    return len(_FROZEN_MODULE_ENTRY.findall(match.group(1)))


def _build_nuitka_exe_extension(
    path: str,
    *,
    standalone: bool,
    no_follow_imports: bool,
    stdout: Stdout | None,
    include_modules: Iterable[str] | None,
    include_packages: Iterable[str] | None,
    include_package_data: Iterable[str] | None,
    include_data_files: Iterable[str] | None,
    extra_flags: Iterable[str] | None,
    extra_search_paths: Iterable[str] | None,
    no_cache: bool,
) -> Extension:
    """
    Runs Nuitka's `--generate-c-only` over the entrypoint script `path` and returns the
    `Extension` describing how to compile+link the result into an executable -- the
    shared implementation behind `compile_nuitka_entrypoint` (`standalone=False`) and
    `assemble_standalone_dist` (`standalone=True`). Does not link; callers pass the
    result to `compile_executable`.

    `standalone=True` additionally passes `--standalone` to Nuitka (pulling in the whole
    reachable standard library, not just what `path` itself imports) and defines
    `_NUITKA_STANDALONE_MODE` plus a correctly counted `_NUITKA_FROZEN` -- together these
    make `MainProgram.c` register every stdlib module reachable by static analysis as a
    real CPython frozen module (`prepareFrozenModules`, called *before*
    `Py_InitializeFromConfig`) sourced from the same bytecode blob smelt already builds,
    so the interpreter's own bootstrap (`encodings`, `site`, ...) resolves without any
    `.py`/`.pyc` file on disk and without depending on the original Python install's
    `sys.prefix` still existing at build time. Verified empirically: a binary built this
    way runs correctly under `env -i` from a location the original interpreter was never
    copied to. Genuine *native* extension modules (`.so` files, e.g. `_ssl`) are not
    covered by this mechanism -- `assemble_standalone_dist` bundles those separately.
    """
    _ensure_nuitka_installed()
    cmd = list(NUITKA_ENTRYPOINT)
    if not no_follow_imports:
        cmd.append("--follow-imports")
    if standalone:
        cmd.append("--standalone")
    cmd.append("--generate-c-only")
    cmd.append(path)
    if no_cache:
        cmd.append("--disable-cache=all")

    if include_modules:
        for mod in include_modules:
            if not _is_importable(mod):
                _logger.warning("Skipping --include-module=%s: module is not importable", mod)
                continue
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    if include_package_data:
        for package in include_package_data:
            cmd.append(f"--include-package-data={package}")

    if include_data_files:
        for entry in include_data_files:
            cmd.append(f"--include-data-files={entry}")

    if extra_flags:
        cmd.extend(extra_flags)

    _logger.debug("Running %s", " ".join(cmd))

    context = get_context()
    env_overrides = _nuitka_pythonpath_overrides(extra_search_paths, include_modules, include_packages)
    with _environ_overridden(**env_overrides):
        cmd_trace = call_command(*cmd, printer=print if stdout == "stdout" else None)
    if context:
        context.add_trace(cmd_trace)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(f"Nuitka failed: {_describe_command_failure(cmd_trace, cmd)}")

    # `--follow-imports` generates one `module.<dotted name>.c` per followed module into
    # this same build folder (named after the entry script, like the `--module` case) --
    # compiling them all together is what produces one self-contained binary instead of
    # `nuitkaify_module`'s one-`.so`-per-module.
    build_folder = os.path.basename(path).replace(".py", ".build")
    assert os.path.exists(build_folder)

    # Reproduce the build steps `--generate-c-only` skips, exactly like `nuitkaify_module`.
    blob_path = run_nuitka_data_composer(build_folder)
    write_constants_incbin(build_folder, blob_path)

    module_sources = [str(src) for src in iterate_nuitka_module_sources(build_folder)]
    assert module_sources, "Nuitka did not produce any C file or build folder path logic is incorrect"

    write_exe_build_definitions(build_folder, standalone=standalone)

    header_sources = [str(f) for f in locate_nuitka_headers()]
    header_sources.append(build_folder)

    provide_nuitka_static_sources(build_folder, sources=NUITKA_EXE_STATIC_SOURCES)
    module_sources.extend(str(src) for src in iterate_nuitka_static_sources(build_folder))

    macros = list(NUITKA_EXE_MACROS)
    if standalone:
        frozen_count = _count_frozen_modules(build_folder)
        macros = [
            (name, frozen_count) if name == "_NUITKA_FROZEN" else (name, value)
            for name, value in macros
        ]
        macros.append(("_NUITKA_STANDALONE_MODE", None))

    exe_name = os.path.basename(path).replace(".py", "")
    return Extension(
        name=exe_name,
        sources=module_sources,
        include_dirs=header_sources,
        define_macros=macros,
        libraries=_runtime_link_libraries(),
        extra_compile_args=list(NUITKA_MINIMAL_FLAGS),
    )


def _bin_name_for(path: str, output_name: str | None) -> str:
    """The `<name>.bin`/`<name>.exe` filename `compile_nuitka_entrypoint`-family
    functions produce for entrypoint script `path`, honoring `output_name` if given."""
    expected_extension = ".exe" if platform.system() == "Windows" else ".bin"
    return (
        f"{output_name}{expected_extension}"
        if output_name
        else os.path.basename(path).replace(".py", expected_extension)
    )


def compile_nuitka_entrypoint(
    path: str,
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
    include_package_data: Iterable[str] | None = None,
    include_data_files: Iterable[str] | None = None,
    extra_flags: Iterable[str] | None = None,
    extra_search_paths: Iterable[str] | None = None,
    output_name: str | None = None,
    no_cache: bool = False,
    debug: bool = False,
) -> str:
    """
    Compiles the entrypoint script given by `path` into a plain native executable,
    dynamically linked against system libs (libpython, libc, ...) -- the
    `compile_with_nuitka` counterpart that smelt links itself (via `compile_executable`)
    instead of delegating the final compile+link to Nuitka's own Scons/`--onefile`
    pipeline. No DLL bundling, no onefile self-extraction: same shape of artifact as
    `compile_extension` produces for a module, but linked as an executable.

    Same options as `compile_with_nuitka` (`no_zig` excluded: Nuitka's `--generate-c-only`
    never itself invokes a C compiler, so there is no Nuitka-side compiler choice to
    steer here -- smelt's own `ZigCompiler` is the only one used, always).

    The runtime is always embedded standalone into the executable (unlike
    `nuitkaify_module`'s `use_runtime=True` option): sharing `lib{RUNTIME_LIB_NAME}.so`
    with already-built modules would mix `_NUITKA_MODULE_MODE` build contexts under one
    runtime binary -- real behavioral differences (GIL/threadstate access, allocator
    paths), not just a naming convention -- so it is left out of scope here.

    Not standalone: relies on the original Python install (`sys.prefix` baked in at
    compile time) still being present at run time, same as Nuitka's own "accelerated"
    mode. See `assemble_standalone_dist` for a self-contained dist directory instead.
    """
    ext = _build_nuitka_exe_extension(
        path,
        standalone=False,
        no_follow_imports=no_follow_imports,
        stdout=stdout,
        include_modules=include_modules,
        include_packages=include_packages,
        include_package_data=include_package_data,
        include_data_files=include_data_files,
        extra_flags=extra_flags,
        extra_search_paths=extra_search_paths,
        no_cache=no_cache,
    )
    built_exe = compile_executable(ext, debug=debug)
    absolute_bin_path = os.path.join(os.getcwd(), _bin_name_for(path, output_name))
    if str(built_exe) != absolute_bin_path:
        shutil.move(str(built_exe), absolute_bin_path)
    assert_path_exists(absolute_bin_path)
    return absolute_bin_path


# Base glibc/kernel-ABI libraries: always present on the target, and not meaningfully
# bundleable in the first place -- glibc refuses to work mixed with a foreign version,
# and the NSS libs (`libnss_*`) must come from the *running system's* libc to match its
# `/etc/nsswitch.conf`, so bundling a copy would silently break name resolution instead
# of fixing anything. Ported from Nuitka's own `freezer.DllDependenciesPosix` ignore
# list, but deliberately *not* excluding `libz.so`/`libstdc++.so` the way Nuitka's list
# does by default: smelt's stated goal is no system-dynlib dependency at all for the
# pieces that *do* vary across distros, and those two do.
# Never bundleable, `own_python` or not: `linux-vdso.so` is a kernel-injected virtual
# DSO with no backing file on disk (nothing to copy); `libnss_*` must come from the
# *running system's* libc to match its `/etc/nsswitch.conf`, so bundling a copy would
# silently break name resolution instead of fixing anything.
_ALWAYS_HOST_DLL_PREFIXES: Final[tuple[str, ...]] = ("linux-vdso.so", "libnss_")

# The rest of glibc's base libraries: excluded by default (today's ldd-the-host
# behavior -- always present on the target, not meaningfully bundleable when that
# target is "whatever host this happens to run on"), but *included* when `own_python`
# is set: there, "the target" is meta-python's own build, so its own copy of these is
# exactly what should get bundled instead -- see `assemble_standalone_dist`.
_LINUX_BASE_LIBC_DLL_PREFIXES: Final[tuple[str, ...]] = (
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

_LINUX_SYSTEM_DLL_IGNORE_PREFIXES: Final[tuple[str, ...]] = (
    *_ALWAYS_HOST_DLL_PREFIXES,
    *_LINUX_BASE_LIBC_DLL_PREFIXES,
)


def _extension_module_dest_path(dotted_name: str, origin: str) -> str:
    """
    Package-relative destination path for the native extension module `dotted_name`
    (e.g. `"pkg.sub._ext"`) whose on-disk file is `origin`, mirroring Nuitka's own
    convention (`freezer.IncludedEntryPoints.addExtensionModuleEntryPoint`): the dotted
    name becomes a directory path, and a compiled-in package (`__init__.<ext>`) keeps its
    own name as the last path component instead of being dropped.

    The suffix is *not* `origin`'s own (e.g. `.abi3.so`/`.cpython-314-x86_64-linux-gnu.so`)
    but the shortest one in `importlib.machinery.EXTENSION_SUFFIXES` (plain `.so` on
    Linux) -- verified necessary empirically: a module compiled directly into the
    executable (anything reachable via static analysis, as opposed to the rest of the
    stdlib swept in by `--standalone` and frozen as bytecode) resolves an "extension
    module" hard dependency like this one via Nuitka's own generated C code, which looks
    it up by exactly that shortest suffix (`Importing.getExtensionModuleSuffix(False)`,
    itself just this same `min(..., key=len)` over the same public stdlib list) --
    keeping the original ABI-tagged name left the file undiscoverable at runtime.
    """
    import importlib.machinery

    suffix = min(importlib.machinery.EXTENSION_SUFFIXES, key=len)
    parts = dotted_name.split(".")
    if os.path.basename(origin).startswith("__init__"):
        package_path = dotted_name.replace(".", "/")
        basename = f"__init__{suffix}"
    else:
        package_path = "/".join(parts[:-1])
        basename = f"{parts[-1]}{suffix}"
    return f"{package_path}/{basename}" if package_path else basename


def _discover_extension_modules(
    script_path: str, extra_search_paths: Iterable[str] = ()
) -> dict[str, str]:
    """
    Runs `script_path` up to (but not through) its own `if __name__ == "__main__":`
    guard in a fresh interpreter, then returns `{dotted_name: source_path}` for every
    native extension module (`.so`/`.pyd`) that ended up in `sys.modules` as a result.

    This sidesteps Nuitka's own extension-module discovery entirely (which, per
    `assemble_standalone_dist`'s docstring, requires its Scons-driven freezer pipeline
    smelt does not otherwise invoke): only the imports actually reachable from
    `script_path` are seen, exactly the same static-analysis limitation Nuitka's own
    `--follow-imports` has -- imports gated deep in runtime-only branches need an
    explicit `include_modules`/`include_packages` hint either way. Loading `script_path`
    under a synthetic module name (not `"__main__"`) is what keeps the guard from firing,
    so this never actually executes the entrypoint's own behavior, only its imports.
    Results are handed back through a temp file rather than stdout: `script_path`'s own
    top-level code is free to print anything as an ordinary side effect of being
    imported, which would otherwise corrupt a stdout-based channel.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp_file:
        output_path = tmp_file.name
    try:
        script = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('__smelt_discover__', {script_path!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "found = {}\n"
            "for name, mod in list(sys.modules.items()):\n"
            "    origin = getattr(getattr(mod, '__spec__', None), 'origin', None)\n"
            "    if isinstance(origin, str) and origin.endswith(('.so', '.pyd')):\n"
            "        found[name] = origin\n"
            f"with open({output_path!r}, 'w') as f:\n"
            "    json.dump(found, f)\n"
        )
        # `spec_from_file_location` (unlike running `script_path` as `python
        # script_path`) does not put the script's own directory on `sys.path`, so a
        # sibling import (e.g. `import fib` from a script living next to `fib.py`) would
        # otherwise fail here even though it works fine for Nuitka's own
        # `--follow-imports`.
        search_paths = [os.path.dirname(os.path.abspath(script_path)), *extra_search_paths]
        env_overrides = _nuitka_pythonpath_overrides(search_paths, None, None)
        with _environ_overridden(**env_overrides):
            cmd_trace = call_command(sys.executable, "-c", script)
        if cmd_trace.exit_code != 0:
            raise RuntimeError(
                "Failed to discover extension modules for "
                f"{script_path!r}: {_describe_command_failure(cmd_trace, (sys.executable, '-c', script))}"
            )
        with open(output_path) as f:
            found: dict[str, str] = json.load(f)
        return found
    finally:
        os.unlink(output_path)


def _ldd_dependencies(binary_path: str) -> dict[str, str]:
    """
    Runs `ldd` on `binary_path`, returning `{basename: resolved_path}` for every
    dependency it reports as resolved (skipping `not found` entries and the vDSO, which
    has no backing file).
    """
    cmd_trace = call_command("ldd", binary_path)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(f"ldd failed on {binary_path!r}: {' '.join(cmd_trace.stderr)}")
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


def _collect_native_dependencies(
    seed_paths: Iterable[str],
    ignore_prefixes: tuple[str, ...] = _LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
) -> dict[str, str]:
    """
    Recursively `ldd`-walks every path in `seed_paths` (and every dependency found along
    the way), returning `{basename: resolved_path}` for everything worth bundling --
    i.e. excluding `ignore_prefixes` (`_LINUX_SYSTEM_DLL_IGNORE_PREFIXES` by default;
    `own_python` narrows this to just `_ALWAYS_HOST_DLL_PREFIXES`, see
    `assemble_standalone_dist`). On a same-basename conflict (two different resolved
    paths reporting the same file name) the first one found is kept and the rest are
    logged and dropped, mirroring (in simplified form) Nuitka's own
    `addIncludedEntryPoint` collision handling.
    """
    collected: dict[str, str] = {}
    pending = list(seed_paths)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for basename, resolved in _ldd_dependencies(current).items():
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


def _run_patchelf(*args: str) -> None:
    """Runs `patchelf` (preferring smelt's bundled copy) and raises on failure."""
    with _bundled_patchelf_on_path():
        cmd_trace = call_command("patchelf", *args)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(f"patchelf failed: {_describe_command_failure(cmd_trace, ('patchelf', *args))}")


def _set_rpath(binary_path: Path, rpath: str) -> None:
    """
    Sets `binary_path`'s ELF RPATH to `rpath`, `:`-joined-entries. Uses `--force-rpath`
    (writes the legacy `DT_RPATH` tag, not `DT_RUNPATH`) to match Nuitka's own choice:
    unlike `DT_RUNPATH`, `DT_RPATH` is searched before `LD_LIBRARY_PATH` and is inherited
    by the binary's own transitive dependencies, both needed for a dist dir that must not
    fall back to whatever same-named library the host system happens to have installed.
    """
    _run_patchelf("--force-rpath", "--set-rpath", rpath, str(binary_path))


def _set_interpreter(binary_path: Path, interpreter: Path) -> None:
    """
    Sets `binary_path`'s ELF `PT_INTERP` (the dynamic loader the kernel runs before any
    of the binary's own code, resolved directly by the kernel -- unlike `DT_NEEDED`
    library lookups, *not* subject to RPATH/`$ORIGIN` expansion) to `interpreter`.

    Only relevant for `own_python` (see `assemble_standalone_dist`): points the exe at
    this build's own bundled `ld-linux*.so*` instead of the host's, so the *dynamic
    linker itself* -- not just the libraries it resolves -- comes from the dist dir.
    `interpreter` must therefore be the dist dir's final absolute path: since `PT_INTERP`
    can't be `$ORIGIN`-relative, moving the dist dir afterward breaks this (a portable,
    movable-anywhere onefile launcher would need a re-exec wrapper instead -- out of
    scope while smelt only produces a dist *directory*).
    """
    _run_patchelf("--set-interpreter", str(interpreter), str(binary_path))


def _package_rpath_dirs(dest_rel_paths: Iterable[str]) -> set[str]:
    """The distinct package subdirectories (relative to the dist dir root, POSIX-style)
    that hold at least one bundled extension module."""
    return {parent for path in dest_rel_paths if (parent := os.path.dirname(path))}


def _nested_rpath(dest_rel_path: str) -> str:
    """
    RPATH for a bundled file sitting `dest_rel_path`-deep inside the dist dir: `$ORIGIN`
    (its own directory, for sibling native deps placed alongside it) plus, if nested,
    `$ORIGIN/../..` etc. back to the dist dir root (where smelt flattens every other
    native dependency) -- matches the `$ORIGIN:$ORIGIN/..` scheme a real Nuitka
    standalone build sets on a package's own extension modules.
    """
    depth = dest_rel_path.count("/")
    if depth == 0:
        return "$ORIGIN"
    return "$ORIGIN:" + "/".join(["$ORIGIN", *([".."] * depth)])


def _copy_extension_modules(extensions: dict[str, str], dist_dir: Path) -> dict[str, str]:
    """
    Copies each discovered extension module (`{dotted_name: source_path}`, as returned by
    `_discover_extension_modules`) into `dist_dir` at its package-relative destination.
    Returns `{dest_rel_path: source_path}` for the copies actually made.
    """
    placed: dict[str, str] = {}
    for dotted_name, source_path in extensions.items():
        dest_rel = _extension_module_dest_path(dotted_name, source_path)
        dest_path = dist_dir / dest_rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)
        placed[dest_rel] = source_path
    return placed


#: File suffixes that are *code*, not data: never copied by `_copy_package_data`.
#: Extension modules (`.so`/`.pyd`) are excluded here because they are handled by
#: `_discover_extension_modules`/`_copy_extension_modules`, which additionally
#: `ldd`-walk them -- a blind copy here would place one with an unpatched RPATH.
_PACKAGE_DATA_CODE_SUFFIXES: Final[tuple[str, ...]] = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyi",
    ".so",
    ".pyd",
    ".dll",
    ".dylib",
)


def _resolve_package_dir(package: str, extra_search_paths: Iterable[str]) -> Path | None:
    """`package`'s own directory, resolved through the import machinery with
    `extra_search_paths` temporarily on `sys.path` (the same paths the Nuitka run is
    given), or None if it isn't importable or isn't a package."""
    extra = [path for path in extra_search_paths if path not in sys.path]
    sys.path[:0] = extra
    try:
        located = _find_module_location(package)
    finally:
        for path in extra:
            sys.path.remove(path)
    if located is None:
        return None
    location, is_package = located
    return location if is_package else None


def _copy_package_data(
    specs: Iterable[str],
    dist_dir: Path,
    extra_search_paths: Iterable[str] = (),
) -> None:
    """
    Copies the data files of each `--include-package-data` spec
    (`PACKAGE[:PATTERN,...]`, Nuitka's own syntax) into `dist_dir`, at the package's
    own dotted-name-derived subdirectory.

    smelt has to do this itself: `--include-package-data` is forwarded to Nuitka, but
    Nuitka only *copies* data files as part of the `--standalone` dist assembly it runs
    after compiling, and smelt stops it at `--generate-c-only` (see
    `_build_nuitka_exe_extension`). Without this the flag is silently inert for a
    smelt-assembled dist -- the build succeeds and the binary fails at runtime, on
    whichever request first reads one of those files.

    Every file under the package directory is copied except code
    (`_PACKAGE_DATA_CODE_SUFFIXES`) and `__pycache__`; a `:`-suffixed comma-separated
    pattern list narrows that to file *names* matching one of the `fnmatch` patterns,
    matching Nuitka's own reading of the option.
    """
    for spec in specs:
        package, _, patterns_decl = spec.partition(":")
        patterns = [p for p in patterns_decl.split(",") if p]
        package_dir = _resolve_package_dir(package, extra_search_paths)
        if package_dir is None:
            _logger.warning(
                "Skipping --include-package-data=%s: %r is not an importable package",
                spec,
                package,
            )
            continue
        dest_root = dist_dir / Path(*package.split("."))
        copied = 0
        for source in package_dir.rglob("*"):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            if source.suffix in _PACKAGE_DATA_CODE_SUFFIXES:
                continue
            if patterns and not any(fnmatch(source.name, pattern) for pattern in patterns):
                continue
            dest = dest_root / source.relative_to(package_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            copied += 1
        _logger.debug("Copied %d data file(s) for package %s", copied, package)


def _copy_native_dependencies(deps: dict[str, str], dist_dir: Path) -> None:
    """Copies every discovered native dependency (`{basename: resolved_path}`) flat into
    `dist_dir`, matching the layout Nuitka's own standalone builds use by default."""
    for basename, resolved in deps.items():
        shutil.copy2(resolved, dist_dir / basename)


_METAPYTHON_CACHE_DIR: Final = Path.home() / ".cache" / "smelt" / "metapython"

#: Zig target triple `build_own_python` builds for when the caller names none. `None`
#: means "whatever `zig build`'s own default target is", i.e. a native build against the
#: host's own libc. That is deliberately *not* musl, even though musl is the only shape
#: that reaches genuine host-independence (see `build_own_python`'s docstring): a musl
#: interpreter cannot load the host's glibc-linked third-party extension modules
#: (`orjson`, `psutil`, `pydantic_core`, ... -- every non-stdlib `.so` a real entrypoint
#: pulls in from site-packages), and smelt does not build those. Targeting musl is only
#: useful once the *whole* dependency set is musl-built, so it stays an explicit
#: opt-in (`own-python-target` in `[tool.smelt.entrypoints]`) rather than the default.
DEFAULT_OWN_PYTHON_TARGET: Final[str | None] = None


def _ensure_metapython_installed() -> None:
    """Raises `ImportError` with an actionable message if `metapython` isn't installed."""
    try:
        import metapython

        _ = metapython
    except ImportError:
        raise ImportError(
            "metapython is not installed. Please install this package with the "
            "metapython extra: `pip install smelt[metapython]`.",
        )


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
    headers don't provide them -- `Modules/posixmodule.c`/`Python/thread_pthread.h`/
    `Python/fileutils.c` then fail to compile against musl's actual `<sched.h>`/
    `<unistd.h>`/(missing) `<sys/pidfd.h>`. Each override was found empirically, one
    real compiler error at a time -- there is no general "musl mode" flag to flip.
    """
    text = pyconfig_path.read_text()
    for macro in _MUSL_UNDEF_OVERRIDES:
        text = re.sub(rf"^#define {macro} 1$", f"/* #undef {macro} */", text, flags=re.MULTILINE)
    for macro in _MUSL_ZERO_OVERRIDES:
        text = re.sub(rf"^#define {macro} 1$", f"#define {macro} 0", text, flags=re.MULTILINE)
    dest.write_text(text)


def _is_musl_target(target: str | None) -> bool:
    """Whether `target` (a Zig target triple, `None` for native) names a musl libc."""
    return target is not None and "musl" in target


def build_own_python(
    dest_dir: str | os.PathLike[str] | None = None,
    *,
    target: str | None = DEFAULT_OWN_PYTHON_TARGET,
    no_cache: bool = False,
    debug: bool = False,
) -> Path:
    """
    Builds smelt's own CPython via the sibling `meta-python` project (Zig-driven:
    `python/cpython` submodule compiled directly through `build.zig`, no `make`) into
    `dest_dir` (a per-user, per-target cache dir, reused across builds, if omitted) and
    returns that directory.

    Built with `libc-linkage=dynamic` + `python-linkage=dynamic`: a real `libpythonX.Y.so`
    both smelt's own entrypoint executable and the stdlib extension modules link against
    at build time (rather than relying on `-rdynamic` symbol export from a monolithic
    exe). `libc-linkage=static` is deliberately *not* used here even though it sounds
    like the more "standalone" choice -- meta-python's own `build/options.zig` rejects
    `libc-linkage=static` combined with any dynamically linked Python at all (verified
    empirically: it's a hard ELF constraint, not a tunable default -- a fully statically
    linked executable has no dynamic section whatsoever, so it cannot depend on *any*
    `.so`, whether via an ordinary build-time link or a runtime `dlopen()`; there is no
    "static libc, dynamic everything else" combination for it to reach). A genuinely
    static build is reachable only by also compiling every extension module *into* the
    interpreter (meta-python's `-Dstatic-modules=`), which in turn requires
    `python-linkage=off` -- and that leaves no `libpython` for smelt's own entrypoint to
    link against, so it is not the shape this function targets.

    `target` is a Zig target triple, or `None` (the default -- see
    `DEFAULT_OWN_PYTHON_TARGET`) for a native build against the host's own libc:

    * **Native (`None`)**: what `own_python` actually delivers today. The produced dist
      still resolves `libc`/`ld-linux` from the host (excluded from bundling as
      "always present" -- see `_LINUX_SYSTEM_DLL_IGNORE_PREFIXES`), but everything
      *above* libc -- the interpreter, `libpythonX.Y.so`, the stdlib's own C extension
      modules -- is smelt's own build rather than the machine's Python install. It keeps
      working with the host's third-party extension modules, which is what makes it
      usable for a real entrypoint at all.
    * **`"x86_64-linux-musl"` (and other musl triples)**: the genuinely
      host-independent shape, with two real costs. First, `./configure` still runs
      *natively* (meta-python never passes `--host=`), so it feature-detects against the
      build machine's glibc and produces a `pyconfig.h` claiming glibc-only features --
      handled here by a two-phase bootstrap: a first `zig_build` expected to fail (its
      only purpose is making `./configure` produce a base `pyconfig.h`), then
      `_patch_musl_pyconfig`, then the real build with `-Dpyconfig-header=`. Second, and
      not fixable here: every *third-party* extension module the entrypoint imports
      still comes from the host's site-packages and is glibc-linked, so it will not load
      under a musl interpreter. Only worth selecting when the whole dependency set is
      musl-built too.

    Bundling glibc itself (rather than targeting musl) was tried and rejected: a build
    that independently `ldd`-resolved `ld-linux`/`libc.so` like any other native
    dependency segfaulted immediately *inside the loader itself*, before any Python code
    ran. `ld.so` and `libc.so` are a tightly ABI-coupled matched pair (internal TLS/data
    layout, not just public symbol versions), and some of Zig's own outputs carry an
    embedded RPATH toward Zig's bundled glibc copy while others fall through to the
    host's, so a generic per-file dependency walk silently mixes two ABI-incompatible
    glibc builds. That is why the native path relies on the host's libc instead of
    shipping one, and why musl -- which ships loader and libc as a *single* combined
    `ld-musl-<arch>.so.1`, with no pair to mismatch -- is the only bundleable option.

    `zlib` statically linked; `openssl`/`libffi` dynamic and `ncurses`/`readline` off
    (see the `libraries=` comments below for why); `sqlite`/`bz2`/`lzma`/`tk` stay at
    meta-python's own dynamic-or-off defaults.

    `debug` keeps debug info and symbols in the produced interpreter; the default
    strips them (`-Dstrip=true`). This is a separate knob from the optimize mode on
    purpose: CPython's `PY_CORE_CFLAGS` come from the `./configure`-generated Makefile
    and carry `-g` unconditionally, so a `ReleaseFast` build still emits full DWARF.
    Stripping takes `libpython3.12.so` from 24.5 MB to 7.3 MB and the 62 `lib-dynload`
    modules from 14 MB to 4 MB, measured -- and every one of those files gets copied
    into each dist this interpreter is bundled into.

    Cached on `dest_dir/bin/python` already existing; pass `no_cache` to force a
    rebuild (e.g. after meta-python's pinned CPython version changes). The cache is
    keyed on target *and* build mode: a stripped and an unstripped build of the same
    target are not interchangeable, and sharing one directory would make whichever ran
    first silently satisfy the other's cache check.
    """
    _ensure_metapython_installed()
    from metapython.compile import (
        BuildOptions,
        LibCLinkage,
        Linkage,
        OptimizeMode,
        VENDORED_PROJECT_DIR,
        zig_build,
    )

    if dest_dir is not None:
        dest = Path(dest_dir)
    else:
        # Per-target and per-build-mode: a native and a musl build of the same
        # CPython are not interchangeable, and neither are a stripped and an
        # unstripped one -- sharing a cache dir would make whichever ran first
        # silently satisfy the other's cache check.
        dest = _METAPYTHON_CACHE_DIR / f"{target or 'native'}{'-debug' if debug else ''}"
    bin_path = dest / "bin" / "python"
    if not no_cache and bin_path.exists():
        return dest

    options = BuildOptions(
        target=target,
        # Explicit, and load-bearing: `zig build`'s own default optimize mode is
        # `Debug`, which meta-python maps to `./configure --with-pydebug` -- a
        # `Py_DEBUG` interpreter. That is not merely a slower build: `Py_DEBUG` changes
        # `PyObject`'s layout, so it defines its own ABI tag (`cpython-312d-...`) and
        # cannot load release-ABI extension modules. Every third-party `.so` an
        # entrypoint pulls in from site-packages is release-ABI, so a debug interpreter
        # would break exactly the imports `own_python` exists to keep working.
        optimize=OptimizeMode.DEBUG if debug else OptimizeMode.RELEASE_FAST,
        # Not implied by `optimize`, see this function's docstring.
        strip=not debug,
        libc_linkage=LibCLinkage.DYNAMIC,
        python_linkage=Linkage.DYNAMIC,
        libraries={
            # Static, and PIC-safe on musl now too (see build.zig's `pie = true`
            # override on zlib's own dependency() call).
            "zlib": Linkage.STATIC,
            # NOT static, unlike zlib: the allyourcodebase `openssl`/`libffi` packages
            # don't expose a PIC toggle the way zlib's does, and Zig's default PIC-ness
            # for a plain static-library compile step doesn't hold for musl (it's
            # PIC-by-default for `*-linux-gnu`, not `*-linux-musl` -- verified
            # empirically) -- their static archives fail to link into the shared
            # extension modules that need them ("recompile with -fPIC") without one.
            # Built as ordinary `.so`s instead; `assemble_standalone_dist`'s existing
            # `ldd`-walk already bundles those like any other native dependency, so this
            # only gives up "fewer separate .so files", not "no host dependency".
            "openssl": Linkage.DYNAMIC,
            "libffi": Linkage.DYNAMIC,
            # No musl-targeted readline/ncurses build available (there's nothing to
            # link `readline`'s module against at all under a musl cross-target), and
            # nothing in a compiled entrypoint needs an interactive line editor -- off
            # rather than a build failure on musl and dead weight everywhere else.
            "readline": Linkage.OFF,
            "ncurses": Linkage.OFF,
        },
    )

    cpython_dir = VENDORED_PROJECT_DIR / "cpython"
    # `runConfigure` (meta-python's build.zig) only ever runs `./configure` when
    # `cpython/Makefile` is *absent* -- a leftover Makefile from some earlier build
    # (this same source tree, shared across every target/option combination) makes it
    # skip straight to compiling with whatever `pyconfig.h` that earlier run left
    # behind. Force a real reconfigure for *this* target: without it, a prior build for
    # a different target would silently carry over.
    for stale in ("Makefile", "pyconfig.h"):
        (cpython_dir / stale).unlink(missing_ok=True)
    # Also clear Zig's own local project cache: verified empirically that when a
    # `zig build` invocation's CLI options are byte-identical to a previous one, Zig can
    # skip re-executing `build.zig`'s `build()` function -- and with it, our
    # `Makefile`/`pyconfig.h` deletion above and `runConfigure`'s own `./configure`
    # re-run -- entirely, silently reusing a stale cached step graph instead.
    # `.zig-cache` is local/rebuildable, not the package fetch cache
    # (`--global-cache-dir`, left untouched -- no need to re-download
    # zlib/openssl/libffi sources).
    shutil.rmtree(VENDORED_PROJECT_DIR / ".zig-cache", ignore_errors=True)

    dest.mkdir(parents=True, exist_ok=True)
    if _is_musl_target(target):
        try:
            # Expected to fail: this first pass exists only to make `./configure` run
            # and produce a base `pyconfig.h` to patch below -- native `./configure`
            # detects against the *host's* glibc, so compiling against musl right after
            # hits exactly the errors `_patch_musl_pyconfig` corrects.
            zig_build(options, cwd=VENDORED_PROJECT_DIR, extra_args=["-p", str(dest)])
        except subprocess.CalledProcessError:
            pass
        unpatched_pyconfig = cpython_dir / "pyconfig.h"
        assert unpatched_pyconfig.exists(), (
            f"{unpatched_pyconfig} missing after the bootstrap zig_build call -- "
            "did ./configure itself fail (not just the musl-specific compile errors "
            "_patch_musl_pyconfig expects to see)?"
        )
        patched_pyconfig = dest / "_smelt_pyconfig_musl.h"
        _patch_musl_pyconfig(unpatched_pyconfig, patched_pyconfig)
        options = replace(options, pyconfig_header=patched_pyconfig)

    try:
        zig_build(options, cwd=VENDORED_PROJECT_DIR, extra_args=["-p", str(dest)])
    except subprocess.CalledProcessError as exc:
        # Best-effort: Zig still installs whatever *did* build even when some
        # module(s) failed (each stdlib C-extension is an independent compile step;
        # `zig build install` reports overall failure if any one of them errors, but
        # doesn't withhold the artifacts that succeeded). A handful of modules with
        # their own platform-specific gaps shouldn't block using the interpreter for
        # scripts that don't import them -- only the core interpreter itself
        # (`bin/python`, `lib/libpythonX.Y.so`) is load-bearing here. Re-raise only if
        # that core didn't come out the other end.
        if not bin_path.exists():
            raise
        _logger.warning(
            "build_own_python: some stdlib extension module(s) failed to build for "
            "target %s (core interpreter succeeded; scripts needing a module that "
            "didn't build will fail to import it): %s",
            target or "native",
            exc,
        )
    assert_path_exists(str(bin_path))
    return dest


def _own_python_toolchain(dest: Path, target: str | None) -> PythonToolchain:
    """
    The `PythonToolchain` (see `compiler.py`) pointing `compile_executable` at a
    `build_own_python`-built interpreter: headers resolved straight from
    meta-python's vendored `cpython` source/build tree (`zig build install` never
    installs them anywhere -- verified empirically, `zig-out` has no `include/`), at
    the same in-tree layout CPython's own Makefile compiles against
    (`-I<cpython_root> -I<cpython_root>/Include`, confirmed from its generated
    `Makefile`'s `PY_CPPFLAGS`); `libpythonX.Y.so` from `dest/lib/` (`build_own_python`
    always builds with `python-linkage=dynamic`).

    `target` is threaded through so the entrypoint's own C is compiled and linked for
    the same libc this interpreter was built against -- see `PythonToolchain.target`.
    """
    from metapython.compile import VENDORED_PROJECT_DIR

    cpython_src = VENDORED_PROJECT_DIR / "cpython"
    lib_dir = dest / "lib"
    candidates = sorted(lib_dir.glob("libpython*.so"))
    assert candidates, (
        f"No libpythonX.Y.so found under {lib_dir} -- "
        "was build_own_python built with python_linkage=dynamic?"
    )
    library_name = candidates[0].name.removeprefix("lib").rsplit(".so", 1)[0]
    return PythonToolchain(
        include_dirs=[str(cpython_src), str(cpython_src / "Include")],
        library_dir=str(lib_dir),
        library_name=library_name,
        target=target,
    )


def _exclude_host_stdlib_extensions(extensions: dict[str, str]) -> dict[str, str]:
    """
    Drops standard-library modules from `_discover_extension_modules`'s result. Only
    meaningful when `own_python` is set: those get replaced wholesale by
    `build_own_python`'s own `lib-dynload` instead (see `_copy_own_python_runtime`) --
    bundling the host's copy would link it against the host's libpython, defeating the
    point of building smelt's own. Genuinely third-party native dependencies (numpy,
    ...) are unaffected: meta-python doesn't build those, so they still have to come
    from the host's site-packages regardless.

    Membership is decided on the module's own top-level name against
    `sys.stdlib_module_names`, not on where its `.so` sits on disk: the host stdlib's
    location varies by flavor (a venv's `platstdlib` is not where its base install
    actually keeps `lib-dynload`, and a `python-build-standalone` interpreter has most
    of these compiled in as builtins with no file at all), and getting that path wrong
    fails silently -- as a host stdlib `.so` bundled next to a foreign `libpython`.
    """
    return {
        name: origin
        for name, origin in extensions.items()
        if name.partition(".")[0] not in sys.stdlib_module_names
    }


def _copy_own_python_runtime(own_python_dir: Path, dist_dir: Path) -> dict[str, str]:
    """
    Copies `build_own_python`'s `libpythonX.Y.so` and stdlib `lib-dynload/*.so`
    modules into `dist_dir` -- the `own_python` counterpart to `_copy_extension_modules`
    for the entrypoint's *own* discovered dependencies. Copies every built
    `lib-dynload` module unconditionally rather than cross-referencing which stdlib
    modules the entrypoint actually needs: simpler, and safe -- unused extra `.so`s
    bundled cost dist-dir size, not correctness, unlike silently dropping one that
    turns out to be needed.

    Flat into the dist dir's root, *not* into a `lib-dynload/` subdirectory mirroring
    the interpreter's own layout: a Nuitka standalone binary points `sys.path` at its
    own directory and nothing else, so a `.so` one level down is simply not importable
    (and `lib-dynload` is only ever found by a normal interpreter because its
    `sys.prefix`-derived path computation puts it there -- machinery standalone mode
    deliberately bypasses). Flat is also what Nuitka's own `--standalone` produces.

    Returns `{dest_rel_path: source_path}` for the copies made, in the same shape
    `_copy_extension_modules` returns, so callers can `ldd`-walk/RPATH-patch both
    uniformly.
    """
    lib_dir = own_python_dir / "lib"
    placed: dict[str, str] = {}
    for so_path in lib_dir.glob("libpython*.so"):
        shutil.copy2(so_path, dist_dir / so_path.name)
        placed[so_path.name] = str(so_path)

    dynload_dir = next(lib_dir.glob("python3.*/lib-dynload"), None)
    if dynload_dir is not None:
        for so_path in dynload_dir.glob("*.so"):
            shutil.copy2(so_path, dist_dir / so_path.name)
            placed[so_path.name] = str(so_path)
    return placed


def assemble_standalone_dist(
    path: str,
    dist_dir: str | os.PathLike[str],
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
    include_package_data: Iterable[str] | None = None,
    include_data_files: Iterable[str] | None = None,
    extra_flags: Iterable[str] | None = None,
    extra_search_paths: Iterable[str] | None = None,
    output_name: str | None = None,
    no_cache: bool = False,
    own_python: bool = False,
    own_python_target: str | None = DEFAULT_OWN_PYTHON_TARGET,
    debug: bool = False,
) -> Path:
    """
    Builds a self-contained dist directory for the entrypoint script `path`: the
    executable plus every native shared-library dependency it needs, with no leftover
    dependency on the original Python install or any host system library that varies
    across distros.

    `own_python`: compile/link the entrypoint against smelt's own Zig-built CPython
    (`build_own_python`) instead of the running interpreter, and bundle *that* build's
    `libpythonX.Y.so` and stdlib C-extension modules instead of the host Python's. Off
    by default: reuses the host Python exactly as today (and as Nuitka's own
    `--standalone` does), which is cheaper and doesn't require a `meta-python` build.
    Only the entrypoint script's *own* (non-stdlib) native dependencies come from the
    host's site-packages either way -- meta-python doesn't build third-party packages.

    `own_python_target` (a Zig target triple, `None` for a native build -- see
    `DEFAULT_OWN_PYTHON_TARGET`) decides how far that goes, and the difference is not
    cosmetic:

    * **Native**: libc and its loader still come from the host, exactly as for a
      host-Python standalone build -- they stay on `_LINUX_SYSTEM_DLL_IGNORE_PREFIXES`
      and the exe keeps the host's `PT_INTERP`. What stops depending on the host is
      everything above libc: the interpreter, `libpython`, and the stdlib's own
      extension modules are smelt's build, not the machine's Python install.
    * **musl**: additionally bundles this build's own combined loader/libc
      (`ld-musl-<arch>.so.1`, ordinarily excluded as "always present on the target" --
      but once "the target" is meta-python's own build rather than an arbitrary host,
      its own copy is exactly what should ship) and repoints the exe's ELF interpreter
      at it (`_set_interpreter`). That is the one piece that is *not* RPATH-relative,
      so a dist dir built this way must stay at the absolute path it was assembled at.
      Requires the entrypoint's third-party extension modules to be musl-built too --
      see `build_own_python`'s docstring.

    Two genuinely different mechanisms are involved, and it is worth being precise about
    which covers what:

    * **Pure-Python code** (the followed script itself, plus the standard library) is
      handled entirely by `_build_nuitka_exe_extension(standalone=True)`: Nuitka embeds
      it as either compiled C or, for anything not statically reachable, real CPython
      frozen modules registered *before* `Py_InitializeFromConfig` runs (see that
      function's docstring). No separate `.py`/`.pyc` file is ever written to `dist_dir`
      for this part -- verified empirically to run under `env -i` from an arbitrary
      location.
    * **Native extension modules** (`.so` files CPython `dlopen()`s at import time --
      `_ssl`, `_socket`, third-party compiled packages, ...) are not covered by that
      mechanism at all and must be discovered and copied here, which is what the rest of
      this function does: `_discover_extension_modules` finds which ones `path` actually
      imports (the same static-reachability limitation `--follow-imports` itself has --
      an extension only imported behind a runtime branch needs an explicit
      `include_modules`/`include_packages` hint), `_collect_native_dependencies` walks
      their own (and the executable's own) `ldd` closure, and both get copied into
      `dist_dir` with RPATHs patched to resolve via `$ORIGIN` alone.

    This deliberately does not call into Nuitka's own DLL-detection/copy machinery
    (`freezer.Standalone.detectUsedDLLs`/`copyDllsUsed`): that only runs inside Nuitka's
    own Scons-driven `--standalone` build, which smelt does not otherwise invoke (and
    which, on top of that, does not work at all in every environment -- e.g. this was
    developed against a host where a real `--standalone` build fails outright with a
    missing `-l:libatomic.a`, a Zig/target issue orthogonal to smelt's own direct-link
    path). Reimplementing discovery independently, against the stable `ldd`/subprocess
    surface rather than Nuitka's internal freezer API, is exactly the "keep this phase
    optional/pluggable" hedge called for before attempting it.

    ELF/Linux only for now, consistent with the rest of this module's `patchelf`
    dependency; raises `SmeltError` on other platforms rather than silently producing an
    incomplete dist dir.
    """
    if platform.system() != "Linux":
        raise SmeltError(
            f"assemble_standalone_dist is only implemented for Linux, not {platform.system()}"
        )

    own_python_dir = (
        build_own_python(target=own_python_target, no_cache=no_cache, debug=debug)
        if own_python
        else None
    )
    python_toolchain = (
        _own_python_toolchain(own_python_dir, own_python_target)
        if own_python_dir is not None
        else None
    )

    ext = _build_nuitka_exe_extension(
        path,
        standalone=True,
        no_follow_imports=no_follow_imports,
        stdout=stdout,
        include_modules=include_modules,
        include_packages=include_packages,
        include_package_data=include_package_data,
        include_data_files=include_data_files,
        extra_flags=extra_flags,
        extra_search_paths=extra_search_paths,
        no_cache=no_cache,
    )
    built_exe = compile_executable(ext, python_toolchain=python_toolchain, debug=debug)

    # `--standalone` makes Nuitka eagerly create its own (always script-basename-named,
    # regardless of `output_name`) dist directory before `--generate-c-only` short-
    # circuits the rest of its pipeline -- empty, but left behind in the cwd otherwise.
    stray_dist_dir = Path(os.path.basename(path).replace(".py", ".dist")).resolve()
    if stray_dist_dir.is_dir() and stray_dist_dir != Path(dist_dir).resolve():
        shutil.rmtree(stray_dist_dir)

    dist_path = Path(dist_dir)
    if dist_path.exists():
        shutil.rmtree(dist_path)
    dist_path.mkdir(parents=True)

    exe_dest = dist_path / _bin_name_for(path, output_name)
    shutil.move(str(built_exe), str(exe_dest))

    extensions = _discover_extension_modules(path, extra_search_paths or ())
    if own_python:
        extensions = _exclude_host_stdlib_extensions(extensions)
    placed = _copy_extension_modules(extensions, dist_path)

    if own_python_dir is not None:
        placed.update(_copy_own_python_runtime(own_python_dir, dist_path))

    # A musl `own_python` build bundles its own libc/loader too (instead of leaving it
    # to `_ALWAYS_HOST_DLL_PREFIXES`-only exclusion, i.e. the "assume the host's libc"
    # default) -- "the target" there is meta-python's own build, so its own copy is
    # exactly what should ship. A *native* own-python build must not: it links against
    # the host's own glibc, whose loader/libc pair cannot be redistributed piecemeal
    # (see `build_own_python`'s docstring -- bundling it segfaults inside the loader).
    bundles_libc = own_python and _is_musl_target(own_python_target)
    ignore_prefixes = (
        _ALWAYS_HOST_DLL_PREFIXES if bundles_libc else _LINUX_SYSTEM_DLL_IGNORE_PREFIXES
    )
    # Walk the *source* copies, not the ones already in `dist_path`: a wheel-shipped
    # extension module typically resolves its own vendored libraries through an RPATH
    # relative to where the wheel installed it (`$ORIGIN/../../../pyzmq.libs`, ...),
    # which resolves to nothing once the `.so` sits in the dist dir instead -- `ldd`
    # would report those as `not found` and they would be silently dropped rather than
    # bundled. `placed` maps each destination back to exactly that original path.
    native_deps = _collect_native_dependencies(
        [str(exe_dest), *placed.values()],
        ignore_prefixes,
    )
    _copy_native_dependencies(native_deps, dist_path)

    _copy_package_data(include_package_data or (), dist_path, extra_search_paths or ())

    for entry in include_data_files or ():
        source, _, dest_rel = entry.partition("=")
        if not dest_rel:
            continue
        dest_path = dist_path / dest_rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest_path)

    package_dirs = _package_rpath_dirs(placed)
    main_rpath = ":".join(["$ORIGIN", *(f"$ORIGIN/{d}" for d in sorted(package_dirs))])
    _set_rpath(exe_dest, main_rpath)
    for dest_rel in placed:
        _set_rpath(dist_path / dest_rel, _nested_rpath(dest_rel))
    for basename in native_deps:
        _set_rpath(dist_path / basename, "$ORIGIN")

    if bundles_libc:
        interpreters = [name for name in native_deps if name.startswith("ld-musl")]
        assert interpreters, (
            f"own_python_target={own_python_target!r} but no ld-musl-*.so* found among "
            "the bundled native deps -- was build_own_python built with "
            "libc_linkage=dynamic?"
        )
        _set_interpreter(exe_dest, dist_path.resolve() / interpreters[0])

    return dist_path
