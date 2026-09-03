"""
Defines a distutils compiler based on Zig.

@date: 27.05.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from distutils.compilers.C.unix import Compiler
from setuptools import Extension

from smelt.process import call_command
from smelt.utils import (
    ImportPath,
    PathExists,
    PathSolver,
    SmeltError,
    assert_path_exists,
    get_extension_suffix,
    path_exists,
)

if TYPE_CHECKING:
    from os import PathLike

_SMELT_ROOT: Final[str] = os.path.dirname(__file__)
PYCONFIG_PATH: Final[str] = os.path.join(_SMELT_ROOT, "pyconfig")

_logger = logging.getLogger(__name__)

# MSVC (cl.exe) optimization-level flags, mapped to their zig/clang equivalent.
_MSVC_OPT_LEVELS: Final[dict[str, str]] = {
    "/Od": "-O0",
    "/O1": "-O1",
    "/O2": "-O2",
    "/Ox": "-O3",
}


def sanity_msvc_flags(flags: Iterable[str]) -> list[str]:
    """
    Strip or translate MSVC-style (`/`-prefixed) compiler flags out of a flag list.

    Smelt standardizes on Zig as its single, cross-platform compiler interface:
    every extension is built through `zig cc`, a clang/GNU-style driver, on every
    host platform, Windows included. Some backends we shell out to (e.g. mypyc's
    `mypycify`) pick their compiler flags by calling distutils'
    `ccompiler.new_compiler()`, which selects flags based on the *host platform*
    rather than the compiler actually in use, so on Windows they hand back
    MSVC-flavored flags (`/O2`, `/DEBUG:FULL`, `/wd4102`, ...) regardless of the
    fact that zig, not cl.exe, will consume them.

    `zig cc` doesn't recognize `/`-prefixed arguments as flags at all: it parses
    them as positional input files, so a flag like `/O2` fails the build with an
    "unknown file" error instead of being silently ignored. This helper closes
    that gap: known MSVC flags are translated to their zig/clang equivalent,
    and unrecognized MSVC-only flags (warning suppression, runtime linkage,
    debug format, ...) are dropped rather than left to break the build.

    Parameters
    ----------
    flags: Iterable[str]
        Raw compiler flags, as produced by an upstream backend.

    Returns
    -------
    list[str]
        `flags` with every MSVC-style entry translated or removed. Flags not
        starting with `/` (already zig/clang-compatible) pass through unchanged.
    """
    sanitized: list[str] = []
    for flag in flags:
        if not flag.startswith("/"):
            sanitized.append(flag)
        elif flag in _MSVC_OPT_LEVELS:
            sanitized.append(_MSVC_OPT_LEVELS[flag])
        elif flag.startswith("/D"):
            sanitized.append("-D" + flag[2:])
        elif flag.startswith("/I"):
            sanitized.append("-I" + flag[2:])
        # else: MSVC-only flag (e.g. /DEBUG:*, /wd####, /W3, /MD, /GX) with no
        # zig/clang equivalent needed for a shared-library build: drop it.
    return sanitized


#: Compile-time flags distinguishing a release build from a debug one.
#:
#: `-g0` is not redundant: unlike gcc/clang, where debug info is opt-in via `-g`,
#: `zig cc` emits it by *default* (verified: the same source compiled with and
#: without `-g0` yields 4 debug sections vs 0). Nothing in smelt's own flag set ever
#: asked for `-g`, yet DWARF accounted for 81% of a real entrypoint binary -- 221 MB
#: of 277 MB -- because nothing turned it off either. Nuitka's own Scons backend
#: strips by default; smelt bypasses Scons, so this is where that has to happen.
_RELEASE_COMPILE_FLAGS: Final[tuple[str, ...]] = ("-g0",)
_DEBUG_COMPILE_FLAGS: Final[tuple[str, ...]] = ("-g",)

#: Link-time counterpart. `-g0` above stops debug info being generated at all; `-s`
#: additionally drops the symbol table (`.symtab`), which `-g0` leaves behind -- worth
#: a few more MB on a large binary, and it also covers any object smelt did not
#: compile itself.
_RELEASE_LINK_FLAGS: Final[tuple[str, ...]] = ("-s",)


def build_mode_compile_flags(debug: bool) -> list[str]:
    """Compiler flags selecting a debug (full DWARF) or release (no debug info) build.

    Appended *after* the caller's own flags so they win on conflict: clang-style
    drivers take the last of `-g`/`-g0`.
    """
    return list(_DEBUG_COMPILE_FLAGS if debug else _RELEASE_COMPILE_FLAGS)


def build_mode_link_flags(debug: bool) -> list[str]:
    """Linker flags selecting a debug (symbols kept) or release (stripped) build."""
    return [] if debug else list(_RELEASE_LINK_FLAGS)


def _zig_shared_lib_name(name: str) -> str:
    """
    Name of the shared library `zig build-lib --name {name}` (or `zig build`
    building a matching target) produces on the current host platform.
    """
    system = platform.system()
    if system == "Windows":
        return f"{name}.dll"
    if system == "Darwin":
        return f"lib{name}.dylib"
    return f"lib{name}.so"


class SupportedPlatforms(StrEnum):
    """
    All the target platforms supported from cross-compilation.
    Value of the enum corresponds the platform name as expected by Zig compiler.
    """

    # TODO: parametrize OS
    AARCH64_LINUX = "aarch64-linux"
    ARMV7L_LINUX = "arm-linux-gnueabihf"
    X86_64_LINUX = "x86_64-linux"
    # TODO: add more

    def get_triple_name(self) -> str:
        """
        Returns the "triple" platform name <arch>-<os>-<libc>
        as used by Python for this target.
        Note: automatically assumed libc here, as there's no support currently for other options.
        """
        # Note: Python can be built for multiple LibCs:
        # gnu, musl, android...
        # Currently hard-coding LibC, which would be the choices for 95%+ projects
        # out there. Other libC might be considered later
        if self == SupportedPlatforms.ARMV7L_LINUX:
            return self.value
        return self.value + "-gnu"


class ZigCompiler(Compiler):
    """
    Base class for zig compiler.
    Sets the C/C++ compiler exe to zig cc and zig c++ and
    add .zig files to the list of accepted extensions.
    """

    zig_base_path: ClassVar[list[str]] = [sys.executable, "-m", "ziglang"]
    # Expanding to add .zig files
    src_extensions: ClassVar[list[str]] = Compiler.src_extensions + [".zig"]

    executables: ClassVar[dict[str, list[str] | None]] = {
        "preprocessor": None,
        "compiler": [*zig_base_path, "cc"],
        "compiler_so": [*zig_base_path, "cc"],
        "compiler_cxx": [*zig_base_path, "c++"],
        "compiler_so_cxx": [*zig_base_path, "c++"],
        "linker_so": [*zig_base_path, "cc", "-shared"],
        "linker_so_cxx": [*zig_base_path, "c++", "-shared"],
        "linker_exe": [*zig_base_path, "cc"],
        "linker_exe_cxx": [*zig_base_path, "c++", "-shared"],
        "archiver": ["ar", "-cr"],
        "ranlib": None,
    }


def zig_build_lib(
    name: str,
    object_files: list[str],
    crosscompile: SupportedPlatforms | None = None,
) -> str:
    """
    Builds a shared library using Zig's native interface (build-lib)
    instead of zig cc.

    Parameters
    ----------
    name: str
        The name of the library to build, without extension.

    object_files: list[str]
        List of object files to include in the shared library.

    crosscompile: SupportedPlatforms | None
        The target platform for cross-compilation.
        If None, builds for the current platform.

    Returns
    -------
    str
        The path to the built shared library.
    """
    cmd = ["python", "-m", "ziglang", "build-lib"]
    if crosscompile is not None:
        cmd.extend(["-target", crosscompile.value])
    # we need position independant code
    cmd.append("-dynamic")
    # as we'll wrapp this into a shared library later
    cmd.append("-fPIC")
    # Python itself is only linked at runtime,
    # so we need to allow undefined symbols
    cmd.append("-fallow-shlib-undefined")
    cmd.extend(["--name", name])
    cmd.extend(object_files)

    _logger.info("Running zig build-lib: \n%s", " ".join(cmd))
    subprocess.run(cmd)
    # Note: zig build-lib produces a file named per _zig_shared_lib_name(name)

    if crosscompile is not None:
        suffix = get_extension_suffix(crosscompile.get_triple_name())
    else:
        suffix = sysconfig.get_config_var("EXT_SUFFIX")
    # copying
    dest_name = f"{name}{suffix}"
    # copying the shared library to the expected name
    shutil.copy(_zig_shared_lib_name(name), dest_name)
    return dest_name


def zig_build_exe(
    name: str,
    object_files: list[str],
    crosscompile: SupportedPlatforms | None = None,
) -> str:
    """
    Builds a shared library using Zig's native interface (build-lib)
    instead of zig cc.

    Parameters
    ----------
    name: str
        The name of the library to build, without extension.

    object_files: list[str]
        List of object files to include in the shared library.

    crosscompile: SupportedPlatforms | None
        The target platform for cross-compilation.
        If None, builds for the current platform.

    Returns
    -------
    str
        The path to the built shared library.
    """
    cmd = ["python", "-m", "ziglang", "build-exe"]
    if crosscompile is not None:
        cmd.extend(["-target", crosscompile.value])
    # we need position independant code
    # Python itself is only linked at runtime,
    # so we need to allow undefined symbols
    cmd.extend(["--name", name])
    cmd.extend(object_files)

    _logger.info("Running zig build-lib: \n%s", " ".join(cmd))
    subprocess.run(cmd)
    # Note: zig build-lib will produce a file named lib{name}.so

    if crosscompile is not None:
        suffix = get_extension_suffix(crosscompile.get_triple_name())
    else:
        suffix = sysconfig.get_config_var("EXT_SUFFIX")
    # copying
    dest_name = f"{name}{suffix}"
    # copying the shared library to the expected name
    # shutil.copy(f"lib{name}.so", dest_name)
    return dest_name


def compile_zig_module(
    name: str,
    folder: PathExists,
    import_path: ImportPath,
    path_solver: PathSolver | None = None,
    flags: list[str] | None = None,
) -> PathExists:
    path_solver = path_solver or PathSolver()
    flags = flags or []
    with contextlib.chdir(folder):
        call_command("zig", "build", *flags)
        lib_path = Path.cwd() / "zig-out" / "lib" / _zig_shared_lib_name(name)
    if not path_exists(lib_path):
        raise SmeltError(
            f"Ran `zig build` successfully, but no library `{lib_path}` was found afterwards"
            "Check that the name of the project is properly configured, as well as your build.zig"
        )
    # TODO crosscompile
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    target_path = path_solver.resolve_import_path(
        import_path, file_extension=suffix, should_exist=False
    )
    shutil.move(
        lib_path,
        target_path,
    )
    assert path_exists(target_path)
    return target_path


def python_import_library_link_args() -> tuple[list[str], list[str]]:
    """
    Extra `library_dirs`/`libraries` needed to resolve the Python C-API at link time.

    Unlike ELF/Mach-O shared libs, Windows DLLs must resolve __declspec(dllimport)
    symbols (e.g. PyModule_Create2) at link time: link explicitly against the
    Python import library. On POSIX those symbols are resolved at runtime against
    the running interpreter, so nothing extra is needed there.
    """
    if platform.system() != "Windows":
        return [], []
    return (
        [os.path.join(sys.base_prefix, "libs")],
        [f"python{sys.version_info.major}{sys.version_info.minor}"],
    )


def python_embed_library_link_args() -> tuple[list[str], list[str]]:
    """
    Extra `library_dirs`/`libraries` needed to link libpython into an executable that
    embeds the interpreter.

    Unlike an extension module -- loaded *by* an already-running interpreter, which
    resolves the Python C-API against it (see `python_import_library_link_args`) -- an
    embedding executable is the one starting the interpreter, so it must link against
    libpython explicitly on every platform, not just Windows.
    """
    if platform.system() == "Windows":
        return python_import_library_link_args()
    libdir = sysconfig.get_config_var("LIBDIR")
    ldversion = sysconfig.get_config_var("LDVERSION") or sysconfig.get_config_var("VERSION")
    assert ldversion is not None, "sysconfig did not report a Python version to link against"
    return ([libdir] if libdir is not None else []), [f"python{ldversion}"]


@dataclass(frozen=True, slots=True)
class PythonToolchain:
    """
    Overrides `compile_executable`'s default "link against the running
    interpreter" behavior: compile/link against a *different* Python install
    instead (e.g. smelt's own Zig-built CPython -- see
    `nuitkaify.build_own_python` -- for a fully standalone dist with no
    dependency on the host's libc/libpython at all).

    `include_dirs` must resolve `Python.h`/`pyconfig.h` for that install;
    `library_dir`/`library_name` its `libpython<ldversion>.so` (or `.a`).

    `target`, when set, is a Zig target triple (e.g. `"x86_64-linux-musl"`)
    passed straight through to `zig cc` as `--target=` for both the compile and
    the link step. It is *not* optional decoration when the toolchain's Python
    was built for a non-host libc: object code and `libpython` must agree on
    the libc they are compiled against, or the link fails (missing musl-only
    symbols) or, worse, succeeds and produces a binary mixing two libcs.
    Left `None` for a toolchain built natively, where `zig cc`'s own default
    target is already the right one.
    """

    include_dirs: list[str]
    library_dir: str
    library_name: str
    target: str | None = None


def _compile_extension_sources(
    compiler: Compiler,
    extension_obj: Extension,
    include_dirs: list[str],
    crosscompile: SupportedPlatforms | None,
    build_folder: str,
    extra_preargs: Iterable[str] = (),
    extra_postargs: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """
    Compiles `extension_obj`'s sources into `build_folder`, returning the produced
    object files alongside the `extra_preargs` (the crosscompile `--target`, if any)
    the caller must also pass to its own link step.

    Shared by `compile_extension` and `compile_executable`: both compile the same way
    and only differ in how the resulting objects are linked. `extra_preargs` are
    caller-supplied flags prepended to that same list, so they reach the link step
    too -- `compile_executable` passes its `PythonToolchain`'s `--target=` this way.
    """
    extra_preargs = list(extra_preargs)
    if crosscompile is not None:
        # TODO: generate/obtain pyconfig.h for the target platform
        warnings.warn(
            "Support for cross-compiling is experimental.\n"
            "Do not assume stability from the built artifacts"
        )
        extra_preargs.append(f"--target={crosscompile.value}")
        # adding pyconfig
        include_dirs.append(PYCONFIG_PATH)

    objects = compiler.compile(
        sources=extension_obj.sources,
        output_dir=build_folder,
        include_dirs=include_dirs + extension_obj.include_dirs,
        extra_preargs=extra_preargs,
        # Caller flags last: `build_mode_compile_flags`'s `-g0`/`-g` must be able to
        # override anything the extension itself asked for.
        extra_postargs=[
            *sanity_msvc_flags(extension_obj.extra_compile_args or []),
            *extra_postargs,
        ],
        macros=extension_obj.define_macros,
    )
    return objects, extra_preargs


def compile_extension(
    extension: Path | str | Extension,
    compiler: Compiler | None = None,
    dest_folder: PathLike[str] | None = None,
    crosscompile: SupportedPlatforms | None = None,
    use_zig_native_interface: bool = False,
    debug: bool = False,
) -> PathExists:
    """
    Standalone function compiling a low-level extension (C, C++ or Zig)
    into a shared library.

    Parameters
    ----------
    extension_path: Path | str | Extension
        Path to the source file to compile,
        or a pre-built Extension object

    compiler: Compiler | None
        The compiler to use,
        spawns a ZigCompiler if omitted

    dest_folder: PathLike[str]
        The folder in which to place the built shared library.
        Defaults to cwd.
    """
    compiler = compiler or ZigCompiler()
    include_dirs = [sysconfig.get_path("include"), sysconfig.get_path("platinclude")]
    libdir = sysconfig.get_config_var("LIBDIR")
    library_dirs = [libdir] if libdir is not None else []
    libraries: list[str] = []

    win_library_dirs, win_libraries = python_import_library_link_args()
    library_dirs += win_library_dirs
    libraries += win_libraries

    if isinstance(extension, (str, Path)):
        if not os.path.exists(extension):
            raise FileNotFoundError(f"Extension does not exists: {extension}")

        # building an extension object for a single source file
        extension = Path(extension)
        ext_name = extension.name

        if extension.suffix not in compiler.src_extensions:
            raise ValueError(
                f"Unsupported extension: {extension.suffix} "
                f"Supported values: {','.join(compiler.src_extensions)}"
            )
        extension_obj = Extension(
            name=extension.name.replace(extension.suffix, ""),
            sources=[
                str(extension),
            ],
        )
    else:
        extension_obj = extension
        ext_name = extension.name

    # Compile the C file
    if crosscompile is not None:
        so_suffix = get_extension_suffix(crosscompile.get_triple_name())
    else:
        so_suffix = sysconfig.get_config_var("EXT_SUFFIX")

    with tempfile.TemporaryDirectory() as build_folder:
        # TODO: investigate the pure setuptools alternative
        # as the distutils compiler is deprecated
        objects, extra_preargs = _compile_extension_sources(
            compiler,
            extension_obj,
            include_dirs,
            crosscompile,
            build_folder,
            extra_postargs=build_mode_compile_flags(debug),
        )

        # Link it into a shared object
        ext_name = extension_obj.name + so_suffix

        output_dir = dest_folder or "."

        if not use_zig_native_interface:
            compiler.link_shared_object(
                objects,
                ext_name,
                output_dir=str(output_dir),
                libraries=extension_obj.libraries + libraries,
                library_dirs=extension_obj.library_dirs + library_dirs,
                runtime_library_dirs=extension_obj.runtime_library_dirs,
                extra_preargs=extra_preargs,
                extra_postargs=build_mode_link_flags(debug),
            )
        else:
            zig_build_lib(extension.name, objects, crosscompile=crosscompile)
    so_path = os.path.join(output_dir, ext_name)
    return assert_path_exists(so_path)


def compile_executable(
    extension: Path | str | Extension,
    compiler: Compiler | None = None,
    dest_folder: PathLike[str] | None = None,
    crosscompile: SupportedPlatforms | None = None,
    python_toolchain: PythonToolchain | None = None,
    debug: bool = False,
) -> PathExists:
    """
    Standalone function compiling a low-level source (C, C++ or Zig) into a native,
    Python-embedding executable -- the executable counterpart of `compile_extension`.

    Parameters
    ----------
    extension: Path | str | Extension
        Path to the source file to compile,
        or a pre-built Extension object

    compiler: Compiler | None
        The compiler to use,
        spawns a ZigCompiler if omitted

    dest_folder: PathLike[str]
        The folder in which to place the built executable.
        Defaults to cwd.

    python_toolchain: PythonToolchain | None
        Compile/link against this Python install instead of the running
        interpreter. See `PythonToolchain`.
    """
    compiler = compiler or ZigCompiler()
    target_preargs: list[str] = []
    if python_toolchain is not None:
        include_dirs = list(python_toolchain.include_dirs)
        embed_library_dirs = [python_toolchain.library_dir]
        embed_libraries = [python_toolchain.library_name]
        if python_toolchain.target is not None:
            target_preargs.append(f"--target={python_toolchain.target}")
    else:
        include_dirs = [sysconfig.get_path("include"), sysconfig.get_path("platinclude")]
        embed_library_dirs, embed_libraries = python_embed_library_link_args()

    if isinstance(extension, (str, Path)):
        if not os.path.exists(extension):
            raise FileNotFoundError(f"Extension does not exists: {extension}")

        extension = Path(extension)
        if extension.suffix not in compiler.src_extensions:
            raise ValueError(
                f"Unsupported extension: {extension.suffix} "
                f"Supported values: {','.join(compiler.src_extensions)}"
            )
        extension_obj = Extension(
            name=extension.name.replace(extension.suffix, ""),
            sources=[
                str(extension),
            ],
        )
    else:
        extension_obj = extension

    exe_suffix = sysconfig.get_config_var("EXE") or (
        ".exe" if platform.system() == "Windows" else ""
    )

    with tempfile.TemporaryDirectory() as build_folder:
        objects, extra_preargs = _compile_extension_sources(
            compiler,
            extension_obj,
            include_dirs,
            crosscompile,
            build_folder,
            extra_preargs=target_preargs,
            extra_postargs=build_mode_compile_flags(debug),
        )

        exe_name = extension_obj.name + exe_suffix
        output_dir = dest_folder or "."

        compiler.link_executable(
            objects,
            exe_name,
            output_dir=str(output_dir),
            libraries=extension_obj.libraries + embed_libraries,
            library_dirs=extension_obj.library_dirs + embed_library_dirs,
            runtime_library_dirs=extension_obj.runtime_library_dirs,
            extra_preargs=extra_preargs,
            extra_postargs=build_mode_link_flags(debug),
        )
    exe_path = os.path.join(output_dir, exe_name)
    return assert_path_exists(exe_path)
