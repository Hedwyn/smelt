from __future__ import annotations

import pytest

from smelt.config import MypycConfig, CythonConfig, NativeExtensionsConfig, SmeltConfig

MOCK_IMPORT_MAP = {
    "modules": {
        "a/b/c.py": "a.b.c",
        "d/e/f.py": "d.e.f",
    }
}

CONFIG_DATA: dict[str, dict[str, str] | str] = {
    "mypyc": MOCK_IMPORT_MAP.copy(),
    "cython": MOCK_IMPORT_MAP.copy(),
    "native_extensions": {"modules": {"a/b/c.c": "a.b.c"}},
}


@pytest.mark.parametrize(
    "config_cls", [MypycConfig, CythonConfig, NativeExtensionsConfig, SmeltConfig]
)
def test_all_fields_are_defaulted(config_cls: type) -> None:
    config_cls()


def test_config_instanciation_from_dict() -> None:
    config = SmeltConfig(**CONFIG_DATA)
    for section in ("mypyc", "cython", "native_extensions"):
        assert hasattr(config, section)
        # assert len(getattr(config, section).modules) > 0
