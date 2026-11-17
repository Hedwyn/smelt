# CLI

Installing Smelt with the `cli` extra (`pip install "smelt[cli]"`) provides the `smelt` command-line tool. It exposes everything the [build hook](build-hook.md) does, but callable directly - useful outside of a Hatchling build (e.g. producing a standalone binary, or driving a `[tool.smelt]`-configured build from a script or CI job).

```
$ smelt --help
Usage: smelt [OPTIONS] COMMAND [ARGS]...

  Entrypoint for Smelt frontend

Options:
  --help  Show this message and exit.

Commands:
  bundle                   Assembles a bundle for an entrypoint: smelt-built...
  build-extensions         Runs the smelt backend on the passed project...
  build-standalone-binary
  clean-artifacts          Deletes built dynlibs (and their mypyc...
  compile-module           Standalone command to run the nuitka wrapper...
  nuitkaify                Standalone command to run the nuitka wrapper...
  show-config              Shows the smelt config as defined in the...
```

Every command reads its configuration from a `pyproject.toml`, from `[tool.smelt]` (see [Build Hook](build-hook.md#configuration-location)).

## build-standalone-binary

The headline feature of Smelt's CLI: compiles a `[project.scripts]` entrypoint (and everything it imports) into a single, fully standalone executable via Nuitka, with no interpreter or system dependency required on the target machine.

```
$ smelt build-standalone-binary --help
Usage: smelt build-standalone-binary [OPTIONS]

Options:
  -p, --package-path EXISTING_PATH
  -l, --logging-level [critical|fatal|error|warn|warning|info|debug|notset]
                                  Logging level to apply. Logs are emitted to
                                  stdout
  -r, --report TEXT               Produces a report at the given path
  -e, --entrypoint TEXT           Restrict the build to this entrypoint:
                                  either its script name as declared in
                                  [project.scripts] (e.g. 'afpu'), or its
                                  'module.path'/'module.path:func_name' key as
                                  declared in [tool.smelt.bundle.entrypoints]
                                  or [tool.smelt.nuitka-binary.entrypoints].
                                  Builds all configured entrypoints if omitted.
  --package-data TEXT             Collect a package's data files into the
                                  binary. Syntax: PACKAGE[:PATTERN,...].
                                  Repeatable.
  --no-cache                      Disable Nuitka's build cache.
  --help                          Show this message and exit.
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

## build-extensions

Builds every native extension declared in the Smelt config (`extensions.mypyc`, `.cython`, `.nuitka`, `.c`, `.zig`) for the current project, without touching entrypoints. This is what the [build hook](build-hook.md) runs automatically during a Hatchling build; use this command to run the same step manually, e.g. for a project configured via plain `[tool.smelt]`, or to rebuild extensions in place during local development.

```
$ smelt build-extensions --help
Usage: smelt build-extensions [OPTIONS]

  Runs the smelt backend on the passed project and builds all extensions
  defined by smelt.

Options:
  -p, --package EXISTING_PATH  Path the the package to build extensions for,
                               expects to find a pyproject.toml
  --help                       Show this message and exit.
```

## clean-artifacts

Deletes the native artifacts Smelt built (and their mypyc runtime, where applicable) for a project, unshadowing each module's `.py` source back to being the one Python imports. Useful to reset a development checkout back to pure Python.

```
$ smelt clean-artifacts --help
Usage: smelt clean-artifacts [OPTIONS]

  Deletes built dynlibs (and their mypyc runtime, where applicable) for the
  passed project, unshadowing their `.py` counterpart back to being the one
  Python imports.

Options:
  -p, --package EXISTING_PATH  Path the the package to clean built artifacts
                               for, expects to find a pyproject.toml
  --shadowed-only              Only clean modules with a pure-Python fallback
                               to unshadow, excluding handwritten C/Zig
                               extensions
  --help                       Show this message and exit.
```

## Other commands

A few commands are lower-level, mainly intended for manually testing a single module rather than everyday project builds:

* **`show-config`** - parses and prints the Smelt config resolved from a `pyproject.toml`, for debugging what the build hook/CLI actually sees.
* **`compile-module MODULE_IMPORT_PATH`** - compiles a single module with a chosen backend (`-b/--backend mypyc|nuitka|cython`), optionally cross-compiling it for one or more platforms (`-t/--target`, Zig triples or `host`, comma-separated, see [Advanced Use Cases](advanced.md)).
* **`nuitkaify ENTRYPOINT_PATH`** - runs Smelt's Nuitka wrapper directly on a single entrypoint, bypassing the rest of the config.

Run `smelt <command> --help` for the full option list of any of these.
