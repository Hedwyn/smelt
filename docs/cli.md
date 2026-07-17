# CLI

Installing Smelt with the `cli` extra (`pip install "smelt[cli]"`) provides the `smelt` command-line tool. It exposes everything the [build hook](build-hook.md) does, but callable directly - useful outside of a Hatchling build (e.g. producing a standalone binary, or driving a `[tool.smelt]`-configured build from a script or CI job).

```
$ smelt --help
Usage: smelt [OPTIONS] COMMAND [ARGS]...

  Entrypoint for Smelt frontend

Options:
  --help  Show this message and exit.

Commands:
  build-extensions         Runs the smelt backend on the passed project...
  build-standalone-binary
  clean-artifacts          Deletes built dynlibs (and their mypyc...
  compile-module           Standalone command to run the nuitka wrapper...
  nuitkaify                Standalone command to run the nuitka wrapper...
  show-config              Shows the smelt config as defined in the...
```

Every command reads its configuration from a `pyproject.toml`, either from `[tool.smelt]` or `[tool.hatch.build.hooks.smelt]` (whichever is present - see [Build Hook](build-hook.md#standalone-toolsmelt-config)).

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
                                  declared in [tool.smelt.entrypoints]. Builds
                                  all configured entrypoints if omitted.
  --help                          Show this message and exit.
```

Without `-e/--entrypoint`, every entrypoint declared in `[project.scripts]` (plus any extra entrypoint declared under `entrypoints` in the Smelt config) is built. Per-entrypoint options such as extra Nuitka flags or forced module/package inclusion are set via the `entrypoints` table described in [Build Hook](build-hook.md#configuration-reference).

## build-extensions

Builds every native extension declared in the Smelt config (`mypyc_modules`, `cython_modules`, `nuitka_modules`, `c_extensions`, `zig_modules`) for the current project, without touching entrypoints. This is what the [build hook](build-hook.md) runs automatically during a Hatchling build; use this command to run the same step manually, e.g. for a project configured via plain `[tool.smelt]`, or to rebuild extensions in place during local development.

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
* **`compile-module MODULE_IMPORT_PATH`** - compiles a single module with a chosen backend (`-b/--backend mypyc|nuitka|cython`), optionally cross-compiling it for another platform (`-cp/--crosscompile`, see [Advanced Use Cases](advanced.md)).
* **`nuitkaify ENTRYPOINT_PATH`** - runs Smelt's Nuitka wrapper directly on a single entrypoint, bypassing the rest of the config.

Run `smelt <command> --help` for the full option list of any of these.
