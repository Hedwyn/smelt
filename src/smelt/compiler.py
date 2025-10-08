"""
Defines a distutils compiler based on Zig.

@date: 27.05.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import warnings
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from distutils.compilers.C.unix import Compiler
from setuptools import Extension

from smelt.utils import get_extension_suffix

if TYPE_CHECKING:
    from os import PathLike

_SMELT_ROOT: Final[str] = os.path.dirname(__file__)
PYCONFIG_PATH: Final[str] = os.path.join(_SMELT_ROOT, "pyconfig")

_logger = logging.getLogger(__name__)


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

    executables = {
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
    # Note: zig build-lib will produce a file named lib{name}.so

    if crosscompile is not None:
        suffix = get_extension_suffix(crosscompile.get_triple_name())
    else:
        suffix = sysconfig.get_config_var("EXT_SUFFIX")
    # copying
    dest_name = f"{name}{suffix}"
    # copying the shared library to the expected name
    shutil.copy(f"lib{name}.so", dest_name)
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


def compile_extension(
    extension: Path | str | Extension,
    compiler: Compiler | None = None,
    dest_folder: PathLike[str] | None = None,
    crosscompile: SupportedPlatforms | None = None,
    use_zig_native_interface: bool = False,
) -> str:
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
    library_dirs = [sysconfig.get_config_var("LIBDIR")]

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
    extra_preargs: list[str] = []
    if crosscompile is not None:
        # TODO: generate/obtain pyconfig.h for the target platform
        warnings.warn(
            "Support for cross-compiling is experimental.\n"
            "Do not assume stability from the built artifacts"
        )
        extra_preargs.append(f"--target={crosscompile.value}")
        # adding pyconfig
        include_dirs.append(PYCONFIG_PATH)
        so_suffix = get_extension_suffix(crosscompile.get_triple_name())
    else:
        so_suffix = sysconfig.get_config_var("EXT_SUFFIX")

    with tempfile.TemporaryDirectory() as build_folder:
        # TODO: investigate the pure setuptools alternative
        # as the distutils compiler is deprecated
        objects = compiler.compile(
            sources=extension_obj.sources,
            output_dir=build_folder,
            include_dirs=include_dirs + extension_obj.include_dirs,
            extra_preargs=extra_preargs,
            extra_postargs=extension_obj.extra_compile_args or [],
            macros=extension_obj.define_macros,
        )

        # Link it into a shared object
        ext_name = extension_obj.name + so_suffix

        output_dir = dest_folder or "."

        if not use_zig_native_interface:
            compiler.link_shared_object(
                objects,
                ext_name,
                output_dir=str(output_dir),
                libraries=extension_obj.libraries,
                library_dirs=extension_obj.library_dirs + library_dirs,
                runtime_library_dirs=extension_obj.runtime_library_dirs,
                extra_preargs=extra_preargs,
            )
        else:
            zig_build_lib(extension.name, objects, crosscompile=crosscompile)
    so_path = os.path.join(output_dir, ext_name)
    assert os.path.exists(so_path), so_path
    return so_path
