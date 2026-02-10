"""
Config definition for Smelt.
Uses attrs for validation and conversion.

@date: 18.12.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from typing import Callable, cast
import os
from pathlib import Path

from attrs import asdict, define, field

from smelt.utils import (
    ImportPath,
    SmeltError,
    SmeltImpossibleImportPath,
    is_valid_import_path,
)

type ImportMap = dict[Path, ImportPath]

type TomlData = dict[str, str | list[str] | TomlData]


class SmeltConfigError(SmeltError): ...


def _make_converter[T](base_cls: type[T]) -> Callable[[T | dict[str, object]], T]:
    """
    Solves the issue of `attrs` feeding the value of `factory` into `converter`
    when a field uses both of them. When using a nested attrs dataclass which itself had conversions,
    it creates the awkward situation where `factory` should supply unconverted data instead of providing
    the correct value right away.
    This skips the conversion if the value if already of the correct type,
    and otherwise unpacks the values as keyword arguments and pass it to the
    nested attrs dataclass.
    """

    def converter(value: T | dict[str, object]) -> T:
        if isinstance(value, base_cls):
            return value
        # Mypy can't find out that value: T is always instance of type[T]
        # and does not narrow properly ?
        value = cast(dict[str, object], value)
        return base_cls(**value)

    return converter


def convert_values_to_import_map(import_map_as_str: dict[str, str]) -> ImportMap:
    import_map: ImportMap = {}
    for path, import_path in import_map_as_str.items():
        if not is_valid_import_path(import_path):
            raise SmeltImpossibleImportPath(
                f"{import_path} contains invalid characters for Python modules, "
                "it cannot  represent a valid Python import path"
            )
        import_map[Path(path)] = import_path
    return import_map


@define
class MypycConfig:
    modules: dict[Path, ImportPath] = field(
        factory=dict, converter=convert_values_to_import_map
    )


@define
class CythonConfig:
    modules: dict[Path, ImportPath] = field(
        factory=dict, converter=convert_values_to_import_map
    )


@define
class NativeExtensionsConfig:
    modules: dict[Path, ImportPath] = field(
        factory=dict, converter=convert_values_to_import_map
    )


@define
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    # Note: with attrs, the output of `factory` is passed to `converter`.
    # So we need to make the factory a dict - and let the subclasses converters
    # to their job
    mypyc: MypycConfig = field(
        factory=MypycConfig, converter=_make_converter(MypycConfig)
    )

    cython: CythonConfig = field(
        factory=CythonConfig, converter=_make_converter(CythonConfig)
    )
    native_extensions: NativeExtensionsConfig = field(
        factory=NativeExtensionsConfig,
        converter=_make_converter(NativeExtensionsConfig),
    )
    entrypoint: str | None = None
    debug: bool = False

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
