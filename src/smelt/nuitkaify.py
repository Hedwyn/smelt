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

import logging
import os
import platform
import re
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from setuptools import Extension

from smelt.compiler import ZigCompiler, python_import_library_link_args
from smelt.config import NuitkaModule
from smelt.context import create_context_if_enabled, get_context
from smelt.utils import GenericExtension, PathSolver

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


def provide_nuitka_static_sources(build_folder: str) -> None:
    """
    Copies the static runtime C sources that `--generate-c-only` omits into the build
    folder's `static_src`. Used by the standalone build path, which compiles the runtime
    straight into the module `.so`.
    """
    src_dir = _nuitka_root() / "build" / "static_src"
    dst_dir = Path(build_folder) / "static_src"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for filename in NUITKA_MODULE_STATIC_SOURCES:
        shutil.copyfile(src_dir / filename, dst_dir / filename)


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
            extra_postargs=list(NUITKA_MINIMAL_FLAGS),
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
    """
    # Symbols per Nuitka's contract for the "__constant.bin" blob:
    # INCBIN(constant_bin, ...) defines `constant_bin_data`; getConstantBlobData()
    # is declared by NUITKA_DECLARE_CONSTANT_BLOB(constant_bin, ConstantBlob, ...).
    contents = f"""\
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
    try:
        import nuitka

        # not using the import - just checking if it is available
        # as following logic would fail otherwise
        _ = nuitka
    except ImportError:
        raise ImportError(
            "Nuitka is not installed. Please install this package with nuitka extra: `pip install smelt[nuitka]`.",
        )
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

    env_overrides: dict[str, str] = {}
    if extra_search_paths:
        search_roots = list(extra_search_paths)
    elif include_modules or include_packages:
        # `--include-module`/`--include-package` name modules that are never actually
        # `import`-ed anywhere (e.g. a mypyc runtime extension), so Nuitka can't infer
        # their location the way it does for followed imports; it resolves them via
        # plain `sys.path` lookup instead. Each name's own root is resolved
        # independently via import machinery (see `import_path_search_root`), rather
        # than assumed to share `path`'s root.
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
    if search_roots:
        existing = os.environ.get("PYTHONPATH")
        env_overrides["PYTHONPATH"] = os.pathsep.join(
            [*search_roots, existing] if existing else search_roots,
        )

    if platform.system() == "Windows" and "CC" not in os.environ:
        # This is the only step that invokes an actual C backend compiler, and Windows
        # has none by default. Nuitka's fallback is to download its own MinGW64 gcc, but
        # smelt uses Zig for every other compile step, so point it at our already-
        # installed Zig instead: Nuitka's Scons backend recognizes a `CC` binary named
        # "zig" and drives it correctly (`zig cc ...`), no Nuitka-version-specific flag
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
        build_nuitka_runtime(header_sources, dest_folder)
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
