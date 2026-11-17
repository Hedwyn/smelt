# CLI

Installing Smelt with the `cli` extra (`pip install "smelt[cli]"`) provides the `smelt` command-line tool. It exposes everything the [build hook](build-hook.md) does, but callable directly - useful outside of a Hatchling build (e.g. producing a standalone binary, or driving a `[tool.smelt]`-configured build from a script or CI job).

```
$ smelt --help
Usage: smelt [OPTIONS] COMMAND [ARGS]...

  Entrypoint for Smelt frontend

Options:
  -p, --project EXISTING_PATH     Project to act on: a folder holding a
                                  pyproject.toml. Default: the current folder.
  -l, --log-level [critical|fatal|error|warn|warning|info|debug|notset]
                                  Logging level to apply. Logs are emitted to
                                  stdout.
  --report TEXT                   Write a report to the given path.
  --help                          Show this message and exit.

Commands:
  build          Builds the extensions declared in the project's Smelt...
  bundle         Assembles a bundle for an entrypoint: smelt-built...
  clean          Deletes built dynlibs (and their mypyc runtime, where...
  compile        One-off compile of a single installed module with the...
  config         Shows the resolved config: the project-wide settings,...
  nuitka-binary  Builds a Nuitka whole-program executable per entrypoint,...
```

`-p/--project`, `-l/--log-level` and `--report` are options of the `smelt` group, so they go **before** the command: `smelt -p path/to/project bundle`.

Every command reads its configuration from a `pyproject.toml`, from `[tool.smelt]` (see [Build Hook](build-hook.md#configuration-location)).

## nuitka-binary

The headline feature of Smelt's CLI: compiles a `[project.scripts]` entrypoint (and everything it imports) into a single, fully standalone executable via Nuitka, with no interpreter or system dependency required on the target machine.

```
$ smelt nuitka-binary --help
Usage: smelt nuitka-binary [OPTIONS]

  Builds a Nuitka whole-program executable per entrypoint, configured by
  [tool.smelt.nuitka-binary].

Options:
  -e, --entrypoint TEXT  Restrict the build to this entrypoint: either its
                         script name as declared in [project.scripts] (e.g.
                         'afpu'), or its 'module.path'/'module.path:func_name'
                         key as declared in
                         [tool.smelt.nuitka-binary.entrypoints]. Builds all
                         configured entrypoints if omitted.
  --package-data TEXT    Collect a package's data files into the binary, on
                         top of [tool.smelt.nuitka-binary] package-data.
                         Syntax: PACKAGE[:PATTERN,...] (e.g.
                         'mypkg:*.json,*.yaml'). Repeatable.
  --no-cache             Disable Nuitka's build cache, forcing a full rebuild.
  --help                 Show this message and exit.
```

Without `-e/--entrypoint`, every entrypoint declared in `[project.scripts]` (plus any declared under `[tool.smelt.nuitka-binary.entrypoints]` or `[tool.smelt.bundle.entrypoints]`) is built.

Options live in `[tool.smelt.nuitka-binary]`, which is independent from `[tool.smelt.bundle]`; `[tool.smelt.nuitka-binary.entrypoints.<ep>]` overrides them per entrypoint (scalars replace, lists append):

```toml
[tool.smelt.nuitka-binary]
extra-flags = ["--lto=yes"]            # passed to Nuitka as is
zig = true                             # compile with Smelt's Zig toolchain (default)
include-modules = ["pkg.plugins.a"]    # modules Nuitka's import-follower can't see
include-packages = ["pkg.plugins"]
package-data = { pkg = ["*.json"] }    # data files collected from a package

[tool.smelt.nuitka-binary.entrypoints.greet]
extra-flags = ["--onefile"]
```

`--package-data PACKAGE[:PATTERN,...]` adds to `package-data` for the run. The former `--embed-file` is deprecated in favor of it.

This compiles the whole program to native code via Nuitka. For an application that assembles bytecode and compiled extensions into a distribution folder instead - optionally with its own bundled interpreter - see `bundle` below, and the [Bundler](bundler-get-started.md) section.

## bundle

Smelt's other route to a standalone application: assembles a distribution folder for one entrypoint out of Smelt-built extensions plus bytecode for everything else it imports, and optionally an interpreter to run on. Settings come from `[tool.smelt.bundle]`; every one of them also has a flag. See [Bundler: Get Started](bundler-get-started.md) for a walkthrough and [Bundler: Reference](bundler-reference.md) for the full list.

```
$ smelt bundle --help
Usage: smelt bundle [OPTIONS]

  Assembles a bundle for an entrypoint: smelt-built extensions plus every
  other module it imports, shipped as bytecode.

  With `--python byo` (the default) the result runs on any machine with a
  matching CPython installed. With `--python own` the interpreter is built and
  shipped in the folder too, which then runs on a machine with no Python at
  all.
```

## build

Builds every native extension declared in the Smelt config (`extensions.mypyc`, `.cython`, `.nuitka`, `.c`, `.zig`) for the project, without touching entrypoints. This is what the [build hook](build-hook.md) runs automatically during a Hatchling build; use this command to run the same step manually, e.g. for a project configured via plain `[tool.smelt]`, or to rebuild extensions in place during local development.

```
$ smelt build
```

## clean

Deletes the native artifacts Smelt built (and their mypyc runtime, where applicable) for a project, unshadowing each module's `.py` source back to being the one Python imports. Useful to reset a development checkout back to pure Python.

```
$ smelt clean [--shadowed-only]
```

`--shadowed-only` only cleans modules with a pure-Python fallback to unshadow, excluding handwritten C/Zig extensions.

## config

Prints the resolved config: the project-wide settings, then the bundler settings of every entrypoint, each followed by the level it came from (`default`, a pyproject table, or `cli`). Use it to debug precedence between `[tool.smelt.bundle]` and its per-entrypoint overrides.

## compile

`smelt compile MODULE_IMPORT_PATH` compiles a single installed module with a chosen backend (`-b/--backend mypyc|nuitka|cython`), optionally cross-compiling it for one or more platforms (`-t/--target`, Zig triples or `host`, comma-separated, see [Advanced Use Cases](advanced.md)). Mainly for trying a backend on one module.

## Renamed commands

The 0.7 command names still work as hidden aliases that print a deprecation line, and are removed in 0.9: `show-config` (`config`), `build-extensions` (`build`), `build-standalone-binary` (`nuitka-binary`), `compile-module` (`compile`), `clean-artifacts` (`clean`). Their project/logging/report options keep their old spelling when used through the alias. `smelt nuitkaify ENTRYPOINT` moved to `python -m smelt.nuitkaify ENTRYPOINT`.
