"""
Statically links smelt's own compiled extension modules into a mode `own`
distribution's interpreter, instead of shipping them as loose `.so` files for the
ordinary `dlopen()`-driven import path to find.

The mechanism is `PyImport_AppendInittab`: CPython's public, documented API for an
embedding program to register an extension module's `PyInit_<name>` function *before*
`Py_Initialize` runs, so that importing it needs no file at all -- no `.so` on disk, no
RPATH for it to resolve, no dynamic loader call at import time. Mode `own` already
embeds CPython behind a thin `main()` (meta-python's `Programs/python.c`, calling
`Py_BytesMain`); this module replaces that `main()` with one carrying the extra
`PyImport_AppendInittab` calls and the modules' own object code linked straight in.

Deliberately built on nothing meta-python does not already provide: a mode `own`
prefix's `libpythonX.Y.so` is a real, ordinarily-linkable shared library (see
`own_python.build_own_python`'s `python-linkage=dynamic`), so the replacement `main()`
is just another program smelt compiles and links against it -- through the same
`ZigCompiler` every other smelt-built extension goes through, not a second toolchain.
No change to meta-python's own build is needed: it already builds everything this
module links against, unmodified.

@date: 08.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import re
import shutil
import sysconfig
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from distutils.compilers.C.unix import Compiler
from setuptools import Extension

from smelt.compiler import ZigCompiler, _compile_extension_sources
from smelt.native_deps import set_rpath
from smelt.own_python import INTERPRETER_REL_PATH
from smelt.utils import PathExists, SmeltError, assert_path_exists

_logger = logging.getLogger(__name__)

#: A module name has to survive being spelled both as a C identifier (`PyInit_<name>`)
#: and as a bare, unquoted-safe string literal (the `_PyImport_AppendInittab` name) --
#: i.e. exactly a single, non-dotted Python identifier. A dotted (submodule) name is
#: refused rather than guessed at: CPython's own builtin/frozen modules are already
#: overwhelmingly top-level for the same reason, and misspelling a submodule's inittab
#: entry fails at import time with no indication this is why.
_VALID_MODULE_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The RPATH mode `own`'s replacement `bin/python` needs to find `libpythonX.Y.so`
#: next to it -- identical to the one meta-python's own shim carries (see
#: `own_python.py`'s docstring and meta-python's `build.zig`,
#: `shim_mod.addRPathSpecial("$ORIGIN/../lib")`), so a static-linked prefix stays just
#: as relocatable as an ordinary one.
_INTERPRETER_RPATH: Final = "$ORIGIN/../lib"

_SHIM_TEMPLATE: Final = """\
#include <Python.h>

{externs}

int main(int argc, char **argv) {{
{appends}
    return Py_BytesMain(argc, argv);
}}
"""


class StaticPythonError(SmeltError):
    """
    Raised when smelt's own extension modules cannot be statically linked into a mode
    `own` interpreter.
    """


def _check_module_name(name: str) -> None:
    if not _VALID_MODULE_NAME.match(name):
        raise StaticPythonError(
            f"Cannot statically link {name!r}: not a valid top-level module name "
            "(must be a single C-identifier-safe name, no dots)."
        )


def generate_inittab_shim(module_names: Iterable[str]) -> str:
    """
    Renders the replacement `main()` that registers `module_names` with
    `PyImport_AppendInittab` before starting the interpreter (see this module's
    docstring). Each name must already provide `PyInit_<name>` in the object code
    linked alongside this shim (see `build_static_interpreter`).
    """
    names = list(module_names)
    for name in names:
        _check_module_name(name)
    externs = "\n".join(f"extern PyObject* PyInit_{name}(void);" for name in names)
    appends = "\n".join(f'    PyImport_AppendInittab("{name}", PyInit_{name});' for name in names)
    return _SHIM_TEMPLATE.format(externs=externs, appends=appends)


def _find_libpython(prefix: PathExists) -> PathExists:
    """
    The real `libpythonX.Y.so` a mode `own` prefix built with `python-linkage=dynamic`
    ships at `lib/` (see `own_python.build_own_python`), to link the replacement
    `main()` against. Picks the shortest matching name (`libpython3.12.so`, not one of
    its versioned-suffix siblings): the one every other consumer in this codebase
    treats as the canonical one (see e.g. `own_python.py`'s own `libpython*.so*` globs).
    """
    candidates = sorted((Path(prefix) / "lib").glob("libpython*.so*"), key=lambda p: len(p.name))
    if not candidates:
        raise StaticPythonError(
            f"No libpythonX.Y.so under {Path(prefix) / 'lib'}: static linking needs a "
            "real shared libpython to link against (python-linkage=dynamic), which is "
            "what own_python.build_own_python always builds -- was a different prefix "
            "passed in?"
        )
    return assert_path_exists(candidates[0])


def build_static_interpreter(
    prefix: PathExists,
    static_modules: Mapping[str, Iterable[PathExists]],
    *,
    compiler: Compiler | None = None,
) -> PathExists:
    """
    Replaces `prefix`'s `bin/python` (see `own_python.INTERPRETER_REL_PATH`) with a
    thin custom entrypoint that statically links `static_modules`' object code
    straight in and registers each with `PyImport_AppendInittab`, instead of leaving
    them to be shipped as loose `.so` files.

    `static_modules` maps each module's import name to its already-compiled object
    files/static archives, in link order (see `smelt.compiler.compile_extension_objects`
    -- the same object files a caller would otherwise pass to `compile_extension`'s
    final link step, just not linked into a `.so` here). They must already be built
    against a Python of the same `(major, minor)` as `prefix`'s own interpreter: the
    same ABI assumption mode `own` already relies on for every dlopen()'d extension
    module it ships (see `own_python`'s module docstring).

    `prefix` is expected to be a distribution's own staged interpreter directory (what
    `own_python.stage_interpreter` writes into `dist_root`), not meta-python's shared
    build cache: unlike a library toggle, which extension modules are statically
    linked in is a property of one application, and baking it into the cache meta-python
    builds are reused from would corrupt that cache for every other project.

    Returns the path to the replacement `bin/python`. A no-op (returns the existing
    `bin/python` unchanged) when `static_modules` is empty.
    """
    prefix = assert_path_exists(prefix)
    bin_path = Path(prefix) / INTERPRETER_REL_PATH
    if not static_modules:
        return assert_path_exists(bin_path)
    if not bin_path.is_file():
        raise StaticPythonError(f"No interpreter at {bin_path}.")

    libpython = _find_libpython(prefix)
    compiler = compiler or ZigCompiler()
    include_dirs = [sysconfig.get_path("include"), sysconfig.get_path("platinclude")]

    with tempfile.TemporaryDirectory() as build_folder:
        shim_source = Path(build_folder) / "_smelt_static_main.c"
        shim_source.write_text(generate_inittab_shim(static_modules.keys()))
        shim_extension = Extension(name="_smelt_static_main", sources=[str(shim_source)])
        shim_objects, extra_preargs = _compile_extension_sources(
            compiler, shim_extension, include_dirs, None, build_folder
        )

        objects = [
            *shim_objects,
            *(str(obj) for objs in static_modules.values() for obj in objs),
            str(libpython),
        ]
        new_bin_name = "python.static-new"
        compiler.link_executable(
            objects,
            new_bin_name,
            output_dir=build_folder,
            libraries=[],
            library_dirs=[],
            runtime_library_dirs=[],
            extra_preargs=extra_preargs,
        )
        new_bin_path = assert_path_exists(Path(build_folder) / new_bin_name)
        set_rpath(new_bin_path, _INTERPRETER_RPATH)

        # Replace atomically: `bin_path` may already be the target of an in-progress
        # run of a previous build, and a half-written executable there is worse than
        # either the old or the new one.
        tmp_dest = bin_path.with_name(bin_path.name + ".tmp")
        shutil.copy2(new_bin_path, tmp_dest)
        tmp_dest.chmod(0o755)
        tmp_dest.replace(bin_path)

    _logger.info(
        "Statically linked %s into %s (no .so, no dlopen for %s)",
        sorted(static_modules),
        bin_path,
        ", ".join(sorted(static_modules)),
    )
    return assert_path_exists(bin_path)
