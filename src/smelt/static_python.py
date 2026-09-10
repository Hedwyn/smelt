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

A mode `own` prefix built with `python-linkage=dynamic` ships a real, ordinarily-
linkable `libpythonX.Y.so` (see `own_python.build_own_python`) to link the replacement
`main()` against -- through the same `ZigCompiler` every other smelt-built extension
goes through, not a second toolchain. A prefix built `linkage="static"` (static-PIE,
no `.so` at all) ships a linkable `libpythonX.Y.a` instead, which meta-python's
`off`-mode build produces for exactly this purpose; `_find_libpython` picks whichever
of the two is present.

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

from smelt.compiler import ZigCompiler
from smelt.native_deps import elf_dynamic_entries, set_rpath
from smelt.own_python import (
    INTERPRETER_HOST_DLL_PREFIXES,
    INTERPRETER_REL_PATH,
    REAL_INTERPRETER_REL_PATH,
    is_musl_zig_target,
)
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


def _find_libpython(prefix: PathExists) -> tuple[PathExists, bool]:
    """
    The library a mode `own` prefix ships at `lib/` to link the replacement `main()`
    against, and whether it is a static archive rather than a shared library.

    A `python-linkage=dynamic` prefix (`own_python.build_own_python`'s default) ships a
    real `libpythonX.Y.so`, picked by the shortest matching name (`libpython3.12.so`,
    not one of its versioned-suffix siblings: the one every other consumer in this
    codebase treats as the canonical one, see e.g. `own_python.py`'s own
    `libpython*.so*` globs) -- preferred when present. A `linkage="static"` prefix
    (`own_python_static`, static-PIE, no `.so` at all) ships `libpythonX.Y.a` instead:
    meta-python's `off`-mode build produces this archive alongside the monolithic exe
    for exactly this purpose.
    """
    lib_dir = Path(prefix) / "lib"
    so_candidates = sorted(lib_dir.glob("libpython*.so*"), key=lambda p: len(p.name))
    if so_candidates:
        return assert_path_exists(so_candidates[0]), False
    archive_candidates = sorted(lib_dir.glob("libpython*.a"), key=lambda p: len(p.name))
    if archive_candidates:
        return assert_path_exists(archive_candidates[0]), True
    raise StaticPythonError(
        f"No libpythonX.Y.so or libpythonX.Y.a under {lib_dir}: static linking needs "
        "one of the two to link the replacement main() against -- was a different "
        "prefix passed in?"
    )


def _unexpected_dependencies(binary_path: PathExists) -> list[str]:
    """
    Tier 2 (authoritative, after the trial static link) of the static-linking
    eligibility check described in `compiling_pipeline_refactor.md`: every shared
    library `binary_path` declares that is neither host-supplied
    (`INTERPRETER_HOST_DLL_PREFIXES`) nor `libpython` itself.

    Tier 1 (`smelt.backend.is_static_link_eligible`) only catches a module that
    *declares* an external library via `Extension.libraries`/`extra_link_args`; this
    catches one pulled in implicitly, which Tier 1's purely structural check cannot
    see by construction.

    Read out of the binary's own `DT_NEEDED` entries rather than resolved against this
    machine: what matters is that the interpreter would demand a library at startup,
    not whether the build host happens to have a copy -- and for a cross-built
    interpreter it would not.
    """
    needed, _rpaths = elf_dynamic_entries(binary_path)
    return sorted(
        name
        for name in needed
        if not name.startswith(INTERPRETER_HOST_DLL_PREFIXES) and not name.startswith("libpython")
    )


def build_static_interpreter(
    prefix: PathExists,
    static_modules: Mapping[str, Iterable[PathExists]],
    *,
    compiler: Compiler | None = None,
    zig_target: str | None = None,
    include_dirs: Iterable[str] = (),
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

    `zig_target` is the target the prefix was built for, and must be passed whenever it
    is not the host: the replacement is a fresh link, so it has to be produced for the
    same target as the interpreter it replaces. For a musl target it additionally
    decides libc linkage -- `zig cc` links musl *statically* by default, which would
    quietly cost the interpreter its `dlopen()` (see `own_python.is_musl_zig_target`
    and meta-python's `build/musl.zig`), so the link is forced dynamic to match.

    `include_dirs` is prepended to the shim's own include path. A cross-built prefix
    needs its own `pyconfig.h` there -- the header describes the *target*, while
    `Python.h` comes from the running interpreter.

    Returns the path to the replacement interpreter: `REAL_INTERPRETER_REL_PATH` for a
    staged prefix that has one (the stub at `bin/python` starts whatever is there, so
    it needs no rebuilding), `bin/python` otherwise. A no-op (returns the existing
    interpreter unchanged) when `static_modules` is empty.
    """
    prefix = assert_path_exists(prefix)
    real_path = Path(prefix) / REAL_INTERPRETER_REL_PATH
    bin_path = real_path if real_path.is_file() else Path(prefix) / INTERPRETER_REL_PATH
    if not static_modules:
        return assert_path_exists(bin_path)
    if not bin_path.is_file():
        raise StaticPythonError(f"No interpreter at {bin_path}.")

    libpython, is_static_archive = _find_libpython(prefix)
    compiler = compiler or ZigCompiler()
    header_dirs = [
        *(str(entry) for entry in include_dirs),
        sysconfig.get_path("include"),
        sysconfig.get_path("platinclude"),
    ]

    # The same flags on both the compile and the link: this is one small C file
    # (`generate_inittab_shim`) compiled and linked back-to-back, so `compiler.compile`
    # is called directly rather than through `compiler`'s Extension-shaped path -- that
    # one only understands `SupportedPlatforms`, while a mode `own` target is any zig
    # triple (see `own_python.build_own_python`).
    extra_preargs = [f"--target={zig_target}"] if zig_target is not None else []

    with tempfile.TemporaryDirectory() as build_folder:
        shim_source = Path(build_folder) / "_smelt_static_main.c"
        shim_source.write_text(generate_inittab_shim(static_modules.keys()))
        shim_objects = compiler.compile(
            sources=[str(shim_source)],
            output_dir=build_folder,
            include_dirs=header_dirs,
            extra_preargs=extra_preargs,
        )
        if is_musl_zig_target(zig_target) and not is_static_archive:
            # `zig cc` links musl *statically* by default, and a statically linked musl
            # program has no working `dlopen()` at all -- so the replacement has to be
            # told to keep the dynamic linkage (and the shipped musl loader) the
            # interpreter it replaces was built with.
            extra_preargs = [*extra_preargs, "-dynamic"]
        if is_static_archive:
            # Mirrors meta-python's own `off`-mode executable (`build.zig`, built with
            # `libc-linkage=static` + `exe.pie = true`): the replacement is just as
            # monolithic as the one it replaces, so it needs the same shape.
            # `-static-pie` rather than plain `-static` to match it exactly, and
            # `-rdynamic` so anything still resolving Python C-API symbols against the
            # executable keeps working. What this shape cannot do is `dlopen()` -- a
            # statically linked program has no loader to do it with, whichever libc it
            # was built against -- which is why every native module has to be linked in
            # here (`dist._assert_static_interpreter_needs_no_dlopen` refuses a folder
            # where one was left out).
            extra_preargs = [*extra_preargs, "-static-pie", "-rdynamic"]

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

        if unexpected := _unexpected_dependencies(new_bin_path):
            raise StaticPythonError(
                f"Statically linking {sorted(static_modules)} pulled in {unexpected} "
                "beyond the host-supplied libc/loader and libpython itself. Folding "
                "that into bin/python would turn a missing dependency into a hard, "
                "whole-process startup failure (refused before main() even runs) "
                "instead of the soft, per-import one a loose .so gets. The existing "
                "bin/python was left untouched; ship the offending module as an "
                "ordinary .so instead."
            )

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
