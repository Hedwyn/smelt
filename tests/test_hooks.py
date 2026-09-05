from __future__ import annotations

import importlib.util

import pytest

from smelt.hooks import MODULE_HOOKS, ModuleHook, hidden_imports
from smelt.utils import assert_is_valid_import_path


def test_every_hook_states_what_goes_wrong_without_it() -> None:
    """
    The registry's one rule. A hook nobody can justify is a hook nobody can ever remove,
    and a bundler that collects those ends up shipping the standard library again one
    prudent addition at a time.
    """
    assert MODULE_HOOKS
    for hook in MODULE_HOOKS:
        assert hook.hidden_imports, hook.module
        assert "Verified" in hook.reason, hook.module


def test_a_hook_without_a_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="does not say what goes wrong"):
        ModuleHook(
            module=assert_is_valid_import_path("pkg"),
            hidden_imports=(assert_is_valid_import_path("pkg.hidden"),),
            reason="",
        )


def test_a_hook_that_adds_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="adds nothing"):
        ModuleHook(
            module=assert_is_valid_import_path("pkg"),
            hidden_imports=(),
            reason="Verified: nothing at all",
        )


@pytest.mark.parametrize("hook", MODULE_HOOKS, ids=lambda hook: hook.module)
def test_every_hooked_name_exists_in_this_interpreter(hook: ModuleHook) -> None:
    """
    A hook names standard library modules, so a typo -- or a stdlib reshuffle in a later
    version -- turns a curated fix into a module the distribution reports as missing and
    silently does without.
    """
    for name in (hook.module, *hook.hidden_imports):
        assert importlib.util.find_spec(name) is not None, name


def test_a_hook_fires_on_its_own_module_only() -> None:
    assert hidden_imports(frozenset({assert_is_valid_import_path("logging")})) == {
        "logging.config",
        "logging.handlers",
    }
    assert hidden_imports(frozenset({assert_is_valid_import_path("json")})) == set()


def test_hooks_are_applied_to_a_fixed_point() -> None:
    """
    A hidden import may have a hook of its own, and which entries fire must not depend
    on the order they happen to be written in.

    `concurrent.futures` pulls in `concurrent.futures.process`, which imports
    `multiprocessing` -- whose own hook is what makes a spawned child work.
    """
    added = hidden_imports(frozenset({assert_is_valid_import_path("concurrent.futures")}))
    assert {"concurrent.futures.process", "concurrent.futures.thread"} <= added


def test_a_name_already_present_is_not_reported_as_added() -> None:
    """
    The return value is what to add, so a caller can tell whether the registry had
    anything to say.
    """
    modules = frozenset(
        assert_is_valid_import_path(name)
        for name in ("logging", "logging.config", "logging.handlers")
    )
    assert hidden_imports(modules) == set()
