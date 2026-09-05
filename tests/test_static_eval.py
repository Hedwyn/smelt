from __future__ import annotations

import ast

import pytest

from smelt.static_eval import TargetEnvironment, condition_value, static_names

#: A fixed target, so the expectations below do not move with the interpreter running
#: the tests.
LINUX_312 = TargetEnvironment(
    platform="linux", os_name="posix", version_info=(3, 12, 12, "final", 0)
)


def decide(source: str) -> bool | None:
    """
    Whether the condition of the last `if` statement in `source` holds on `LINUX_312`.

    Takes a whole module rather than an expression, because the module is where the
    constants a guard refers to are declared (see `static_names`).
    """
    tree = ast.parse(source)
    guard = tree.body[-1]
    assert isinstance(guard, ast.If), "the last statement of the probe must be the guard"
    return condition_value(guard.test, static_names(tree, LINUX_312))


DECIDED_GUARDS = {
    'if sys.platform == "win32": pass': False,
    'if sys.platform == "linux": pass': True,
    'if sys.platform != "win32": pass': True,
    'if sys.platform.startswith("win"): pass': False,
    'if sys.platform.startswith("linux"): pass': True,
    'if sys.platform.lower().startswith("linux"): pass': True,
    'if sys.platform in ("win32", "cygwin"): pass': False,
    'if sys.platform in {"linux", "darwin"}: pass': True,
    'if sys.platform not in ("win32",): pass': True,
    'if sys.platform[:3] == "win": pass': False,
    'if os.name == "nt": pass': False,
    'if os.name == "posix": pass': True,
    "if sys.version_info >= (3, 13): pass": False,
    "if sys.version_info >= (3, 12): pass": True,
    "if sys.version_info[:2] == (3, 12): pass": True,
    "if sys.version_info[0] < 3: pass": False,
    "if sys.version_info.minor > 10: pass": True,
    'if not sys.platform == "win32": pass': True,
    'if os.name == "nt" and sys.platform == "win32": pass': False,
    'if os.name == "posix" and sys.platform == "linux": pass': True,
    'if os.name == "nt" or sys.platform == "win32": pass': False,
    'if os.name == "posix" or sys.platform == "win32": pass': True,
    "if False: pass": False,
    "if (): pass": False,
}


@pytest.mark.parametrize(("source", "expected"), DECIDED_GUARDS.items())
def test_guards_are_decided_against_the_target(source: str, expected: bool) -> None:
    assert decide(source) is expected


UNDECIDABLE_GUARDS = (
    "if verbose: pass",
    "if os.environ.get('SMELT'): pass",
    "if hasattr(os, 'fork'): pass",
    'if sys.platform == "linux" and verbose: pass',
    'if sys.platform == "win32" or verbose: pass',
    "if sys.maxsize > 2 ** 32: pass",
    'if sys.implementation.name == "cpython": pass',
    "if sys.version_info > threshold: pass",
)


@pytest.mark.parametrize("source", UNDECIDABLE_GUARDS)
def test_unknown_guards_stay_unknown(source: str) -> None:
    """
    Anything outside the closed list is undecidable, and the caller has to follow both
    branches. Over-collecting costs size; deciding wrongly drops a module the target
    needs.
    """
    assert decide(source) is None


def test_a_short_circuiting_operand_settles_an_otherwise_unknown_guard() -> None:
    """
    The shape `click._compat` guards its Windows console support with, and the reason
    `ctypes` used to end up in a Linux closure. `WIN` is unknown to a reader of the
    source, but `and` cannot recover from a false left-hand side.
    """
    assert decide('if sys.platform.startswith("win") and WIN: pass') is False


def test_module_level_constants_are_resolved() -> None:
    """
    Hardly any module guards on `sys.platform` directly: it is assigned to a name once
    and guarded on afterwards, which is what `subprocess` and `click._compat` do.
    """
    assert decide('WIN = sys.platform == "win32"\nif WIN: pass') is False
    assert decide('POSIX = os.name == "posix"\nif POSIX: pass') is True
    assert decide("NEW = sys.version_info >= (3, 13)\nif not NEW: pass") is True


REBOUND_CONSTANTS = {
    "assigned-twice": 'WIN = sys.platform == "win32"\nWIN = True\nif WIN: pass',
    "assigned-in-a-function": (
        'WIN = sys.platform == "win32"\ndef override() -> None:\n'
        "    global WIN\n    WIN = True\nif WIN: pass"
    ),
    "shadowed-by-an-import": 'WIN = sys.platform == "win32"\nimport WIN\nif WIN: pass',
    "assigned-conditionally": (
        'if os.sep == "/":\n    WIN = False\nelse:\n    WIN = True\nif WIN: pass'
    ),
}


@pytest.mark.parametrize(("case", "source"), REBOUND_CONSTANTS.items())
def test_a_name_bound_more_than_once_is_not_a_constant(case: str, source: str) -> None:
    """
    Which binding a guard sees would depend on control flow, so the name is treated as
    unknown -- wherever the second binding is, and whatever it holds.
    """
    assert decide(source) is None
