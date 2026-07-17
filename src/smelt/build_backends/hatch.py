"""
Build hook hatchling backend.

@date: 03.09.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import fields
from functools import cached_property
from importlib import metadata as importlib_metadata
from importlib.util import find_spec
from typing import TYPE_CHECKING, Protocol

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl
from packaging.utils import canonicalize_name

from smelt.backend import run_backend, write_auto_mode_report
from smelt.config import SmeltConfig
from smelt.utils import ImportPath, ModpathType

if TYPE_CHECKING:
    from packaging.requirements import Requirement


class _ExtrasAwareModule(Protocol):
    """
    Structural type for the module config dataclasses (`MypycModule`,
    `CythonExtension`, `NuitkaModule`, `NativeExtension`, `ZigModule`):
    all of them declare `import_path` and `extras`.
    """

    import_path: ImportPath
    extras: list[str]


class HatchlingBuildHook(BuildHookInterface):
    PLUGIN_NAME = "smelt"

    @cached_property
    def is_debug(self) -> bool:
        if not self.smelt_config.debug:
            return False
        print("Smelt: SMELT_DEBUG is set, enabling debug mode")
        return True

    @cached_property
    def smelt_config(self) -> SmeltConfig:
        try:
            config = SmeltConfig.from_toml_data(
                self.config, project_scripts=self.metadata.core.scripts
            )
        except Exception as exc:
            raise ValueError(
                "Smelt config is invalid:"
                f"Current config: {self.config}"
                "Valid parameters are:\n"
                f"{[f.name for f in fields(SmeltConfig)]}"
            ) from exc
        config.load_env()
        return config

    @cached_property
    def _installed_packages_by_distribution(self) -> dict[str, set[str]]:
        """
        Maps every distribution (PyPI/canonicalized name) installed in the current
        environment to the set of top-level packages it provides, e.g. `"pyyaml" ->
        {"yaml"}`. Used to check, for a given `[project.optional-dependencies]` extra,
        whether the packages it requires are actually importable right now.
        """
        installed: dict[str, set[str]] = {}
        for package, distributions in importlib_metadata.packages_distributions().items():
            for distribution in distributions:
                installed.setdefault(canonicalize_name(distribution), set()).add(package)
        return installed

    def _requirements_for_extra(self, extra: str) -> dict[str, Requirement]:
        """
        Resolves `extra` to its parsed requirements, as declared in
        `[project.optional-dependencies]` of the consuming project's pyproject.toml.
        """
        try:
            return self.metadata.core.optional_dependencies_complex[extra]
        except KeyError:
            available = sorted(self.metadata.core.optional_dependencies_complex)
            raise ValueError(
                f"Smelt: module declares unknown extra {extra!r}. Available extras: {available}",
            ) from None

    def _missing_packages_for_extra(self, extra: str) -> set[str]:
        """
        Returns the distribution names required by `extra` that don't have any of
        their packages importable in the current environment, i.e. that weren't
        installed (the extra wasn't requested at install time).
        """
        missing: set[str] = set()
        for requirement in self._requirements_for_extra(extra).values():
            dist_name = canonicalize_name(requirement.name)
            provided_packages = self._installed_packages_by_distribution.get(dist_name, set())
            if not provided_packages or not any(find_spec(pkg) for pkg in provided_packages):
                missing.add(requirement.name)
        return missing

    def _skip_reason(self, extras: list[str]) -> str | None:
        """
        Returns a human-readable reason to skip a module declaring `extras`,
        or None if every one of these extras is satisfied.
        """
        for extra in extras:
            if missing := self._missing_packages_for_extra(extra):
                return f"extra {extra!r} is not installed (missing: {sorted(missing)})"
        return None

    def _filter_modules_by_extras[M: _ExtrasAwareModule](self, modules: list[M]) -> list[M]:
        """
        Drops modules whose `extras` requirement isn't satisfied in the current
        environment, logging why in debug mode.
        """
        kept: list[M] = []
        for module in modules:
            if reason := self._skip_reason(module.extras):
                self.debug_log(f"Smelt: skipping {module.import_path}: {reason}")
                continue
            kept.append(module)
        return kept

    def debug_log(self, message: str) -> None:
        """
        Prints `message` if debug mode is set.
        """
        if not self.is_debug:
            return
        print(message)

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if self.target_name == "sdist":
            # disabling ourselves - we only want to include source code.
            # TODO: consider adding an environment variable to force building
            # extensions even in sdist mode, if there's ever a use for that
            return

        self.debug_log(f"Smelt: Calling build hook with config:\n{self.config}")
        config = self.smelt_config
        config.mypyc_modules = self._filter_modules_by_extras(config.mypyc_modules)
        config.cython_modules = self._filter_modules_by_extras(config.cython_modules)
        config.nuitka_modules = self._filter_modules_by_extras(config.nuitka_modules)
        config.c_extensions = self._filter_modules_by_extras(config.c_extensions)
        config.zig_modules = self._filter_modules_by_extras(config.zig_modules)
        try:
            run_backend(
                config,
                strategy=ModpathType.FS,
                without_entrypoint=True,
                stdout="stdout",
            )
        except Exception as exc:
            raise RuntimeError(f"Smelt build failed: {exc}")
        finally:
            if config.report_path is not None:
                write_auto_mode_report(config.report_path)


@hookimpl
def hatch_register_build_hook() -> type[BuildHookInterface]:
    """
    Registers Smelt's build hook as a hatch plugin
    """
    return HatchlingBuildHook
