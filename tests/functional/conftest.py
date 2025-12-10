"""
Main configuration for pytest.
Tests in this folder are inteded to be run with nox,
but running manually with pytest is supported for certain
specific cases.

@date: 11.12.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations
from pytest import Parser


def pytest_addoption(parser: Parser) -> None:
    parser.addoption(
        "--package",
        action="store",
        default=None,
        help="Package name from which to test fibonacci function",
    )
