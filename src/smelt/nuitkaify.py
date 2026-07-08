"""
Wrapper on top of nuitka to compile a Python script into a standalone executable.

Currently nuitka is called as a subprocess, as it would be from `python -m nuitka`.
Options are passed as CLI arguments.

This is the simple option as nuitka is not really designed for library use: some of the business logic
is run on import, a few critical components are handled global variables, so there a some major drawbacks to
trying to import the code and call directly.
This might be changed later.

@date: 11.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
import sys
from pathlib import Path
from typing import Final, Iterable, Iterator, Literal

from setuptools import Extension

from .process import call_command

from smelt.context import get_context
from smelt.utils import GenericExtension, PathSolver
from smelt.config import NuitkaModule

_logger = logging.getLogger(__name__)


NUITKA_ENTRYPOINT: Final[tuple[str, ...]] = (sys.executable, "-m", "nuitka")

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


def locate_nuitka_headers() -> list[Path]:
    header_folders: list[Path] = []
    import nuitka

    nuitka_root = Path(nuitka.__file__).parent
    header_folders.append(nuitka_root / "build" / "static_src")
    header_folders.append(nuitka_root / "build" / "inline_copy" / "libbacktrace")
    header_folders.append(nuitka_root / "build" / "inline_copy" / "zlib")
    header_folders.append(nuitka_root / "build" / "include")

    return header_folders


def iterate_nuitka_c_sources(build_folder: str) -> Iterator[Path]:
    """
    Iterates over all C sources that should be compiled from the passed build fodler
    """
    root = Path(build_folder)
    for f in os.listdir(build_folder):
        if f.endswith(".c"):
            yield root / f

    static_src = root / "static_src"
    for f in os.listdir(static_src):
        if f.endswith(".c"):
            yield static_src / f


# Static runtime C sources that a full Nuitka module build copies into the build
# folder but `--generate-c-only` does not. Mirrors
# `nuitka.build.SconsInterface.provideStaticSourceFilesBackend` for module mode
# (exe/dll additionally pull `MainProgram.c`, which we never build here).
# Hardcoded on purpose: calling Nuitka's own function requires its Options global
# state to be initialised, which the subprocess-based invocation deliberately avoids.
NUITKA_MODULE_STATIC_SOURCES: Final[tuple[str, ...]] = ("CompiledFunctionType.c",)


def _nuitka_root() -> Path:
    import nuitka

    return Path(nuitka.__file__).parent


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
            f"Nuitka data composer failed with exitcode {cmd_trace.exit_code}: {' '.join(cmd)}"
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


def provide_nuitka_static_sources(build_folder: str) -> None:
    """
    Copies the static runtime C sources that `--generate-c-only` omits into the build
    folder's `static_src`, mirroring `provideStaticSourceFilesBackend` for module mode.
    """
    src_dir = _nuitka_root() / "build" / "static_src"
    dst_dir = Path(build_folder) / "static_src"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for filename in NUITKA_MODULE_STATIC_SOURCES:
        shutil.copyfile(src_dir / filename, dst_dir / filename)


def compile_with_nuitka(
    path: str,
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
) -> str:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.
    """
    context = get_context()
    try:
        import nuitka

        # not using the import - just checking if it is available
        # as following logic would fail otherwise
        _ = nuitka
    except ImportError:
        raise ImportError(
            "Nuitka is not installed. Please install this package with nuitka extra: `pip install smelt[nuitka]`."
        )
    cmd = list(NUITKA_ENTRYPOINT)
    if not no_follow_imports:
        cmd.append("--follow-imports")
    cmd.append("--onefile")
    cmd.append(path)

    # handling special flags
    if include_modules:
        for mod in include_modules:
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    _logger.debug("Running %s", " ".join(cmd))

    # Standalone/onefile builds shell out to `patchelf`; prefer our bundled, known-good
    # copy over whatever the system ships (some distro releases are Nuitka-blacklisted).
    with _bundled_patchelf_on_path():
        cmd_trace = call_command(*cmd, printer=print)
    if context:
        context.add_trace(cmd_trace)
    if cmd_trace.exit_code != 0:
        raise RuntimeError(
            f"Nuitka failed with exitcode {cmd_trace.exit_code}: {' '.join(cmd)}"
        )

    expected_extension = ".exe" if sys.platform == "Windows" else ".bin"
    bin_path = os.path.basename(path).replace(".py", expected_extension)
    absolute_bin_path = os.path.join(os.getcwd(), bin_path)
    assert os.path.exists(absolute_bin_path), (
        f"Nuitka binary not found at {absolute_bin_path}"
    )
    return absolute_bin_path


def nuitkaify_module(
    module: NuitkaModule,
    path_solver: PathSolver | None = None,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
) -> GenericExtension:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.
    """
    path_solver = path_solver or PathSolver()
    context = get_context()
    src_path = module.source or path_solver.resolve_import_path(module.import_path)
    mod_filename = os.path.basename(src_path)
    cmd = list(NUITKA_ENTRYPOINT)
    cmd.append("--module")
    cmd.append(str(src_path))
    # We compile ourselves, so ask Nuitka for the C sources only. `--generate-c-only`
    # returns early (MainControl.shallNotDoExecCCompilerCall) and therefore skips three
    # steps a full build performs: the data composer (constants blob), the constants-blob
    # C wrapper, and copying static runtime sources. We reproduce all three below, which
    # avoids the previous behaviour of letting Nuitka compile fully just to recompile.
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
        raise RuntimeError(
            f"Nuitka failed with exitcode {cmd_trace.exit_code}: {' '.join(cmd)}"
        )

    build_folder = mod_filename.replace(".py", ".build")
    assert os.path.exists(build_folder)

    # Reproduce the build steps `--generate-c-only` skips (see comment above).
    blob_path = run_nuitka_data_composer(build_folder)
    write_constants_incbin(build_folder, blob_path)
    provide_nuitka_static_sources(build_folder)

    c_sources = [str(src) for src in iterate_nuitka_c_sources(build_folder)]
    assert c_sources, (
        "Nuitka did not produce any C file or build folder path logic is incorrect"
    )
    header_sources = [str(f) for f in locate_nuitka_headers()]
    header_sources.append(build_folder)
    # patching build_definitions.h, as we don't need extensions
    open(os.path.join(build_folder, "build_definitions.h"), "w+").close()
    setup_tools_ext = Extension(
        name=mod_filename.replace(".py", ""),
        sources=c_sources,
        include_dirs=header_sources,
        define_macros=NUITKA_MACROS,
        libraries=["m", "dl", "z"],
        extra_compile_args=list(NUITKA_MINIMAL_FLAGS),
    )
    return GenericExtension(
        import_path=module.import_path,
        src_path=str(src_path),
        extension=setup_tools_ext,
        dest_folder=Path(src_path).parent,
    )
