"""
Curated from-source build recipes for third-party packages `isolated-build`
(see `smelt.isolated_build`) cannot find a wheel for on the target platform --
e.g. cffi, which has no wheel for many embedded Linux targets but only needs a
C compiler and libffi to build. Checked *before* any wheel lookup: a package
registered here never goes through `unearth`/the package index at all.

Mirrors `meta-python/vendoring/{lzma,sqlite3}`'s existing meaning of
"vendoring" in this project (a curated build recipe checked into the repo for a
specific third-party library) -- one level up: a *Python package* build instead
of a CPython-internal one.

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import shutil
import sysconfig
from pathlib import Path

from smelt.compiler import link_extension_objects
from smelt.isolated_build import canonicalize_distribution_name
from smelt.own_python import TargetPythonHeaders
from smelt.utils import PathExists, SmeltError, assert_path_exists, get_extension_suffix
from smelt.vendoring.base import VendoredExtension, VendoredProvider, VendoringDeclined
from smelt.vendoring.cffi import CffiProvider
from smelt.vendoring.cryptography import CryptographyProvider

_PROVIDERS: dict[str, VendoredProvider] = {
    "cffi": CffiProvider(),
    "cryptography": CryptographyProvider(),
}


def get_provider(dist_name: str) -> VendoredProvider | None:
    """
    The registered `VendoredProvider` for `dist_name`, or `None` if this
    distribution has no from-source build recipe -- the caller falls back to
    `smelt.isolated_build.fetch_wheel` in that case.
    """
    return _PROVIDERS.get(canonicalize_distribution_name(dist_name))


def build_vendored_extension(
    provider: VendoredProvider,
    version_requirement: str,
    target: str | None,
    python_version: tuple[int, int],
    *,
    build_dir: Path,
    static_build_dir: Path | None,
    py_headers: TargetPythonHeaders | None = None,
) -> tuple[VendoredExtension, PathExists | None]:
    """
    Compiles `provider`'s extension for `target`. Returns the compiled
    `VendoredExtension` always, plus the linked `.so` path when
    `static_build_dir` is `None` -- `None` there when `static_build_dir` is
    given instead, since the caller stages `VendoredExtension.objects` for
    static linking and never links a `.so` at all, mirroring
    `smelt.backend._compile_place_or_stage`'s own branch for every other
    backend.

    `py_headers`, forwarded to `provider.compile`, is the target-correct
    `Python.h`/`pyconfig.h` pair `smelt.isolated_build.prepare_isolated_natives`
    already resolved (see `smelt.own_python.target_python_headers_for`) --
    unused when `target` is `None` (a native build needs none).

    Propagates `VendoringDeclined` as-is (see `VendoredProvider.compile`) --
    nothing to build here, the caller falls back to a wheel instead.
    """
    vext = provider.compile(
        version_requirement,
        target,
        python_version,
        build_dir=build_dir,
        py_headers=py_headers,
    )
    if vext.already_linked:
        if static_build_dir is not None:
            raise SmeltError(
                f"{provider!r} produced an already-linked shared object for "
                f"{vext.import_path!r}, which cannot be staged for static linking "
                "(no object-file seam to fold into a static interpreter's "
                "inittab) -- static_build_dir is unsupported for this provider."
            )
        (object_path,) = vext.objects
        so_suffix = (
            get_extension_suffix(target)
            if target is not None
            else sysconfig.get_config_var("EXT_SUFFIX")
        )
        so_path = build_dir / f"{vext.module_name}{so_suffix}"
        shutil.copy(object_path, so_path)
        return vext, assert_path_exists(so_path)
    if static_build_dir is not None:
        return vext, None
    so_suffix = (
        get_extension_suffix(target)
        if target is not None
        else sysconfig.get_config_var("EXT_SUFFIX")
    )
    so_path = link_extension_objects(
        vext.objects,
        f"{vext.module_name}{so_suffix}",
        dest_folder=build_dir,
        # Without this, the link step defaults to the host's own architecture --
        # `vext.objects` were compiled `--target={target}` (see
        # `smelt.vendoring._compile.compile_extension_for_target`), so linking
        # them without the same flag mismatches (e.g. "incompatible with
        # elf64-x86-64" for objects actually built for arm-linux-gnueabihf).
        extra_preargs=[f"--target={target}"] if target is not None else (),
    )
    return vext, so_path
