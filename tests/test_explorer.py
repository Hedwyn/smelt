from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from smelt.explorer import (
    build_dependency_graph,
    flatten_dependency_graph,
    optional_modules,
)
from smelt.static_eval import TargetEnvironment
from smelt.utils import ImportPath, assert_is_valid_import_path


@pytest.fixture
def module_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    An importable directory to write probe modules into.

    The dependency walk resolves names through the import machinery, so a probe has to
    be reachable from `sys.path` -- and the cached directory listings have to be
    dropped, or a module written after the first import of `tmp_path` is invisible.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return tmp_path


def write_module(
    root: Path,
    name: str,
    source: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    stdlib: bool = False,
) -> ImportPath:
    """
    Writes a probe module and returns its import path, optionally passing it off as a
    standard library module.

    `stdlib` is what the deferred-import rule keys on, and `sys.stdlib_module_names` is
    where the explorer reads it from -- so adding the probe's name to that set is
    exactly the difference between the two treatments, without needing a real standard
    library module that happens to have the shape under test.
    """
    (root / f"{name}.py").write_text(source)
    if stdlib:
        monkeypatch.setattr(sys, "stdlib_module_names", sys.stdlib_module_names | {name})
    importlib.invalidate_caches()
    return assert_is_valid_import_path(name)


def closure_of(entrypoint: ImportPath) -> set[str]:
    """
    The top-level names reachable from `entrypoint`, itself included.
    """
    graph = build_dependency_graph(entrypoint)
    return {node.name.partition(".")[0] for node in flatten_dependency_graph(graph)}


MAIN_GUARD_SPELLINGS = {
    "canonical": 'if __name__ == "__main__":',
    "reversed-operands": 'if "__main__" == __name__:',
}


@pytest.mark.parametrize(("case", "guard"), MAIN_GUARD_SPELLINGS.items())
def test_main_guard_imports_are_not_followed(
    case: str, guard: str, module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A module reached by an import never has `__name__ == "__main__"`, so nothing in
    that block can run -- which is how `heapq` would otherwise reach `doctest`.
    """
    entrypoint = write_module(
        module_root,
        f"probe_main_guard_{case.replace('-', '_')}",
        f"import base64\n{guard}\n    import json\n",
        monkeypatch=monkeypatch,
    )
    reachable = closure_of(entrypoint)
    assert "base64" in reachable
    assert "json" not in reachable


def test_main_guard_else_branch_is_followed(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The `else:` of a main guard is the branch an import *does* take.
    """
    entrypoint = write_module(
        module_root,
        "probe_main_guard_else",
        'if __name__ == "__main__":\n    import json\nelse:\n    import base64\n',
        monkeypatch=monkeypatch,
    )
    reachable = closure_of(entrypoint)
    assert "base64" in reachable
    assert "json" not in reachable


def test_inverted_main_guard_is_left_alone(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `!=` swaps which branch runs on import, so it is not treated as a guard at all:
    following both branches over-collects, which costs size rather than correctness.
    """
    entrypoint = write_module(
        module_root,
        "probe_inverted_main_guard",
        'if __name__ != "__main__":\n    import base64\nelse:\n    import json\n',
        monkeypatch=monkeypatch,
    )
    assert "base64" in closure_of(entrypoint)


def test_function_body_imports_are_followed_in_application_code(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Outside the standard library a lazy import is usually load-bearing -- deferred to
    break a cycle or to keep start-up cheap -- so dropping it would ship an application
    that fails on the first call.
    """
    entrypoint = write_module(
        module_root,
        "probe_deferred_app",
        "def load() -> object:\n    import json\n\n    return json\n",
        monkeypatch=monkeypatch,
    )
    assert "json" in closure_of(entrypoint)


def test_function_body_imports_are_not_followed_in_the_stdlib(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The same import, in a standard library module, is assumed to be a self-test, CLI or
    diagnostic path a shipped application never enters.
    """
    entrypoint = write_module(
        module_root,
        "probe_deferred_stdlib",
        "import base64\n\n\ndef _test() -> object:\n    import json\n\n    return json\n",
        monkeypatch=monkeypatch,
        stdlib=True,
    )
    reachable = closure_of(entrypoint)
    assert "base64" in reachable
    assert "json" not in reachable


def test_class_body_imports_are_followed_in_the_stdlib(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A class body executes on import, exactly like module level -- only function bodies
    are deferred.
    """
    entrypoint = write_module(
        module_root,
        "probe_class_body_stdlib",
        "class Holder:\n    import json\n",
        monkeypatch=monkeypatch,
        stdlib=True,
    )
    assert "json" in closure_of(entrypoint)


def test_stdlib_self_test_paths_do_not_reach_the_test_frameworks() -> None:
    """
    The measurement this rule exists for, on the real standard library: `difflib._test`
    is a single deferred statement that otherwise drags in
    `doctest -> unittest -> asyncio -> multiprocessing`, while `difflib`'s actual
    module-level imports are unaffected.
    """
    reachable = closure_of(assert_is_valid_import_path("difflib"))
    assert "heapq" in reachable
    assert not reachable.intersection({"doctest", "unittest", "asyncio", "multiprocessing"})


def test_platform_guarded_imports_are_not_followed(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The branch of a platform guard the target does not take. `msvcrt` is the case that
    matters: on Linux it cannot even be imported, so following the guard produces a
    module the distribution can only report as missing.
    """
    entrypoint = write_module(
        module_root,
        "probe_platform_guard",
        'import sys\n\nif sys.platform == "win32":\n    import msvcrt\nelse:\n    import base64\n',
        monkeypatch=monkeypatch,
    )
    reachable = closure_of(entrypoint)
    assert "base64" in reachable
    assert "msvcrt" not in reachable


def test_only_the_taken_arm_of_an_elif_chain_is_followed(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = write_module(
        module_root,
        "probe_elif_chain",
        "import sys\n\n"
        'if sys.platform == "win32":\n    import msvcrt\n'
        'elif sys.platform == "darwin":\n    import plistlib\n'
        "else:\n    import base64\n",
        monkeypatch=monkeypatch,
    )
    reachable = closure_of(entrypoint)
    assert "base64" in reachable
    assert not reachable.intersection({"msvcrt", "plistlib"})


def test_an_undecidable_guard_keeps_both_branches(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The safe direction: over-collecting costs size, while deciding a guard wrongly ships
    a distribution missing a module the target needs.
    """
    entrypoint = write_module(
        module_root,
        "probe_unknown_guard",
        "import os\n\nif os.environ.get('PROBE'):\n    import json\nelse:\n    import base64\n",
        monkeypatch=monkeypatch,
    )
    assert closure_of(entrypoint).issuperset({"json", "base64"})


def test_a_target_can_be_something_other_than_the_host(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The same source, decided for Windows: the guard that drops `msvcrt` on this machine
    is the one that keeps it, and drops the POSIX branch instead.
    """
    entrypoint = write_module(
        module_root,
        "probe_windows_target",
        'import sys\n\nif sys.platform == "win32":\n    import json\nelse:\n    import base64\n',
        monkeypatch=monkeypatch,
    )
    windows = TargetEnvironment(
        platform="win32", os_name="nt", version_info=(3, 12, 12, "final", 0)
    )
    graph = build_dependency_graph(entrypoint, windows)
    reachable = {node.name.partition(".")[0] for node in flatten_dependency_graph(graph)}
    assert "json" in reachable
    assert "base64" not in reachable


OPTIONAL_IMPORT_HANDLERS = {
    "ImportError": "except ImportError:\n    json = None\n",
    "ModuleNotFoundError": "except ModuleNotFoundError:\n    json = None\n",
    "in-a-tuple": "except (ImportError, AttributeError):\n    json = None\n",
    "bare": "except:\n    json = None\n",
}


@pytest.mark.parametrize(("case", "handler"), OPTIONAL_IMPORT_HANDLERS.items())
def test_an_optional_import_is_followed_by_default_and_droppable_on_request(
    case: str, handler: str, module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A module that handles the failure of its own import says, in its own source, that
    it works without it. Both answers are legitimate, which is why the caller chooses:
    the fallback behind one is sometimes invisible and sometimes a lost feature.
    """
    entrypoint = write_module(
        module_root,
        f"probe_optional_{case.replace('-', '_')}",
        f"try:\n    import json\n{handler}",
        monkeypatch=monkeypatch,
    )
    assert "json" in closure_of(entrypoint)
    required = flatten_dependency_graph(build_dependency_graph(entrypoint, follow_optional=False))
    assert "json" not in {node.name for node in required}
    assert "json" in optional_modules(entrypoint)


def test_an_import_whose_handler_re_raises_is_mandatory(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A handler that re-raises is making the opposite promise: the module does not work
    without what it just tried to import, however the statement is spelled.
    """
    entrypoint = write_module(
        module_root,
        "probe_reraising_handler",
        'try:\n    import json\nexcept ImportError:\n    raise RuntimeError("json is required")\n',
        monkeypatch=monkeypatch,
    )
    assert "json" not in optional_modules(entrypoint)


def test_the_handler_of_an_optional_import_is_followed_instead(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    What runs when the optional import fails is the handler, so its own imports are
    required rather than optional -- that is where a module reaches for its fallback.
    """
    entrypoint = write_module(
        module_root,
        "probe_optional_fallback",
        "try:\n    import json\nexcept ImportError:\n    import base64\n",
        monkeypatch=monkeypatch,
    )
    assert closure_of(entrypoint).issuperset({"json", "base64"})
    optional = optional_modules(entrypoint)
    assert "json" in optional
    assert "base64" not in optional


def test_the_else_of_an_optional_import_goes_with_its_body(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An `else:` block runs only when the `try:` succeeded, so it cannot survive the
    import it is conditioned on.
    """
    entrypoint = write_module(
        module_root,
        "probe_optional_else",
        "try:\n    import json\nexcept ImportError:\n    pass\nelse:\n    import base64\n",
        monkeypatch=monkeypatch,
    )
    assert optional_modules(entrypoint).issuperset({"json", "base64"})


def test_a_module_reached_both_ways_is_not_optional(
    module_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Optionality is a property of every path to a module, not of one import statement.
    """
    entrypoint = write_module(
        module_root,
        "probe_optional_and_required",
        "import json\n\ntry:\n    import json\nexcept ImportError:\n    pass\n",
        monkeypatch=monkeypatch,
    )
    assert "json" not in optional_modules(entrypoint)


def test_optional_stdlib_accelerators_are_reported_for_the_real_stdlib() -> None:
    """
    The measurement this rule exists for: `hashlib` imports `_hashlib` optimistically
    and falls back to the builtin hash implementations, and `_hashlib` is 5.1 MB of
    statically linked OpenSSL in a shipped interpreter.
    """
    assert "_hashlib" in optional_modules(assert_is_valid_import_path("hashlib"))
