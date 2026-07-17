# Build Hook

The primary way to use Smelt is as a [Hatchling](https://hatch.pypa.io/) build hook. Once configured, every `pip install` / `build` of your project automatically compiles the native extensions and entrypoints you declared - no manual step required, and no extra system dependency (Smelt uses Zig as its compiler, which ships as a standalone toolchain through the `ziglang` PyPI package).

## Minimal setup

Add `smelt` to your build requirements, select `hatchling` as the build backend, and declare the `smelt` build hook:

```toml
[build-system]
requires = ["hatchling", "smelt"]
build-backend = "hatchling.build"

[project]
name = "my-project"
version = "0.1.0"
dependencies = ["click"]

[project.scripts]
my-cli = "my_project.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/my_project"]

[tool.hatch.build.hooks.smelt]
packages_location = { "my_project" = "src/my_project" }

[[tool.hatch.build.hooks.smelt.mypyc_modules]]
import_path = "my_project.fib"

[[tool.hatch.build.hooks.smelt.nuitka_modules]]
import_path = "my_project.cli"

[[tool.hatch.build.hooks.smelt.c_extensions]]
import_path = "my_project.hello"
sources = ["src/my_project/hello.c"]

[[tool.hatch.build.hooks.smelt.zig_modules]]
import_path = "my_project.zighello"
name = "zighello"
folder = "src/my_project/zighello"

[[tool.hatch.build.hooks.smelt.cython_modules]]
import_path = "my_project.fib_cython"
source = "src/my_project/fib_cython.pyx"
```

Every module is compiled to a native `.so`/`.pyd` sitting right next to its pure-Python counterpart, which it then shadows - so the rest of your codebase, tooling, and imports don't need to change.

## Configuration reference

All of the following keys live under `[tool.hatch.build.hooks.smelt]`.

| Key | Type | Description |
| --- | --- | --- |
| `packages_location` | `dict[str, str]` | Maps a package's top-level import name to the folder it lives in (e.g. `{"my_project" = "src/my_project"}`). Used to resolve `import_path`s declared below to actual files on disk. |
| `mypyc_modules` | list of tables | Modules to compile with [mypyc](https://mypyc.readthedocs.io/). Each entry has an `import_path` and an optional `source` (path override) and `extras`. |
| `cython_modules` | list of tables | Modules to compile with [Cython](https://cython.org/). Each entry has an `import_path`, an optional `source` (defaults to a `.py`/`.pyx` file matching the import path) and `extras`. |
| `nuitka_modules` | list of tables | Modules to compile with [Nuitka](https://nuitka.net/), as thin per-module extensions sharing one runtime library. See [Nuitka internals](advanced.md#nuitka-internals) for why this needs special handling. Each entry has an `import_path`, an optional `source`, and `extras`. |
| `c_extensions` | list of tables | Handwritten C/C++ extensions, compiled with Zig's C compiler (`zig cc`). Each entry has an `import_path`, a `sources` list, and `extras`. |
| `zig_modules` | list of tables | Handwritten Zig extensions, built with `zig build`. Each entry has an `import_path`, a `name` (the library name Zig produces), a `folder` (containing a `build.zig`), optional `flags` passed to `zig build`, and `extras`. |
| `entrypoints` | table of tables | Customizes the standalone-binary build for one entrypoint (see [CLI](cli.md#build-standalone-binary)). Keyed by the script name from `[project.scripts]`, or by an explicit `module.path`/`module.path:func`. Every `[project.scripts]` entry is picked up automatically with default options; declaring it here only lets you override those options (`include-modules`, `include-package`, `include-package-data`, `extra_flags`) or declare an additional entrypoint that isn't in `[project.scripts]`. |
| `auto_mode` | `"off"` \| `"package"` \| `"all"` | Instead of listing modules by hand, walk the package tree and propose every `.py` file as a compile target (see limitation below). |
| `backend_priority_order` | list of `"nuitka"`/`"mypyc"`/`"cython"` | Fallback order used to pick a backend for modules discovered by `auto_mode`. Defaults to `["nuitka"]`. |
| `platforms` | list of str | Restricts the hook to run only on the listed host platforms; the build is skipped (not failed) on any other platform. |
| `debug` | bool | Enables verbose logging from the build hook. Also settable via the `SMELT_DEBUG` environment variable. |
| `report_path` | str | If set, writes a build report to this path once the hook completes. Also settable via the `SMELT_REPORT` environment variable. |

### `extras`-gated modules

Any of the module tables above (`mypyc_modules`, `cython_modules`, `nuitka_modules`, `c_extensions`, `zig_modules`) accepts an `extras` list. If any of the listed `[project.optional-dependencies]` extras isn't installed in the current environment, the build hook silently skips that module instead of failing - handy for optional native acceleration that shouldn't block a plain install:

```toml
[[tool.hatch.build.hooks.smelt.mypyc_modules]]
import_path = "my_project.fast_path"
extras = ["speedups"]
```

### `auto_mode` and packages

`auto_mode` (`"package"`/`"all"`) currently never proposes a package's own `__init__.py`, at any depth - only its concrete submodules. This isn't an oversight: see [Nuitka internals](advanced.md#nuitka-internals) for why whole-package compilation doesn't fit Smelt's per-module model yet.

## Standalone `[tool.smelt]` config

If you don't use Hatchling, the same schema can be declared under `[tool.smelt]` instead of `[tool.hatch.build.hooks.smelt]`. This form isn't wired into any build backend automatically - it's read by the [CLI](cli.md) (`smelt build-extensions`, `smelt build-standalone-binary`, ...) so you can drive the build yourself, e.g. from a `Makefile` or CI step. The two forms are mutually exclusive: Smelt raises an error if both are present in the same `pyproject.toml`.
