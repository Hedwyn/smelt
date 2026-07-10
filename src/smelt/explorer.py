"""
Discovers python modules shipped by installed distributions,
and explores the module dependency graph reachable from a given entrypoint.

@date: 09.07.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterator
from importlib.metadata import Distribution
from pathlib import Path
from typing import NamedTuple

from smelt.utils import (
    ImportPath,
    ModuleName,
    PathExists,
    assert_is_valid_import_path,
    is_valid_import_path,
    is_valid_module_name,
    path_exists,
)


class Node(NamedTuple):
    """
    A module in the dependency graph.
    Hashed and compared by `name` only, so a module reached through several
    parents always resolves to the same node instead of being duplicated.
    """

    name: ImportPath
    deps: set[Node]

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Node) and self.name == other.name


def find_modules_in_distribution(dist: Distribution) -> set[ModuleName]:
    """
    Finds the top-level python modules/packages shipped by `dist`.

    Derived from `dist.files` rather than `top_level.txt`: the latter is
    unreliable for src-layout editable installs, where setuptools records
    the layout folder (`src`) instead of the actual top-level package name.
    """
    modules: set[ModuleName] = set()
    for file in dist.files or []:
        if file.suffix != ".py":
            continue
        parts = file.parts[1:] if file.parts[0] == "src" else file.parts
        if not parts:
            continue
        name = parts[0].removesuffix(".py")
        if is_valid_module_name(name):
            modules.add(name)
    return modules


def _iter_raw_imports(source: str) -> Iterator[tuple[str | None, int]]:
    """
    Yields `(module, level)` for every `import`/`from ... import` statement
    found anywhere in `source`'s AST, `level` being 0 for absolute imports.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module, node.level


def _resolve_relative_import(
    current: ImportPath, is_package: bool, module: str | None, level: int
) -> ImportPath | None:
    """
    Resolves a relative `from . import ...` statement found in `current`
    into an absolute import path, given whether `current` is itself a package.
    """
    parts = current.split(".")
    base_len = (len(parts) if is_package else len(parts) - 1) - (level - 1)
    if base_len < 0:
        return None
    base = parts[:base_len]
    if module:
        base.extend(module.split("."))
    if not base:
        return None
    candidate = ".".join(base)
    if not is_valid_import_path(candidate):
        return None
    return assert_is_valid_import_path(candidate)


def _resolve_module_path(import_path: ImportPath) -> tuple[PathExists, bool] | None:
    """
    Locates the source file of `import_path`, along with whether it is a package.
    Returns None if `import_path` cannot be resolved to a `.py` source file
    (e.g. builtin, frozen, C extension, or namespace package).
    """
    try:
        spec = importlib.util.find_spec(import_path)
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin)
    if not path_exists(path) or path.suffix != ".py":
        return None
    return path, spec.submodule_search_locations is not None


def find_modules_under_root(
    import_path: ImportPath, root: PathExists
) -> set[ImportPath]:
    """
    Finds every python module under `root`, the filesystem location `import_path`
    resolves to, as fully dotted import paths.

    Unlike `find_modules_in_distribution`, this walks the filesystem directly and
    does not require the package to be part of an installed distribution (nor its
    RECORD to be up to date), at the cost of not resolving namespace packages
    spread across several roots.
    """
    modules: set[ImportPath] = set()
    for py_file in Path(root).rglob("*.py"):
        if py_file.name == "__init__.py":
            # A package's own `__init__.py` can't be compiled the way Smelt compiles
            # every other module: the result would sit right next to the still-present
            # package directory under the same import name, and a directory always
            # wins over a same-named extension module on import. Only compile a
            # package's concrete submodules, never the package itself.
            continue
        parts = py_file.relative_to(root).with_suffix("").parts
        if not all(is_valid_module_name(p) for p in parts):
            continue
        modules.add(assert_is_valid_import_path(".".join([import_path, *parts])))
    return modules


def has_local_source(import_path: ImportPath) -> bool:
    """
    True if `import_path` resolves to a local, non-native `.py` source file,
    as opposed to a builtin, frozen, C extension, or namespace package.
    """
    return _resolve_module_path(import_path) is not None


def _expand_prefixes(import_path: ImportPath) -> Iterator[ImportPath]:
    """
    Yields every package prefix of `import_path`, included itself,
    since importing `a.b.c` also imports `a` and `a.b`.
    """
    parts = import_path.split(".")
    for i in range(1, len(parts) + 1):
        yield assert_is_valid_import_path(".".join(parts[:i]))


def _get_or_create_node(
    import_path: ImportPath, registry: dict[ImportPath, Node]
) -> Node:
    node = registry.get(import_path)
    if node is None:
        node = Node(import_path, set())
        registry[import_path] = node
    return node


def _walk_module(
    node: Node, registry: dict[ImportPath, Node], visited: set[ImportPath]
) -> None:
    if node.name in visited:
        return
    visited.add(node.name)

    resolved = _resolve_module_path(node.name)
    if resolved is None:
        return
    path, is_package = resolved

    try:
        raw_imports = list(_iter_raw_imports(path.read_text()))
    except (SyntaxError, UnicodeDecodeError):
        # Unparseable source (e.g. a work-in-progress file): treat as a leaf,
        # same as a module we can't resolve at all.
        return

    targets: set[ImportPath] = set()
    for module, level in raw_imports:
        target: ImportPath | None
        if level:
            target = _resolve_relative_import(node.name, is_package, module, level)
        elif module is not None and is_valid_import_path(module):
            target = assert_is_valid_import_path(module)
        else:
            target = None
        if target is None:
            continue
        targets.update(_expand_prefixes(target))

    for target in targets:
        dep_node = _get_or_create_node(target, registry)
        node.deps.add(dep_node)
        _walk_module(dep_node, registry, visited)


def build_dependency_graph(entrypoint: ImportPath) -> Node:
    """
    Recursively parses `entrypoint` and every module it imports,
    building the module dependency graph reachable from it.
    """
    registry: dict[ImportPath, Node] = {}
    root = _get_or_create_node(entrypoint, registry)
    _walk_module(root, registry, set())
    return root


def flatten_dependency_graph(root: Node) -> set[Node]:
    """
    Flattens a module dependency graph into the set of all nodes it contains.
    """
    flattened: set[Node] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in flattened:
            continue
        flattened.add(current)
        stack.extend(current.deps)
    return flattened
