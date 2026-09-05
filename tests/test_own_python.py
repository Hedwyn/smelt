"""
Tests for mode `own`: the interpreter smelt builds itself and stages inside a
distribution folder.

Two tiers, and the split is deliberate. Everything above `_CACHE_AVAILABLE` runs
anywhere in seconds, against a *fake* interpreter prefix -- a shell script standing in
for `bin/python` and a handful of `.py` files standing in for a standard library --
because what those tests check (what gets pruned, that the stdlib ends up sourceless,
that the landmark is asserted, that the skew guard and the launcher behave) is
file-shuffling logic that a real 63 MB CPython tree would only make slower to
exercise.

The tests below it are the ones making the actual claim -- a folder that runs where no
Python is installed -- and they need a genuinely built interpreter, which takes minutes
to produce. They are skipped when one is not cached, the same way
`test_native_deps.py` skips what needs `patchelf`.

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from smelt.bytecode import PycTargetTag, compile_tree
from smelt.config import EntrypointOptions, SmeltConfig
from smelt.dist import (
    DEFAULT_TAILOR_INTERPRETER,
    INSTRUCTIONS_NAME,
    LAUNCHER_RESERVED_NAMES,
    MANIFEST_NAME,
    PAYLOAD_DIR_NAME,
    DistError,
    DistReport,
    assert_no_version_skew,
    build_dist,
    launcher_name,
    resolve_dist_python,
    resolve_tailor_interpreter,
    run_instructions,
    write_launcher_shim,
)
from smelt.frontend import parse_config_from_pyproject
from smelt.native_deps import is_supported_platform
from smelt.own_python import (
    ALWAYS_KEEP,
    ATOMIC_PACKAGES,
    DEFAULT_STDLIB_PRUNE,
    INTERPRETER_HOST_DLL_PREFIXES,
    INTERPRETER_REL_PATH,
    LIBRARY_MODULES,
    MINIMAL_VIABLE_STDLIB,
    OPTIONAL_STDLIB_GROUPS,
    OwnPythonError,
    StagedInterpreter,
    bootstrap_modules,
    interpreter_version,
    minimal_viable_stdlib,
    own_python_cache_dir,
    plan_disabled_libraries,
    resolve_requirements,
    stage_interpreter,
)
from smelt.utils import assert_is_valid_import_path, assert_path_exists

HATCHDEMO_ROOT = Path(__file__).parent.parent / "examples" / "hatchdemo"

#: A real interpreter build takes minutes, so the functional tests reuse whatever
#: `build_own_python` has already cached rather than triggering one.
_CACHED_PREFIX = own_python_cache_dir()
_CACHE_AVAILABLE = (_CACHED_PREFIX / INTERPRETER_REL_PATH).exists()

needs_own_python = pytest.mark.skipif(
    not _CACHE_AVAILABLE or not is_supported_platform(),
    reason=f"needs Linux and an interpreter already built at {_CACHED_PREFIX}",
)


def _fake_interpreter_prefix(root: Path) -> Path:
    """
    An interpreter prefix in the shape `stage_interpreter` walks, cheap enough to
    build in every test: a `bin/python` answering the three questions the staging asks
    an interpreter about itself, and a standard library made of a few files chosen to
    cover each case the staging has to handle.

    `test/badsyntax.py` is not padding: CPython's own standard library ships
    deliberately-invalid-syntax fixtures under `test/`, and they were the only
    bytecode-compilation failures in the whole tree -- so the tree used here has to be
    able to reproduce both the failure and the fact that pruning `test` removes it.
    """
    prefix = root / "built"
    executable = prefix / INTERPRETER_REL_PATH
    executable.parent.mkdir(parents=True)
    # Dispatching on the probe rather than answering one question, so the tailoring
    # decision (which asks for the bootstrap set and for the builtin/frozen modules)
    # is exercisable without a real 63 MB interpreter. The markers are the ones each
    # probe's own source carries, see `own_python._probe_interpreter`'s callers.
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "    *version_info*) echo '3 12' ;;\n"
        "    *sys.modules*) echo 'builtins marshal sys zipimport' ;;\n"
        '    *FrozenImporter*) echo \'["builtins", "marshal", "sys", "zipimport"]\' ;;\n'
        "    *) echo 'unexpected probe' >&2; exit 1 ;;\n"
        "esac\n"
    )
    executable.chmod(0o755)

    stdlib = prefix / "lib" / "python3.12"
    stdlib.mkdir(parents=True)
    (prefix / "lib" / "libpython3.12.so").write_bytes(b"")
    (stdlib / "os.py").write_text("sep = '/'\n")
    (stdlib / "json").mkdir()
    (stdlib / "json" / "__init__.py").write_text("VERSION = 1\n")
    (stdlib / "json" / "__pycache__").mkdir()
    (stdlib / "json" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"stale")
    (stdlib / "tkinter").mkdir()
    (stdlib / "tkinter" / "__init__.py").write_text("TK = 1\n")
    (stdlib / "test").mkdir()
    (stdlib / "test" / "badsyntax.py").write_text("def (:\n")
    (stdlib / "config-3.12-x86_64-linux-gnu").mkdir()
    (stdlib / "config-3.12-x86_64-linux-gnu" / "Makefile").write_text("all:\n")
    dynload = stdlib / "lib-dynload"
    dynload.mkdir()
    (dynload / "_json.cpython-312-x86_64-linux-gnu.so").write_bytes(b"")
    (dynload / "_tkinter.cpython-312-x86_64-linux-gnu.so").write_bytes(b"")
    return prefix


def test_interpreter_version_asks_the_interpreter(tmp_path: Path) -> None:
    prefix = _fake_interpreter_prefix(tmp_path)
    assert interpreter_version(assert_path_exists(prefix)) == (3, 12)


def test_interpreter_version_refuses_a_prefix_without_one(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    with pytest.raises(OwnPythonError):
        interpreter_version(assert_path_exists(tmp_path))


def test_stage_interpreter_prunes_and_goes_sourceless(tmp_path: Path) -> None:
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()

    # `bundle_dependencies=False`: the fake tree's `.so` files are empty, so there is
    # nothing for `ldd` to walk. What bundling does is covered functionally below.
    staged = stage_interpreter(assert_path_exists(prefix), dist_root, bundle_dependencies=False)

    stdlib = dist_root / "lib" / "python3.12"
    assert (dist_root / INTERPRETER_REL_PATH).is_file()
    assert (dist_root / "lib" / "libpython3.12.so").is_file()
    # what it was told to prune is gone, whether a directory or a name pattern
    assert not (stdlib / "test").exists()
    assert not (stdlib / "tkinter").exists()
    assert not (stdlib / "config-3.12-x86_64-linux-gnu").exists()
    assert not list(stdlib.rglob("__pycache__"))
    assert not (stdlib / "lib-dynload" / "_tkinter.cpython-312-x86_64-linux-gnu.so").exists()
    # ... and only what it was told to prune
    assert (stdlib / "json" / "__init__.pyc").is_file()
    # the rest of lib-dynload is copied wholesale, whether or not anything needs it
    assert (stdlib / "lib-dynload" / "_json.cpython-312-x86_64-linux-gnu.so").is_file()
    # sourceless: every module that compiled is shipped as bytecode and only bytecode
    assert not list(stdlib.rglob("*.py"))
    assert (stdlib / "os.pyc").is_file()

    assert staged.version == (3, 12)
    assert staged.sourceless
    # the *patterns* that matched, not the hundreds of paths they matched
    assert set(staged.pruned) <= set(DEFAULT_STDLIB_PRUNE)
    assert {"test", "tkinter", "__pycache__", "config-*", "_tkinter*"} <= set(staged.pruned)
    # the prefix is the distribution root itself: that is what makes CPython's
    # executable-relative prefix detection find the stdlib staged here
    assert staged.prefix_rel_path == Path(".")
    assert staged.executable_rel_path == INTERPRETER_REL_PATH
    assert staged.size_bytes > 0
    # after pruning `test`, nothing in the tree fails to compile
    assert staged.compile_failures == {}


def test_stage_interpreter_keeps_the_source_of_what_did_not_compile(tmp_path: Path) -> None:
    """
    A module that failed to compile must keep its `.py`: deleting it would turn a
    recorded warning into a missing module.
    """
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()

    staged = stage_interpreter(
        assert_path_exists(prefix),
        dist_root,
        # `test` deliberately left in, so its invalid-syntax fixture is reached
        prune=("__pycache__",),
        bundle_dependencies=False,
    )

    stdlib = dist_root / "lib" / "python3.12"
    assert list(staged.compile_failures) == [Path("test/badsyntax.py")]
    assert (stdlib / "test" / "badsyntax.py").is_file()
    assert not (stdlib / "test" / "badsyntax.pyc").exists()
    # everything that *did* compile is still sourceless
    assert not (stdlib / "os.py").exists()
    assert (stdlib / "os.pyc").is_file()


def test_stage_interpreter_refuses_to_ship_without_the_stdlib_landmark(tmp_path: Path) -> None:
    """
    Regression guard for the landmark: `lib/pythonX.Y/os.pyc` is what CPython's prefix
    detection looks for, and `-I` (which implies `-E`) means `PYTHONHOME` cannot stand
    in for it. Losing it has to fail here, not on the target machine.
    """
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()

    with pytest.raises(OwnPythonError, match="os.pyc"):
        stage_interpreter(
            assert_path_exists(prefix),
            dist_root,
            prune=(*DEFAULT_STDLIB_PRUNE, "os.py"),
            bundle_dependencies=False,
        )


def test_stage_interpreter_keeps_the_source_landmark_when_not_sourceless(tmp_path: Path) -> None:
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()

    staged = stage_interpreter(
        assert_path_exists(prefix), dist_root, sourceless=False, bundle_dependencies=False
    )

    stdlib = dist_root / "lib" / "python3.12"
    assert not staged.sourceless
    assert (stdlib / "os.py").is_file()
    assert not (stdlib / "os.pyc").exists()


def test_bootstrap_modules_reports_what_the_interpreter_loaded_itself(tmp_path: Path) -> None:
    """
    `__main__` is the probe, not a module the interpreter needs, so it must not come
    back as one.
    """
    prefix = _fake_interpreter_prefix(tmp_path)

    assert bootstrap_modules(assert_path_exists(prefix)) == frozenset(
        {"builtins", "marshal", "sys", "zipimport"}
    )


def test_plan_disabled_libraries_turns_off_only_what_nothing_needs() -> None:
    # `_ssl` names the extension module, `sqlite3` only the pure-Python wrapper: both
    # have to keep their library, or the module they name cannot import.
    disabled = plan_disabled_libraries(["_ssl", "sqlite3", "json"])

    assert "openssl" not in disabled
    assert "sqlite" not in disabled
    assert disabled == frozenset(set(LIBRARY_MODULES) - {"openssl", "sqlite"})


def test_plan_disabled_libraries_honors_forced_modules() -> None:
    assert "lzma" in plan_disabled_libraries(["json"])
    assert "lzma" not in plan_disabled_libraries(["json"], include_modules=["lzma"])


def test_plan_disabled_libraries_never_touches_a_library_always_keep_needs() -> None:
    """
    `ALWAYS_KEEP` is unioned in before the decision, so a safety-net module cannot end
    up in an interpreter built without what it needs.
    """
    disabled = plan_disabled_libraries([])
    for library in disabled:
        assert not set(LIBRARY_MODULES[library]).intersection(ALWAYS_KEEP)


def test_resolve_requirements_widens_the_closure(tmp_path: Path) -> None:
    prefix = assert_path_exists(_fake_interpreter_prefix(tmp_path))

    requirements = resolve_requirements(["email.parser"], prefix, include_modules=["zoneinfo"])

    # the closure's own names, and every package they live under
    assert {"email.parser", "email"} <= requirements.keep_modules
    # the safety net
    assert set(ALWAYS_KEEP) <= requirements.keep_modules
    # what the interpreter said it loads by itself
    assert {"builtins", "zipimport"} <= requirements.keep_modules
    # and what the caller insisted on, recorded as such
    assert "zoneinfo" in requirements.keep_modules
    assert requirements.forced == frozenset({"zoneinfo"})
    # pruning happens at top-level granularity
    assert requirements.needs("email")
    assert not requirements.needs("sqlite3")


def test_stage_interpreter_keeps_everything_without_requirements(tmp_path: Path) -> None:
    """
    Step 1's behaviour has to stay reachable: no requirements, no closure-driven
    pruning, and `lib-dynload` copied wholesale.
    """
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()

    staged = stage_interpreter(assert_path_exists(prefix), dist_root, bundle_dependencies=False)

    stdlib = dist_root / "lib" / "python3.12"
    assert not staged.tailored
    assert staged.pruned_modules == []
    assert staged.pruned_extensions == []
    assert staged.disabled_libraries == []
    assert (stdlib / "json" / "__init__.pyc").is_file()
    assert (stdlib / "lib-dynload" / "_json.cpython-312-x86_64-linux-gnu.so").is_file()


def test_stage_interpreter_prunes_what_the_closure_did_not_reach(tmp_path: Path) -> None:
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    # An empty closure: nothing but the safety net and the bootstrap set survives.
    requirements = resolve_requirements([], assert_path_exists(prefix))

    staged = stage_interpreter(
        assert_path_exists(prefix),
        dist_root,
        bundle_dependencies=False,
        requirements=requirements,
    )

    stdlib = dist_root / "lib" / "python3.12"
    assert staged.tailored
    assert staged.pruned_modules == ["json"]
    assert staged.pruned_extensions == ["_json"]
    assert sorted(staged.disabled_libraries) == sorted(LIBRARY_MODULES)
    assert not (stdlib / "json").exists()
    assert not (stdlib / "lib-dynload" / "_json.cpython-312-x86_64-linux-gnu.so").exists()
    # `os` is in ALWAYS_KEEP, which is also what stops the landmark being prunable
    assert (stdlib / "os.pyc").is_file()
    assert staged.size_before_prune_bytes >= staged.size_bytes


def test_stage_interpreter_keeps_what_the_closure_reached(tmp_path: Path) -> None:
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    requirements = resolve_requirements(["json", "_json"], assert_path_exists(prefix))

    staged = stage_interpreter(
        assert_path_exists(prefix),
        dist_root,
        bundle_dependencies=False,
        requirements=requirements,
    )

    stdlib = dist_root / "lib" / "python3.12"
    assert staged.pruned_modules == []
    assert staged.pruned_extensions == []
    assert (stdlib / "json" / "__init__.pyc").is_file()
    assert (stdlib / "lib-dynload" / "_json.cpython-312-x86_64-linux-gnu.so").is_file()


def _with_nested_packages(prefix: Path) -> Path:
    """
    Adds submodules to a fake prefix's packages, so that pruning *inside* a package has
    something to work on.

    The names mirror the host's real `json` and `logging`, because that is what
    `_widen_kept_packages` reads: the decision about a package's interior is made from
    the package's own source on this machine, while the pruning happens in the staged
    tree.
    """
    stdlib = prefix / "lib" / "python3.12"
    for name in ("decoder", "encoder", "scanner", "tool"):
        (stdlib / "json" / f"{name}.py").write_text("VALUE = 1\n")
    package = stdlib / "logging"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n")
    for name in ("config", "handlers"):
        (package / f"{name}.py").write_text("VALUE = 1\n")
    return prefix


def test_stage_interpreter_prunes_inside_a_kept_package(tmp_path: Path) -> None:
    """
    A package the closure reached ships the submodules it can reach, not all of them.
    `json.tool` is `json`'s command line interface: nothing in the package imports it,
    so nothing that imports `json` can need it.
    """
    prefix = _with_nested_packages(_fake_interpreter_prefix(tmp_path))
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    requirements = resolve_requirements(["json"], assert_path_exists(prefix))

    staged = stage_interpreter(
        assert_path_exists(prefix),
        dist_root,
        bundle_dependencies=False,
        requirements=requirements,
    )

    stdlib = dist_root / "lib" / "python3.12"
    # `logging` goes whole, the closure not having reached it at all
    assert staged.pruned_modules == ["logging"]
    assert staged.pruned_submodules == ["json.tool"]
    assert not (stdlib / "json" / "tool.pyc").exists()
    # what `json/__init__.py` itself imports stays, `__init__` included
    assert (stdlib / "json" / "__init__.pyc").is_file()
    for name in ("decoder", "encoder", "scanner"):
        assert (stdlib / "json" / f"{name}.pyc").is_file()


def test_stage_interpreter_keeps_an_atomic_package_whole(tmp_path: Path) -> None:
    """
    `logging.config` resolves handler classes from dotted strings in a config file, so
    no import statement anywhere points at `logging.handlers` and pruning by closure
    would take it. `ATOMIC_PACKAGES` is what stops that.
    """
    prefix = _with_nested_packages(_fake_interpreter_prefix(tmp_path))
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    requirements = resolve_requirements(["logging"], assert_path_exists(prefix))

    staged = stage_interpreter(
        assert_path_exists(prefix),
        dist_root,
        bundle_dependencies=False,
        requirements=requirements,
    )

    stdlib = dist_root / "lib" / "python3.12"
    # nothing was cut from inside `logging`, though `json` went whole
    assert staged.pruned_modules == ["json"]
    assert staged.pruned_submodules == []
    for name in ("config", "handlers"):
        assert (stdlib / "logging" / f"{name}.pyc").is_file()


def test_a_deferred_import_inside_a_package_still_keeps_its_target(tmp_path: Path) -> None:
    """
    The one assumption pruning inside a package must not make. Dropping `asyncio`
    because only a function body imports it is the whole point of the closure rules;
    dropping `email.parser` for the same reason breaks `email.message_from_string()`,
    whose import of it is exactly such a function body.
    """
    prefix = _fake_interpreter_prefix(tmp_path)
    requirements = resolve_requirements(["email.message"], assert_path_exists(prefix))

    assert {"email", "email.message", "email.parser"} <= requirements.keep_modules
    # ... and the widening stays inside the package it was asked about
    assert not any(name.startswith("http") for name in requirements.keep_modules)


def test_every_atomic_package_says_why_it_is_atomic() -> None:
    """
    Same rule as `StdlibGroup`: an exception to pruning that cannot state its reason is
    a place for dead weight to accumulate unchallenged.
    """
    assert ATOMIC_PACKAGES
    for package, reason in ATOMIC_PACKAGES.items():
        assert package.replace(".", "").isidentifier(), package
        assert len(reason) > 20, package


def test_stage_interpreter_refuses_to_ship_without_a_module_it_needs(tmp_path: Path) -> None:
    """
    The safety net: a prune pattern matching a module the application needs has to
    fail the build, not produce a folder that dies on an import somewhere else.
    """
    prefix = _fake_interpreter_prefix(tmp_path)
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    requirements = resolve_requirements(["json"], assert_path_exists(prefix))

    with pytest.raises(OwnPythonError, match="json"):
        stage_interpreter(
            assert_path_exists(prefix),
            dist_root,
            bundle_dependencies=False,
            prune=(*DEFAULT_STDLIB_PRUNE, "json"),
            requirements=requirements,
        )


def test_own_python_cache_dir_keeps_the_vanilla_name() -> None:
    """
    An all-defaults build has to keep hitting the directory it has always used --
    renaming its key turns every untailored build into a full CPython rebuild.
    """
    assert own_python_cache_dir().name == "native"
    assert own_python_cache_dir("x86_64-linux-musl").name == "x86_64-linux-musl"
    assert own_python_cache_dir(debug=True).name == "native-debug"


def test_own_python_cache_dir_separates_library_option_sets() -> None:
    keyed = own_python_cache_dir(disabled_libraries=["tk", "sqlite"])

    assert keyed != own_python_cache_dir()
    # order-independent, so the same option set never builds twice
    assert keyed == own_python_cache_dir(disabled_libraries=["sqlite", "tk"])


def test_resolve_tailor_interpreter_prefers_the_caller_then_the_declaration() -> None:
    assert resolve_tailor_interpreter(EntrypointOptions()) is DEFAULT_TAILOR_INTERPRETER
    assert resolve_tailor_interpreter(EntrypointOptions({"tailor-interpreter": False})) is False
    assert (
        resolve_tailor_interpreter(EntrypointOptions({"tailor-interpreter": False}), True) is True
    )


def test_compile_tree_reports_both_halves(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "good.py").write_text("VALUE = 1\n")
    (root / "pkg" / "nested.py").write_text("VALUE = 2\n")
    (root / "pkg" / "broken.py").write_text("def (:\n")
    (root / "pkg" / "__pycache__" / "nested.cpython-312.pyc").write_bytes(b"stale")

    result = compile_tree(assert_path_exists(root), tmp_path / "out")

    assert result.compiled == {
        Path("good.py"): Path("good.pyc"),
        Path("pkg/nested.py"): Path("pkg/nested.pyc"),
    }
    assert list(result.failed) == [Path("pkg/broken.py")]
    assert (tmp_path / "out" / "pkg" / "nested.pyc").is_file()
    # the cache layout is never walked into: it is only ever found through the `.py`
    assert not (tmp_path / "out" / "pkg" / "__pycache__").exists()


def test_compile_tree_honors_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "keep.py").write_text("VALUE = 1\n")
    (root / "pkg" / "drop.py").write_text("VALUE = 2\n")

    result = compile_tree(assert_path_exists(root), tmp_path / "out", exclude=[Path("pkg")])

    assert list(result.compiled) == [Path("keep.py")]


def test_version_skew_guard_accepts_a_patch_difference() -> None:
    # 3.12.12-built artifacts under a 3.12.13 interpreter: proven to work, because the
    # bytecode magic is per minor version and the C ABI is stable within one
    assert_no_version_skew(PycTargetTag((3, 12), b"\xcb\r\r\n", 0), (3, 12))


def test_version_skew_guard_refuses_a_minor_difference() -> None:
    with pytest.raises(DistError, match="3.13"):
        assert_no_version_skew(PycTargetTag((3, 12), b"\xcb\r\r\n", 0), (3, 13))


def _a_staged_interpreter() -> StagedInterpreter:
    return StagedInterpreter(prefix_rel_path=Path("."), version=(3, 12))


def test_launcher_shim_is_generated_and_executable(tmp_path: Path) -> None:
    dist_root = tmp_path / "myapp.dist"
    (dist_root / PAYLOAD_DIR_NAME).mkdir(parents=True)

    launcher = write_launcher_shim("myapp", dist_root, _a_staged_interpreter())

    assert launcher == Path("myapp")
    script = dist_root / launcher
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert body.startswith("#!/bin/sh\n")
    assert f'exec "$here/bin/python" "$here/{PAYLOAD_DIR_NAME}" "$@"' in body
    # no interpreter flags: the generated __main__ adds -I -S -B by re-executing
    assert " -I " not in body
    # and a Windows sibling, so the folder is not silently POSIX-only
    windows = dist_root / "myapp.cmd"
    assert windows.is_file()
    assert PAYLOAD_DIR_NAME in windows.read_text()


def test_launcher_shim_finds_its_folder_without_a_path(tmp_path: Path) -> None:
    """
    The invocation mode B exists for: a machine with nothing installed. `readlink` and
    `dirname` are not callable there, so the shim must not need them.
    """
    dist_root = tmp_path / "myapp.dist"
    payload = dist_root / PAYLOAD_DIR_NAME
    payload.mkdir(parents=True)
    (dist_root / "bin").mkdir()
    # stands in for the interpreter: all the shim has to get right is the folder and
    # the arguments it hands over
    (dist_root / "bin" / "python").write_text('#!/bin/sh\necho "ran $*"\n')
    (dist_root / "bin" / "python").chmod(0o755)

    write_launcher_shim("myapp", dist_root, _a_staged_interpreter())

    completed = subprocess.run(
        ["./myapp", "--flag"],
        cwd=dist_root,
        env={"PATH": "/nonexistent"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == f"ran ./{PAYLOAD_DIR_NAME} --flag"


@pytest.mark.parametrize("name", LAUNCHER_RESERVED_NAMES)
def test_launcher_shim_refuses_a_reserved_name(tmp_path: Path, name: str) -> None:
    dist_root = tmp_path / "myapp.dist"
    dist_root.mkdir()
    with pytest.raises(DistError, match="launcher cannot be named"):
        write_launcher_shim(name, dist_root, _a_staged_interpreter())


def test_launcher_shim_refuses_the_name_of_an_existing_entry(tmp_path: Path) -> None:
    """
    The collision hit in practice: a launcher named after the very package it
    launches, whose directory is already at the distribution root.
    """
    dist_root = tmp_path / "hatchdemo.dist"
    (dist_root / "hatchdemo").mkdir(parents=True)
    with pytest.raises(DistError, match="already exists"):
        write_launcher_shim("hatchdemo", dist_root, _a_staged_interpreter())


def test_launcher_name_follows_the_folder_name() -> None:
    config = SmeltConfig(
        entrypoints={"pkg.cli:main": {}},
        script_names={"my-app": "pkg.cli:main"},
    )
    assert launcher_name(config, "pkg.cli:main") == "my-app"


def test_resolve_dist_python_defaults_to_byo() -> None:
    assert resolve_dist_python(EntrypointOptions()) == "byo"


def test_resolve_dist_python_reads_the_entrypoint_declaration() -> None:
    assert resolve_dist_python(EntrypointOptions({"python": "own"})) == "own"


def test_resolve_dist_python_lets_the_cli_win() -> None:
    # both directions: the flag overrides the declaration whichever way it points
    assert resolve_dist_python(EntrypointOptions({"python": "byo"}), "own") == "own"
    assert resolve_dist_python(EntrypointOptions({"python": "own"}), "byo") == "byo"


def test_resolve_dist_python_refuses_an_unknown_mode() -> None:
    with pytest.raises(DistError, match="'byo'"):
        resolve_dist_python(EntrypointOptions({"python": "system"}))


# ---------------------------------------------------------------------------
# Functional: needs a real interpreter build. See this module's docstring.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hatchdemo_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    A mode `own` distribution of `examples/hatchdemo`, built once for the whole
    module.

    `hatchdemo` is the subject because it exercises every backend at once -- mypyc,
    cython, a Nuitka module and its shared runtime, a handwritten C extension and a
    Zig one -- so one run of it covers every kind of artifact the staged interpreter
    has to be able to load.
    """
    with open(HATCHDEMO_ROOT / "pyproject.toml", "rb") as toml_file:
        toml_data = tomllib.load(toml_file)
    config = parse_config_from_pyproject(toml_data, project_root=HATCHDEMO_ROOT)
    output_dir = tmp_path_factory.mktemp("own-python-dist")
    report = build_dist(
        config,
        output_dir=output_dir,
        path_solver=config.get_path_solver(project_root=HATCHDEMO_ROOT),
        python="own",
        # Untailored on purpose, and not because tailoring is untested (it is, above,
        # and functionally in `test_a_tailored_interpreter_runs_what_it_kept`): a
        # tailored interpreter is compiled without the libraries the application does
        # not need, which is a *different build* under a different cache key. Leaving
        # this at the default would make this whole tier trigger a ten-minute CPython
        # build instead of reusing what is already cached.
        tailor_interpreter=False,
    )
    assert report.interpreter is not None, "--python own must stage an interpreter"
    return report.dist_root


def _run_without_an_environment(dist_root: Path, *args: str) -> str:
    """
    Runs the distribution's launcher the way a target machine with no Python (and no
    PATH worth having) would: `env -i PATH=/nonexistent`, expressed directly rather
    than through `env`, which would itself have to be found.
    """
    completed = subprocess.run(
        [f"./{dist_root.name.removesuffix('.dist')}", *args],
        cwd=dist_root,
        env={"PATH": "/nonexistent"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"the distribution failed with no interpreter on the system: {completed.stderr}"
    )
    return completed.stdout


@needs_own_python
def test_the_distribution_runs_with_no_python_on_the_system(hatchdemo_dist: Path) -> None:
    """
    The claim mode B makes, and the one test to keep if only one survives.
    """
    assert "Hello World!" in _run_without_an_environment(hatchdemo_dist, "say-hello")
    fib = _run_without_an_environment(hatchdemo_dist, "compute-fib", "12")
    assert "Result is 144" in fib
    # every backend's artifact loaded, not just the pure-python path
    assert "mypyc" in fib
    assert "cython" in fib


@needs_own_python
def test_the_distribution_still_runs_after_being_moved(
    hatchdemo_dist: Path, tmp_path: Path
) -> None:
    """
    Nothing in the folder may hold an absolute path to where it was built: the
    interpreter finds its own `libpython` and standard library relative to its
    executable, and every bundled library through an `$ORIGIN` RPATH.
    """
    moved = tmp_path / "elsewhere" / hatchdemo_dist.name
    moved.parent.mkdir(parents=True)
    shutil.copytree(hatchdemo_dist, moved, symlinks=True)

    assert "Hello World!" in _run_without_an_environment(moved, "say-hello")


@needs_own_python
def test_nothing_in_the_distribution_reaches_outside_it(hatchdemo_dist: Path) -> None:
    """
    `ldd` over every ELF file in the folder: what it resolves must either be inside
    the folder or be one of the libraries that deliberately come from the target
    machine (libc, its loader, and the rest of glibc's base set -- see
    `INTERPRETER_HOST_DLL_PREFIXES`).
    """
    root = hatchdemo_dist.resolve()
    escaping: dict[str, set[str]] = {}
    candidates = [
        entry
        for entry in root.rglob("*")
        if entry.is_file()
        and not entry.is_symlink()
        and (".so" in entry.suffixes or entry.name == "python" or ".so" in entry.name)
    ]
    assert candidates, "no ELF files found: the audit would pass vacuously"
    for elf_file in candidates:
        completed = subprocess.run(
            ["ldd", str(elf_file)], capture_output=True, text=True, check=False
        )
        for line in completed.stdout.splitlines():
            _, separator, resolved_part = line.partition("=>")
            if not separator:
                continue
            resolved = resolved_part.strip().split(" ")[0]
            if not resolved.startswith("/") or resolved.startswith(str(root)):
                continue
            # by basename: `ldd` reports the loader itself under an absolute
            # `DT_NEEDED` name (`/lib64/ld-linux-x86-64.so.2`), not a bare soname
            soname = Path(line.strip().split(" ")[0]).name
            if soname.startswith(INTERPRETER_HOST_DLL_PREFIXES):
                continue
            escaping.setdefault(str(elf_file.relative_to(root)), set()).add(
                f"{soname} -> {resolved}"
            )
    assert escaping == {}


@needs_own_python
def test_a_tailored_interpreter_runs_what_it_kept(tmp_path: Path) -> None:
    """
    Tailoring against a *real* interpreter, without paying for a build: staging is
    what does the pruning, and it works on whatever prefix it is handed -- so the
    cached vanilla build is enough to check that a tailored tree still starts, still
    finds the modules the safety net keeps, and is meaningfully smaller.
    """
    prefix = assert_path_exists(_CACHED_PREFIX)
    dist_root = tmp_path / "tailored.dist"
    dist_root.mkdir()
    reference = tmp_path / "vanilla.dist"
    reference.mkdir()

    vanilla = stage_interpreter(prefix, reference, bundle_dependencies=False)
    tailored = stage_interpreter(
        prefix,
        dist_root,
        bundle_dependencies=False,
        # A closure of one module, so almost everything is up for pruning.
        requirements=resolve_requirements(["json", "_json"], prefix),
    )

    assert tailored.tailored
    assert tailored.size_bytes < vanilla.size_bytes
    assert "sqlite3" in tailored.pruned_modules
    assert "nis" in tailored.pruned_extensions
    # what the closure named, and what the safety net keeps whatever it says
    probe = (
        "import encodings.idna, json, linecache, runpy, traceback, warnings, zipimport\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [str(dist_root / INTERPRETER_REL_PATH), "-I", "-c", probe],
        capture_output=True,
        text=True,
        env={"PATH": "/nonexistent"},
    )
    assert completed.stdout.strip() == "ok", completed.stderr


@needs_own_python
def test_the_manifest_and_instructions_describe_mode_own(hatchdemo_dist: Path) -> None:
    manifest = json.loads((hatchdemo_dist / MANIFEST_NAME).read_text())
    assert manifest["mode"] == "own"
    assert manifest["launcher"] == "hatchdemo"
    interpreter = manifest["interpreter"]
    assert interpreter["executable"] == "bin/python"
    assert interpreter["version"] == "3.12"
    assert interpreter["size_bytes"] > 0
    assert interpreter["sourceless_stdlib"] is True

    instructions = (hatchdemo_dist / INSTRUCTIONS_NAME).read_text()
    # the honest claim, and the caveat that goes with it
    assert "nothing has to" in instructions
    assert "C library" in instructions


@needs_own_python
def test_run_instructions_fall_back_to_the_interpreter_when_there_is_no_launcher() -> None:
    """
    The launcher is a convenience, so the instructions must still say how to run the
    folder without one.
    """
    report = DistReport(
        dist_root=Path("myapp.dist"),
        entrypoint="pkg.cli:main",
        entrypoint_module=assert_is_valid_import_path("pkg.cli"),
        tag=PycTargetTag((3, 12), b"\xcb\r\r\n", 0),
        interpreter=_a_staged_interpreter(),
    )
    assert f"./bin/python ./{PAYLOAD_DIR_NAME}" in run_instructions(report)


def test_every_optional_stdlib_group_states_its_consequence() -> None:
    """
    A group is only worth being a group if dropping it can be chosen, and a choice
    whose cost cannot be stated is a trap rather than an option -- so `StdlibGroup`
    enforces it and this pins the invariant.
    """
    for group in MINIMAL_VIABLE_STDLIB:
        assert group.rationale, group.name
        if group.optional:
            assert group.consequence, group.name
    assert OPTIONAL_STDLIB_GROUPS, "a split with nothing droppable would be pointless"


def test_minimal_viable_stdlib_defaults_to_every_group() -> None:
    assert minimal_viable_stdlib() == frozenset(ALWAYS_KEEP)
    assert frozenset(ALWAYS_KEEP) == frozenset(
        module for group in MINIMAL_VIABLE_STDLIB for module in group.modules
    )


def test_dropping_a_group_removes_exactly_its_modules() -> None:
    for name in OPTIONAL_STDLIB_GROUPS:
        (group,) = [g for g in MINIMAL_VIABLE_STDLIB if g.name == name]
        assert frozenset(ALWAYS_KEEP) - minimal_viable_stdlib([name]) == frozenset(group.modules)


def test_a_mandatory_group_cannot_be_dropped() -> None:
    """
    Dropping the import system would produce a folder that cannot start, so this is
    refused rather than honoured -- and the error names what can be dropped instead.
    """
    mandatory = next(g.name for g in MINIMAL_VIABLE_STDLIB if not g.optional)
    with pytest.raises(OwnPythonError) as excinfo:
        minimal_viable_stdlib([mandatory])
    assert mandatory in str(excinfo.value)
    assert str(list(OPTIONAL_STDLIB_GROUPS)) in str(excinfo.value)


def test_an_unknown_group_is_refused() -> None:
    with pytest.raises(OwnPythonError, match="Unknown standard library group"):
        minimal_viable_stdlib(["no_such_group"])


@needs_own_python
def test_dropping_a_group_narrows_the_keep_set() -> None:
    prefix = assert_path_exists(own_python_cache_dir())
    kept = resolve_requirements([], prefix)
    narrowed = resolve_requirements([], prefix, drop_stdlib_groups=["international_hostnames"])
    assert narrowed.dropped_stdlib_groups == frozenset({"international_hostnames"})
    assert narrowed.keep_modules < kept.keep_modules
    assert "unicodedata" in kept.keep_modules - narrowed.keep_modules
