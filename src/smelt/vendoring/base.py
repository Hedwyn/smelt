"""
Protocol every vendoring provider implements (see `smelt.vendoring`).

@date: 16.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from smelt.own_python import TargetPythonHeaders
from smelt.utils import ImportPath, PathExists, SmeltError


class VendoringDeclined(SmeltError):
    """
    Raised by `VendoredProvider.compile` to mean "this distribution has a provider
    registered, but not for this resolved version/target" -- distinct from a
    provider-specific error (e.g. `CffiVendoringError`), which means the build is
    actually broken and should stop. The caller
    (`smelt.isolated_build.prepare_isolated_natives`) catches this and falls back to
    the ordinary wheel-fetch path, exactly as if no provider had been registered at
    all (see `smelt.vendoring.cryptography.CryptographyProvider`, which raises this
    for a version before `cryptography`'s Rust transition -- out of scope for that
    provider, but with ordinary wheels available for most platforms).
    """


@dataclass
class VendoredExtension:
    """
    The result of building a vendored provider's extension module for one target.
    `objects` normally holds this module's own compiled-but-unlinked object files
    *and* any pre-built static archives it links against (e.g. a vendored libffi
    build) -- deliberately opaque entries, the same way
    `smelt.backend.BackendResult.static_modules` treats them: the final link step
    (`smelt.compiler.link_extension_objects`, or
    `smelt.static_python.build_static_interpreter`) does not care which of them it
    compiled itself.

    `already_linked`, when set, means `objects` instead holds exactly one entry:
    a finished, already-linked shared object (e.g. a `maturin`-built Rust cdylib,
    whose own build step both compiles and links) that `smelt.vendoring.
    build_vendored_extension`'s loose-`.so` path copies into place directly rather
    than re-linking through `smelt.compiler.link_extension_objects` -- re-linking a
    finished `.so` as if it were relocatable object code does not work. Such a
    provider does not support `static_build_dir` (there is no object-file seam to
    fold into a static interpreter's inittab); `build_vendored_extension` raises if
    asked to.
    """

    import_path: ImportPath
    module_name: str
    objects: list[PathExists]
    already_linked: bool = False


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
        py_headers: TargetPythonHeaders | None = None,
    ) -> VendoredExtension:
        """
        Fetches (from source) and compiles this provider's extension for `target`
        (a Zig-triple-shaped string, `None` meaning the host's own platform --
        same convention as `smelt.isolated_build.fetch_wheel`), matching
        `version_requirement` (as `smelt.isolated_build.resolve_isolated_build_version`
        produces it).

        Raises `VendoringDeclined` if `version_requirement` resolves to a version
        this provider cannot (or does not attempt to) build from source at all
        (e.g. requires a toolchain out of scope for this provider) -- the caller
        falls back to `smelt.isolated_build.fetch_wheel` for that case, same as if
        no provider had been registered. Any other failure (a genuinely broken
        build) should raise a provider-specific error instead, not this one.

        `py_headers`, when `target` is set, is the target-correct `Python.h`/
        `pyconfig.h` pair the caller already resolved (see
        `smelt.own_python.target_python_headers_for`) -- forwarded to whatever
        compiles this provider's extension (typically
        `smelt.vendoring._compile.compile_extension_for_target`) instead of letting
        it fall back to a generic, wrong-for-this-target one.

        Compiles into `build_dir` but never links (unless `VendoredExtension.
        already_linked` is set -- see its own doc): the caller decides whether the
        result is staged for static linking or linked into a loose `.so` (see
        `smelt.vendoring.build_vendored_extension`), mirroring the
        compile/link split every other smelt backend goes through
        (`smelt.backend.compile_generic_extension`/`link_generic_extension`).
        """
        ...
