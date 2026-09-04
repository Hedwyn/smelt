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
    run_instructions,
    write_launcher_shim,
)
from smelt.frontend import parse_config_from_pyproject
from smelt.native_deps import is_supported_platform
from smelt.own_python import (
    DEFAULT_STDLIB_PRUNE,
    INTERPRETER_HOST_DLL_PREFIXES,
    INTERPRETER_REL_PATH,
    OwnPythonError,
    StagedInterpreter,
    interpreter_version,
    own_python_cache_dir,
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
    build in every test: a `bin/python` that answers the one question
    `interpreter_version` asks it, and a standard library made of a few files chosen
    to cover each case the staging has to handle.

    `test/badsyntax.py` is not padding: CPython's own standard library ships
    deliberately-invalid-syntax fixtures under `test/`, and they were the only
    bytecode-compilation failures in the whole tree -- so the tree used here has to be
    able to reproduce both the failure and the fact that pruning `test` removes it.
    """
    prefix = root / "built"
    executable = prefix / INTERPRETER_REL_PATH
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho '3 12'\n")
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
