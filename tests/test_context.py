"""
Unit tests for the context management.

@date: 26.11.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from typing import Generator

import pytest

from smelt.context import (
    clear_contexts,
    create_context_if_enabled,
    enable_global_context,
    get_context,
    is_global_context_enabled,
    reset_contexts,
)
from smelt.utils import ModpathType, PathResolutionTrace


@pytest.fixture(autouse=True)
def clear_contexts_before_and_after() -> Generator[None, None, None]:
    clear_contexts()
    try:
        yield
    finally:
        clear_contexts()


def test_global_context_toggling() -> None:
    assert is_global_context_enabled() is False
    enable_global_context()
    assert is_global_context_enabled() is True
    reset_contexts()
    assert is_global_context_enabled() is True
    clear_contexts()
    assert is_global_context_enabled() is False


def test_get_global_context() -> None:
    assert get_context() is None
    enable_global_context()
    ctx1 = get_context()

    ctx2 = get_context()
    assert ctx1 is ctx2
    clear_contexts()
    assert get_context() is None


def test_add_trace() -> None:
    enable_global_context()
    ctx = get_context()
    import_path = "a.b"
    fs_path = "a/b"
    trace1 = PathResolutionTrace(
        import_path, fs_path, resolution_type=ModpathType.IMPORT
    )
    trace2 = PathResolutionTrace(import_path, fs_path, resolution_type=ModpathType.FS)
    assert ctx is not None
    ctx.add_trace(trace1)
    ctx.add_trace(trace2)
    # TODO: add snapshot. For now only checking basic stuff
    rendered = ctx.render()
    assert rendered.count("a/b") == 2
    assert rendered.count("a.b") == 2


class DummyContext:
    def render(self) -> str:
        return "dummy"


def test_get_context() -> None:
    enable_global_context()
    ctx1 = create_context_if_enabled("ctx1", DummyContext)
    ctx2 = create_context_if_enabled("ctx2", DummyContext)
    assert isinstance(ctx1, DummyContext)
    assert isinstance(ctx2, DummyContext)
    assert ctx1 is not ctx2
    assert get_context("ctx1") is ctx1
    assert get_context("ctx2") is ctx2
    global_ctx = get_context()
    assert global_ctx is not None
    assert get_context() is global_ctx
    rendered = global_ctx.render()
    assert rendered.count("dummy") == 2
