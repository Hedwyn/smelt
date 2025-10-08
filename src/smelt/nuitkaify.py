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
import subprocess
import sys
from pathlib import Path
from typing import Final, Iterable, Iterator, Literal

from setuptools import Extension

_logger = logging.getLogger(__name__)

NUITKA_ENTRYPOINT: Final[tuple[str, ...]] = (sys.executable, "-m", "nuitka")

type Stdout = Literal["stdout", "logger"]

# TODO: this should be built dynamically, obviously
NUITKA_MACROS = [
    ("_XOPEN_SOURCE", None),
    ("__NUITKA_NO_ASSERT__", None),
    ("_NUITKA_CONSTANTS_FROM_CODE", None),
    ("_NUITKA_FROZEN", 0),
    # TODO:
    # Note: seems that that one was NUITKA_MODULE_MODE
    # and was renamed around Nuitka 2.7.9 to _NUIKA_MODULE
    ("_NUITKA_MODULE_MODE", 1),
]

NUITKA_MINIMAL_FLAGS: Final[tuple[str, ...]] = (
    "-std=c11",
    "-fwrapv",
    "-pipe",
    "-w",
    "-fvisibility=hidden",
    "-fvisibility-inlines-hidden",
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

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    while True:
        assert proc.stdout is not None, "Process not created with stdout in PIPE mode"
        line = proc.stdout.readline()
        if not line:
            break
        decoded_line = line.decode().rstrip()
        if stdout == "logger":
            _logger.info(decoded_line)
        elif stdout == "stdout":
            print(decoded_line)

    if proc.returncode is None:
        proc.wait(timeout=1.0)

    if proc.returncode != 0:
        raise RuntimeError(
            f"Nuitka failed with exitcode {proc.returncode}: {' '.join(cmd)}"
        )
    expected_extension = ".exe" if sys.platform == "Windows" else ".bin"
    bin_path = os.path.basename(path).replace(".py", expected_extension)
    absolute_bin_path = os.path.join(os.getcwd(), bin_path)
    assert os.path.exists(
        absolute_bin_path
    ), f"Nuitka binary not found at {absolute_bin_path}"
    return absolute_bin_path


def nuitkaify_module(
    path: str,
    no_follow_imports: bool = False,
    stdout: Stdout | None = None,
    include_modules: Iterable[str] | None = None,
    include_packages: Iterable[str] | None = None,
) -> Extension:
    """
    Compiles the module given by `path`.
    Follows imports by default, but can be disabled with `no_follow_imports`.
    """
    cmd = list(NUITKA_ENTRYPOINT)
    cmd.append("--module")
    cmd.append(path)
    # TODO: the clean approach will to use the `--generate-c-only` since we want to
    # compile ourselves, however, when enabled nuitka generates the C code
    # but does not copy the static source files from its own package
    # These static files can be found in the .build folder when running a full build
    # but not when using generate-c-only.
    # The logic to find the ones that are required is not so trivial (nuitka only includes the ones)
    # that are actually needed, we we would have to tap into the scons stuff to extract them
    # ideal way would to do a dry run to extract all the build system data instead of trying
    # to reproduce it
    # As it now, nuitka will do the full compilation only for us to recompile again
    # cmd.append("--generate-c-only")

    # handling special flags
    if include_modules:
        for mod in include_modules:
            cmd.append(f"--include-module={mod}")

    if include_packages:
        for package in include_packages:
            cmd.append(f"--include-package={package}")

    _logger.debug("Running %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    while True:
        assert proc.stdout is not None, "Process not created with stdout in PIPE mode"
        line = proc.stdout.readline()
        if not line:
            break
        decoded_line = line.decode().rstrip()
        if stdout == "logger":
            _logger.info(decoded_line)
        elif stdout == "stdout":
            print(decoded_line)

    if proc.returncode is not None:
        _logger.info("[Nuitka]: %d", proc.returncode)
    modname = os.path.basename(path)
    build_folder = modname.replace(".py", ".build")
    assert os.path.exists(build_folder)
    c_sources = [str(src) for src in iterate_nuitka_c_sources(build_folder)]
    assert (
        c_sources
    ), "Nuitka did not produce any C file or build folder path logic is incorrect"
    header_sources = [str(f) for f in locate_nuitka_headers()]
    header_sources.append(build_folder)
    # patching build_definitions.h, as we don't need extensions
    open(os.path.join(build_folder, "build_definitions.h"), "w+").close()
    return Extension(
        name=modname.replace(".py", ""),
        sources=c_sources,
        include_dirs=header_sources,
        define_macros=NUITKA_MACROS,
        libraries=["m", "dl", "z"],
        extra_compile_args=list(NUITKA_MINIMAL_FLAGS),
    )
