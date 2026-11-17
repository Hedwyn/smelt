"""
Tests for the `smelt bundle` command line: flags generated from the schema, and the
checks run before a build starts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest
from click.testing import CliRunner

import smelt.frontend as frontend
from smelt.bundle_flags import FLAGS, NO_GENERATED_FLAG, schema_keys, settings_from_params
from smelt.dist import DistReport

PYPROJECT = """\
[project]
name = "flagpkg"
version = "0.1"

[project.scripts]
flag-app = "flagpkg.cli:main"

[tool.smelt]
package-dirs = { flagpkg = "src/flagpkg" }
{extra}
"""


def _project(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.replace("{extra}", extra))
    package = tmp_path / "src" / "flagpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("def main() -> int:\n    return 0\n")
    return tmp_path


def _run(project: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(frontend.smelt, ["bundle", "-p", str(project), *args])
    return result.exit_code, result.output


def test_every_bundle_schema_key_has_a_flag() -> None:
    keys = set(schema_keys())
    assert keys - NO_GENERATED_FLAG == set(FLAGS)
    help_text = CliRunner().invoke(frontend.smelt, ["bundle", "--help"]).output
    for key in keys - NO_GENERATED_FLAG:
        assert FLAGS[key].decls[0].split("/")[0] in help_text


def test_flags_become_a_settings_level() -> None:
    settings = settings_from_params(
        {
            "python": "own",
            "content_include_modules": ("a", "b"),
            "content_package_data": ("pkg:*.json,*.yaml", "other"),
            "interpreter_tailor": False,
            "onefile_keep_folder": False,
            "onefile_compression_preset": 3,
        }
    )
    assert settings.python == "own"
    assert settings.content.include_modules == ["a", "b"]
    assert settings.content.package_data == {"pkg": ["*.json", "*.yaml"], "other": []}
    assert settings.interpreter.tailor is False
    assert settings.onefile.keep_folder is False
    assert settings.onefile.compression_preset == 3
    assert settings.guard_version is None


@pytest.mark.parametrize(
    ("extra", "args", "message"),
    [
        ("", ["--tailor"], "only applies to `python = \"own\"`"),
        (
            '[tool.smelt.bundle.interpreter]\nstatic = true\n',
            [],
            "only applies to `python = \"own\"`",
        ),
        ("", ["--onefile-compression-preset", "0", "--onefile-compression", "gzip"], "1-9"),
        ("", ["--python", "own", "--python-version", "2.7.18"], "minor version"),
        (
            '[tool.smelt.bundle.isolated-build]\nversions = "lock"\n',
            [],
            "versions",
        ),
    ],
)
def test_invalid_settings_are_rejected_before_building(
    tmp_path: Path, extra: str, args: list[str], message: str
) -> None:
    code, output = _run(_project(tmp_path, extra), *args)
    assert code != 0
    assert message in output


def test_compression_none_is_accepted() -> None:
    from smelt.dist import BUNDLE_DEFAULTS, resolve_bundle, validate_bundle
    from smelt.schema import BundleSettings, OnefileGroup

    resolved = resolve_bundle([], BundleSettings(onefile=OnefileGroup(compression="none")))
    validate_bundle(resolved)
    assert resolved.onefile_compression == "none"
    assert BUNDLE_DEFAULTS.onefile.compression == "xz"


@pytest.mark.parametrize(
    ("args", "nested"),
    [([], False), (["-t", "host"], False), (["-t", "x86_64-linux-musl,aarch64-linux-musl"], True)],
)
def test_output_is_nested_by_target_only_when_several(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], nested: bool
) -> None:
    outputs: list[Path] = []

    def fake_build_dist(*_: object, output_dir: Path, **__: object) -> DistReport:
        outputs.append(output_dir)
        return create_autospec(DistReport, instance=True)

    monkeypatch.setattr(frontend, "build_dist", fake_build_dist)
    monkeypatch.setattr(frontend, "run_instructions", lambda _: "")
    code, output = _run(_project(tmp_path), "-o", "out", *args)
    assert code == 0, output
    assert (len(outputs) > 1 and all(o.parent == Path("out") for o in outputs)) == nested
    if not nested:
        assert outputs == [Path("out")]
