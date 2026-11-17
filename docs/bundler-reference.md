# Bundler: Reference

`smelt bundle` assembles a distribution folder for one entrypoint: Smelt-built extensions plus every other module it imports, shipped as bytecode - and, optionally, an interpreter to run on. See [Get Started](bundler-get-started.md) for a minimal walkthrough and [Overview](bundler-overview.md) for what each mode actually does; terms like closure, discovery, payload, keep-set and tailoring are defined in the [Glossary](glossary.md).

## Configuration

Settings live in `[tool.smelt.bundle]`, split into the groups below, and apply to every entrypoint. A single entrypoint overrides them under `[tool.smelt.bundle.entrypoints.<name>]` with the same keys and groups, `<name>` being its `[project.scripts]` name or a `module.path:func` spec (which also declares an entrypoint that is not a script):

```toml
[tool.smelt.bundle]
python = "own"

[tool.smelt.bundle.content]
include-packages = ["pkg.plugins"]
package-data = { pkg = ["*.json"] }

[tool.smelt.bundle.onefile]
enabled = true
compression = "gzip"

[tool.smelt.bundle.entrypoints.greet]
python = "byo"
```

Precedence, highest first: command-line flag, `bundle.entrypoints.<name>`, `[tool.smelt.bundle]`, built-in default. A scalar replaces the lower level's value; a list appends to it. Unknown keys and wrong types are configuration errors.

Every key has a flag named after it: `[tool.smelt.bundle.<group>] <key>` becomes `--<group>-<key>`, the prefix being dropped for `content` and `interpreter`. Booleans are `--x/--no-x`, list keys a repeatable singular flag. Run `smelt bundle --help` for the full text.

## Command-line only

| Flag | Description |
| --- | --- |
| `-p, --project` | Project folder, holding a `pyproject.toml`. Defaults to `.`. |
| `-e, --entrypoint` | Entrypoint to bundle. Can be omitted when the project declares a single one. |
| `-o, --output-dir` | Folder the bundle is assembled in. Defaults to `./dist`. |
| `-t, --target` | Zig target triple(s), comma-separated or repeated, and/or `host`. One bundle is built per target; the output is `<output-dir>/<script>.dist` for a single target and `<output-dir>/<target>/<script>.dist` when several are built. Defaults to `host`. |
| `--no-build` | Skip the extension build and reuse the artifacts already on disk. |
| `-l, --log-level` | Logging level; logs go to stdout. |
| `--report` | Write a build report to this path. |

`supported-targets` (accepted at `[tool.smelt.bundle]` and entrypoint level) restricts what `-t` may request; it has no flag of its own.

## General

| Key | Flag | Description |
| --- | --- | --- |
| `python` | `--python` | Interpreter the bundle runs on. 'byo' (bring your own) ships the application only and needs a matching CPython on the target. 'own' also builds an interpreter and ships it in the folder, so it runs where no Python is installed; the first such build takes several minutes and is then cached per target. Default: byo. |
| `optimize` | `--optimize` | Bytecode optimization level (0, 1 or 2). Defaults to the level this interpreter runs with. Level 2 strips docstrings, which breaks help(), pydoc, doctests and libraries building their help text from docstrings. |
| `guard-version` | `--guard-version` / `--no-guard-version` | Check the interpreter version in the generated entrypoint. Without it, a mismatched interpreter fails with `can't find '__main__' module` instead of a readable message. Default: on. |
| `isolate` | `--isolate` / `--no-isolate` | Add an isolation guard to the generated entrypoint. Without it, running the bundle without -I -S leaves the host's site-packages on sys.path, where it can silently supply a missing module. Default: on. |

## Content

`[tool.smelt.bundle.content]`

| Key | Flag | Description |
| --- | --- | --- |
| `discovery` | `--discovery` | How to find the modules to ship. 'static' reads import statements, 'trace' imports the entrypoint in a subprocess and reports what it loaded (this runs its module-level code), 'both' unions them. Default: both. |
| `include-modules` | `--include-module` | Force a module into the bundle, on top of what discovery finds. For modules imported dynamically. Repeatable. |
| `include-packages` | `--include-package` | Force a whole package (every module it contains, recursively) into the bundle. For a package that loads its own internals dynamically. Repeatable. |
| `exclude-modules` | `--exclude-module` | Drop a module, and everything under it, from the bundle. For trees discovery reached but that are not needed at runtime. Repeatable. |
| `drop-optional-imports` | `--drop-optional-imports` / `--keep-optional-imports` | Leave out the modules only reachable through an import whose importer handles the failure itself (`try: import x` / `except ImportError`). None of them can raise an ImportError by being absent, but what the fallback costs varies: dropping `_hashlib` saves 5 MB with no visible change, dropping `ssl` leaves urllib unable to speak HTTPS. Default: keep; the build output lists them either way. |
| `package-data` | `--package-data` | Collect a package's data files into the bundle. Syntax: PACKAGE[:PATTERN,...] (e.g. 'mypkg' or 'mypkg:*.json,*.yaml'). Repeatable. |
| `distribution-metadata` | `--distribution-metadata` | Collect a distribution's *.dist-info metadata beyond those owning a shipped module. Needed when importlib.metadata is queried for a package whose modules are not themselves shipped. Repeatable. |

## Interpreter (`python = "own"` only)

`[tool.smelt.bundle.interpreter]`

| Key | Flag | Description |
| --- | --- | --- |
| `version` | `--python-version` | CPython release to build the 'own' interpreter from (e.g. '3.12.3'). Only the minor version of the Python running smelt can be shipped, since the bytecode is compiled by it: run smelt under the minor version you want to ship. Default: the supported release matching the running minor version. |
| `tailor` | `--tailor` / `--no-tailor` | Ship only the standard library modules, extension modules and libraries the application was found to need, instead of all of them. Keeps an 'own' bundle a reasonable size, but a module reached only through a name absent from the source is invisible to discovery and becomes a runtime ImportError: put it back with --include-module. Each distinct set of disabled libraries needs its own interpreter build (minutes), cached separately. Default: tailored. |
| `drop-stdlib-groups` | `--drop-stdlib-group` | Ship the 'own' interpreter without an optional group of the minimal standard library (currently: international_hostnames, 1.1 MB, which costs support for non-ASCII hostnames). Refused for groups nothing can run without. Repeatable. |
| `static` | `--static` / `--no-static` | Build one monolithic, static-PIE 'own' interpreter (no libpythonX.Y.so) instead of the dynamic shape. A static interpreter has no working dlopen(), so every native module has to be compiled into it: --static-module for standard library modules, --inittab for smelt's own extensions. Any .so left in the bundle fails the build. Verified only for x86_64-linux-musl. Default: off. |
| `static-modules` | `--static-module` | With --static, compile a standard library module (e.g. '_socket', 'zlib') into the interpreter as a builtin. Repeatable. |
| `inittab` | `--inittab` / `--no-inittab` | With the 'own' interpreter, link smelt's own compiled extensions into it instead of shipping .so files, which skips dlopen() and RPATH for them. This replaces the interpreter's own entrypoint, and a bad build has no .so fallback. Experimental. Default: off. |

## Onefile

`[tool.smelt.bundle.onefile]`

| Key | Flag | Description |
| --- | --- | --- |
| `enabled` | `--onefile` / `--no-onefile` | Also pack the finished folder into a single file. With 'byo' that is an executable zip application (a /bin/sh preamble locating a CPython, followed by a zip). With 'own' it is a compiled launcher carrying the whole folder, interpreter included, as a compressed payload it unpacks into a cache directory on first run. Default: off. |
| `compression` | `--onefile-compression` | Payload compression. 'xz' (default) is smallest and slowest to inflate on the first run, 'gzip' trades size for speed, 'none' stores it as is. A 'byo' payload is imported straight out of its zip, so it is deflated for 'xz' and 'gzip' alike. |
| `compression-preset` | `--onefile-compression-preset` | Compression preset: 0-9 for xz, 1-9 for gzip. Default: 6. Higher presets shrink the file but cost build time and first-run inflation time. |
| `cache` | `--onefile-cache` / `--no-onefile-cache` | Reuse a previous extraction found in the cache directory instead of always re-extracting. Set SMELT_ONEFILE_VERBOSE=1 on the built artifact to see the decision at runtime. Default: on. |
| `keep-folder` | `--onefile-keep-folder` / `--onefile-only` | Keep the distribution folder once it is packed (default). --onefile-only deletes it and leaves only the single file, for a job that publishes one artifact. Requires --onefile. |

## Isolated build (experimental)

`[tool.smelt.bundle.isolated-build]`

| Key | Flag | Description |
| --- | --- | --- |
| `enabled` | `--isolated-build` / `--no-isolated-build` | Experimental. Reinstall every third-party native dependency for the target instead of copying the build installed here. Needs the 'isolated-build' extra and a network fetch per native dependency. Default: off. |
| `versions` | `--isolated-build-versions` | Which version of each dependency --isolated-build fetches: 'local' (the one installed here, default) or 'pyproject' (as declared in [project.dependencies]). |
