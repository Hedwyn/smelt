"""
Protocol every vendoring provider implements (see `smelt.vendoring`).

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from smelt.utils import ImportPath, PathExists


@dataclass
class VendoredExtension:
    """
    The compiled-but-unlinked result of building a vendored provider's extension
    module for one target. `objects` holds this module's own compiled object
    files *and* any pre-built static archives it links against (e.g. a vendored
    libffi build) -- deliberately opaque entries, the same way
    `smelt.backend.BackendResult.static_modules` treats them: the final link step
    (`smelt.compiler.link_extension_objects`, or
    `smelt.static_python.build_static_interpreter`) does not care which of them it
    compiled itself.
    """

    import_path: ImportPath
    module_name: str
    objects: list[PathExists]


class VendoredProvider(Protocol):
    """
    Builds one third-party distribution's native extension from source for a
    target `isolated-build` (see `smelt.isolated_build`) could not find a wheel
    for. Checked *before* any wheel lookup (see `smelt.vendoring.get_provider`'s
    caller in `smelt.isolated_build.prepare_isolated_natives`) -- a registered
    distribution never touches `unearth`/the package index at all.

    Only a single extension module per provider is supported today (see
    `prepare_isolated_natives`'s own note) -- a distribution needing more than one
    would need a richer return type and caller-side plumbing this does not have
    yet.
    """

    def compile(
        self,
        version_requirement: str,
        target: str | None,
        python_version: tuple[int, int],
        *,
        build_dir: Path,
    ) -> VendoredExtension:
        """
        Fetches (from source) and compiles this provider's extension for `target`
        (a Zig-triple-shaped string, `None` meaning the host's own platform --
        same convention as `smelt.isolated_build.fetch_wheel`), matching
        `version_requirement` (as `smelt.isolated_build.resolve_isolated_build_version`
        produces it).

        Compiles into `build_dir` but never links: the caller decides whether the
        result is staged for static linking or linked into a loose `.so` (see
        `smelt.vendoring.build_vendored_extension`), mirroring the
        compile/link split every other smelt backend goes through
        (`smelt.backend.compile_generic_extension`/`link_generic_extension`).
        """
        ...
