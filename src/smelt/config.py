"""
Config definitions for Smelt.

@date: 19.02.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Literal, Self

from smelt.resolver import Level
from smelt.schema import (
    BundleSettings,
    NuitkaBinarySettings,
    ProjectConfig,
    SmeltDeprecationWarning,
    TomlValue,
    build_datacls_from_toml,
    merge_settings,
    parse_project_config,
)
from smelt.utils import (
    ImportPath,
    PackageRootPath,
    PathExists,
    PathSolver,
    SmeltConfigError,
    assert_path_exists,
)

__all__ = ["build_datacls_from_toml"]


def _format_context(context: Iterable[str]) -> str:
    """
    A small helper that builds a human-friendly hint from
    the path of a parameter in the config.
    Meant to be used when reporting config errors.
    """
    if not context:
        return ""

    *nodes, leaf = context

    param_path = ".".join(nodes) + ":" + leaf if nodes else leaf
    return f"In [{param_path}]:"


type _TomlData = str | list[_TomlData] | dict[str, _TomlData]
type TomlData = dict[str, _TomlData]


def select_smelt_table(toml_data: TomlData) -> dict[str, _TomlData]:
    """
    Returns the table holding Smelt's configuration: `[tool.smelt]`.

    `[tool.hatch.build.hooks.smelt]` only enables the hatch hook. Configuration
    written there by earlier releases is still read when `[tool.smelt]` is absent,
    with a deprecation warning; having settings in both tables is an error.

    Raises
    ------
    SmeltConfigError
        If no Smelt table exists, or both tables carry settings.
    """
    tool_table = toml_get_nested_section(toml_data, "tool", "smelt")
    hooks = toml_get_nested_section(toml_data, "tool", "hatch", "build", "hooks")
    assert isinstance(hooks, dict)
    has_tool = "smelt" in toml_get_nested_section(toml_data, "tool")
    has_hook = "smelt" in hooks
    hook_table = toml_get_nested_section(toml_data, "tool", "hatch", "build", "hooks", "smelt")
    assert isinstance(tool_table, dict) and isinstance(hook_table, dict)

    if has_tool and hook_table:
        if tool_table:
            raise SmeltConfigError(
                "Smelt settings found both in [tool.smelt] and "
                "[tool.hatch.build.hooks.smelt]. Keep them in [tool.smelt] only; "
                "the hook table must stay empty."
            )
        raise SmeltConfigError(
            "[tool.smelt] is empty but [tool.hatch.build.hooks.smelt] carries settings. "
            "Move them to [tool.smelt]."
        )
    if has_tool:
        return tool_table
    if has_hook:
        if hook_table:
            warnings.warn(
                "Smelt settings in [tool.hatch.build.hooks.smelt] are deprecated; "
                "move them to [tool.smelt] and leave the hook table empty.",
                SmeltDeprecationWarning,
                stacklevel=2,
            )
        return hook_table
    raise SmeltConfigError("No smelt config detected")


def toml_get_nested_section(toml_data: TomlData, *path: str) -> _TomlData:
    """
    Extracts the sub section given by `path` from `toml_data`.
    Verifies that the extracted TOML object is a dictionary.

    Raises
    ------
    SmeltConfigError
        If the section is not found or if the found object is not a section.
    """
    ctx: list[str] = []
    section: _TomlData = toml_data
    for subsection_name in path:
        ctx.append(subsection_name)
        section = section.get(subsection_name, {})
        if not isinstance(section, dict):
            raise SmeltConfigError(f"{_format_context(ctx)}Expected section, found {section}")
    return section


class Backend(StrEnum):
    """
    A backend for modules discovered by auto mode, selectable via `backend_priority_order`.

    `BYTECODE` compiles nothing: the module stays pure Python. It is the "no native
    build" outcome, so listing it last means auto mode never fails a build. `smelt bundle`
    ships such a module as `.pyc` like every other pure-Python module; a wheel build
    leaves it as source.
    """

    NUITKA = "nuitka"
    MYPYC = "mypyc"
    CYTHON = "cython"
    BYTECODE = "bytecode"


type AutoMode = Literal["off", "package", "all"]


@dataclass
class NuitkaModule:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class NativeExtension:
    import_path: ImportPath
    sources: list[PathExists]
    extras: list[str] = field(default_factory=list)


@dataclass
class CythonExtension:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class MypycModule:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class ZigModule:
    name: str
    import_path: ImportPath
    folder: PathExists = assert_path_exists(".")
    flags: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


@dataclass
class ZigExecutable:
    """
    Executable produced by a project's own `build.zig`, installed as a wheel script.

    `name` is the command name after installation (like a `[project.scripts]` key).
    `exe` is the file name `zig build` leaves in `zig-out/bin` (without `.exe`),
    defaulting to `name`.
    """

    name: str
    exe: str | None = None
    folder: PathExists = assert_path_exists(".")
    flags: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


#: The declared (non-CLI, non-default) bundler levels of one entrypoint, least specific first.
type EntrypointLevels = tuple[Level[BundleSettings], ...]


def package_data_specs(package_data: dict[str, list[str]]) -> list[str]:
    """`{pkg = ["*.json", "*.yaml"]}` -> `["pkg:*.json,*.yaml"]`, the form the builders read."""
    return [f"{package}:{','.join(patterns)}" for package, patterns in package_data.items()]


@dataclass
class NuitkaBinaryOptions:
    """
    Resolved `[tool.smelt.nuitka-binary]` options of one entrypoint.
    """

    extra_flags: list[str] = field(default_factory=list)
    zig: bool = True
    include_modules: list[str] = field(default_factory=list)
    include_packages: list[str] = field(default_factory=list)
    package_data: dict[str, list[str]] = field(default_factory=dict)

    def package_data_specs(self) -> list[str]:
        return package_data_specs(self.package_data)


@dataclass
class SmeltConfig:
    """
    Resolved, internal configuration the build code consumes.

    Built from the user-facing schema (`smelt.schema.ProjectConfig`) plus the
    `[project]` table by `from_toml_data`; users cannot set its fields directly.
    """

    packages_location: dict[str, str] = field(default_factory=dict)
    mypyc_modules: list[MypycModule] = field(default_factory=list)
    cython_modules: list[CythonExtension] = field(default_factory=list)
    nuitka_modules: list[NuitkaModule] = field(default_factory=list)
    c_extensions: list[NativeExtension] = field(default_factory=list)
    zig_modules: list[ZigModule] = field(default_factory=list)
    zig_executables: list[ZigExecutable] = field(default_factory=list)
    #: Declared `supported-targets` of `[tool.smelt]` (Zig-triple patterns).
    supported_targets: list[str] | None = None
    #: Declared bundler levels per entrypoint, keyed by `module.path:func` spec: the
    #: `[tool.smelt]`, `[tool.smelt.bundle]` and entrypoint tables, least specific
    #: first. Not merged here: `smelt.dist.resolve_bundle` adds the defaults and the
    #: command line around them and resolves.
    entrypoints: dict[str, EntrypointLevels] = field(default_factory=dict)
    #: `[tool.smelt.nuitka-binary]` options per entrypoint.
    nuitka_binary: dict[str, NuitkaBinaryOptions] = field(default_factory=dict)
    script_names: dict[str, str] = field(default_factory=dict)
    auto_mode: AutoMode = "off"
    backend_priority_order: list[Backend] = field(default_factory=lambda: [Backend.NUITKA])
    #: Raw `[project.dependencies]` specifier strings (PEP 621), for `isolated-build`'s
    #: `"pyproject"` version-resolution strategy -- see
    #: `smelt.isolated_build.resolve_isolated_build_version`.
    dependencies: list[str] = field(default_factory=list)

    @classmethod
    def from_toml_data(
        cls,
        toml_data: dict[str, TomlValue],
        project_root: Path | None = None,
        project_scripts: dict[str, str] | None = None,
        project_dependencies: list[str] | None = None,
    ) -> Self:
        """
        Validates `toml_data` (the `[tool.smelt]` table) against the schema and
        derives the internal config from it.
        """
        return cls.from_project_config(
            parse_project_config(toml_data, project_root=project_root),
            project_root=project_root,
            project_scripts=project_scripts,
            project_dependencies=project_dependencies,
        )

    @classmethod
    def from_project_config(
        cls,
        schema: ProjectConfig,
        project_root: Path | None = None,
        project_scripts: dict[str, str] | None = None,
        project_dependencies: list[str] | None = None,
    ) -> Self:
        root = project_root or Path.cwd()
        ext = schema.extensions

        # entrypoints: every `[project.scripts]` target is picked up automatically
        # (default options), `bundle.entrypoints` / `nuitka-binary.entrypoints`
        # declarations on top of that only customize options for one of them, or
        # declare additional entrypoints of their own.
        script_names: dict[str, str] = {}
        auto_keys: list[str] = []
        for name, target in (project_scripts or {}).items():
            auto_keys.append(target)
            script_names[name] = target
        declared_keys = [*schema.bundle.entrypoints, *schema.nuitka_binary.entrypoints]
        # a declaration keyed by a `[project.scripts]` name (e.g. `entrypoints.afpu`
        # for `afpu = "pkg.cli:afpu"`) customizes that script's auto-added target --
        # resolve it upfront so it lines up with `script_names` exactly like a
        # declaration spelled out as "pkg.cli:afpu" would.
        resolved_key = {key: script_names.get(key, key) for key in declared_keys}
        entrypoint_keys = dict.fromkeys(auto_keys)
        for explicit_key in resolved_key.values():
            if ":" in explicit_key:
                # already a full `module.path:func_name` spec, so it matches (at
                # most) one auto-added entry exactly by key. Hunting for other
                # auto-added entries sharing its module path would wrongly sweep up
                # unrelated sibling scripts from the same module (e.g. two distinct
                # `[project.scripts]` targets both living in `pkg.cli`).
                continue
            # bare module path (the pre-`[project.scripts]` convention, e.g. a
            # declaration keyed `"pkg.cli"` customizing the auto-added
            # `"pkg.cli:func"`) -- drop the auto entry so it isn't built a second
            # time, unconfigured, alongside the explicit one.
            for auto_key in [
                key
                for key in entrypoint_keys
                if key != explicit_key and key.partition(":")[0] == explicit_key
            ]:
                del entrypoint_keys[auto_key]
                for name, target in script_names.items():
                    if target == auto_key:
                        script_names[name] = explicit_key
        for explicit_key in resolved_key.values():
            entrypoint_keys[explicit_key] = None

        bundle_by_key: dict[str, BundleSettings] = {}
        nuitka_by_key: dict[str, NuitkaBinarySettings] = {}
        for declared, resolved in resolved_key.items():
            if declared in schema.bundle.entrypoints:
                bundle_by_key[resolved] = schema.bundle.entrypoints[declared]
            if declared in schema.nuitka_binary.entrypoints:
                nuitka_by_key[resolved] = schema.nuitka_binary.entrypoints[declared]
        entrypoints: dict[str, EntrypointLevels] = {}
        project_level = Level(
            "[tool.smelt]", BundleSettings(supported_targets=schema.supported_targets)
        )
        bundle_level = Level("[tool.smelt.bundle]", schema.bundle)
        nuitka_binary: dict[str, NuitkaBinaryOptions] = {}
        for key in entrypoint_keys:
            declared_as = next(
                (name for name, resolved in resolved_key.items() if resolved == key), None
            )
            entrypoints[key] = (
                project_level,
                bundle_level,
                Level(
                    f"[tool.smelt.bundle.entrypoints.{declared_as}]"
                    if declared_as in schema.bundle.entrypoints
                    else "",
                    bundle_by_key.get(key),
                ),
            )
            nuitka_merged = merge_settings(
                NuitkaBinarySettings, schema.nuitka_binary, nuitka_by_key.get(key)
            )
            nuitka_binary[key] = NuitkaBinaryOptions(
                extra_flags=nuitka_merged.extra_flags,
                zig=True if nuitka_merged.zig is None else nuitka_merged.zig,
                include_modules=nuitka_merged.include_modules,
                include_packages=nuitka_merged.include_packages,
                package_data=nuitka_merged.package_data,
            )

        def modules[S, T](entries: list[S], build: Callable[[S], T]) -> list[T]:
            return [build(entry) for entry in entries]

        return cls(
            packages_location=schema.package_dirs,
            mypyc_modules=modules(
                ext.mypyc, lambda e: MypycModule(e.import_path, e.source, e.extras)
            ),
            cython_modules=modules(
                ext.cython, lambda e: CythonExtension(e.import_path, e.source, e.extras)
            ),
            nuitka_modules=modules(
                ext.nuitka, lambda e: NuitkaModule(e.import_path, e.source, e.extras)
            ),
            c_extensions=modules(
                ext.c, lambda e: NativeExtension(e.import_path, e.sources, e.extras)
            ),
            zig_modules=modules(
                ext.zig,
                lambda e: ZigModule(
                    name=e.name or e.import_path.rpartition(".")[2],
                    import_path=e.import_path,
                    folder=e.folder or assert_path_exists(root),
                    flags=e.flags,
                    extras=e.extras,
                ),
            ),
            zig_executables=modules(
                schema.executables.zig,
                lambda e: ZigExecutable(
                    name=e.name,
                    exe=e.exe,
                    folder=e.folder or assert_path_exists(root),
                    flags=e.flags,
                    extras=e.extras,
                ),
            ),
            supported_targets=schema.supported_targets,
            entrypoints=entrypoints,
            nuitka_binary=nuitka_binary,
            script_names=script_names,
            auto_mode=ext.auto.mode,
            backend_priority_order=[Backend(name) for name in ext.auto.backends],
            dependencies=project_dependencies or [],
        )

    def declares_compiled_modules(self) -> bool:
        """
        Whether a build of this project compiles any Python module: declared
        extensions, or modules auto mode picks up. Zig executables are not counted:
        they are wheel scripts, not importable modules.
        """
        return self.auto_mode != "off" or any(
            (
                self.mypyc_modules,
                self.cython_modules,
                self.nuitka_modules,
                self.c_extensions,
                self.zig_modules,
            )
        )

    def get_path_solver(self, project_root: Path | None = None) -> PathSolver:
        """
        Builds a PathSolver based on the package configuration.
        """
        root = project_root or Path.cwd()
        return PathSolver(
            known_roots=[
                PackageRootPath(alias, assert_path_exists(root / path))
                for alias, path in self.packages_location.items()
            ],
            project_root=root,
        )


@dataclass(frozen=True)
class PyprojectConfig:
    """
    What Smelt reads from a `pyproject.toml`: the `[tool.smelt]` table validated
    against the schema, plus the `[project]` fields it depends on. Kept apart from
    `SmeltConfig` so that user-facing output (`smelt config`) can show the keys as
    the user writes them.
    """

    schema: ProjectConfig
    scripts: dict[str, str]
    dependencies: list[str]

    def resolve(self, project_root: Path | None = None) -> SmeltConfig:
        """Derives the internal config the build code consumes."""
        return SmeltConfig.from_project_config(
            self.schema,
            project_root=project_root,
            project_scripts=self.scripts,
            project_dependencies=self.dependencies,
        )


def parse_config_from_pyproject(
    toml_data: TomlData,
    project_root: Path | None = None,
) -> SmeltConfig:
    """
    Extracts Smelt config from TOML data coming out of a pyproject.toml.
    Shared by the CLI and the build hook, so both see the same config.
    """
    return load_pyproject_config(toml_data, project_root).resolve(project_root)


def load_pyproject_config(
    toml_data: TomlData,
    project_root: Path | None = None,
) -> PyprojectConfig:
    """
    Validates the Smelt-relevant parts of a pyproject.toml's TOML data, without
    deriving the internal config yet (see `PyprojectConfig.resolve`).
    """
    tool_config = toml_data.get("tool", {})
    if not isinstance(tool_config, dict):
        raise SmeltConfigError(
            f"`tool` section in toml data is not a dictionary, got {tool_config}. "
            "Does the TOML data come from a valid pyproject ?",
        )
    smelt_config = select_smelt_table(toml_data)

    project_scripts_decl = toml_get_nested_section(toml_data, "project", "scripts")
    if not isinstance(project_scripts_decl, dict):
        raise SmeltConfigError(
            f"`project.scripts` section should be a dictionary, got {project_scripts_decl}. ",
        )
    project_scripts: dict[str, str] = {}
    for name, target in project_scripts_decl.items():
        if not isinstance(target, str):
            raise SmeltConfigError(f"`project.scripts.{name}` should be a string, got {target}. ")
        project_scripts[name] = target

    project_section = toml_get_nested_section(toml_data, "project")
    project_dependencies_decl = project_section.get("dependencies", [])
    if not isinstance(project_dependencies_decl, list):
        raise SmeltConfigError(
            f"`project.dependencies` section should be a list, got {project_dependencies_decl}. "
        )
    project_dependencies: list[str] = []
    for dependency in project_dependencies_decl:
        if not isinstance(dependency, str):
            raise SmeltConfigError(
                f"`project.dependencies` entries should be strings, got {dependency}. "
            )
        project_dependencies.append(dependency)

    return PyprojectConfig(
        schema=parse_project_config(smelt_config, project_root=project_root),
        scripts=project_scripts,
        dependencies=project_dependencies,
    )
