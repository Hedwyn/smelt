"""
Test suite for smelt frontend.

@date: 13.06.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from smelt.config import SmeltConfigError
from smelt.frontend import TomlData, parse_config_from_pyproject

#: A project declaring one module per supported backend, in the shape the parser reads:
#: **an array of tables per section**, each entry naming the module it produces and the
#: source it is built from. Entrypoints are not declared here at all -- `smelt` picks up
#: every `[project.scripts]` target by itself.
SAMPLE_CONFIG: str = """\
[project]
name = "minimal"

[project.scripts]
minimal-demo = "minimal.cli:minimal"

[tool.smelt]
packages_location = { "minimal" = "src/minimal" }

[[tool.smelt.c_extensions]]
import_path = "minimal.hello"
sources = ["src/minimal/hello.c"]

[[tool.smelt.mypyc_modules]]
import_path = "minimal.fib"
source = "src/minimal/fib.py"

[[tool.smelt.cython_modules]]
import_path = "minimal.fib_cython"
source = "src/minimal/fib_cython.pyx"

[[tool.smelt.nuitka_modules]]
import_path = "minimal.cli"
"""

#: The same declarations in the `module = "source"` mapping form module sections used
#: before they became arrays of tables. Kept as a fixture because a `pyproject.toml`
#: written against it is what a user upgrading actually has on disk, and the parser owes
#: them a sentence they can act on rather than an `AttributeError` (see
#: `config.build_datacls_from_toml`).
LEGACY_CONFIG: str = """\
[project]
name = "minimal"

[tool.smelt.c_extensions]
"minimal.hello" = "src/minimal/hello.c"
"""

#: Files `SAMPLE_CONFIG` points at. Their *contents* are irrelevant -- path fields are
#: resolved with `assert_path_exists`, so what is under test is that the parser looks
#: them up relative to the project root it was given.
SAMPLE_SOURCES: tuple[str, ...] = (
    "src/minimal/__init__.py",
    "src/minimal/hello.c",
    "src/minimal/fib.py",
    "src/minimal/fib_cython.pyx",
    "src/minimal/cli.py",
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """
    A source tree matching `SAMPLE_CONFIG`, so that the declared paths resolve.
    """
    for source in SAMPLE_SOURCES:
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return tmp_path


@pytest.fixture
def toml_data() -> TomlData:
    """
    Example TOML data as it would be extracted from pyproject.toml
    """
    return tomllib.loads(SAMPLE_CONFIG)


def test_parse(toml_data: TomlData, project_root: Path) -> None:
    """
    Verifies that the pyproject parse extracts Smelt config properly
    """
    config = parse_config_from_pyproject(toml_data, project_root=project_root)

    # entrypoints come from [project.scripts], and are keyed by their target so that a
    # declaration under [tool.smelt.entrypoints] can customize the same one
    assert set(config.entrypoints) == {"minimal.cli:minimal"}
    assert config.script_names == {"minimal-demo": "minimal.cli:minimal"}

    assert [module.import_path for module in config.c_extensions] == ["minimal.hello"]
    assert [module.import_path for module in config.mypyc_modules] == ["minimal.fib"]
    assert [module.import_path for module in config.cython_modules] == ["minimal.fib_cython"]
    assert [module.import_path for module in config.nuitka_modules] == ["minimal.cli"]


def test_parse_resolves_sources_against_the_project_root(
    toml_data: TomlData, project_root: Path
) -> None:
    """
    Every declared source is resolved relative to the project root, not to the current
    working directory -- the frontend is routinely run from elsewhere (`-p PATH`), and
    the build hook always is.
    """
    config = parse_config_from_pyproject(toml_data, project_root=project_root)
    (extension,) = config.c_extensions
    assert extension.sources == [project_root / "src/minimal/hello.c"]
    (mypyc_module,) = config.mypyc_modules
    assert mypyc_module.source == project_root / "src/minimal/fib.py"
    # a module declared without a source keeps None: the backend resolves it from the
    # import path instead
    (nuitka_module,) = config.nuitka_modules
    assert nuitka_module.source is None


def test_parse_reports_a_missing_source(toml_data: TomlData, project_root: Path) -> None:
    """
    A declared source that is not there is a configuration error, named as one, rather
    than a failure in the middle of the build.
    """
    (project_root / "src/minimal/hello.c").unlink()
    with pytest.raises(SmeltConfigError, match="hello.c"):
        parse_config_from_pyproject(toml_data, project_root=project_root)


def test_parse_reports_a_declaration_in_the_legacy_mapping_form() -> None:
    """
    `[tool.smelt.c_extensions]` as a `module = "source"` mapping is the format module
    declarations had before they became arrays of tables. Iterating a table yields its
    keys, so each entry reaches the dataclass builder as a bare string; without a check
    there that surfaces as `AttributeError: 'str' object has no attribute 'get'`, which
    names neither the section nor the fix.
    """
    with pytest.raises(SmeltConfigError) as error:
        parse_config_from_pyproject(tomllib.loads(LEGACY_CONFIG))
    message = str(error.value)
    assert "extensions.c" in message
    assert "import-path" in message and "sources" in message
    assert "minimal.hello" in message


def test_parse_reports_an_option_that_is_not_one() -> None:
    """
    Anything left in `[tool.smelt]` after the known sections is passed through as a
    plain option, so a stale or misspelled one used to surface as
    `TypeError: SmeltConfig.__init__() got an unexpected keyword argument` -- which
    names the class rather than the file to edit. The singular `entrypoint` is the one
    that turns up in practice, `[project.scripts]` having replaced it.
    """
    for option in ("entrypoint", "mypyc", "auto_moed"):
        with pytest.raises(SmeltConfigError) as error:
            parse_config_from_pyproject(
                tomllib.loads(f'[project]\nname = "minimal"\n\n[tool.smelt]\n{option} = "x"\n')
            )
        assert option in str(error.value)


def test_parse_requires_a_smelt_section() -> None:
    with pytest.raises(SmeltConfigError, match="No smelt config"):
        parse_config_from_pyproject(tomllib.loads('[project]\nname = "minimal"\n'))


NUITKA_PROJECT: str = """\
[project]
name = "demo"

[project.scripts]
demo = "demo.cli:main"

[tool.smelt.nuitka-binary]
extra-flags = ["--lto=yes"]
package-data = { demo = ["*.json"] }
"""


def _stub_run_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    from smelt import frontend

    seen: dict[str, object] = {}

    def fake_run_backend(config: object, **kwargs: object) -> None:
        seen["config"] = config
        seen["kwargs"] = kwargs

    monkeypatch.setattr(frontend, "run_backend", fake_run_backend)
    return seen


def test_nuitka_binary_passes_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`[tool.smelt.nuitka-binary]` options reach `run_backend`; `--package-data` appends."""
    from click.testing import CliRunner

    from smelt import frontend

    (tmp_path / "pyproject.toml").write_text(NUITKA_PROJECT)
    seen = _stub_run_backend(monkeypatch)
    result = CliRunner().invoke(
        frontend.smelt,
        ["-p", str(tmp_path), "nuitka-binary", "--package-data", "demo:*.yaml"],
    )
    assert result.exit_code == 0, result.output
    config = seen["config"]
    assert isinstance(config, frontend.SmeltConfig)
    options = config.nuitka_binary["demo.cli:main"]
    assert options.extra_flags == ["--lto=yes"]
    assert options.package_data == {"demo": ["*.json", "*.yaml"]}


def test_group_options_apply_to_every_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-p` and `--report` are read from the group by commands that take no such option."""
    from click.testing import CliRunner

    from smelt import frontend

    (tmp_path / "pyproject.toml").write_text(NUITKA_PROJECT)
    seen = _stub_run_backend(monkeypatch)
    report = tmp_path / "report.log"
    result = CliRunner().invoke(
        frontend.smelt, ["-p", str(tmp_path), "--report", str(report), "build"]
    )
    assert result.exit_code == 0, result.output
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["without_entrypoint"] is True
    assert report.exists()


@pytest.mark.parametrize(
    ("old", "new", "project_flag"),
    [
        ("build-extensions", "build", "--package"),
        ("build-standalone-binary", "nuitka-binary", "--package-path"),
    ],
)
def test_deprecated_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str, project_flag: str
) -> None:
    """Old command names still run, with their old project flag, and print a deprecation line."""
    from click.testing import CliRunner

    from smelt import frontend

    (tmp_path / "pyproject.toml").write_text(NUITKA_PROJECT)
    seen = _stub_run_backend(monkeypatch)
    result = CliRunner().invoke(frontend.smelt, [old, project_flag, str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"use `smelt {new}`" in result.output
    assert "config" in seen


def test_aliases_are_hidden() -> None:
    from click.testing import CliRunner

    from smelt import frontend

    output = CliRunner().invoke(frontend.smelt, ["--help"]).output
    for name in ("config", "build", "bundle", "nuitka-binary", "compile", "clean"):
        assert name in output
    for old in ("show-config", "build-extensions", "compile-module", "clean-artifacts"):
        assert old not in output


def test_config_shows_sources(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from smelt import frontend

    (tmp_path / "pyproject.toml").write_text(
        NUITKA_PROJECT + "\n[tool.smelt.bundle]\npython = \"own\"\n"
    )
    result = CliRunner().invoke(frontend.smelt, ["-p", str(tmp_path), "config"])
    assert result.exit_code == 0, result.output
    assert "python = " in result.output and "own" in result.output
    assert "[default]" in result.output
