# Bundler: Get Started

A *bundler* takes a Python application and turns it into something that runs on a target machine without a separate install step - PyInstaller, Nuitka's standalone mode, and PyOxidizer are existing examples of tools that do this in the Python ecosystem.

Smelt is both a build backend (compiling native extensions - see [Get Started](get-started.md)) and a bundler: `smelt bundle` assembles a standalone distribution out of a `[project.scripts]` entrypoint, reusing whichever of Smelt's own compile backends (mypyc, Cython, Nuitka, handwritten C/Zig) the rest of the project already declares.

## A minimal example

`src/greet_demo/cli.py`:

```python
def main() -> None:
    print("Hello from a standalone binary!")
```

`pyproject.toml`:

```toml
[build-system]
requires = ["hatchling", "smelt"]
build-backend = "hatchling.build"

[project]
name = "greet-demo"
version = "0.1.0"
requires-python = ">=3.12"

[project.scripts]
greet = "greet_demo.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/greet_demo"]

[tool.hatch.build.hooks.smelt]

[tool.smelt]
package-dirs = { "greet_demo" = "src/greet_demo" }
```

No native extensions declared here - the entrypoint is plain Python, so the distribution ships it as bytecode. Add `mypyc_modules`/`nuitka_modules`/etc. the same way as for a regular build (see [Build Hook](build-hook.md)) and the bundler carries the compiled result alongside it.

## Building it

Install Smelt's CLI extra and run `smelt bundle`:

```
$ pip install "smelt[cli]"
$ smelt bundle
```

This produces `dist/greet.dist/` - named after the `greet` entrypoint, not the package:

```
dist/greet.dist/
  smelt-dist.json   manifest: what went in, and which interpreter it needs
  HOW_TO_RUN.txt     instructions generated for this specific build
  app/               the application itself - the only folder on sys.path
```

Nothing was asked to be shipped beyond the application, so this is a `byo` ("bring your own [interpreter]") build: it needs a matching CPython already installed on the machine that runs it.

```
$ python3.12 dist/greet.dist/app
Hello from a standalone binary!
```

## Shipping the interpreter too

Add `--python own` to also build and bundle a CPython interpreter inside the folder, so it runs on a machine with no Python installed at all:

```
$ smelt bundle --python own
$ ./dist/greet.dist/greet
Hello from a standalone binary!
```

The first such build compiles that interpreter, which takes a few minutes; later builds reuse it. Add `--onefile` on top of either mode to additionally pack the folder into a single file - an executable zip application for `byo`, a compiled self-extracting launcher for `own`.

## Where to go next

* **[Overview](bundler-overview.md)** - what the bundler actually does under the hood, and how it compares to PyInstaller, Nuitka's standalone mode, and PyOxidizer.
* **[Reference](bundler-reference.md)** - the full `smelt bundle` option list.
