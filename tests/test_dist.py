from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from smelt.config import SmeltConfig
from smelt.dist import (
    INSTRUCTIONS_NAME,
    MANIFEST_NAME,
    PAYLOAD_DIR_NAME,
    DistError,
    build_dist,
    collect_closure,
    collect_distribution_metadata,
    collect_package_data,
    dist_folder_name,
    project_search_paths,
    resolve_entrypoint_spec,
    trace_imported_modules,
)
from smelt.explorer import ModuleKind
from smelt.utils import PackageRootPath, PathSolver, assert_is_valid_import_path, assert_path_exists


def test_resolve_entrypoint_spec_by_script_name() -> None:
    config = SmeltConfig(
        entrypoints={"pkg.cli:main": {}},
        script_names={"my-app": "pkg.cli:main"},
    )
    assert resolve_entrypoint_spec(config, "my-app") == "pkg.cli:main"
    assert resolve_entrypoint_spec(config, "pkg.cli:main") == "pkg.cli:main"
    # a single declared entrypoint is unambiguous, so naming it is optional
    assert resolve_entrypoint_spec(config, None) == "pkg.cli:main"


def test_resolve_entrypoint_spec_rejects_ambiguity_and_unknowns() -> None:
    config = SmeltConfig(entrypoints={"pkg.cli:main": {}, "pkg.other:main": {}})
    with pytest.raises(DistError):
        # a distribution holds a single __main__, so there is nothing to default to
        resolve_entrypoint_spec(config, None)
    with pytest.raises(DistError):
        resolve_entrypoint_spec(config, "nope")
    with pytest.raises(DistError):
        resolve_entrypoint_spec(SmeltConfig(), None)


def test_dist_folder_name_prefers_the_script_name() -> None:
    config = SmeltConfig(
        entrypoints={"pkg.cli:main": {}, "pkg.tool:run": {}},
        script_names={"my-app": "pkg.cli:main"},
    )
    assert dist_folder_name(config, "pkg.cli:main") == "my-app.dist"
    # not declared in [project.scripts]: fall back to the entrypoint function
    assert dist_folder_name(config, "pkg.tool:run") == "run.dist"
    assert dist_folder_name(config, "pkg.tool") == "tool.dist"


def test_project_search_paths_from_declared_roots(tmp_path: Path) -> None:
    package_root = tmp_path / "src" / "pkg"
    package_root.mkdir(parents=True)
    path_solver = PathSolver(
        known_roots=[
            PackageRootPath(
                assert_is_valid_import_path("pkg"),
                assert_path_exists(package_root),
            )
        ],
        project_root=tmp_path,
    )
    # the importable root is the folder *holding* the package, not the package itself
    assert project_search_paths(path_solver) == [str(tmp_path / "src")]


def test_project_search_paths_falls_back_to_conventional_layouts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert project_search_paths(PathSolver(project_root=tmp_path)) == [
        str(tmp_path / "src"),
        str(tmp_path),
    ]


def _write_project(root: Path, name: str = "pkg") -> Path:
    """
    A src-layout project, deliberately not installed in the environment running the
    tests -- which is the normal case when building a distribution.

    `name` is parametrized because resolving `name.cli` imports `name` itself, so two
    projects sharing a package name within one test session would have the first one
    cached in `sys.modules` while the second is being resolved.
    """
    package = root / "src" / name
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "sub" / "__init__.py").write_text("")
    (package / "sub" / "helper.py").write_text("def helper() -> int:\n    return 1\n")
    (package / "cli.py").write_text(
        f"import json\n\nfrom {name}.sub.helper import helper\n\n\ndef main() -> int:\n"
        "    return helper() and json.loads('1')\n"
    )
    return root / "src"


def test_collect_closure_reaches_the_whole_project(tmp_path: Path) -> None:
    search_root = _write_project(tmp_path, "pkg_whole")
    closure = collect_closure(assert_is_valid_import_path("pkg_whole.cli"), [str(search_root)])

    assert closure["pkg_whole.cli"].kind == ModuleKind.SOURCE
    assert closure["pkg_whole.sub.helper"].kind == ModuleKind.SOURCE
    # parent packages are pulled in even though nothing imports them explicitly:
    # importing `pkg.sub.helper` requires both of them
    assert closure["pkg_whole"].is_package
    assert closure["pkg_whole.sub"].is_package
    # the standard library is reached, and flagged so it is not shipped
    assert closure["json"].is_stdlib


def test_collect_closure_does_not_leak_search_paths(tmp_path: Path) -> None:
    search_root = _write_project(tmp_path, "pkg_leak")
    before = list(sys.path)
    collect_closure(assert_is_valid_import_path("pkg_leak.cli"), [str(search_root)])
    assert sys.path == before


def test_collect_closure_follows_a_module_shadowed_by_its_own_artifact(tmp_path: Path) -> None:
    """
    Once a module is built, the import machinery only reports its `.so` -- the walk has
    to keep reading imports from the source it was compiled from, or the graph stops at
    the first compiled module.
    """
    search_root = _write_project(tmp_path, "pkg_shadowed")
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    # not a real extension module: resolution only looks at the file's name
    (search_root / "pkg_shadowed" / f"cli{suffix}").write_bytes(b"")

    closure = collect_closure(assert_is_valid_import_path("pkg_shadowed.cli"), [str(search_root)])
    resolved = closure["pkg_shadowed.cli"]
    assert resolved.kind == ModuleKind.EXTENSION
    assert resolved.shadowed_source is not None
    assert resolved.parsable_source == search_root / "pkg_shadowed" / "cli.py"
    # and the walk got past it
    assert "pkg_shadowed.sub.helper" in closure


def _write_package_with_data(root: Path, name: str) -> Path:
    package = root / "src" / name / "assets"
    package.mkdir(parents=True)
    (root / "src" / name / "__init__.py").write_text("")
    (root / "src" / name / "helper.py").write_text("")
    (package / "config.json").write_text("{}\n")
    (package / "notes.txt").write_text("notes\n")
    (package / "nested").mkdir()
    (package / "nested" / "more.json").write_text("[]\n")
    cache = root / "src" / name / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-312.pyc").write_bytes(b"")
    return root / "src"


def test_collect_package_data_copies_data_and_leaves_code_out(tmp_path: Path) -> None:
    search_root = _write_package_with_data(tmp_path, "datapkg")
    dist_root = tmp_path / "dist"
    dist_root.mkdir()

    collected = collect_package_data(["datapkg"], dist_root, [str(search_root)])

    copied = sorted(str(data_file.dest_rel_path) for data_file in collected)
    assert copied == [
        "datapkg/assets/config.json",
        "datapkg/assets/nested/more.json",
        "datapkg/assets/notes.txt",
    ]
    # code is the other passes' business, and __pycache__ is never shipped
    assert not list(dist_root.rglob("*.py"))
    assert not list(dist_root.rglob("__pycache__"))
    assert (dist_root / "datapkg" / "assets" / "nested" / "more.json").read_text() == "[]\n"


def test_collect_package_data_honors_name_patterns(tmp_path: Path) -> None:
    search_root = _write_package_with_data(tmp_path, "patternpkg")
    dist_root = tmp_path / "dist"
    dist_root.mkdir()

    collected = collect_package_data(["patternpkg:*.json"], dist_root, [str(search_root)])

    assert sorted(str(data_file.dest_rel_path) for data_file in collected) == [
        "patternpkg/assets/config.json",
        "patternpkg/assets/nested/more.json",
    ]


def test_collect_package_data_skips_an_unresolvable_package(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    # a package that is not installed is a warning, not a build failure: the spec may
    # legitimately target an optional dependency
    assert collect_package_data(["no_such_package_xyz"], dist_root) == []


def test_collect_closure_follows_a_submodule_imported_by_name(tmp_path: Path) -> None:
    """
    `from pkg import mod` is the only single-statement way to import `pkg.mod`, so the
    imported names have to be resolved too -- dropping them loses real modules.
    """
    package = tmp_path / "src" / "frompkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "leaf.py").write_text("VALUE = 1\n")
    (package / "sibling.py").write_text("VALUE = 2\n")
    (package / "cli.py").write_text(
        "from frompkg import leaf\nfrom frompkg.leaf import VALUE\nfrom . import sibling\n"
    )

    closure = collect_closure(assert_is_valid_import_path("frompkg.cli"), [str(tmp_path / "src")])
    assert "frompkg.leaf" in closure
    # `from . import sibling` names nothing else at all
    assert "frompkg.sibling" in closure
    # an attribute of a module is not a module, and must not end up in the closure
    assert "frompkg.leaf.VALUE" not in closure


def _write_dynamic_import_project(root: Path, name: str) -> Path:
    """
    A project whose real dependency is imported by computed name -- i.e. invisible to
    any amount of source reading, and the case tracing exists for.
    """
    package = root / "src" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "plugin.py").write_text("VALUE = 42\n")
    (package / "static_dep.py").write_text("")
    (package / "cli.py").write_text(
        "import importlib\n"
        f"from {name} import static_dep\n"
        "\n"
        f'_plugin = importlib.import_module("{name}." + "plug" + "in")\n'
        "\n"
        "def main() -> int:\n"
        "    return _plugin.VALUE\n"
    )
    return root / "src"


def test_static_discovery_misses_a_computed_import(tmp_path: Path) -> None:
    search_root = _write_dynamic_import_project(tmp_path, "dynpkg_static")
    closure = collect_closure(
        assert_is_valid_import_path("dynpkg_static.cli"),
        [str(search_root)],
        discovery="static",
    )
    assert "dynpkg_static.static_dep" in closure
    # the point of the test: reading the source cannot find this one
    assert "dynpkg_static.plugin" not in closure


def test_tracing_finds_a_computed_import(tmp_path: Path) -> None:
    search_root = _write_dynamic_import_project(tmp_path, "dynpkg_trace")
    traced = trace_imported_modules(
        assert_is_valid_import_path("dynpkg_trace.cli"), [str(search_root)]
    )
    assert "dynpkg_trace.plugin" in traced
    assert "dynpkg_trace.static_dep" in traced


def test_both_discovery_unions_the_two(tmp_path: Path) -> None:
    search_root = _write_dynamic_import_project(tmp_path, "dynpkg_both")
    closure = collect_closure(
        assert_is_valid_import_path("dynpkg_both.cli"),
        [str(search_root)],
        discovery="both",
    )
    assert "dynpkg_both.plugin" in closure
    assert "dynpkg_both.static_dep" in closure


def test_tracing_an_unimportable_entrypoint_is_not_fatal(tmp_path: Path) -> None:
    """
    A trace that fails degrades to static discovery: the build goes on, with a warning.
    """
    package = tmp_path / "src" / "brokenpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("raise RuntimeError('boom')\n")
    assert (
        trace_imported_modules(
            assert_is_valid_import_path("brokenpkg.cli"), [str(tmp_path / "src")]
        )
        == set()
    )


def test_type_checking_only_imports_are_not_collected(tmp_path: Path) -> None:
    """
    A `TYPE_CHECKING` guard never runs, so following it would drag whole libraries in
    for the sake of an annotation.
    """
    package = tmp_path / "src" / "annotpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "heavy.py").write_text("")
    (package / "runtime_dep.py").write_text("")
    (package / "cli.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from annotpkg import heavy\n"
        "else:\n"
        "    from annotpkg import runtime_dep\n"
    )

    closure = collect_closure(
        assert_is_valid_import_path("annotpkg.cli"),
        [str(tmp_path / "src")],
        discovery="static",
    )
    assert "annotpkg.heavy" not in closure
    # the `else` branch is what actually runs, and must be kept
    assert "annotpkg.runtime_dep" in closure


def test_exclude_modules_drops_a_whole_subtree(tmp_path: Path) -> None:
    search_root = _write_project(tmp_path, "exclpkg")
    closure = collect_closure(
        assert_is_valid_import_path("exclpkg.cli"),
        [str(search_root)],
        discovery="static",
        exclude_modules=["exclpkg.sub"],
    )
    assert "exclpkg.sub" not in closure
    assert "exclpkg.sub.helper" not in closure
    # and the entrypoint is never excluded, whatever is asked
    assert "exclpkg.cli" in collect_closure(
        assert_is_valid_import_path("exclpkg.cli"),
        [str(search_root)],
        discovery="static",
        exclude_modules=["exclpkg"],
    )


def test_collect_distribution_metadata_ships_dist_info(tmp_path: Path) -> None:
    """
    Against a real installed distribution: `importlib.metadata` resolves against
    `*.dist-info` directories found on `sys.path`, so the directory name has to survive.
    """
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    collected = collect_distribution_metadata([assert_is_valid_import_path("pytest")], dist_root)
    assert collected, "expected pytest's own metadata to be collected"
    dist_infos = {entry.dest_rel_path.parts[0] for entry in collected}
    assert any(name.startswith("pytest-") and name.endswith(".dist-info") for name in dist_infos)
    assert any(entry.dest_rel_path.name == "METADATA" for entry in collected)
    for entry in collected:
        assert (dist_root / entry.dest_rel_path).is_file()
    # RECORD describes an installation, not this folder
    assert not any(entry.dest_rel_path.name == "RECORD" for entry in collected)


def test_collect_distribution_metadata_skips_unknown_names(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    assert (
        collect_distribution_metadata([], dist_root, extra_distributions=["no-such-dist-xyz"]) == []
    )


def test_build_dist_keeps_the_application_out_of_the_distribution_root(tmp_path: Path) -> None:
    """
    The application goes under `app/`, and nothing else does.

    The separation is load-bearing rather than tidy: the root has to hold files that
    are not modules (the manifest, the instructions, later a launcher and a bundled
    interpreter's `bin/`+`lib/`), and any of those could otherwise collide with a
    top-level module name -- a package called `lib`, or a launcher named after the
    package it launches.
    """
    package = tmp_path / "src" / "layoutpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "def main() -> int:\n    print('ran from the payload')\n    return 0\n"
    )
    config = SmeltConfig(
        packages_location={"layoutpkg": "src/layoutpkg"},
        entrypoints={"layoutpkg.cli:main": {}},
        script_names={"layout-app": "layoutpkg.cli:main"},
    )

    report = build_dist(
        config,
        output_dir=tmp_path / "out",
        path_solver=config.get_path_solver(project_root=tmp_path),
        build_extensions=False,
        discovery="static",
    )

    assert report.dist_root == tmp_path / "out" / "layout-app.dist"
    assert report.payload_root == report.dist_root / PAYLOAD_DIR_NAME
    assert sorted(entry.name for entry in report.dist_root.iterdir()) == [
        INSTRUCTIONS_NAME,
        PAYLOAD_DIR_NAME,
        MANIFEST_NAME,
    ]
    # the module tree, and `__main__`, are in the payload -- that is the sys.path entry
    assert (report.payload_root / "layoutpkg" / "cli.pyc").is_file()
    assert (report.payload_root / "layoutpkg" / "__init__.pyc").is_file()
    # the generated entrypoint is the one file shipped as source, so its version guard
    # can run under an interpreter that cannot load the bytecode (see
    # `write_entrypoint_module`)
    assert report.entrypoint_file == Path("__main__.py")
    assert [path.name for path in report.dist_root.rglob("*.py")] == ["__main__.py"]

    manifest = json.loads((report.dist_root / MANIFEST_NAME).read_text())
    # recorded, because every path in the manifest is relative to it
    assert manifest["payload"] == PAYLOAD_DIR_NAME
    assert manifest["bytecode_modules"]["layoutpkg.cli"] == "layoutpkg/cli.pyc"

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(report.payload_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ran from the payload"


def test_run_instructions_point_at_the_payload_directory(tmp_path: Path) -> None:
    package = tmp_path / "src" / "instrpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("def main() -> int:\n    return 0\n")
    config = SmeltConfig(
        packages_location={"instrpkg": "src/instrpkg"},
        entrypoints={"instrpkg.cli:main": {}},
    )
    report = build_dist(
        config,
        output_dir=tmp_path / "out",
        path_solver=config.get_path_solver(project_root=tmp_path),
        build_extensions=False,
        discovery="static",
    )
    instructions = (report.dist_root / INSTRUCTIONS_NAME).read_text()
    # the command a user copies has to include the payload folder, or it fails on
    # `can't find '__main__' module`. No flags: the entrypoint's isolation guard adds
    # them by re-executing itself.
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert f"python{version} {report.payload_root}" in instructions
    assert f"-I -S {report.payload_root}" not in instructions


def _guarded_dist(tmp_path: Path, name: str, **kwargs: object) -> Path:
    """
    Builds a distribution for a one-module application that reports how it was run.
    """
    package = tmp_path / "src" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import sys\n"
        "\n"
        "def main() -> int:\n"
        "    print('isolated=%s no_site=%s argv=%r'\n"
        "          % (bool(sys.flags.isolated), bool(sys.flags.no_site), sys.argv[1:]))\n"
        "    print('site-packages=%s' % any('site-packages' in p for p in sys.path))\n"
        "    return 0\n"
    )
    config = SmeltConfig(
        packages_location={name: f"src/{name}"},
        entrypoints={f"{name}.cli:main": {}},
    )
    report = build_dist(
        config,
        output_dir=tmp_path / "out",
        path_solver=config.get_path_solver(project_root=tmp_path),
        build_extensions=False,
        discovery="static",
        **kwargs,  # type: ignore[arg-type]
    )
    return report.payload_root


def test_entrypoint_is_shipped_as_source_when_guarded(tmp_path: Path) -> None:
    """
    A version guard held in a `.pyc` could never run: the magic number is checked
    before any code is executed, so a mismatched interpreter rejects the guard along
    with everything it was meant to explain. Source compiles under any version.
    """
    payload = _guarded_dist(tmp_path, "srcguard")
    assert (payload / "__main__.py").is_file()
    assert not (payload / "__main__.pyc").exists()
    # and it is the *only* source file: application modules stay bytecode
    assert [p.name for p in payload.rglob("*.py")] == ["__main__.py"]


def test_unguarded_entrypoint_stays_bytecode(tmp_path: Path) -> None:
    payload = _guarded_dist(tmp_path, "nosrcguard", guard_version=False, isolate=False)
    assert (payload / "__main__.pyc").is_file()
    assert not list(payload.rglob("*.py"))


def test_isolation_guard_re_execs_when_flags_are_missing(tmp_path: Path) -> None:
    """
    Run without `-I -S`, the entrypoint re-executes itself with them -- so the host's
    site-packages cannot silently supply a module the distribution is missing.
    """
    payload = _guarded_dist(tmp_path, "isoguard")
    completed = subprocess.run(
        [sys.executable, str(payload), "one", "two"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "isolated=True no_site=True" in completed.stdout
    assert "argv=['one', 'two']" in completed.stdout
    assert "site-packages=False" in completed.stdout


def test_isolation_guard_does_not_re_exec_when_already_isolated(tmp_path: Path) -> None:
    """
    The guard is self-limiting: it checks the flags it would set, so it cannot loop.
    """
    payload = _guarded_dist(tmp_path, "isoguard_twice")
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "isolated=True no_site=True" in completed.stdout


def test_isolation_guard_is_omitted_when_not_asked_for(tmp_path: Path) -> None:
    payload = _guarded_dist(tmp_path, "noiso", isolate=False)
    completed = subprocess.run(
        [sys.executable, str(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "isolated=False no_site=False" in completed.stdout
