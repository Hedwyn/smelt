# Smelt

Shipping a Python project as native code is hard in two ways. Native extensions (C, Zig, mypyc, Cython, Nuitka) need a C toolchain on every machine that builds them, and each tool has its own build pipeline. Shipping an application to a machine without Python means bundling an interpreter, which tools like PyInstaller do by copying a whole, generic one.

Smelt addresses both, using [Zig](https://ziglang.org/) (installed from PyPI with `ziglang`) as a fully standalone compiler, so nothing needs to be installed on the system:

* **A build backend**: compiles native extensions declared in `pyproject.toml` (mypyc, Cython, Nuitka, handwritten C or Zig) during a regular build, with no system compiler. Backends can be mixed per module, and swapped without touching the code. Cross-compilation for other platforms is experimental.
* **A bundler**: `smelt bundle` turns an entrypoint into a standalone distribution, as a folder or a single file. It ships the application's compiled extensions plus bytecode for the rest, and can build and ship its own CPython interpreter. That interpreter is tailored to the standard-library modules and native libraries the application actually uses, so the target machine needs no Python at all.

Documentation: <https://hedwyn.github.io/smelt>

## Build backend

```toml
[build-system]
requires = ["hatchling", "smelt[mypyc]"]
build-backend = "hatchling.build"

[project]
name = "fib-demo"
version = "0.1.0"

[tool.hatch.build.hooks.smelt]   # enables the hook

[tool.smelt]
package-dirs = { fib_demo = "src/fib_demo" }

[tool.smelt.extensions]
mypyc = ["fib_demo.fib"]
```

`pip install .` (or `python -m build`) compiles `fib.py` with mypyc and places the native module next to it, shadowing the pure-Python source.

Other backends are declared the same way: `cython`, `nuitka`, `[[tool.smelt.extensions.c]]`, `[[tool.smelt.extensions.zig]]`. See [Build Hook](https://hedwyn.github.io/smelt/build-hook/).

## Bundler

Every `[project.scripts]` entry can be bundled:

```toml
[project.scripts]
greet = "greet_demo.cli:main"
```

```
$ uv pip install "smelt[cli,byp]"   # `byp` is only needed for --python own
$ smelt bundle                 # dist/greet.dist/, runs on a matching installed CPython
$ smelt bundle --python own    # also builds and ships a tailored CPython interpreter
$ smelt bundle --python own --onefile   # packs it into a single self-extracting file
```

Settings live in `[tool.smelt.bundle]` (with per-entrypoint overrides), and every setting has a matching command-line flag. See [Bundler: Get Started](https://hedwyn.github.io/smelt/bundler-get-started/) and the [Reference](https://hedwyn.github.io/smelt/bundler-reference/).

## Command line

| Command | Purpose |
| --- | --- |
| `smelt build` | Build the declared extensions in place |
| `smelt bundle` | Bundle an entrypoint (folder or single file, with or without its own interpreter) |
| `smelt nuitka-binary` | Compile an entrypoint into one executable with Nuitka's whole-program mode |
| `smelt compile MODULE` | Compile a single module with a chosen backend, optionally for another platform |
| `smelt clean` | Delete built extensions, un-shadowing the pure-Python sources |
| `smelt config` | Show the resolved configuration and where each setting comes from |

See [CLI](https://hedwyn.github.io/smelt/cli/).

## Status

Smelt targets Python 3.12+. The bundler, the `own` interpreter and the build backend are tested on Linux x86_64 (glibc). Cross-compilation, fully static interpreters (`--static`, musl only), static linking of extensions into the interpreter (`--inittab`), the isolated dependency pipeline, and Windows single-file bundles are experimental.

Upgrading from 0.7: see the [changelog](https://github.com/Hedwyn/smelt/blob/master/CHANGELOG.md). Configuration keys and command names from 0.7 still work in 0.8 with a deprecation warning.
