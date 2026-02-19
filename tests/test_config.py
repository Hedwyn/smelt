from __future__ import annotations
from typing import TYPE_CHECKING


from smelt.config import (
    NuitkaModule,
    MypycModule,
    CythonExtension,
    SmeltConfig,
    TomlData,
    build_datacls_from_toml,
)


import pytest

from smelt.utils import SmeltConfigError, TomlData

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


@pytest.mark.parametrize("ConfigCls", [NuitkaModule, MypycModule])
@pytest.mark.parametrize(
    ["toml_data", "valid"],
    [
        ({"import_path": "a.b.c"}, True),
        ({"import_path": "a.$.c"}, False),
        ({"import_path": "a.$./"}, False),
        ({"import_path": ""}, False),
    ],
)
def test_config_conversion_import_path(
    ConfigCls: type[NuitkaModule | MypycModule],
    toml_data: TomlData,
    valid: bool,
) -> None:
    if valid:
        datacls = build_datacls_from_toml(ConfigCls, toml_data=toml_data)
        assert datacls.import_path == toml_data["import_path"]
    else:
        with pytest.raises(SmeltConfigError):
            build_datacls_from_toml(ConfigCls, toml_data=toml_data)
