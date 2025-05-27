
from __future__ import annotations
from typing import ClassVar
from distutils.compilers.C.unix import Compiler  # type: ignore[import-not-found]

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
