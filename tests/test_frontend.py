"""
Test suite for smelt frontend.

@date: 13.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import pytest
import tomllib

from smelt.frontend import TomlData, parse_config_from_pyproject

SAMPLE_CONFIG: str = """\
[tool.smelt]
mypyc = ["minimal.fib"]
c_extensions=["minimal.hello"]
entrypoint="minimal.cli"
"""


@pytest.fixture
def toml_data() -> TomlData:
    """
    Example TOML data as it would be extracted from pyproject.toml
    """
    return tomllib.loads(SAMPLE_CONFIG)


def test_parse(toml_data: TomlData) -> None:
    """
    Verifies that the pyproject parse extracts Smelt config properly
    """
    config = parse_config_from_pyproject(toml_data)
    assert config.entrypoint == "minimal.cli"
    assert config.c_extensions == ["minimal.hello"]
    assert config.mypyc == ["minimal.fib"]
