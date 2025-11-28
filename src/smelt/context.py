"""
A utility to provide convenience functions around managing
global/shared contexts within the scope of an application.
This implementation is designed to allow contexts to be optional
by design - i.e., they would only be used if the entrypoint application
has allowed them in the first place.
That allows to make the mutable shared state an opt-in thing,
and for library-style usage, these contexts should remain transparent
as long as they are not enabled.

Enable contexts with `enable_global_context()`.
use `get_context(<name>)` to get a global context instance
registered at the given `name`.
Create one under that name with `create_context(name)`.

Contexts objects themselves are defined by the `Context` protocol.
They should provid a `render` method that allows exporting them to a report.

@date: 27.11.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, overload


class Context(Protocol):
    """
    Base protocol for context objects.
    """

    def render(self) -> str: ...


@dataclass
class GlobalContext:
    """
    The main object manging all other contexts.
    Manages mainly two types of context objects:
    * Persistent contexts, that are stored under a given name
    * Traces, that contain information about a given operation in history,
    and are stored in an ordered way.
    """

    persistent_contexts: dict[str, Context] = field(default_factory=dict)
    traces: list[Context] = field(default_factory=list)

    def add_trace(self, trace: Context) -> None:
        """
        Stores a new trace in the global context.
        Added traces will be rendered in order of addition.
        """
        self.traces.append(trace)

    def clear_traces(self) -> None:
        """
        Clears all stored traces.
        """
        self.traces.clear()

    def create_context(self, name: str, context: Context) -> None:
        """
        Creates persistent context under the given name.
        """
        self.persistent_contexts[name] = context

    def get_context(self, name: str) -> Context | None:
        """
        Retrieves the persistent context stored under the given name.
        Returns None if no such context exists.
        """
        return self.persistent_contexts[name]

    def render(self) -> str:
        """
        Renders all persistent contexts and traces into a report.
        """
        lines: list[str] = []
        if self.persistent_contexts:
            lines.append("General context\n------------")
        for context_name, context in self.persistent_contexts.items():
            lines.append(context_name)
            lines.append("-" * len(context_name))
            lines.append(context.render())
        if self.traces:
            lines.append("Trace\n-------------")
        for trace in self.traces:
            lines.append("> " + trace.render())
        return "\n".join(lines)


@dataclass
class PathTrace:
    """
    Path from which we are operating.
    """

    cwd: Path = field(default_factory=Path.cwd)


@dataclass
class TaskTrace:
    """
    A generic context that takes abitrary string elements.
    Can be used to explain what part of the business logic is being
    executed at a given time in a given sequence, without any specific
    expectation on the format.
    """

    elements: list[str | Context] = field(default_factory=list)

    def comment(self, text: str) -> None:
        self.elements.append(text)

    def add_sub_context(self, context: Context) -> None:
        self.elements.append(context)

    def render(self) -> str:
        rendered_elements = [
            element if isinstance(element, str) else element.render()
            for element in self.elements
        ]
        return "\n".join(rendered_elements)


_GLOBAL_CONTEXT: None | GlobalContext = None


@overload
def get_context(name: None = None) -> GlobalContext | None: ...


@overload
def get_context(name: str) -> Context | None: ...


def get_context(name: str | None = None) -> Context | None:
    if name is None:
        return _GLOBAL_CONTEXT
    global_context = get_context()
    if global_context is None:
        return None
    return global_context.get_context(name)


def is_global_context_enabled() -> bool:
    """
    Whether contexts are enabled globally.
    """
    return _GLOBAL_CONTEXT is not None


def enable_global_context() -> None:
    """
    Creates the gloabl context and enables context operations
    within this interpreter scope.
    """
    global _GLOBAL_CONTEXT
    if _GLOBAL_CONTEXT is None:
        _GLOBAL_CONTEXT = GlobalContext()


def reset_contexts() -> None:
    """
    Resets all context.
    The global context is re-created,
    meaning all stored traces and persistent context will be lost.
    """
    global _GLOBAL_CONTEXT
    _GLOBAL_CONTEXT = None
    _GLOBAL_CONTEXT = GlobalContext()


def clear_contexts() -> None:
    """
    Clears all contexts, and disables context operations.
    """
    global _GLOBAL_CONTEXT
    _GLOBAL_CONTEXT = None


def create_context_if_enabled[Ctx: Context](
    name: str, context: type[Ctx] | Ctx
) -> Ctx | None:
    global_ctx = get_context()
    if global_ctx is None:
        return None
    if isinstance(context, type):
        context = context()

    global_ctx.create_context(name, context)
    return context
