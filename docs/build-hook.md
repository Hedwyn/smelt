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

[tool.smelt]
package-dirs = { "my_project" = "src/my_project" }

[tool.smelt.extensions]
mypyc = ["my_project.fib"]
nuitka = ["my_project.cli"]
cython = [
  { import-path = "my_project.fib_cython", source = "src/my_project/fib_cython.pyx" },
]

[[tool.smelt.extensions.c]]
import-path = "my_project.hello"
sources = ["src/my_project/hello.c"]

[[tool.smelt.extensions.zig]]
import-path = "my_project.zighello"
folder = "src/my_project/zighello"
```

Every module is compiled to a native `.so`/`.pyd` sitting right next to its pure-Python counterpart, which it then shadows - so the rest of your codebase, tooling, and imports don't need to change.

## Configuration reference

All of the following keys live under `[tool.smelt]`. The `[tool.hatch.build.hooks.smelt]` table stays empty: it only enables the hook.

| Key | Type | Description |
| --- | --- | --- |
| `package-dirs` | `dict[str, str]` | Maps a package's top-level import name to the folder it lives in (e.g. `{"my_project" = "src/my_project"}`). Used to resolve `import_path`s declared below to actual files on disk. |
| `extensions.mypyc` | list of import paths or tables | Modules to compile with [mypyc](https://mypyc.readthedocs.io/). Each entry is a bare import path, or a table with `import-path` and optional `source` (path override) and `extras`. |
| `extensions.cython` | list of import paths or tables | Modules to compile with [Cython](https://cython.org/). Same entry shape as `mypyc`; `source` defaults to a `.py`/`.pyx` file matching the import path. |
| `extensions.nuitka` | list of import paths or tables | Modules to compile with [Nuitka](https://nuitka.net/), as thin per-module extensions sharing one runtime library. See [Nuitka internals](advanced.md#nuitka-internals). Same entry shape as `mypyc`. |
| `extensions.c` | array of tables | Handwritten C/C++ extensions, compiled with Zig's C compiler (`zig cc`). Each entry has an `import-path`, a `sources` list, and `extras`. |
| `extensions.zig` | array of tables | Handwritten Zig extensions, built with `zig build`. Each entry has an `import-path`, an optional `name` (library name Zig produces; defaults to the last part of `import-path`), a `folder` (containing a `build.zig`, defaults to the project root), optional `flags` passed to `zig build`, and `extras`. |
| `executables.zig` | array of tables | Executables produced by the project's own `build.zig`, installed as wheel scripts. Each entry has a `name`, optional `exe`, `folder`, `flags` and `extras`. |
| `nuitka-binary` | table | Options of the Nuitka whole-program build (`smelt nuitka-binary`, see [CLI](cli.md#nuitka-binary)): `extra-flags`, `zig`, `include-modules`, `include-packages`, `package-data`. Per-entrypoint overrides go in `[tool.smelt.nuitka-binary.entrypoints.<ep>]`, keyed by the `[project.scripts]` name or an explicit `module.path[:func]`; lists append to the project-wide ones, scalars replace. Bundler settings are separate: see `[tool.smelt.bundle]` in the [Bundler: Reference](bundler-reference.md). |
| `extensions.auto.mode` | `"off"` \| `"package"` \| `"all"` | Instead of listing modules by hand, walk the package tree and propose every `.py` file as a compile target (see limitation below). |
| `extensions.auto.backends` | list of `"nuitka"`/`"mypyc"`/`"cython"`/`"bytecode"` | Backends tried in order, per module discovered by auto mode. Defaults to `["nuitka"]`. `"bytecode"` compiles nothing: the module stays pure Python (shipped as `.pyc` by `smelt bundle`, left as source in a wheel). Listing it last means auto mode never fails a build; `["bytecode"]` alone compiles nothing. |
| `supported-targets` | list of str | Zig-triple patterns (`x86_64-linux-*`, `*-linux-*`, `host`). The native build is skipped (not failed) for any target not matching, host included. Also written into the package's manifest, so downstream cross-builds skip it too. Replaces `platforms` and `archs`. |

The hook has no command line: set `SMELT_DEBUG` (verbose logging) and `SMELT_REPORT` (path of the auto-mode report, written when the hook completes) in the environment. The CLI reads the same variables; `-l/--log-level` and `--report` win over them.

### `extras`-gated modules

Any of the module tables above (`extensions.mypyc`, `.cython`, `.nuitka`, `.c`, `.zig`) accepts an `extras` list. If any of the listed `[project.optional-dependencies]` extras isn't installed in the current environment, the build hook silently skips that module instead of failing - handy for optional native acceleration that shouldn't block a plain install:

```toml
[tool.smelt.extensions]
mypyc = [{ import-path = "my_project.fast_path", extras = ["speedups"] }]
```

### Auto mode and packages

Auto mode (`"package"`/`"all"`) currently never proposes a package's own `__init__.py`, at any depth - only its concrete submodules. This isn't an oversight: see [Nuitka internals](advanced.md#nuitka-internals) for why whole-package compilation doesn't fit Smelt's per-module model yet.

## Configuration location

The hook and the [CLI](cli.md) read the same `[tool.smelt]` table. Settings written under `[tool.hatch.build.hooks.smelt]` by earlier releases are still read, with a deprecation warning, when `[tool.smelt]` is absent; having settings in both tables is an error.
