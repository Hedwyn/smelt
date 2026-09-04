"""
Discovers python modules shipped by installed distributions,
and explores the module dependency graph reachable from a given entrypoint.

@date: 09.07.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import logging
import sys
from collections.abc import Iterable, Iterator
from enum import StrEnum
from importlib.metadata import Distribution
from pathlib import Path
from typing import NamedTuple

from smelt.static_eval import (
    DEFAULT_TARGET,
    StaticNames,
    TargetEnvironment,
    condition_value,
    static_names,
)
from smelt.utils import (
    ImportPath,
    ModuleName,
    PathExists,
    assert_is_valid_import_path,
    is_valid_import_path,
    is_valid_module_name,
    path_exists,
)

_logger = logging.getLogger(__name__)


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


def _is_type_checking_test(test: ast.expr) -> bool:
    """
    Whether `test` is the `TYPE_CHECKING` guard, in any of the spellings in use
    (`if TYPE_CHECKING:`, `if typing.TYPE_CHECKING:`, `if t.TYPE_CHECKING:`).
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _is_main_guard_test(test: ast.expr) -> bool:
    """
    Whether `test` is the `if __name__ == "__main__":` guard, in either operand order.

    Only the `==` spelling is recognised. `!=` inverts which branch runs, and letting
    it fall through to the ordinary handling -- following both branches -- errs towards
    over-collection, which costs size rather than correctness.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return _names_main_module(left, right) or _names_main_module(right, left)


def _names_main_module(name: ast.expr, literal: ast.expr) -> bool:
    """
    Whether `name` is `__name__` and `literal` the string `"__main__"`.
    """
    return (
        isinstance(name, ast.Name)
        and name.id == "__name__"
        and isinstance(literal, ast.Constant)
        and literal.value == "__main__"
    )


def _branch_taken(test: ast.expr, names: StaticNames) -> bool | None:
    """
    Which branch of `if test:` a plain import of the module takes: True for the body,
    False for the `else:`, None when it depends on something only running the module
    could reveal.

    Two guards are decided here rather than by `static_eval`, because neither is about
    the target at all -- both are false by construction for a module that is *imported*:

    * `if TYPE_CHECKING:` exists for type checkers and is guaranteed not to run.
      Following it is the single largest source of over-collection: one annotation-only
      import of a large library would otherwise drag that library, and everything it
      imports, into the distribution;
    * `if __name__ == "__main__":` cannot hold for a module reached by an import, which
      is how `heapq` would otherwise reach `doctest`.
    """
    if _is_type_checking_test(test) or _is_main_guard_test(test):
        return False
    return condition_value(test, names)


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    """
    Whether `handler` catches a failed import: `except ImportError:`,
    `except ModuleNotFoundError:`, either of them in a tuple, or a bare `except:`.
    """
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    named = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in caught
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    return bool(named.intersection({"ImportError", "ModuleNotFoundError"}))


def _is_optional_import(node: ast.Try) -> bool:
    """
    Whether `node` is the "optional import" idiom: a `try` whose failure to import is
    caught and handled, with no `raise` in the handler.

    That shape is a promise the module makes about itself -- it works without what it
    just tried to import -- which is what makes the import droppable at all. A handler
    that re-raises is making the opposite promise, and its import is mandatory however
    it is spelled.
    """
    for handler in node.handlers:
        if not _catches_import_error(handler):
            continue
        return not any(isinstance(statement, ast.Raise) for statement in ast.walk(handler))
    return False


def _iter_import_nodes(
    nodes: Iterable[ast.AST],
    *,
    follow_deferred: bool,
    follow_optional: bool,
    names: StaticNames,
) -> Iterator[ast.Import | ast.ImportFrom]:
    """
    Yields every import statement under `nodes` that a plain `import` of the module
    would actually execute, given the values `names` holds on the target.

    Three things are skipped, because the import cannot run, will not run, or need not:

    * the branch of a conditional that is not taken (`_branch_taken`) -- a
      `TYPE_CHECKING` or `__main__` guard, or a platform/version guard decided against
      the target. An undecidable condition keeps both branches;
    * function bodies, when `follow_deferred` is false. These are deferred to call
      time, so following them assumes every function is called. The caller decides
      where that assumption is worth making (see `_walk_module`);
    * the `try` and `else` blocks of an optional import (`_is_optional_import`), when
      `follow_optional` is false. The handlers are followed instead, since they are
      what runs when the import fails -- often importing the fallback the module
      settles for. The `else` goes with the body because it only runs when the body
      succeeded.

    Class bodies are always followed: they execute on import, like module level.
    """
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not follow_deferred:
            continue
        reachable: Iterable[ast.AST]
        if isinstance(node, ast.If):
            taken = _branch_taken(node.test, names)
            reachable = (
                ast.iter_child_nodes(node)
                if taken is None
                else (node.body if taken else node.orelse)
            )
        elif isinstance(node, ast.Try) and not follow_optional and _is_optional_import(node):
            reachable = [*node.handlers, *node.finalbody]
        else:
            reachable = ast.iter_child_nodes(node)
        yield from _iter_import_nodes(
            reachable,
            follow_deferred=follow_deferred,
            follow_optional=follow_optional,
            names=names,
        )


def _iter_raw_imports(
    source: str,
    *,
    follow_deferred: bool = True,
    follow_optional: bool = True,
    target: TargetEnvironment = DEFAULT_TARGET,
) -> Iterator[tuple[str | None, int, tuple[str, ...]]]:
    """
    Yields `(module, level, names)` for every `import`/`from ... import` statement
    in `source` that importing it would actually run: `follow_deferred` and
    `follow_optional` go straight through to `_iter_import_nodes`, and `target` is what
    the conditionals guarding those statements are decided against.

    `names` holds what a `from ... import a, b` statement pulls out of `module`, and
    matters because the language does not distinguish the two things it can be: a
    plain attribute of `module`, or a submodule of it. `from pkg import mod` is the
    *only* way to import `pkg.mod` in a single statement, so ignoring these names
    loses real modules -- `from . import mod` names nothing else at all. Whether each
    one is a module is decided by resolving it, in `_walk_module`.

    `*` is dropped: a star import pulls in names, never a submodule of its own.
    """
    tree = ast.parse(source)
    names = static_names(tree, target)
    for node in _iter_import_nodes(
        [tree], follow_deferred=follow_deferred, follow_optional=follow_optional, names=names
    ):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0, ()
        else:
            yield node.module, node.level, tuple(a.name for a in node.names if a.name != "*")


def _resolve_relative_import(
    current: ImportPath,
    is_package: bool,
    module: str | None,
    level: int,
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


class ModuleKind(StrEnum):
    """
    What a resolved import path actually *is*, which decides what a distribution has
    to do with it.

    The graph walk only ever follows `SOURCE` (nothing else has Python source to
    parse), but a distribution needs every case named: an `EXTENSION` has to be
    copied and have its own native dependencies resolved, a `NAMESPACE` package is a
    directory with no `__init__` at all, and `BUILTIN`/`FROZEN` modules live inside
    the interpreter with no file to ship.
    """

    SOURCE = "source"
    EXTENSION = "extension"
    NAMESPACE = "namespace"
    BUILTIN = "builtin"
    FROZEN = "frozen"
    MISSING = "missing"


class ResolvedModule(NamedTuple):
    """
    An import path resolved against the import machinery.

    `origin` is the file backing the module, and is None exactly for the kinds that
    have no file (namespace packages, builtins, frozen modules, and anything that
    failed to resolve).

    `shadowed_source` is the `.py` file an `EXTENSION` module was built from, when one
    still sits next to it. That is smelt's normal steady state rather than an oddity:
    every backend drops its `.so` next to the source it compiled, and an extension
    module wins over a source module on import -- so once a module has been built,
    the import machinery only ever reports the `.so`.
    """

    import_path: ImportPath
    kind: ModuleKind
    origin: PathExists | None
    is_package: bool
    is_stdlib: bool
    shadowed_source: PathExists | None = None

    @property
    def has_file(self) -> bool:
        """
        Whether this module is backed by a file that can be copied or compiled.
        """
        return self.origin is not None

    @property
    def parsable_source(self) -> PathExists | None:
        """
        The Python source to read this module's own imports from, if any.

        A built module resolves to its `.so`, whose imports can only be read from the
        source it was compiled from -- so a dependency walk that stopped at the first
        compiled module would see nothing beyond it.
        """
        if self.kind == ModuleKind.SOURCE:
            return self.origin
        return self.shadowed_source


def _is_extension_origin(origin: str) -> bool:
    """
    Whether `origin` names a native extension module, i.e. carries one of the
    suffixes the import machinery `dlopen`s (`.so`, `.abi3.so`,
    `.cpython-312-x86_64-linux-gnu.so`, `.pyd`, ...).
    """
    return origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES))


def _find_shadowed_source(origin: PathExists, *, is_package: bool) -> PathExists | None:
    """
    The `.py` file sitting next to the extension module at `origin`, i.e. the source it
    was compiled from, if it is still there.

    Matched on the extension's own name up to its first dot, which is how every smelt
    backend names what it produces (`fib.py` -> `fib.cpython-312-x86_64-linux-gnu.so`).
    """
    stem = origin.name.split(".")[0]
    candidate = origin.parent / ("__init__.py" if is_package else f"{stem}.py")
    return candidate if path_exists(candidate) else None


def resolve_module(import_path: ImportPath) -> ResolvedModule:
    """
    Resolves `import_path` against the import machinery and classifies it.

    Never raises: an import path that cannot be resolved (not installed, an import
    error while importing its parent package, ...) comes back as
    `ModuleKind.MISSING` rather than an exception, since discovery routinely
    proposes names that are not resolvable in this environment (a soft dependency,
    a platform-specific import, a typo in a string-literal import).
    """
    is_stdlib = import_path.partition(".")[0] in sys.stdlib_module_names
    try:
        spec = importlib.util.find_spec(import_path)
    except Exception as exc:  # noqa: BLE001 -- see below
        # Resolving `a.b` *imports* `a`, so this executes arbitrary third-party
        # module-level code and can raise absolutely anything: an ImportError for a
        # missing dependency, but also a bare `assert sys.platform == "win32"` in a
        # platform-specific module, a custom meta-path finder's own errors, or a
        # package that refuses to be imported outside its own runtime. All of them
        # mean the same thing here -- this name cannot be resolved in this
        # environment -- and none of them should abort a whole build.
        _logger.debug("Could not resolve %s: %r", import_path, exc)
        spec = None
    if spec is None:
        return ResolvedModule(import_path, ModuleKind.MISSING, None, False, is_stdlib)

    is_package = spec.submodule_search_locations is not None
    origin = spec.origin
    if origin is None or origin == "namespace":
        # PEP 420: a namespace package has no single origin (it may be spread over
        # several roots), which is precisely what makes it a namespace package.
        kind = ModuleKind.NAMESPACE if is_package else ModuleKind.MISSING
        return ResolvedModule(import_path, kind, None, is_package, is_stdlib)
    if origin == "built-in":
        return ResolvedModule(import_path, ModuleKind.BUILTIN, None, is_package, is_stdlib)
    if origin == "frozen":
        return ResolvedModule(import_path, ModuleKind.FROZEN, None, is_package, is_stdlib)

    path = Path(origin)
    if not path_exists(path):
        return ResolvedModule(import_path, ModuleKind.MISSING, None, is_package, is_stdlib)
    if path.suffix == ".py":
        kind = ModuleKind.SOURCE
    elif _is_extension_origin(origin):
        return ResolvedModule(
            import_path,
            ModuleKind.EXTENSION,
            path,
            is_package,
            is_stdlib,
            shadowed_source=_find_shadowed_source(path, is_package=is_package),
        )
    else:
        # e.g. an already-sourceless `.pyc`, or a module provided by a custom loader
        # from some other file format: nothing this codebase knows how to handle.
        kind = ModuleKind.MISSING
    return ResolvedModule(import_path, kind, path, is_package, is_stdlib)


def iter_package_modules(import_path: ImportPath) -> set[ImportPath]:
    """
    Every module contained in the package `import_path`, recursively, itself included.

    Unlike `find_modules_under_root`, this walks what the *import machinery* resolves
    the package to (so it works for an installed third-party package, and covers every
    root of a namespace package), and it reports native extension modules and
    subpackages too, not just `.py` files. That is what makes it usable to pull in a
    whole package whose internals are loaded dynamically -- the case static import
    discovery cannot see.
    """
    modules: set[ImportPath] = set()
    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for directory in package_directories(import_path):
        modules.add(import_path)
        for entry in directory.rglob("*"):
            if "__pycache__" in entry.parts:
                continue
            relative = entry.relative_to(directory)
            if entry.is_dir():
                parts = relative.parts
            elif entry.name.endswith(extension_suffixes):
                # `mod.cpython-312-x86_64-linux-gnu.so` -> `mod`
                parts = (*relative.parts[:-1], entry.name.split(".")[0])
            elif entry.suffix == ".py":
                parts = (
                    relative.parts[:-1]
                    if entry.stem == "__init__"
                    else (
                        *relative.parts[:-1],
                        entry.stem,
                    )
                )
            else:
                continue
            if not all(is_valid_module_name(part) for part in parts):
                continue
            candidate = ".".join([import_path, *parts])
            if is_valid_import_path(candidate):
                modules.add(assert_is_valid_import_path(candidate))
    return modules


def package_directories(import_path: ImportPath) -> list[PathExists]:
    """
    Every directory the package `import_path` occupies, or an empty list if it does not
    resolve to a package at all.

    Usually one directory, but a PEP 420 namespace package is spread over as many
    roots as contribute to it -- and a distribution collecting a package's data files
    has to look in all of them.
    """
    try:
        spec = importlib.util.find_spec(import_path)
    except Exception as exc:  # noqa: BLE001 -- resolution imports the parent, see `resolve_module`
        _logger.debug("Could not resolve package %s: %r", import_path, exc)
        return []
    if spec is None or spec.submodule_search_locations is None:
        return []
    return [
        directory
        for location in spec.submodule_search_locations
        if path_exists(directory := Path(location))
    ]


def _resolve_module_path(import_path: ImportPath) -> tuple[PathExists, bool] | None:
    """
    Locates the source file of `import_path`, along with whether it is a package.
    Returns None if `import_path` cannot be resolved to a `.py` source file
    (e.g. builtin, frozen, C extension, or namespace package).
    """
    resolved = resolve_module(import_path)
    if resolved.kind != ModuleKind.SOURCE or resolved.origin is None:
        return None
    return resolved.origin, resolved.is_package


def find_modules_under_root(import_path: ImportPath, root: PathExists) -> set[ImportPath]:
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


def _submodules_among(target: ImportPath, names: Iterable[str]) -> Iterator[ImportPath]:
    """
    Of the `names` a `from {target} import ...` statement pulls out, the ones that are
    actually submodules of `target` rather than plain attributes of it.

    Decided by looking for a matching file in `target`'s own directory, deliberately
    *not* by resolving `{target}.{name}` through the import machinery: resolving a
    dotted name imports its parent, so that would execute `target`'s module-level code
    at build time -- for every candidate name, in every module walked. Beyond being
    slow, plenty of modules cannot be imported here at all (a platform-specific one
    guarded by a bare `assert sys.platform == ...` raises outright), and a dependency
    walk has no business running the code it is reading.
    """
    directories = package_directories(target)
    if not directories:
        # Not a package: `from module import name` cannot be naming a submodule.
        return
    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for name in names:
        candidate = f"{target}.{name}"
        if not is_valid_module_name(name) or not is_valid_import_path(candidate):
            continue
        for directory in directories:
            if (
                (directory / name).is_dir()
                or path_exists(directory / f"{name}.py")
                or any(path_exists(directory / f"{name}{suffix}") for suffix in extension_suffixes)
            ):
                yield assert_is_valid_import_path(candidate)
                break


def _get_or_create_node(import_path: ImportPath, registry: dict[ImportPath, Node]) -> Node:
    node = registry.get(import_path)
    if node is None:
        node = Node(import_path, set())
        registry[import_path] = node
    return node


def _walk_module(
    node: Node,
    registry: dict[ImportPath, Node],
    visited: set[ImportPath],
    target: TargetEnvironment,
    follow_optional: bool,
) -> None:
    if node.name in visited:
        return
    visited.add(node.name)

    # Deliberately not `_resolve_module_path`: the walk follows a built module through
    # to the source it was compiled from (`parsable_source`), so that compiling a
    # module does not truncate the dependency graph at it.
    module = resolve_module(node.name)
    path = module.parsable_source
    if path is None:
        return
    is_package = module.is_package

    # A function-body import is deferred to call time, so following it assumes the
    # function gets called. In the standard library that assumption is wrong far more
    # often than it is right: the imports there are overwhelmingly self-test, CLI and
    # diagnostic paths that a shipped application never enters, and they are what makes
    # the closure explode. `difflib._test()` alone is what pulls in
    # `doctest -> unittest -> asyncio -> multiprocessing`, and `pdb` inside
    # `bdb.set_trace()` is what pulls in `pydoc -> http.server -> ssl`. Application and
    # third-party code gets the opposite treatment: there a lazy import is usually
    # load-bearing (deferred to break a cycle, or to keep start-up cheap), and dropping
    # it would ship a distribution that fails on the first call.
    try:
        raw_imports = list(
            _iter_raw_imports(
                path.read_text(),
                follow_deferred=not module.is_stdlib,
                follow_optional=follow_optional,
                target=target,
            )
        )
    except (SyntaxError, UnicodeDecodeError):
        # Unparseable source (e.g. a work-in-progress file): treat as a leaf,
        # same as a module we can't resolve at all.
        return

    dependencies: set[ImportPath] = set()
    for imported, level, names in raw_imports:
        resolved: ImportPath | None
        if level:
            resolved = _resolve_relative_import(node.name, is_package, imported, level)
        elif imported is not None and is_valid_import_path(imported):
            resolved = assert_is_valid_import_path(imported)
        else:
            resolved = None
        if resolved is None:
            continue
        dependencies.update(_expand_prefixes(resolved))
        dependencies.update(_submodules_among(resolved, names))

    for dependency in dependencies:
        dep_node = _get_or_create_node(dependency, registry)
        node.deps.add(dep_node)
        _walk_module(dep_node, registry, visited, target, follow_optional)


def build_dependency_graph(
    entrypoint: ImportPath,
    target: TargetEnvironment = DEFAULT_TARGET,
    *,
    follow_optional: bool = True,
) -> Node:
    """
    Recursively parses `entrypoint` and every module it imports,
    building the module dependency graph reachable from it.

    Only what an import actually executes is followed, and `target` is what the
    platform and version guards in the source are decided against -- the host by
    default, which is the only machine smelt can currently assemble a distribution for
    (see `TargetEnvironment.host`).

    `follow_optional` decides what to do with an import a module already handles the
    failure of (`_is_optional_import`). Default on, because "optional" is not
    "unwanted": the fallback behind one is often slower, or narrower, than what it
    replaces. Turning it off gives the smallest closure a module's own promises allow,
    and the difference between the two graphs is exactly the set of modules a
    distribution could leave out (see `optional_modules`).
    """
    registry: dict[ImportPath, Node] = {}
    root = _get_or_create_node(entrypoint, registry)
    _walk_module(root, registry, set(), target, follow_optional)
    return root


def optional_modules(
    entrypoint: ImportPath, target: TargetEnvironment = DEFAULT_TARGET
) -> set[ImportPath]:
    """
    The modules reachable from `entrypoint` *only* through imports it, or something it
    imports, already handles the failure of.

    Every one of them can be left out of a distribution without an `ImportError`: the
    module that wanted it says so itself, in its own `except ImportError:` handler. What
    that costs is not visible from here -- `hashlib` falls back to the builtin hashes
    and nobody notices, while `urllib.request` without `ssl` quietly stops speaking
    HTTPS -- which is why this reports rather than prunes.
    """
    required = {
        node.name
        for node in flatten_dependency_graph(
            build_dependency_graph(entrypoint, target, follow_optional=False)
        )
    }
    reachable = {
        node.name for node in flatten_dependency_graph(build_dependency_graph(entrypoint, target))
    }
    return reachable - required


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
