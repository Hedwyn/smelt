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

_CONVERSIONS = {
    "ImportPath": assert_is_valid_import_path,
    "PathExists": assert_path_exists,
    "list[PathExists]": lambda values: [assert_path_exists(p) for p in values],
}


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
) -> T:
    context = context if context is not None else []
    sentinel = object()
    kwargs: dict[str, object] = {}
    for f in fields(datacls):
        local_ctx = list(context)
        field_name = f.name
        value_decl = toml_data.get(field_name, sentinel)
        if value_decl is sentinel:
            if f.default is MISSING:
                raise SmeltConfigError(
                    f"{_format_context(local_ctx)}Missing mandatory argument: {f.name}"
                )
            continue
        local_ctx.append(f.name)
        assert isinstance(f.type, str), (
            "Expected annotations from __future__ to be used"
        )
        try:
            value = (
                conversion(value_decl)  # type: ignore[arg-type]
                if (conversion := _CONVERSIONS.get(f.type)) is not None
                else value_decl
            )
        except SmeltError as exc:
            raise SmeltConfigError(f"{_format_context(local_ctx)}{exc}") from exc
        kwargs[field_name] = value
    return datacls(**kwargs)


type _TomlData = str | list[_TomlData] | dict[str, _TomlData]
type TomlData = dict[str, _TomlData]


def toml_get_nested_section(toml_data: TomlData, *path: str) -> _TomlData:
    ctx: list[str] = []
    section: _TomlData = toml_data
    for subsection_name in path:
        ctx.append(subsection_name)
        if not isinstance(section, dict):
            raise SmeltConfigError(
                f"{_format_context(ctx)}Expected section, found {section}"
            )
        section = section.get(subsection_name, {})
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
    source: PathExists


@dataclass
class MypycModule:
    import_path: ImportPath
    source: PathExists | None = None


@dataclass
class ZigModule:
    name: PathExists
    import_path: ImportPath
    folder: PathExists = assert_path_exists(".")


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
    entrypoint: str | None = None
    debug: bool = False

    @classmethod
    def from_toml_data(cls, toml_data: dict[str, Any]) -> Self:
        # native code
        native_extensions_decl = toml_data.pop("c_extensions", [])
        native_extensions = [
            build_datacls_from_toml(NativeExtension, decl)
            for decl in native_extensions_decl
        ]
        # zig modules
        zig_modules_decl = toml_data.pop("zig_modules", [])
        zig_modules = [
            build_datacls_from_toml(ZigModule, decl) for decl in zig_modules_decl
        ]
        # mypyc modules
        mypyc_modules_decl = toml_data.pop("mypyc_modules", [])
        mypyc_modules = [
            build_datacls_from_toml(MypycModule, decl) for decl in mypyc_modules_decl
        ]

        # cython
        cython_modules_decl = toml_data.pop("cython_modules", [])
        cython_modules = [
            build_datacls_from_toml(CythonExtension, decl)
            for decl in cython_modules_decl
        ]
        # nuitka
        nuitka_modules_decl = toml_data.pop("nuitka_modules", [])
        nuitka_modules = [
            build_datacls_from_toml(NuitkaModule, decl) for decl in nuitka_modules_decl
        ]

        return cls(
            mypyc_modules=mypyc_modules,
            c_extensions=native_extensions,
            zig_modules=zig_modules,
            cython_modules=cython_modules,
            nuitka_modules=nuitka_modules,
            **toml_data,
        )

    def get_path_solver(self) -> PathSolver:
        """
        Builds a PathSolver based on the package configuration.
        """
        return PathSolver(
            known_roots=[
                PackageRootPath(alias, Path(path))
                for alias, path in self.packages_location.items()
            ]
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
