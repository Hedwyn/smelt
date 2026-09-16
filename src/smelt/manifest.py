"""
Exposes a package's own smelt-compiled modules to a downstream build once this
package is installed, since `pyproject.toml` (where they were declared) does not
survive installation -- see `transitive_smelt_data_review.md`.

Written by `smelt.backend.run_backend` as a plain Python module living inside the
package itself (`write_manifest_module`), so a consumer needs no `smelt` dependency
to read it back (`read_manifest_module`). What it carries is deliberately narrow:
which import paths were compiled and by which backend, not `SmeltConfig` -- a
downstream build only needs the former, to find a shared runtime file invisible to
plain import-graph analysis (`discover_transitive_manifests`).

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib
import importlib.util
import sysconfig
from pathlib import Path
from typing import Final, Iterable

from smelt.config import Backend
from smelt.nuitkaify import RUNTIME_LIB_NAME
from smelt.utils import ImportPath, PathExists, PathSolver, get_module_name, path_exists

#: Name of the generated module, placed at the root of every package root that has
#: something to declare (see `write_manifest_module`).
MANIFEST_MODULE_NAME: Final[str] = "_smelt_manifest"


def _render_manifest_module(modules: dict[ImportPath, Backend]) -> str:
    entries = ",\n".join(
        f'    "{import_path}": "{backend.value}"'
        for import_path, backend in sorted(modules.items())
    )
    body = f"{entries}\n" if entries else ""
    return (
        '"""\n'
        "Smelt build manifest -- generated, do not edit.\n"
        "\n"
        "Records which of this package's modules smelt compiled, and with which\n"
        "backend, so a downstream build can find shared runtime files invisible to\n"
        "plain import-graph analysis (see `smelt.manifest`).\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        f"MODULES: dict[str, str] = {{\n{body}}}\n"
    )


def write_manifest_module(
    compiled_backends: dict[ImportPath, Backend],
    path_solver: PathSolver,
) -> list[Path]:
    """
    Writes `_smelt_manifest.py` into every package root (`path_solver.known_roots`)
    that owns at least one entry of `compiled_backends`, so it ships inside that
    package's own wheel next to its `__init__.py`. A root with nothing to declare
    gets no file at all. Returns the paths written.
    """
    written: list[Path] = []
    for root_import_path, root_path in path_solver.known_roots:
        under_root = {
            import_path: backend
            for import_path, backend in compiled_backends.items()
            if import_path == root_import_path or import_path.startswith(f"{root_import_path}.")
        }
        if not under_root:
            continue
        dest = root_path / f"{MANIFEST_MODULE_NAME}.py"
        dest.write_text(_render_manifest_module(under_root))
        written.append(dest)
    return written


def read_manifest_module(top_level_package: str) -> dict[ImportPath, Backend] | None:
    """
    Reads back a manifest written by `write_manifest_module` for the installed
    top-level package `top_level_package`, or None if it declares none -- not built
    by smelt, or built without anything for `write_manifest_module` to record.

    Never raises: an installed dependency not built by smelt is the common case, not
    an error (mirrors `explorer.resolve_module`'s own "never raises" convention) --
    which also covers a manifest naming a backend this version of smelt does not know
    (written by a newer one), rather than aborting the whole build over one entry it
    cannot use anyway.
    """
    qualified = f"{top_level_package}.{MANIFEST_MODULE_NAME}"
    try:
        spec = importlib.util.find_spec(qualified)
    except Exception:
        return None
    if spec is None:
        return None
    try:
        module = importlib.import_module(qualified)
        raw = module.MODULES
        return {ImportPath(import_path): Backend(backend) for import_path, backend in raw.items()}
    except Exception:
        return None


def discover_transitive_manifests(import_paths: Iterable[ImportPath]) -> dict[ImportPath, Backend]:
    """
    For every distinct top-level package among `import_paths`, checks for a smelt
    manifest and merges what it declares.

    Meant to run once over an already-resolved dependency closure (e.g.
    `explorer.flatten_dependency_graph`'s output), not during the graph walk itself:
    a manifest only ever adds *invisible* runtime files smelt already knows the
    naming convention for -- it never changes which modules the walk should follow
    into, only what a bundler ships alongside what it already found.
    """
    discovered: dict[ImportPath, Backend] = {}
    seen_packages: set[str] = set()
    for import_path in import_paths:
        top_level = import_path.partition(".")[0]
        if top_level in seen_packages:
            continue
        seen_packages.add(top_level)
        manifest = read_manifest_module(top_level)
        if manifest is not None:
            discovered.update(manifest)
    return discovered


def transitive_mypyc_runtime(import_path: ImportPath, origin: PathExists) -> PathExists | None:
    """
    The mypyc shared-runtime file `import_path`'s own compiled module (at `origin`)
    needs at load time, if it is still sitting next to it -- the same naming
    convention `smelt.backend.collect_built_artifacts` uses for a module this smelt
    run compiled itself, applied to one from an already-installed dependency instead.

    mypyc's runtime is `dlopen`'d at C level, never through a literal `import`
    statement, so nothing about `origin` alone reveals it is needed -- the caller
    already knows to look because `discover_transitive_manifests` said this module
    was compiled by `Backend.MYPYC`.
    """
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    candidate = origin.parent / f"{get_module_name(import_path)}__mypyc{suffix}"
    return candidate if path_exists(candidate) else None


def mypyc_runtime_import_name(import_path: ImportPath) -> str:
    """
    The dotted name mypyc's shared runtime for `import_path` would be imported under
    (`pkg.mod__mypyc` for a module `pkg.mod`) -- what Nuitka's `--include-module=`
    needs to bundle a `dlopen`'d file it cannot discover by following imports.
    """
    package, _, _ = import_path.rpartition(".")
    runtime_name = f"{get_module_name(import_path)}__mypyc"
    return f"{package}.{runtime_name}" if package else runtime_name


def transitive_nuitka_runtime(origin: PathExists) -> PathExists | None:
    """
    Smelt's shared Nuitka runtime (`lib{RUNTIME_LIB_NAME}.so`) next to `origin` (a
    dependency's Nuitka-compiled module), if it is still sitting there.

    Unlike mypyc's, this file is a genuine ELF `DT_NEEDED` dependency with an
    `$ORIGIN` RPATH, so `smelt.native_deps.bundle_native_dependencies`'s own ELF scan
    already finds and bundles it without this function's help -- but that scan is an
    implementation detail this manifest entry should not be load-bearing on. Declared
    (and, in `smelt.dist.build_dist`, explicitly copied) the same explicit way as
    mypyc's runtime, so finding it does not depend on staying on ELF/`ldd` forever.
    """
    candidate = origin.parent / f"lib{RUNTIME_LIB_NAME}.so"
    return candidate if path_exists(candidate) else None
