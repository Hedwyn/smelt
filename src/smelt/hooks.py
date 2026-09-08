"""
Curated per-module knowledge: what a module needs that no amount of reading its source
can tell you.

Every rule in `smelt.explorer` answers the question "what does importing this module
execute?", and answers it from the source. That leaves one class of dependency
permanently out of reach -- a module resolved from a name the source never spells out.
`logging.config` builds a handler from the dotted string `"logging.handlers.
RotatingFileHandler"` found in someone's config file; `xml.sax.make_parser()` imports
whatever `default_parser_list` names; `multiprocessing` starts a child that imports
`spawn` in a fresh interpreter. No import statement points at any of them, so no import
graph contains them, and the distribution is short exactly what it needs at the moment
it needs it.

The same idea as PyInstaller's `hiddenimports`, and the same idea as
`own_python.MINIMAL_VIABLE_STDLIB` and `own_python.ATOMIC_PACKAGES` -- applied to the
closure rather than to the interpreter. The three are deliberately separate: this one
adds modules to what the *application* was found to need, so it also drives what a
tailored interpreter keeps, which library toggles are safe, and what mode `byo` ships.

Every entry here is a bug that was reproduced first. The registry is not a list of
things that seemed prudent: each one is a distribution that was built, run, and seen to
fail with the recorded error.

@date: 05.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from smelt.utils import ImportPath, assert_is_valid_import_path


@dataclass(frozen=True)
class ModuleHook:
    """
    One entry of the registry: when `module` is in the closure, `hidden_imports` join
    it.

    `reason` is required, and required to name the observed failure rather than a
    worry. A registry of hunches is how a bundler ends up shipping half the standard
    library again, one prudent addition at a time -- and there is no way to tell, later,
    which entries were ever needed. `MINIMAL_VIABLE_STDLIB` and `ATOMIC_PACKAGES` hold
    themselves to the same rule.
    """

    module: ImportPath
    hidden_imports: tuple[ImportPath, ...]
    reason: str

    def __post_init__(self) -> None:
        if not self.hidden_imports:
            raise ValueError(f"Hook for {self.module!r} adds nothing.")
        if not self.reason:
            raise ValueError(
                f"Hook for {self.module!r} does not say what goes wrong without it. A "
                "hook whose reason cannot be stated is a guess that nobody can ever "
                "safely remove."
            )


def _hook(module: str, *hidden: str, reason: str) -> ModuleHook:
    """
    Spelling helper, so the registry below reads as data rather than as constructor
    calls.
    """
    return ModuleHook(
        module=assert_is_valid_import_path(module),
        hidden_imports=tuple(assert_is_valid_import_path(name) for name in hidden),
        reason=reason,
    )


#: What each module needs that its source does not say it needs.
#:
#: Two shapes recur, and both are visible below:
#:
#: * a module picks an implementation by name at call time. `xml.etree.ElementTree`,
#:   `xml.dom.minidom` and `xml.sax` all end up at the expat parser this way, and all
#:   three fail on the first parse rather than on import -- which is what makes the gap
#:   easy to ship and hard to notice;
#: * a package is shipped whole by `own_python.ATOMIC_PACKAGES` because its interior
#:   cannot be described by a closure, but the closure never walked that interior, so
#:   what the unwalked half imports from *outside* the package was never collected.
#:   `logging.handlers` and `concurrent.futures.process` both import `queue`, and
#:   neither was reachable from anything the application wrote.
MODULE_HOOKS: Final[tuple[ModuleHook, ...]] = (
    _hook(
        "xml.etree.ElementTree",
        "xml.parsers.expat",
        reason=(
            "`XMLParser.__init__` does `from xml.parsers import expat` in its own body, "
            "so nothing at module level names it and the closure stops before `pyexpat`. "
            "Verified: `ET.fromstring('<a/>')` in a tailored interpreter raises "
            "`ImportError: No module named expat; use SimpleXMLTreeBuilder instead`"
        ),
    ),
    _hook(
        "xml.dom.minidom",
        "xml.parsers.expat",
        reason=(
            "same expat, reached through `xml.dom.expatbuilder` at parse time. Verified: "
            "`minidom.parseString('<a/>')` raises `ModuleNotFoundError: No module named "
            "'pyexpat'`"
        ),
    ),
    _hook(
        "xml.sax",
        "xml.sax.expatreader",
        reason=(
            "`make_parser()` imports each name in `default_parser_list` -- a list of "
            "strings -- and the only entry is this one. Verified: `xml.sax.expatreader` "
            "is unimportable in a tailored interpreter (`SAXReaderNotAvailable: expat "
            "not supported`)"
        ),
    ),
    _hook(
        "importlib",
        "importlib._abc",
        "importlib.machinery",
        "importlib.readers",
        "importlib.util",
        reason=(
            "four of `importlib`'s own modules -- `_bootstrap`, `_bootstrap_external`, "
            "`machinery` and `util` -- are *frozen* into the interpreter, so they resolve "
            "with no source at all and a dependency walk stops dead at them. Their "
            "imports are therefore invisible rather than merely deferred: `util` does "
            "`from ._abc import Loader` at module level, and `_bootstrap_external` picks "
            "up `importlib.readers` inside `get_resource_reader`. The one blind spot no "
            "rule can cover, so it is named. Verified: `import zipfile` in a tailored "
            "interpreter raises `ModuleNotFoundError: No module named 'importlib._abc'` "
            "without this"
        ),
    ),
    _hook(
        "hashlib",
        "_md5",
        "_sha1",
        "_sha2",
        "_sha3",
        "_blake2",
        reason=(
            "`hashlib`'s implementations are all optional imports *and alternatives to "
            "each other*: `__get_builtin_constructor` tries `_md5`, `_sha1`, `_sha2`, "
            "`_sha3` and `_blake2` in turn, each in its own `try/except ImportError`, "
            "and `_hashlib` is a sixth. The optional-import rule judges each one "
            "separately and so is willing to drop every one of them, which leaves "
            "`hashlib` unable to produce any hash at all -- and `random` does "
            "`from hashlib import sha512` at module level. Verified: with "
            "`--drop-optional-imports` and without this, `import random` fails with "
            "`ImportError: cannot import name 'sha512' from 'hashlib'`, which is most "
            "of the standard library gone. `_hashlib` is deliberately *not* here: it is "
            "5 MB of OpenSSL and the builtins cover the same hashes"
        ),
    ),
    _hook(
        "logging",
        "logging.config",
        "logging.handlers",
        reason=(
            "`logging.config.dictConfig` builds handlers from dotted strings in a config "
            "file, so no source names `logging.handlers`. `own_python.ATOMIC_PACKAGES` "
            "ships the package whole for that reason, but the closure never walked these "
            "two and so never collected the `queue` they import. Verified: "
            "`import logging.handlers` in a tailored interpreter raises "
            "`ModuleNotFoundError: No module named 'queue'`"
        ),
    ),
    _hook(
        "multiprocessing",
        "multiprocessing.resource_tracker",
        "multiprocessing.spawn",
        "multiprocessing.heap",
        "multiprocessing.sharedctypes",
        reason=(
            "a spawned child re-imports `spawn` and `resource_tracker` by name in a "
            "fresh interpreter, and `context` picks a `popen_*` module by start-method "
            "name -- none of which any source spells out. `ATOMIC_PACKAGES` ships the "
            "package whole for that reason, so what its interior imports from *outside* "
            "has to be collected too. Verified: `resource_tracker` needs `_posixshmem`, "
            "`heap` needs `mmap` and `sharedctypes` needs `ctypes`, and all three raise "
            "`ModuleNotFoundError` in a tailored interpreter without this"
        ),
    ),
    _hook(
        "concurrent.futures",
        "concurrent.futures.process",
        "concurrent.futures.thread",
        reason=(
            "a module-level `__getattr__` imports these on the first use of "
            "`ProcessPoolExecutor`/`ThreadPoolExecutor`. Both import `queue`, which "
            "nothing else in the closure does. Verified: importing either in a tailored "
            "interpreter raises `ModuleNotFoundError: No module named 'queue'`"
        ),
    ),
)


_BY_MODULE: Final[dict[ImportPath, ModuleHook]] = {hook.module: hook for hook in MODULE_HOOKS}


def hidden_imports(modules: frozenset[ImportPath]) -> set[ImportPath]:
    """
    Everything the registry adds for `modules`, to a fixed point: a hidden import may
    itself have a hook, and stopping after one round would make the registry's behaviour
    depend on the order entries happen to be written in.

    Returns only the *new* names, so a caller can tell whether anything was added.
    """
    added: set[ImportPath] = set()
    frontier = set(modules)
    while frontier:
        pending: set[ImportPath] = set()
        for module in frontier:
            hook = _BY_MODULE.get(module)
            if hook is None:
                continue
            pending.update(hook.hidden_imports)
        frontier = pending - modules - added
        added.update(frontier)
    return added
