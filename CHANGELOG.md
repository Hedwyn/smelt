# Changelog

All notable changes to Smelt are documented here. Entries before 0.8.0 were reconstructed retroactively from commit history, since changes were not logged release by release at the time; they may be less exhaustive than later entries.

## [0.8.1]

### Added

* Smelt settings can live in a standalone `smelt.toml` next to `pyproject.toml`, so a project can be built or bundled without editing its `pyproject.toml`. It carries the keys of `[tool.smelt]` at its top level (e.g. `[bundle]` instead of `[tool.smelt.bundle]`). Declaring settings in both `smelt.toml` and `[tool.smelt]` is an error.

### Fixed

* Zig and C extensions living under a package root declared in `packages-location` were written to a `.py` file (e.g. `_native.py`, actually an ELF binary) instead of a file with the platform extension suffix (`_native.cpython-312-x86_64-linux-gnu.so`). Projects without `packages-location` were not affected.
* Import path resolution for declared package roots matched on a bare string prefix, so a root `pkg` also captured modules of an unrelated package such as `pkg2`.

## [0.8.0]

The headline of this release is the bundler, `smelt bundle`: it turns an entrypoint into a standalone distribution (a folder or a single file), optionally with its own interpreter. The configuration and the CLI were also reworked into one consistent interface; see **Changed** and **Deprecated** for migrating a 0.7 project.

### Added

#### Bundler (`smelt bundle`, configured in `[tool.smelt.bundle]`)

* Bundles an entrypoint into a distribution folder: Smelt-built extensions next to bytecode-compiled (`.pyc`) versions of every other module it imports, third-party native packages, data files (`content.package-data`) and distribution metadata (`content.distribution-metadata`). Application code lives in its own `app/` subfolder, so it cannot clash with local sources.
* Two interpreter modes (`python`): `byo` (bring your own: needs a matching CPython on the target) and `own` (Smelt builds a CPython interpreter through `build-your-python`, installed with the `byp` extra, and ships it in the folder). Python 3.12 to 3.15 can be shipped: the minor version is the one Smelt runs under (the shipped bytecode is tied to it), and `interpreter.version` picks the patch release.
* `own` interpreters are tailored to the application (`interpreter.tailor`, on by default): only the standard library parts the application reaches are built in, and unused libraries (OpenSSL, SQLite, ...) are disabled at configure time. A minimum set of modules that discovery cannot see is always kept; optional parts of it can be dropped (`interpreter.drop-stdlib-groups`).
* Experimental fully static `own` interpreters on musl targets (`interpreter.static`, `interpreter.static-modules`), and static linking of Smelt-built extensions into the interpreter through Python's inittab (`interpreter.inittab`).
* Single-file output (`onefile.enabled`): an executable zip application for `byo`, a compressed self-extracting launcher for `own`. Configurable compression (`onefile.compression`: `xz`, `gzip` or `none`; `onefile.compression-preset`), reuse of a previous extraction (`onefile.cache`), and removal of the folder once packed (`--onefile-only`). Initial Windows support.
* Module discovery by static analysis, by tracing an actual import of the entrypoint, or both (`content.discovery`). Discovery skips imports guarded by checks for other platforms and `if __name__ == "__main__":` blocks, and can drop imports that have a `try/except ImportError` fallback (`content.drop-optional-imports`). Modules can be forced in or left out explicitly (`content.include-modules`, `content.include-packages`, `content.exclude-modules`).
* The generated entrypoint checks the interpreter version (`guard-version`) and runs isolated from the host's `site-packages` (`isolate`), both on by default.
* Multiple targets in one run (`-t/--target`), restricted by `supported-targets`. That list is checked at every level declaring it (`[tool.smelt]`, `[tool.smelt.bundle]`, `bundle.entrypoints.<ep>`), lower levels can only narrow it, and without `-t` the concrete triples of the most specific level are built.
* Every setting has a `[tool.smelt.bundle]` key and a matching flag. Precedence: command line > `bundle.entrypoints.<ep>` > `[tool.smelt.bundle]` > default. Scalars replace, lists append.
* Experimental isolated dependency pipeline (`isolated-build`): third-party native dependencies are fetched, version-resolved and rebuilt for the target instead of copied from the local environment. Known packages (`cffi`/`libffi`, `cryptography`, `pyomq`) are built from source through vendored build pipelines.
* A build lock keeps concurrent `own` interpreter builds from interfering with each other.

#### Build backend

* `bytecode` auto-mode backend (`extensions.auto.backends`): leaves a module as pure Python (`.pyc` in `smelt bundle`), so it can end a fallback chain.
* Zig executables (`[[tool.smelt.executables.zig]]`): binaries produced by a project's own `build.zig`, installed as wheel scripts.
* Optional Rust/Maturin extension pipeline (`rust` extra), using an isolated Rust toolchain fetched through `puccinialin` (no system Rust needed), cross targets included.
* Cross-compilation of extensions uses a `pyconfig.h` generated for the target, instead of only the bundled Python 3.12 headers.
* Packages built by Smelt ship a private manifest module describing their Smelt-managed native modules and supported targets, so that downstream isolated builds can find them and rebuild them for the target, transitive dependencies included.

#### Documentation

* Bundler pages: Get Started, Overview and Reference.

### Changed

These break 0.7 configurations or scripts; the old spellings that still work are listed under **Deprecated**.

* `[tool.smelt]` is the only configuration table, read by both the build hook and the CLI. `[tool.hatch.build.hooks.smelt]` only enables the hook and should stay empty. Settings found there are still read, with a warning, when `[tool.smelt]` is absent; settings in both tables are an error.
* The configuration is validated against a typed schema. Unknown keys (with a closest-match hint), wrongly typed values and invalid choices are errors naming the table to fix. Internal fields (`dependencies`, `script_names`, ...) can no longer be set from `pyproject.toml`.
* New layout, kebab-case keys throughout:
  * modules: `[tool.smelt.extensions]` with `mypyc`, `cython`, `nuitka` (bare import path or table), `c`, `zig` (arrays of tables), and `extensions.auto` (`mode`, `backends`);
  * `packages_location` is `package-dirs`;
  * `platforms` (OS names) and `archs` are replaced by `supported-targets` (Zig-triple patterns such as `*-linux-*` or `x86_64-*`, or `host`);
  * Nuitka binary options move from `[tool.smelt.entrypoints.<ep>]` to `[tool.smelt.nuitka-binary]` and `nuitka-binary.entrypoints.<ep>`: `extra-flags`, `zig` (replaces `no-zig`, inverted), `include-modules`, `include-packages` (was `include-package`), `package-data` (a table `{pkg = ["*.json"]}` instead of the `"pkg:*.json"` string syntax).
* CLI commands renamed: `config` (was `show-config`, now also shows where each bundler setting comes from), `build` (was `build-extensions`), `nuitka-binary` (was `build-standalone-binary`), `compile` (was `compile-module`), `clean` (was `clean-artifacts`).
* `-p/--project`, `-l/--log-level` and `--report` are options of the `smelt` command itself, identical for every subcommand, and go **before** it: `smelt -p DIR nuitka-binary`. They replace `-p/--path`, `-p/--package`, `-p/--package-path`, `-l/--logging-level` and `-r/--report`.
* `compile -t/--target` takes Zig triples (e.g. `aarch64-linux-gnu`) or `host`.
* `nuitka-binary --package-data` replaces `--embed-file`.
* `nuitkaify` is no longer a CLI command: use `python -m smelt.nuitkaify`.
* Smelt now builds with Zig 0.17 (`ziglang` is pinned to the 0.17 series). Zig 0.17 removed `@cImport`, so a single-file `.zig` module listed in `extensions.c` sources must import the Python C-API with `@import("python")` (Smelt translates `Python.h` for it). Projects built with `extensions.zig` / `executables.zig` must use a `build.zig` compatible with Zig 0.17.
* Nuitka is capped below 4.2, as 4.2 changed the generated sources Smelt patches for its shared-runtime builds.

### Deprecated

Still accepted in 0.8 with a warning, removed in 0.9:

* `[tool.smelt]` keys: `packages_location`; `platforms`, `archs`; `mypyc_modules`, `cython_modules`, `nuitka_modules`, `c_extensions`, `zig_modules`, `zig_executables`; `auto_mode`, `backend_priority_order`; the Nuitka keys of `entrypoints.<ep>` (`extra_flags`, `no-zig`, `include-modules`, `include-package`, `include-package-data`). Each is rewritten to its new location.
* snake_case spellings of keys (e.g. `import_path`).
* Settings under `[tool.hatch.build.hooks.smelt]`.
* The 0.7 command names (`show-config`, `build-extensions`, `build-standalone-binary`, `compile-module`, `clean-artifacts`), as hidden aliases printing a deprecation line, with their old option spellings.
* `nuitka-binary --embed-file`.
* Bare `[tool.smelt.entrypoints.<name>]` declarations (an entrypoint outside `[project.scripts]`): use `[tool.smelt.bundle.entrypoints.<name>]`.

### Removed

* `mypyc_options`, `cython_options` (never read), `debug` and `report_path` in `[tool.smelt]`: ignored, with a warning. Use `SMELT_DEBUG`, `--report` or `SMELT_REPORT`.

### Fixed

* Linking extensions on Windows.

## [0.7.5] - 2026-07-23

### Added

* `--no-zig` option for Nuitka entrypoints, to disable passing Zig as the compiler and let Nuitka use its default toolchain.
* Flag to disable the Nuitka build cache.

## [0.7.4] - 2026-07-23

### Added

* `--embed-file` flag to include arbitrary data files in an entrypoint's Nuitka build.

### Fixed

* Forced inclusion of `.pyd` files was not applied properly on Windows.

## [0.7.3] - 2026-07-20

### Fixed

* Commands originating from the same module could collide with each other when building entrypoints.
* Pinned Nuitka's `onefile` extra to a working version.

## [0.7.2] - 2026-07-20

### Added

* Nuitka entrypoint compilation now checks that included modules are actually importable, skipping (with a warning) any that aren't.
* Warn when a native module pinned to an extra is used outside isolated mode, where that pinning isn't honored.

### Fixed

* Module discovery did not handle a module being a folder with an `__init__.py`.

## [0.7.1] - 2026-07-17

### Added

* Documentation site (mkdocs-based), built and published via CI.

### Changed

* Publishing workflow migrated to `uv publish`, restricted to tag pushes.

## [0.7.0] - 2026-07-17

### Added

* Per-entrypoint options for module inclusion, package inclusion, and data file inclusion.
* Entrypoints declared under `[project.scripts]` are now targeted automatically; function-based entrypoints get an auto-generated script wrapper.
* `clean-artifacts` command to remove built artifacts and restore pure-Python sources.

### Changed

* **Breaking:** entrypoint configuration is now a dict keyed by entrypoint instead of a single static string, allowing multiple entrypoints to be declared and targeted individually from the CLI (`-e/--entrypoint`).

### Fixed

* Entrypoint mapping fix.

## [0.6.1] - 2026-07-16

### Added

* `extras` field to restrict a native module to a given optional dependency group; the build hook skips building it when that extra's dependencies aren't installed.

## [0.6.0] - 2026-07-16

### Added

* Explorer module: recursively discovers a module's imports/dependencies.
* "Auto mode": automatically discovers a project's modules and picks a backend for each, with an accompanying build report.
* Nuitka shared runtime: modules can now share a common runtime library instead of duplicating it per module.

### Changed

* Nuitka pipeline now uses `--generate-c-only` directly, replacing the previous recompile-cycle approach.
* Vendor `patchelf` from PyPI instead of relying on the system-installed one.

### Fixed

* Numerous Windows-specific compatibility fixes: missing `LIBDIR`, `__declspec` handling, command runner behavior, MSVC flag sanitization, and general Nuitka-on-Windows issues. The Windows restriction on the example project was lifted accordingly.

## [0.5.0] - 2026-03-31

### Added

* Build hook can now be restricted to specific platforms.

### Fixed

* Incorrect build hook configuration in the example project.
* Build hook's `run_backend` step did not honor verbose mode.

## [0.4.5] - 2026-03-17

### Fixed

* Compiler flags were not properly passed through to the Zig module compile function.

## [0.4.3] - 2026-03-17

### Fixed

* Flag handling for Zig modules was not cast to the correct type.

## [0.4.2] - 2026-03-12

### Added

* Frontend command to build extensions as a standalone step.
* Support for injecting a package root path into path resolution when it differs from the current working directory.
* Configurable compiler flags for Zig modules.

### Fixed

* Various issues in frontend-side path/utility handling.
* Backend build hook no longer runs when building an sdist.

## [0.4.1] - 2026-03-06

### Changed

* Migrated to `setuptools-scm` for versioning.

### Fixed

* Zig extension name handling (expected a string, not a `Path`).
* Type handling for Zig extensions when the package location is left unspecified.

## [0.4.0] - 2026-03-03

### Added

* Experimental support for Zig native extensions.
* `nuitka_modules` config entry.
* Context objects to track build state, with an optional build report.
* Generic environment-variable handling in config.
* Functional test suite (via `nox`), including correctness tests that check the output of compiled entrypoints.

### Changed

* **Breaking:** native extension definitions (mypyc, Cython, Zig) were restructured to a new configuration syntax.
* Config loading moved to its own dedicated module.
* Introduced a `PathSolver` helper and reworked root resolution to work from any import path, rather than assuming a single package name.
* Subprocess handling for the Nuitka pipeline moved to shared process utilities, with proper error handling on process failure.

### Fixed

* Entrypoint was not disabled properly when left undefined in config.

## [0.3.0] - 2025-11-19

### Added

* Cython extension support: build primitives plus backend integration.
* Defaults for missing config sections; entrypoint became optional.

### Changed

* Renamed `MypycExtension` to `GenericExtension`, shared across backends.

### Fixed

* Package root path handling.
* mypyc extensions were not compiled via the correct path.
* Cython extension naming.
* Pure-Python fallback implementation was not actually pure Python.

## [0.2.0] - 2025-10-07

### Added

* Hatchling build hook (plugin): run Smelt automatically as part of a Hatchling build.
* Standalone command to run just the mypyc extension-build stage.
* Experimental `build-exe` command for Zig.
* Configurable logging level, applied across all entrypoints.
* Customizable module discovery strategy for locating modules in typical package layouts.

## [0.1.0] - 2025-07-24

Initial release.

### Added

* Zig-based compiler backend: compile C extensions and mypyc-generated extensions using Zig as the compiler.
* Experimental cross-compilation support, including `pyconfig.h` generation/bundling and an initial armv7l target.
* mypyc wrapper for compiling mypyc-generated extensions.
* Basic Nuitka wrapper (`nuitkaify`) to compile modules via Nuitka.
* CLI frontend and build backend driven by `pyproject.toml` configuration.
* Support for `setuptools.Extension` objects as compile targets.
* Recompilation handling: shadow `.py` sources when compiled artifacts exist, and restore them for import when needed.
