"""
Config definitions for Smelt.

@date: 19.02.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Self, TypedDict

from smelt.utils import (
    ImportPath,
    PackageRootPath,
    PathExists,
    PathSolver,
    SmeltConfigError,
    SmeltError,
    assert_is_valid_import_path,
    assert_path_exists,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def assert_type_is(obj: object, typ: type) -> None:
    if not isinstance(obj, typ):
        raise SmeltConfigError(f"Expected type {typ}, got {type(obj)}")


def convert_path(
    path_decl: str | list[str], type_hint: str, project_root: Path | None
) -> ImportPath | PathExists | Path | list[PathExists]:
    if type_hint == "ImportPath":
        assert_type_is(path_decl, str)
        return assert_is_valid_import_path(path_decl)

    match type_hint:
        case "PathExists" | "PathExists | None":
            assert_type_is(path_decl, str)
            path = project_root / path_decl if project_root is not None else Path(path_decl)

            assert_path_exists(path)
            return path
        case "list[PathExists]":
            assert_type_is(path_decl, list)
            return [
                assert_path_exists(project_root / p if project_root else Path(p)) for p in path_decl
            ]

        case "list[str]":
            assert_type_is(path_decl, list)
            return path_decl

        case "str":
            assert_type_is(path_decl, str)
            return path_decl

        case _:
            assert_type_is(path_decl, str)
            raise NotImplementedError(f"Unsupported type hint: {type_hint}")


type ConfigContext = Iterable[str]


def _format_context(context: ConfigContext) -> str:
    """
    A small helper that builds a human-friendly hint from
    the path of a parameter in the config.
    Meant to be used when reporting config errors.
    """
    if not context:
        return ""

    *nodes, leaf = context

    param_path = ".".join(nodes) + ":" + leaf if nodes else leaf
    return f"In [{param_path}]:"


def build_datacls_from_toml[T: DataclassInstance](
    datacls: type[T],
    toml_data: _TomlData,
    context: ConfigContext | None = None,
    project_root: Path | None = None,
) -> T:
    """
    Builds one `datacls` out of the TOML table `toml_data`, resolving its path fields
    against `project_root` (see `convert_path`).

    `toml_data` is typed as any TOML value rather than as a table, because it *is* any
    TOML value: it comes from a file the user wrote. A declaration in some other shape
    has to be reported as a configuration error, which is what the first check does --
    the alternative is an `AttributeError` from deep inside this function, naming
    nothing the reader can act on. The shape that actually reaches it is the
    `module = "source"` mapping module declarations used before they became arrays of
    tables: iterating a table yields its keys, so each "declaration" arrives here as a
    bare string.
    """
    context = context if context is not None else []
    if not isinstance(toml_data, dict):
        field_names = ", ".join(f.name for f in fields(datacls))
        raise SmeltConfigError(
            f"{_format_context(context)}Expected a table declaring {field_names}, "
            f"found {toml_data!r}. Modules are declared one array-of-tables entry "
            "each, e.g. `[[tool.smelt.c_extensions]]` followed by `import_path = "
            '"pkg.mod"` and `sources = ["src/pkg/mod.c"]`, and not as a '
            '`module = "source"` mapping.'
        )
    sentinel = object()
    kwargs: dict[str, object] = {}
    for f in fields(datacls):
        local_ctx = list(context)
        field_name = f.name
        value_decl = toml_data.get(field_name, sentinel)
        if value_decl is sentinel:
            if f.default is MISSING and f.default_factory is None:
                raise SmeltConfigError(
                    f"{_format_context(local_ctx)}Missing mandatory argument: {f.name}"
                )
            continue
        local_ctx.append(f.name)
        assert isinstance(f.type, str), "Expected annotations from __future__ to be used"
        try:
            value = convert_path(value_decl, f.type, project_root)
        except SmeltError as exc:
            raise SmeltConfigError(f"{_format_context(local_ctx)}{exc}") from exc
        kwargs[field_name] = value
    return datacls(**kwargs)


type _TomlData = str | list[_TomlData] | dict[str, _TomlData]
type TomlData = dict[str, _TomlData]


def auto_detect_is_build_hook(toml_data: TomlData) -> bool:
    """
    Given the extracted TOML config `toml_data`,
    detects whether Smelt Config was passed as a build hook
    (in which case it would be nested under whatever subsection_name
    the build backend uses for hooks), or a tool config.
    """
    has_tool_config = "smelt" in toml_data.get("tool", {})
    has_build_hook_conf = "smelt" in toml_get_nested_section(
        toml_data, "tool", "hatch", "build", "hooks"
    )
    if has_tool_config and has_build_hook_conf:
        # TODO: for now, not allowing this.
        # We can however consider using the hatch one only for build time
        # and the tool one for CLI use.
        # that can get confusing though.
        raise SmeltConfigError(
            "Smelt configuration found both in [tool.smelt] and "
            "[tool.hatch.build.hooks.smelt]. Please keep only one."
        )
    if has_build_hook_conf:
        return True
    if has_tool_config:
        return False
    raise SmeltConfigError("No smelt config detected")


def toml_get_nested_section(toml_data: TomlData, *path: str) -> _TomlData:
    """
    Extracts the sub section given by `path` from `toml_data`.
    Verifies that the extracted TOML object is a dictionary.

    Raises
    ------
    SmeltConfigError
        If the section is not found or if the found object is not a section.
    """
    ctx: list[str] = []
    section: _TomlData = toml_data
    for subsection_name in path:
        ctx.append(subsection_name)
        section = section.get(subsection_name, {})
        if not isinstance(section, dict):
            raise SmeltConfigError(f"{_format_context(ctx)}Expected section, found {section}")
    return section


class Backend(StrEnum):
    """
    A pure-python-to-native compilation backend, selectable via `backend_priority_order`.
    """

    NUITKA = "nuitka"
    MYPYC = "mypyc"
    CYTHON = "cython"


type AutoMode = Literal["off", "package", "all"]


@dataclass
class NuitkaModule:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class NativeExtension:
    import_path: ImportPath
    sources: list[PathExists]
    extras: list[str] = field(default_factory=list)


@dataclass
class CythonExtension:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class MypycModule:
    import_path: ImportPath
    source: PathExists | None = None
    extras: list[str] = field(default_factory=list)


@dataclass
class ZigModule:
    name: str
    import_path: ImportPath
    folder: PathExists = assert_path_exists(".")
    flags: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


EntrypointOptions = TypedDict(
    "EntrypointOptions",
    {
        "include-modules": list[str],
        "include-package": list[str],
        "include-package-data": list[str],
        "extra_flags": list[str],
        "no-zig": bool,
        # Distribution-folder options (`smelt build-dist`, see `smelt.dist`).
        # `discovery` is one of "static", "trace" or "both".
        "discovery": str,
        "exclude-modules": list[str],
        "include-distribution-metadata": list[str],
        # `python` is one of "byo" (bring your own interpreter, the default) or "own"
        # (ship one smelt builds itself, so the folder needs no Python installed).
        "python": str,
        # Zig target triple for the `python = "own"` interpreter build; omitted means
        # a native build against the host's own libc.
        "own-python-target": str,
        # Whether the `python = "own"` interpreter's contents follow this entrypoint's
        # dependency closure (the default) instead of being the whole standard
        # library. See `smelt.dist.DEFAULT_TAILOR_INTERPRETER` for the trade-off.
        "tailor-interpreter": bool,
        "drop-stdlib-groups": list[str],
        # Whether to leave out the modules only reachable through an import their own
        # importer already handles the failure of. Off by default: every one of them is
        # droppable without an `ImportError`, but what the fallback costs is not
        # knowable from here. See `smelt.dist.DEFAULT_DROP_OPTIONAL_IMPORTS`.
        "drop-optional-imports": bool,
        # Whether the finished folder is additionally packed into a single file (see
        # `smelt.onefile`). The shape follows `python`: an executable zip application
        # for "byo", a compiled launcher carrying the compressed folder for "own".
        "onefile": bool,
        # How that single file's payload is compressed: "xz" (the default, smallest),
        # "gzip" (faster to inflate on the target's first run) or "none".
        "onefile-compression": str,
    },
    total=False,
)


@dataclass
class SmeltConfig:
    """
    Defines how the smelt backend should run
    """

    packages_location: dict[str, str] = field(default_factory=dict)
    mypyc_options: dict[str, Any] = field(default_factory=dict)
    mypyc_modules: list[MypycModule] = field(default_factory=list)
    cython_options: dict[str, Any] = field(default_factory=dict)
    cython_modules: list[CythonExtension] = field(default_factory=list)
    nuitka_modules: list[NuitkaModule] = field(default_factory=list)
    c_extensions: list[NativeExtension] = field(default_factory=list)
    zig_modules: list[ZigModule] = field(default_factory=list)
    platforms: Iterable[str] | None = None
    entrypoints: dict[str, EntrypointOptions] = field(default_factory=dict)
    script_names: dict[str, str] = field(default_factory=dict)
    debug: bool = False
    auto_mode: AutoMode = "off"
    backend_priority_order: list[Backend] = field(default_factory=lambda: [Backend.NUITKA])
    report_path: str | None = None

    @classmethod
    def from_toml_data(
        cls,
        toml_data: dict[str, Any],
        project_root: Path | None = None,
        project_scripts: dict[str, str] | None = None,
    ) -> Self:
        # operate on a copy: callers (e.g. the hatch build hook) keep their own
        # reference to `toml_data` around for error reporting after this call.
        toml_data = dict(toml_data)

        # entrypoints: every `[project.scripts]` target is picked up automatically
        # (default options), `entrypoints` declarations on top of that only customize
        # options for one of them, or declare additional entrypoints of their own.
        entrypoints: dict[str, EntrypointOptions] = {}
        script_names: dict[str, str] = {}
        for name, target in (project_scripts or {}).items():
            entrypoints[target] = EntrypointOptions()
            script_names[name] = target
        entrypoints_decl_raw: dict[str, EntrypointOptions] = toml_data.pop("entrypoints", {})
        # a declaration keyed by a `[project.scripts]` name (e.g. `entrypoints.afpu`
        # for `afpu = "pkg.cli:afpu"`) customizes that script's auto-added target --
        # resolve it upfront so it lines up with `entrypoints`/`script_names` below
        # exactly like a declaration spelled out as "pkg.cli:afpu" would.
        entrypoints_decl: dict[str, EntrypointOptions] = {
            script_names.get(key, key): value for key, value in entrypoints_decl_raw.items()
        }
        for explicit_key in entrypoints_decl:
            if ":" in explicit_key:
                # already a full `module.path:func_name` spec, so it matches (at
                # most) one auto-added entry exactly by key -- `entrypoints.update`
                # below overwrites that entry's options directly. Hunting for other
                # auto-added entries sharing its module path would wrongly sweep up
                # unrelated sibling scripts from the same module (e.g. two distinct
                # `[project.scripts]` targets both living in `pkg.cli`).
                continue
            # bare module path (the pre-`[project.scripts]` convention, e.g. a
            # declaration keyed `"pkg.cli"` customizing the auto-added
            # `"pkg.cli:func"`) -- drop the auto entry so it isn't built a second
            # time, unconfigured, alongside the explicit one.
            explicit_module_path = explicit_key
            for auto_key in [
                key
                for key in entrypoints
                if key != explicit_key and key.partition(":")[0] == explicit_module_path
            ]:
                del entrypoints[auto_key]
                for name, target in script_names.items():
                    if target == auto_key:
                        script_names[name] = explicit_key
        entrypoints.update(entrypoints_decl)
        # native code
        native_extensions_decl = toml_data.pop("c_extensions", [])
        native_extensions = [
            build_datacls_from_toml(
                NativeExtension, decl, context=["c_extensions"], project_root=project_root
            )
            for decl in native_extensions_decl
        ]
        # zig modules
        zig_modules_decl = toml_data.pop("zig_modules", [])
        zig_modules = [
            build_datacls_from_toml(
                ZigModule, decl, context=["zig_modules"], project_root=project_root
            )
            for decl in zig_modules_decl
        ]
        # mypyc modules
        mypyc_modules_decl = toml_data.pop("mypyc_modules", [])
        mypyc_modules = [
            build_datacls_from_toml(
                MypycModule, decl, context=["mypyc_modules"], project_root=project_root
            )
            for decl in mypyc_modules_decl
        ]

        # cython
        cython_modules_decl = toml_data.pop("cython_modules", [])
        cython_modules = [
            build_datacls_from_toml(
                CythonExtension, decl, context=["cython_modules"], project_root=project_root
            )
            for decl in cython_modules_decl
        ]
        # nuitka
        nuitka_modules_decl = toml_data.pop("nuitka_modules", [])
        nuitka_modules = [
            build_datacls_from_toml(
                NuitkaModule, decl, context=["nuitka_modules"], project_root=project_root
            )
            for decl in nuitka_modules_decl
        ]

        # auto discovery mode
        auto_mode: AutoMode = toml_data.pop("auto_mode", "off")
        if auto_mode not in ("off", "package", "all"):
            raise SmeltConfigError(
                f"Invalid auto_mode: {auto_mode!r}. Expected one of 'off', 'package', 'all'."
            )

        # backend fallback order, used for modules discovered by auto_mode
        backend_priority_order_decl = toml_data.pop("backend_priority_order", ["nuitka"])
        assert_type_is(backend_priority_order_decl, list)
        try:
            backend_priority_order = [Backend(name) for name in backend_priority_order_decl]
        except ValueError as exc:
            raise SmeltConfigError(f"Invalid backend in backend_priority_order: {exc}") from exc

        # Whatever is left is passed through as a plain option below, so an option that
        # is not one reaches `cls(**toml_data)` and comes back out as
        # `TypeError: SmeltConfig.__init__() got an unexpected keyword argument`. That
        # names the internals rather than the file the reader has to edit, and the
        # option most likely to land here is the singular `entrypoint` that
        # `[project.scripts]`/`[tool.smelt.entrypoints]` replaced.
        unknown = sorted(set(toml_data) - {f.name for f in fields(cls)})
        if unknown:
            raise SmeltConfigError(
                f"Unknown option(s) in [tool.smelt]: {unknown}. Modules are declared "
                "in their own sections ([[tool.smelt.mypyc_modules]], "
                "[[tool.smelt.c_extensions]], [[tool.smelt.cython_modules]], "
                "[[tool.smelt.nuitka_modules]], [[tool.smelt.zig_modules]]), and "
                "entrypoints in [project.scripts] or [tool.smelt.entrypoints] -- which "
                "is what the singular `entrypoint` option was replaced by."
            )

        return cls(
            mypyc_modules=mypyc_modules,
            c_extensions=native_extensions,
            zig_modules=zig_modules,
            cython_modules=cython_modules,
            nuitka_modules=nuitka_modules,
            auto_mode=auto_mode,
            backend_priority_order=backend_priority_order,
            entrypoints=entrypoints,
            script_names=script_names,
            **toml_data,
        )

    def get_path_solver(self, project_root: Path | None = None) -> PathSolver:
        """
        Builds a PathSolver based on the package configuration.
        """
        root = project_root or Path.cwd()
        return PathSolver(
            known_roots=[
                PackageRootPath(alias, assert_path_exists(root / path))
                for alias, path in self.packages_location.items()
            ],
            project_root=root,
        )

    def __str__(self) -> str:
        """
        A human-friendly stringified version of this config.
        """
        lines: list[str] = []
        for field_name, value in asdict(self).items():
            if isinstance(value, list):
                value = ",".join(value)
            if isinstance(value, dict):
                value = "".join(("\n * " + f"{key} -> {val}" for key, val in value.items()))
            lines.append(f"{field_name:20}: {value}")
        return "\n".join(lines)

    def load_env(self) -> None:
        """
        Updates internal values based on set environement variables.
        """
        if os.environ.get("SMELT_DEBUG"):
            self.debug = True
        if report_path := os.environ.get("SMELT_REPORT"):
            self.report_path = report_path
