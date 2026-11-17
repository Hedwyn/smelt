from __future__ import annotations

from typing import TYPE_CHECKING

import tomllib

import pytest

from smelt.config import (
    select_smelt_table,
    CythonExtension,
    MypycModule,
    NuitkaModule,
    SmeltConfig,
    TomlData,
    build_datacls_from_toml,
)
from smelt.utils import SmeltConfigError, TomlData

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


@pytest.mark.parametrize("ConfigCls", [NuitkaModule, MypycModule])
@pytest.mark.parametrize(
    ["toml_data", "valid"],
    [
        ({"import_path": "a.b.c"}, True),
        ({"import_path": "a.$.c"}, False),
        ({"import_path": "a.$./"}, False),
        ({"import_path": ""}, False),
    ],
)
def test_config_conversion_import_path(
    ConfigCls: type[NuitkaModule | MypycModule],
    toml_data: TomlData,
    valid: bool,
) -> None:
    if valid:
        datacls = build_datacls_from_toml(ConfigCls, toml_data=toml_data)
        assert datacls.import_path == toml_data["import_path"]
    else:
        with pytest.raises(SmeltConfigError):
            build_datacls_from_toml(ConfigCls, toml_data=toml_data)


# ---------------------------------------------------------------------------
# User-facing schema: strict validation and alias layer
# ---------------------------------------------------------------------------

import tomllib
import warnings
from pathlib import Path

from smelt.config import NuitkaBinaryOptions
from smelt.schema import ProjectConfig, SmeltDeprecationWarning, parse_project_config


def _parse(toml: str, project_root: Path | None = None) -> ProjectConfig:
    return parse_project_config(tomllib.loads(toml), project_root=project_root)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('compresion = "xz"', r"\[tool\.smelt\]: unknown key 'compresion'"),
        ('[bundle]\npythn = "own"', r"\[tool\.smelt\.bundle\]: unknown key 'pythn' \(did you mean 'python'"),
        (
            '[bundle.onefile]\ncompresion = "xz"',
            r"\[tool\.smelt\.bundle\.onefile\]: unknown key 'compresion' \(did you mean 'compression'",
        ),
        ('[bundle.entrypoints.greet.content]\nbogus = 1', r"bundle\.entrypoints\.greet\.content\]: unknown key"),
        ('[nuitka-binary]\nzigg = true', r"\[tool\.smelt\.nuitka-binary\]: unknown key 'zigg'"),
        ('[extensions.auto]\nmod = "all"', r"extensions\.auto\]: unknown key 'mod'"),
        # internal fields are not user-settable
        ('dependencies = ["x"]', r"unknown key 'dependencies'"),
        ('script-names = {}', r"unknown key 'script-names'"),
    ],
)
def test_unknown_keys_are_reported_at_every_level(toml: str, message: str) -> None:
    with pytest.raises(SmeltConfigError, match=message):
        _parse(toml)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[bundle]\noptimize = 3", r"optimize: expected one of 0, 1, 2"),
        ("[bundle]\noptimize = true", r"optimize: expected one of"),
        ('[bundle]\nguard-version = "yes"', r"guard-version: expected a boolean"),
        ('[bundle.onefile]\ncompression = "zip"', r"compression: expected one of 'xz', 'gzip', 'none'"),
        ('[bundle.onefile]\ncompression-preset = "6"', r"expected an integer"),
        ('[bundle.onefile]\ncompression-preset = true', r"expected an integer"),
        ('[bundle.isolated-build]\nversions = "lock"', r"versions: expected one of 'local', 'pyproject'"),
        ('[bundle.content]\npackage-data = { pkg = "*.json" }', r"expected an array"),
        ('[bundle.content]\ninclude-modules = "a"', r"expected an array"),
        ('[extensions]\nmypyc = ["a.b", 3]', r"expected an import path or a table"),
        ('[extensions.auto]\nbackends = ["gcc"]', r"expected one of"),
        ('supported-targets = "x86_64-linux-*"', r"expected an array"),
        ('[extensions]\nmypyc = ["a.$b"]', r"valid Python import path"),
    ],
)
def test_wrong_types_are_reported(toml: str, message: str) -> None:
    with pytest.raises(SmeltConfigError, match=message):
        _parse(toml)


def test_new_schema_parses_in_full(tmp_path: Path) -> None:
    for name in ("fib.py", "hello.c", "fast.pyx"):
        (tmp_path / name).touch()
    config = _parse(
        """
package-dirs = { pkg = "src/pkg" }
supported-targets = ["*-linux-*"]

[extensions]
mypyc = ["pkg.fib", { import-path = "pkg.utils", source = "fib.py", extras = ["fast"] }]
cython = ["pkg.fast"]

[[extensions.c]]
import-path = "pkg.hello"
sources = ["hello.c"]

[[extensions.zig]]
import-path = "pkg.zighello"

[[executables.zig]]
name = "mytool"

[extensions.auto]
mode = "package"
backends = ["nuitka", "mypyc"]

[bundle]
python = "own"
optimize = 2
supported-targets = ["x86_64-linux-musl"]

[bundle.content]
package-data = { pkg = ["*.json", "*.yaml"] }

[bundle.onefile]
enabled = true
compression = "gzip"

[bundle.entrypoints.greet.onefile]
enabled = false

[nuitka-binary]
extra-flags = ["--lto=yes"]
zig = false
""",
        project_root=tmp_path,
    )
    assert [m.import_path for m in config.extensions.mypyc] == ["pkg.fib", "pkg.utils"]
    assert config.extensions.mypyc[1].source == tmp_path / "fib.py"
    assert config.extensions.mypyc[1].extras == ["fast"]
    assert config.extensions.zig[0].name is None
    assert config.extensions.auto.backends == ["nuitka", "mypyc"]
    assert config.bundle.content.package_data == {"pkg": ["*.json", "*.yaml"]}
    assert config.bundle.entrypoints["greet"].onefile.enabled is False
    assert config.bundle.entrypoints["greet"].python is None
    assert config.nuitka_binary.zig is False


def test_missing_mandatory_key_is_reported() -> None:
    with pytest.raises(SmeltConfigError, match=r"extensions\.c\[0\]\]: missing mandatory key 'import-path'"):
        _parse('[[extensions.c]]\nsources = []')


OLD_STYLE_CONFIG = """
packages_location = { pkg = "src/pkg" }
auto_mode = "package"
backend_priority_order = ["mypyc", "nuitka"]
mypyc_options = {}

[[mypyc_modules]]
import_path = "pkg.fib"

[[c_extensions]]
import_path = "pkg.hello"
sources = ["hello.c"]

[[zig_modules]]
name = "zh"
import_path = "pkg.zh"

[[zig_executables]]
name = "mytool"

[entrypoints."pkg.cli:main"]
extra_flags = ["--lto=yes"]
no-zig = true
include-package = ["pkg.plugins"]
include-package-data = ["pkg:*.json,*.yaml"]

[entrypoints."pkg.other:main"]
"""


def test_old_style_config_loads_identically_with_warnings(tmp_path: Path) -> None:
    (tmp_path / "hello.c").touch()
    with pytest.warns(SmeltDeprecationWarning) as recorded:
        config = _parse(OLD_STYLE_CONFIG, project_root=tmp_path)
    messages = [str(w.message) for w in recorded]
    for old in (
        "packages_location",
        "mypyc_modules",
        "c_extensions",
        "zig_modules",
        "zig_executables",
        "auto_mode",
        "backend_priority_order",
        "mypyc_options",
        "entrypoints.pkg.cli:main.extra_flags",
    ):
        assert any(f"`{old}`" in m for m in messages), old
    assert config.package_dirs == {"pkg": "src/pkg"}
    assert [m.import_path for m in config.extensions.mypyc] == ["pkg.fib"]
    assert config.extensions.auto.mode == "package"
    assert config.extensions.auto.backends == ["mypyc", "nuitka"]
    assert config.extensions.zig[0].name == "zh"
    assert config.executables.zig[0].name == "mytool"
    nuitka = config.nuitka_binary.entrypoints["pkg.cli:main"]
    assert nuitka.extra_flags == ["--lto=yes"]
    assert nuitka.zig is False
    assert nuitka.include_packages == ["pkg.plugins"]
    assert nuitka.package_data == {"pkg": ["*.json", "*.yaml"]}
    # a bare declaration still declares an entrypoint
    assert "pkg.other:main" in config.bundle.entrypoints


def test_each_deprecated_key_warns_once() -> None:
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _parse('[[mypyc_modules]]\nimport_path = "a.b"\n[[mypyc_modules]]\nimport_path = "a.c"')
    deprecated = [w for w in recorded if issubclass(w.category, SmeltDeprecationWarning)]
    # one for `mypyc_modules`, one for the snake_case `import_path` spelling
    assert len(deprecated) == 2


def test_old_and_new_spelling_together_is_an_error() -> None:
    with pytest.raises(SmeltConfigError, match="both 'auto_mode'"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _parse('auto_mode = "all"\n[extensions.auto]\nmode = "off"')


def test_bundler_options_under_old_entrypoints_table_point_to_the_new_one() -> None:
    with pytest.raises(SmeltConfigError, match=r"bundle\.entrypoints\.greet"):
        _parse('[entrypoints.greet]\npython = "own"')


def test_old_config_lowers_to_same_internal_config(tmp_path: Path) -> None:
    (tmp_path / "hello.c").touch()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SmeltConfig.from_toml_data(
            tomllib.loads(OLD_STYLE_CONFIG),
            project_root=tmp_path,
            project_scripts={"cli": "pkg.cli:main"},
        )
    assert config.packages_location == {"pkg": "src/pkg"}
    assert config.backend_priority_order == ["mypyc", "nuitka"]
    assert config.zig_modules[0].name == "zh"
    assert config.nuitka_binary["pkg.cli:main"] == NuitkaBinaryOptions(
        extra_flags=["--lto=yes"],
        zig=False,
        include_packages=["pkg.plugins"],
        package_data={"pkg": ["*.json", "*.yaml"]},
    )
    assert set(config.entrypoints) == {"pkg.cli:main", "pkg.other:main"}


def test_entrypoint_levels_are_kept_unmerged() -> None:
    config = SmeltConfig.from_toml_data(
        tomllib.loads(
            """
supported-targets = ["*-linux-*"]
[bundle]
python = "own"
[bundle.content]
include-modules = ["a"]
[bundle.entrypoints.cli.content]
include-modules = ["b"]
"""
        ),
        project_scripts={"cli": "pkg.cli:main", "other": "pkg.other:main"},
    )
    cli_levels = config.entrypoints["pkg.cli:main"]
    assert [level.source for level in cli_levels] == [
        "[tool.smelt]",
        "[tool.smelt.bundle]",
        "[tool.smelt.bundle.entrypoints.cli]",
    ]
    assert cli_levels[0].settings is not None
    assert cli_levels[0].settings.supported_targets == ["*-linux-*"]
    assert cli_levels[2].settings is not None
    assert cli_levels[2].settings.content.include_modules == ["b"]
    # an entrypoint without its own table only has the shared levels
    assert config.entrypoints["pkg.other:main"][2].settings is None


def test_nuitka_options_are_not_shared_with_the_bundler() -> None:
    config = SmeltConfig.from_toml_data(
        tomllib.loads('[nuitka-binary]\ninclude-modules = ["n"]\n[bundle.content]\ninclude-modules = ["b"]'),
        project_scripts={"cli": "pkg.cli:main"},
    )
    (levels,) = [
        level for level in config.entrypoints["pkg.cli:main"] if level.source == "[tool.smelt.bundle]"
    ]
    assert levels.settings is not None
    assert levels.settings.content.include_modules == ["b"]
    assert config.nuitka_binary["pkg.cli:main"].include_modules == ["n"]


def test_select_smelt_table_prefers_tool_smelt_with_empty_hook() -> None:
    data = tomllib.loads(
        '[tool.hatch.build.hooks.smelt]\n\n[tool.smelt]\npackage-dirs = { pkg = "src/pkg" }\n'
    )
    assert select_smelt_table(data) == {"package-dirs": {"pkg": "src/pkg"}}


def test_select_smelt_table_reads_legacy_hook_table_with_warning() -> None:
    data = tomllib.loads('[tool.hatch.build.hooks.smelt]\npackages_location = { pkg = "src/pkg" }\n')
    with pytest.warns(DeprecationWarning, match="hooks.smelt"):
        table = select_smelt_table(data)
    assert table == {"packages_location": {"pkg": "src/pkg"}}


def test_select_smelt_table_rejects_settings_in_both_tables() -> None:
    data = tomllib.loads(
        '[tool.hatch.build.hooks.smelt]\npackages_location = { a = "a" }\n'
        '[tool.smelt]\npackage-dirs = { a = "a" }\n'
    )
    with pytest.raises(SmeltConfigError, match="both"):
        select_smelt_table(data)


def test_select_smelt_table_requires_a_table() -> None:
    with pytest.raises(SmeltConfigError, match="No smelt config"):
        select_smelt_table(tomllib.loads('[project]\nname = "x"\n'))


def test_auto_backends_accepts_bytecode() -> None:
    from smelt.schema import parse_project_config

    config = parse_project_config({"extensions": {"auto": {"backends": ["nuitka", "bytecode"]}}})
    assert config.extensions.auto.backends == ["nuitka", "bytecode"]
