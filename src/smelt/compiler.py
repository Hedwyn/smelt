from __future__ import annotations

import os
import sysconfig
import tempfile
from distutils.compilers.C.unix import Compiler  # type: ignore[import-not-found]
from distutils.sysconfig import customize_compiler  # type: ignore[import-not-found]
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from os import PathLike


class ZigCompiler(Compiler):
    """
    Base class for zig compiler.
    Sets the C/C++ compiler exe to zig cc and zig c++ and
    add .zig files to the list of accepted extensions.
    """

    # Expanding to add .zig files
    src_extensions: ClassVar[list[str]] = Compiler.src_extensions + [".zig"]

    executables = {
        "preprocessor": None,
        "compiler": ["zig cc"],
        "compiler_so": ["zig cc"],
        "compiler_cxx": ["zig c++"],
        "compiler_so_cxx": ["zig c++"],
        "linker_so": ["zig cc", "-shared"],
        "linker_so_cxx": ["zig c++", "-shared"],
        "linker_exe": ["zig cc"],
        "linker_exe_cxx": ["zig c++", "-shared"],
        "archiver": ["ar", "-cr"],
        "ranlib": None,
    }


def compile_extension(
    extension_path: PathLike,
    compiler: Compiler | None = None,
    dest_folder: PathLike | None = None,
) -> str:
    """
    Standalone function compiling a low-level extension (C, C++ or Zig)
    into a shared library.

    Parameters
    ----------
    extension_path: PathLike
        Path to the source file to compile
    
    compiler: Compiler | None
        The compiler to use,
        spawns a ZigCompiler if omitted
    
    dest_folder: PathLike
        The folder in which to place the built shared library.
        Defaults to cwd.
    """
    extension_path = Path(extension_path)
    compiler = compiler or ZigCompiler()
    customize_compiler(compiler)
    ext_name = extension_path.name

    if extension_path.suffix not in compiler.src_extensions:
        raise ValueError(
            f"Unsupported extension: {extension_path.suffix} "
            f"Supported values: {",".join(compiler.src_extensions)}"
        )

    # Compile the C file
    with tempfile.TemporaryDirectory() as build_folder:
        objects = compiler.compile(
            sources=[
                extension_path,
            ],
            output_dir=build_folder,
        )
        # Link it into a shared object
        so_suffix = sysconfig.get_config_var("EXT_SUFFIX")
        ext_name = ext_name.replace(extension_path.suffix, so_suffix)

        output_dir = dest_folder or "."
        compiler.link_shared_object(objects, ext_name, output_dir=str(output_dir))
    return os.path.join(output_dir, ext_name)
