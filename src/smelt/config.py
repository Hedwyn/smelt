"""
Config definitions for Smelt.

@date: 19.02.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from pathlib import Path
import os
from dataclasses import MISSING, dataclass, field, fields, asdict
from typing import TYPE_CHECKING, Any, Iterable, Self

from smelt.utils import (
    ImportPath,
    PackageRootPath,
    PathExists,
    PathSolver,
    SmeltConfigError,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def assert_type_is(obj: object, typ: type) -> None:
    if not isinstance(obj, typ):
        raise SmeltConfigError(f"Expected type {typ}, got {type(obj)}")


def convert_path(
    path_decl: str | list[str], type_hint: str, project_root: Path | None
) -> ImportPath | PathExists | Path | list[PathExists]:
    if type_hint == "ImportPath":
        assert_type_is(path_decl, str)
        return assert_is_valid_import_path(path_decl)

    match type_hint:
        case "PathExists" | "PathExists | None":
            assert_type_is(path_decl, str)
            path = (
                project_root / path_decl
                if project_root is not None
                else Path(path_decl)
            )

            assert_path_exists(path)
            return path
        case "list[PathExists]":
            assert_type_is(path_decl, list)
            return [
                assert_path_exists(project_root / p if project_root else Path(p))
                for p in path_decl
            ]

        case "list[str]":
            assert_type_is(path_decl, list)
            return path_decl

        case "str":
            assert_type_is(path_decl, str)
            return path_decl

        case _:
            assert_type_is(path_decl, str)
            raise NotImplementedError(f"Unsupported type hint: {type_hint}")


type ConfigContext = Iterable[str]


def _format_context(context: ConfigContext) -> str:
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


def build_datacls_from_toml[T: DataclassInstance](
    datacls: type[T],
    toml_data: TomlData,
    context: ConfigContext | None = None,
    project_root: Path | None = None,
) -> T:
    context = context if context is not None else []
    sentinel = object()
    kwargs: dict[str, object] = {}
    for f in fields(datacls):
        local_ctx = list(context)
        field_name = f.name
        value_decl = toml_data.get(field_name, sentinel)
        if value_decl is sentinel:
            if f.default is MISSING and f.default_factory is None:
                raise SmeltConfigError(
                    f"{_format_context(local_ctx)}Missing mandatory argument: {f.name}"
                )
            continue
        local_ctx.append(f.name)
        assert isinstance(f.type, str), (
            "Expected annotations from __future__ to be used"
        )
        try:
            value = convert_path(value_decl, f.type, project_root)
        except SmeltError as exc:
            raise SmeltConfigError(f"{_format_context(local_ctx)}{exc}") from exc
        kwargs[field_name] = value
    return datacls(**kwargs)


type _TomlData = str | list[_TomlData] | dict[str, _TomlData]
type TomlData = dict[str, _TomlData]


def auto_detect_is_build_hook(toml_data: TomlData) -> bool:
    """
    Given the extracted TOML config `toml_data`,
    detects whether Smelt Config was passed as a build hook
    (in which case it would be nested under whatever subsection_name
    the build backend uses for hooks), or a tool config.
    """
    has_tool_config = "smelt" in toml_data.get("tool", {})
    has_build_hook_conf = "smelt" in toml_get_nested_section(
        toml_data, "tool", "hatch", "build", "hooks"
    )
    if has_tool_config and has_build_hook_conf:
        # TODO: for now, not allowing this.
        # We can however consider using the hatch one only for build time
        # and the tool one for CLI use.
        # that can get confusing though.
        raise SmeltConfigError(
            "Smelt configuration found both in [tool.smelt] and "
            "[tool.hatch.build.hooks.smelt]. Please keep only one."
        )
    if has_build_hook_conf:
        return True
    if has_tool_config:
        return False
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
            raise SmeltConfigError(
                f"{_format_context(ctx)}Expected section, found {section}"
            )
    return section


@dataclass
class NuitkaModule:
    import_path: ImportPath
    source: PathExists | None = None


@dataclass
class NativeExtension:
    import_path: ImportPath
    sources: list[PathExists]


@dataclass
class CythonExtension:
    import_path: ImportPath
    source: PathExists | None = None


@dataclass
class MypycModule:
    import_path: ImportPath
    source: PathExists | None = None


@dataclass
class ZigModule:
    name: str
    import_path: ImportPath
    folder: PathExists = assert_path_exists(".")
    flags: list[str] = field(default_factory=list)


@dataclass
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    packages_location: dict[str, str] = field(default_factory=dict)
    mypyc_options: dict[str, Any] = field(default_factory=dict)
    mypyc_modules: list[MypycModule] = field(default_factory=list)
    cython_options: dict[str, Any] = field(default_factory=dict)
    cython_modules: list[CythonExtension] = field(default_factory=list)
    nuitka_modules: list[NuitkaModule] = field(default_factory=list)
    c_extensions: list[NativeExtension] = field(default_factory=list)
    zig_modules: list[ZigModule] = field(default_factory=list)
    platforms: Iterable[str] | None = None
    entrypoint: str | None = None
    debug: bool = False

    @classmethod
    def from_toml_data(
        cls, toml_data: dict[str, Any], project_root: Path | None = None
    ) -> Self:
        # native code
        native_extensions_decl = toml_data.pop("c_extensions", [])
        native_extensions = [
            build_datacls_from_toml(NativeExtension, decl, project_root=project_root)
            for decl in native_extensions_decl
        ]
        # zig modules
        zig_modules_decl = toml_data.pop("zig_modules", [])
        zig_modules = [
            build_datacls_from_toml(ZigModule, decl, project_root=project_root)
            for decl in zig_modules_decl
        ]
        # mypyc modules
        mypyc_modules_decl = toml_data.pop("mypyc_modules", [])
        mypyc_modules = [
            build_datacls_from_toml(MypycModule, decl, project_root=project_root)
            for decl in mypyc_modules_decl
        ]

        # cython
        cython_modules_decl = toml_data.pop("cython_modules", [])
        cython_modules = [
            build_datacls_from_toml(CythonExtension, decl, project_root=project_root)
            for decl in cython_modules_decl
        ]
        # nuitka
        nuitka_modules_decl = toml_data.pop("nuitka_modules", [])
        nuitka_modules = [
            build_datacls_from_toml(NuitkaModule, decl, project_root=project_root)
            for decl in nuitka_modules_decl
        ]

        return cls(
            mypyc_modules=mypyc_modules,
            c_extensions=native_extensions,
            zig_modules=zig_modules,
            cython_modules=cython_modules,
            nuitka_modules=nuitka_modules,
            **toml_data,
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

    def __str__(self) -> str:
        """
        A human-friendly stringified version of this config.
        """
        lines: list[str] = []
        for field_name, value in asdict(self).items():
            if isinstance(value, list):
                value = ",".join(value)
            if isinstance(value, dict):
                value = "".join(
                    ("\n * " + f"{key} -> {val}" for key, val in value.items())
                )
            lines.append(f"{field_name:20}: {value}")
        return "\n".join(lines)

    def load_env(self) -> None:
        """
        Updates internal values based on set environement variables.
        """
        if os.environ.get("SMELT_DEBUG"):
            self.debug = True
