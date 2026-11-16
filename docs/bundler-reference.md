# Bundler: Reference

`smelt build-dist` assembles a distribution folder for one entrypoint: Smelt-built extensions plus every other module it imports, shipped as bytecode - and, optionally, an interpreter to run on. See [Get Started](bundler-get-started.md) for a minimal walkthrough and [Overview](bundler-overview.md) for what each mode actually does; terms like closure, discovery, payload, keep-set and tailoring are defined in the [Glossary](glossary.md).

```
$ smelt build-dist --help
```

Most options can also be set per entrypoint under `[tool.smelt.entrypoints."pkg.cli:main"]` (or the matching script name from `[project.scripts]`), the same table used for [`build-standalone-binary`](cli.md#build-standalone-binary). A CLI flag overrides the entrypoint's own declaration for that one invocation; options with no pyproject key below are CLI-only.

## General

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `-p, --package-path` | - | Path to the package to build, expects a `pyproject.toml`. |
| `-e, --entrypoint` | - | Which entrypoint to build. Can be omitted when the project declares a single one. |
| `-o, --output-dir` | - | Folder the distribution is assembled in. Defaults to `./dist`. |
| `--optimize` | - | Bytecode optimization level, `0`-`2`. `2` strips docstrings, which breaks `help()`, `pydoc`, and doctests. Defaults to the level the running interpreter uses. |
| `--no-build` | - | Skip the extension build and reuse whatever artifacts are already on disk. |
| `-l, --logging-level` | - | Logging level; logs go to stdout. |
| `-r, --report` | - | Write a build report to this path. |

## Discovery

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `--discovery {static,trace,both}` | `discovery` | How to find the modules to ship. `static` reads import statements; `trace` imports the entrypoint in a subprocess and records what it actually loaded; `both` (the default) unions them. |
| `--exclude-module` | `exclude-modules` | Drop a module, and everything under it, that discovery reached but that isn't needed at runtime. Repeatable. |
| `--include-module` | `include-modules` | Force a module into the distribution - for modules imported dynamically, which discovery can't see. Repeatable. |
| `--include-package` | `include-package` | Force a whole package (every module under it) into the distribution. Repeatable. |
| `--include-package-data` | `include-package-data` | Collect a package's data files. Syntax: `PACKAGE[:PATTERN,...]` (e.g. `mypkg:*.json,*.yaml`). Repeatable, additive over the entrypoint's own declaration. |
| `--include-distribution-metadata` | `include-distribution-metadata` | Collect a distribution's `*.dist-info` metadata beyond that of packages already owning a shipped module - needed when `importlib.metadata` is queried for one that isn't. Repeatable. |
| `--drop-optional-imports` / `--keep-optional-imports` | `drop-optional-imports` | Whether to leave out modules only reachable through a `try: import x` / `except ImportError` fallback. Kept by default; both what would be dropped and its cost are listed in the build output either way. |

## Interpreter: `byo` or `own`

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `--python {byo,own}` | `python` | Which interpreter the distribution runs on. `byo` (the default) ships the application only, and needs a matching CPython already installed on the target. `own` also builds an interpreter and ships it in the folder. |
| `-t/--target` | `target` | Platform(s) to build the distribution for: a comma-separated list of Zig target triples (e.g. `x86_64-linux-musl`) and/or `host`. One distribution is built per platform, nested under `<output-dir>/<platform>/` whenever this is passed explicitly. Drives both the `own` interpreter build and the isolated-dependency pipeline below. Defaults to `host`, a native build against the host's own libc. See `supported-platforms` below to restrict which platforms an entrypoint accepts. |
| `--own-python-version` | `own-python-version` | CPython release to build the `own` interpreter from, one of `meta-python`'s curated versions. Defaults to the release matching the running interpreter's own minor version - Smelt runs under the version it ships, since shipped bytecode is checked against it. |
| `--no-version-guard` | - | Omit the interpreter-version check from the generated entrypoint. Without it, a mismatched interpreter fails with an opaque `can't find '__main__' module` instead of a readable message. |
| `--no-isolate` | - | Omit the isolation guard (`-I -S`) from the generated entrypoint. Without it, running the distribution unqualified leaves the host's own `site-packages` on `sys.path`, so a module the distribution is missing could be silently supplied by the host. |

## Tailoring the `own` interpreter

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `--tailor-interpreter` / `--no-tailor-interpreter` | `tailor-interpreter` | Whether the `own` interpreter ships only the standard-library modules, extension modules and libraries the closure needs (tailored, the default) or all of them. A stdlib module reached only on a code path discovery missed is invisible either way; tailoring is what turns that gap into a build-time `ImportError` instead of a silently oversized folder - put such a module back with `--include-module`. |
| `--drop-stdlib-group` | `drop-stdlib-groups` | Ship an `own` interpreter without one of the optional Minimal Viable Stdlib groups (currently: `international_hostnames`). Repeatable. Refused for groups nothing can run without - see [Glossary](glossary.md#keep-set-bootstrap-set-minimal-viable-stdlib). |

## Static linking and static builds

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `--use-inittab` / `--no-use-inittab` | `use-inittab` | With `--python own`, statically link Smelt's own compiled extensions into the interpreter (`PyImport_AppendInittab`) instead of shipping them as `.so` files. Off by default - this replaces the interpreter's own entrypoint, and a bad build has no `.so` fallback. Still experimental. |
| `--own-python-static` / `--no-own-python-static` | `own-python-static` | With `--python own`, build one monolithic, static-PIE interpreter (no `libpythonX.Y.so`) instead of the ordinary dynamically-linked shape. Requires compiling every native module in, since a static interpreter has no working `dlopen()`. Off by default, and verified only for `--target x86_64-linux-musl`. |
| `--own-python-static-module` | `own-python-static-modules` | With `--own-python-static`, compile a CPython stdlib module (e.g. `_socket`, `zlib`) straight into the interpreter as a builtin, instead of a `.so` a static interpreter cannot load. Repeatable. |

## Packing into one file

| CLI flag | pyproject key | Description |
| --- | --- | --- |
| `--onefile` / `--no-onefile` | `onefile` | Additionally pack the finished folder into a single file. With `--python byo` that is an executable zip application - no compiler involved. With `--python own` it is a compiled Zig launcher carrying the whole folder as an appended compressed payload, unpacked into a cache directory on first run. Off by default: the folder is the shape that can be inspected. |
| `--onefile-only` | - | Delete the distribution folder once it has been packed, leaving only the single file. Requires `--onefile`. |
| `--onefile-compression {xz,gzip}` | `onefile-compression` | How the payload is compressed. `xz` (the default) is smallest and slowest to inflate on first run; `gzip` trades size for that. |
| `--onefile-compression-preset` | - | Compression preset, `0`-`9` (`xz`) / `1`-`9` (`gzip`). Defaults to `6`. Higher values shrink the file at the cost of build-time and first-run decompression time. |
| `--onefile-cache` / `--no-onefile-cache` | `onefile-cache` | Whether a self-extracting file reuses a previous extraction found at its cache directory (the default) instead of always re-extracting. Set `SMELT_ONEFILE_VERBOSE=1` on the built artifact to see this decision at runtime. |

## Isolated dependency pipeline (experimental)

Target platform comes from `-t/--target`/`target` above (shared with the `own` interpreter build); the rest is set per entrypoint only:

| pyproject key | Description |
| --- | --- |
| `isolated-build` | Whether a third-party extension module in the closure is reinstalled for the actual target platform instead of copied from the local environment. |
| `isolated-build-versions` | Version-resolution strategy used when reinstalling such a dependency. |
| `supported-platforms` | Allowlist `target` is checked against. Requesting a platform outside it fails the build before anything is built. `host` is always implicitly allowed regardless of this list; omitted, any platform is accepted. |
