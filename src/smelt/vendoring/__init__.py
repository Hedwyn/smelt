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

import sysconfig
from pathlib import Path

from smelt.compiler import link_extension_objects
from smelt.isolated_build import canonicalize_distribution_name
from smelt.utils import PathExists, get_extension_suffix
from smelt.vendoring.base import VendoredExtension, VendoredProvider
from smelt.vendoring.cffi import CffiProvider

_PROVIDERS: dict[str, VendoredProvider] = {"cffi": CffiProvider()}


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
) -> tuple[VendoredExtension, PathExists | None]:
    """
    Compiles `provider`'s extension for `target`. Returns the compiled
    `VendoredExtension` always, plus the linked `.so` path when
    `static_build_dir` is `None` -- `None` there when `static_build_dir` is
    given instead, since the caller stages `VendoredExtension.objects` for
    static linking and never links a `.so` at all, mirroring
    `smelt.backend._compile_place_or_stage`'s own branch for every other
    backend.
    """
    vext = provider.compile(version_requirement, target, python_version, build_dir=build_dir)
    if static_build_dir is not None:
        return vext, None
    so_suffix = (
        get_extension_suffix(target) if target is not None else sysconfig.get_config_var("EXT_SUFFIX")
    )
    so_path = link_extension_objects(
        vext.objects, f"{vext.module_name}{so_suffix}", dest_folder=build_dir
    )
    return vext, so_path
