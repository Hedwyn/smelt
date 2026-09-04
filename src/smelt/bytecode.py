"""
The `bytecode` backend: ships modules as `.pyc` instead of native code.

Unlike the mypyc/cython/nuitka backends, this one produces no native code at all --
it only pre-compiles a module to its bytecode form. It exists for *application
bundling*, not for wheels: a wheel's consumer compiles bytecode on install anyway,
so converting there would be pointless.

It also has no in-place mode, by design. Python resolves imports in the order
extension -> source -> bytecode, so a `.pyc` dropped next to its own `.py` is never
imported -- the exact opposite of how a `.so` shadows its source. A `.pyc` produced
here is therefore only ever meaningful inside a distribution folder, where the `.py`
is not shipped at all (see `smelt.dist`). `compile_tree` does write next to the source
when asked to, but only as a step towards that same end: its caller then deletes the
sources it replaced (see `smelt.own_python.stage_interpreter`).

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import importlib.util
import logging
import py_compile
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterable, NamedTuple

from smelt.utils import (
    ImportPath,
    PathExists,
    SmeltError,
    assert_path_exists,
)

_logger = logging.getLogger(__name__)


#: PEP 552 invalidation mode used for every `.pyc` smelt produces, and not a tunable.
#: The default (timestamp) embeds the source's mtime and size, which is meaningless
#: once the `.py` is not shipped, and makes otherwise identical builds differ;
#: `CHECKED_HASH` would make the interpreter stat a source file that does not exist in
#: the distribution. `UNCHECKED_HASH` carries the source hash without ever validating
#: it, which is both the right semantics for a sourceless distribution and
#: byte-deterministic for a given source.
INVALIDATION_MODE: Final = py_compile.PycInvalidationMode.UNCHECKED_HASH

#: Suffix of the produced files. Deliberately the "sourceless" layout
#: (`pkg/mod.pyc`), not the cache layout (`pkg/__pycache__/mod.cpython-312.pyc`):
#: the latter is only ever discovered *through* the `.py` that is not shipped.
PYC_SUFFIX: Final[str] = ".pyc"


class BytecodeCompilationError(SmeltError):
    """
    Raised when a module cannot be compiled to bytecode.
    """


class PycTargetTag(NamedTuple):
    """
    Everything about the compiling interpreter that a produced `.pyc` is bound to.

    Bytecode is version-locked: the magic number is checked before any user code
    runs, and a mismatch aborts with `RuntimeError: Bad magic number in .pyc file`.
    A distribution has to record this so its launcher can reject a wrong interpreter
    with an actionable message instead of that traceback.
    """

    python_version: tuple[int, int]
    magic_number: bytes
    optimize: int

    @classmethod
    def current(cls, optimize: int = -1) -> PycTargetTag:
        """
        The tag for bytecode compiled by the running interpreter.

        `optimize` follows `py_compile`'s convention, where -1 means "whatever the
        current interpreter runs with"; it is resolved here so the recorded tag names
        an actual optimization level rather than the placeholder.
        """
        resolved = sys.flags.optimize if optimize < 0 else optimize
        return cls(
            python_version=(sys.version_info.major, sys.version_info.minor),
            magic_number=importlib.util.MAGIC_NUMBER,
            optimize=resolved,
        )

    @property
    def version_string(self) -> str:
        major, minor = self.python_version
        return f"{major}.{minor}"

    def serialize(self) -> dict[str, str | int]:
        return {
            "python_version": self.version_string,
            "magic_number": self.magic_number.hex(),
            "optimize": self.optimize,
        }


@dataclass(frozen=True)
class PycArtifact:
    """
    One module compiled to bytecode, and where it belongs in a distribution folder.

    The counterpart of `GenericExtension` for this backend, and deliberately not that
    type: there is no `setuptools` extension, no shared runtime, and no destination
    next to the source -- `dest_rel_path` is relative to a distribution root.
    """

    import_path: ImportPath
    src_path: PathExists
    dest_rel_path: Path


def module_dest_rel_path(
    import_path: ImportPath,
    *,
    is_package: bool,
    suffix: str = PYC_SUFFIX,
) -> Path:
    """
    Distribution-relative path a module should be placed at, so that a plain
    `sys.path` entry pointing at the distribution root imports it under
    `import_path`.

    A package keeps its directory and gets an `__init__` file inside it; a plain
    module becomes a single file in its parent package's directory.
    """
    parts = import_path.split(".")
    if is_package:
        return Path(*parts, f"__init__{suffix}")
    *packages, module_name = parts
    return Path(*packages, f"{module_name}{suffix}")


def compile_to_pyc(
    source: PathExists,
    dest: Path,
    *,
    optimize: int = -1,
) -> PathExists:
    """
    Compiles the Python source file `source` to bytecode at exactly `dest`
    (parent directories created as needed).

    Raises
    ------
    BytecodeCompilationError
        If `source` cannot be compiled (e.g. a syntax error, or source written for a
        different Python version).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        py_compile.compile(
            str(source),
            cfile=str(dest),
            doraise=True,
            optimize=optimize,
            invalidation_mode=INVALIDATION_MODE,
        )
    except (py_compile.PyCompileError, OSError, ValueError) as exc:
        raise BytecodeCompilationError(f"Failed to compile {source} to bytecode: {exc}") from exc
    return assert_path_exists(dest)


def compile_module(
    import_path: ImportPath,
    source: PathExists,
    dest_root: Path,
    *,
    is_package: bool,
    optimize: int = -1,
) -> PycArtifact:
    """
    Compiles the module `import_path` (whose source file is `source`) into
    `dest_root`, at the distribution-relative location it must sit at to stay
    importable under that same name.
    """
    dest_rel_path = module_dest_rel_path(import_path, is_package=is_package)
    compile_to_pyc(source, dest_root / dest_rel_path, optimize=optimize)
    _logger.debug("Compiled %s to bytecode at %s", import_path, dest_rel_path)
    return PycArtifact(
        import_path=import_path,
        src_path=source,
        dest_rel_path=dest_rel_path,
    )


#: Directory name Python writes its own bytecode cache into. Never worth walking into
#: when pyc-ifying a tree: those files carry the cache-layout name
#: (`mod.cpython-312.pyc`), which is only ever discovered *through* the `.py` a
#: sourceless tree does not keep.
CACHE_DIR_NAME: Final[str] = "__pycache__"


@dataclass(frozen=True)
class TreeCompilation:
    """
    The outcome of pyc-ifying a whole directory tree.

    Both halves matter. `compiled` maps each source's tree-relative path to the
    tree-relative path of the `.pyc` written for it, which is what a caller deleting
    the sources needs in order to only delete what it actually replaced. `failed` maps
    the ones that did not compile to why -- a real standard library carries files that
    deliberately do not compile (invalid-syntax fixtures under `test/`), so a failure
    here is data to report, not a reason to abort.
    """

    root: PathExists
    dest_root: Path
    compiled: dict[Path, Path] = field(default_factory=dict)
    failed: dict[Path, str] = field(default_factory=dict)

    def render(self) -> str:
        return f"{len(self.compiled)} file(s) compiled to bytecode under {self.dest_root}" + (
            f", {len(self.failed)} failed" if self.failed else ""
        )


def compile_tree(
    root: PathExists,
    dest_root: Path,
    *,
    exclude: Iterable[Path] = (),
    optimize: int = -1,
) -> TreeCompilation:
    """
    Compiles every `.py` file under `root` to a `.pyc` at the same relative position
    under `dest_root`, and reports what compiled and what did not.

    `dest_root` may be `root` itself, which is how a tree is turned sourceless in
    place: the `.pyc` lands next to its own `.py`, and it is the caller's job to then
    delete the sources it no longer wants shipped (see
    `own_python.stage_interpreter`). Nothing is deleted here.

    `exclude` names tree-relative paths to skip, each matching itself and everything
    under it.

    Per-file failures are collected in the result rather than raised: this is used
    over a whole interpreter's standard library, which legitimately contains sources
    that do not compile (syntax-error fixtures, sources written for another Python
    version). A caller that needs one specific module to compile must check for it --
    `compile_to_pyc` is the raising variant for that case.

    `__pycache__` directories are never walked into (see `CACHE_DIR_NAME`).
    """
    excluded = {Path(entry) for entry in exclude}
    result = TreeCompilation(root=root, dest_root=dest_root)
    for source in sorted(root.rglob("*.py")):
        rel_path = source.relative_to(root)
        if CACHE_DIR_NAME in rel_path.parts:
            continue
        if any(parent in excluded for parent in (rel_path, *rel_path.parents)):
            continue
        dest_rel_path = rel_path.with_suffix(PYC_SUFFIX)
        try:
            compile_to_pyc(assert_path_exists(source), dest_root / dest_rel_path, optimize=optimize)
        except BytecodeCompilationError as exc:
            result.failed[rel_path] = str(exc)
            continue
        result.compiled[rel_path] = dest_rel_path
    _logger.debug("%s", result.render())
    return result
