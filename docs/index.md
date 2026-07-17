# Smelt

Smelt is a Python tool that aims to simplify greatly shipping native code from Python projects. This notably covers:

* Providing a fully standalone way to compile native code in Python projects without any system dependency.
* Cross-compiling Python C extensions.
* Combining multiple tools building or providing native code in Python projects (e.g., mypyc, Nuitka, local C/Zig extensions).
* Providing a single high-level interface to automate binary builds, either for platform-specific wheels or even for standalone fully compiled binary Python projects.

Head to [Get Started](get-started.md) for a minimal working setup - a single mypyc extension declared in a couple of lines of `pyproject.toml`.

## Goals

### Standalone binary builds - ship your source confidently

The main annoyance when shipping native code is that either you pre-build and host binary wheels for all possible platforms yourself
(expensive, and induce a maintenance load on the infrastructure side), either you ship the source and require from users to install
the toolchain for your native code - which is not bundled in the interpreter themselves. <br>
Native extensions will typically require at least a C compiler, and often CMake and/or other build tools.<br>
The consequence is that calling `pip install` on these projects will fail in the toolchain is not installed
on the system, and it breaks the (usual) simplicity of Python package management. <br><br>
Smelt leverages the Zig compiler, which is fully standalone (unlike most C toolchains) and is available behind a PyPI package.
Smelt implements a Zig-based toolchain to build native extensions (as opposed to most existing solutions which use gcc or clang).
This allows shipping your package together with the toolchain to build it, using only standard `pyproject.toml` config entries. `pip install` will just work.
It also enables some interesting cross-compilation capabilities ( for wheels and standalone binaries), although this is still in the experimental zone and comes with limitations.

### Unified, modern pyproject.toml-based interface over serveral binary backends

Besides hand-crafting native extensions in C, C++, Rust or Zig, there are existing tools in the Python ecosystem to convert Python code to native.<br>
The main issue with them is that they often lack a proper, modern interface in `pyproject.toml` to use them. They also do not have a unified API.<br>
Another issue is that their "public"" interface is usually mostly built around a Python -> binary conversion, while internally they are actually Python -> transpiled C -> binary. Smelt provides the following features:

* Supports Cython, mypyc, Nuitka, and vanilla C/Zig native extensions under one unified `pyproject.toml` - based API.
* Isolates the C transpilation from the actual compilation in all of these, and inject Smelt own's Zig-based toolchain, to get truly standalone building (and enable *some* cross-compilation support)
* Provides a CLI frontend for devs to easily manipulate them (e.g. toggle native code on and off).
* Specifically for nuitka, provides a way to build native extensions module-per-module and have them share a common runtime.

### Building standalone binaries out of your package

* Smelt uses [Nuitka](https://nuitka.net/) to build standalone binaries from Python entry-points.
While Nuitka is designed for that purpose and already capable by itself of doing that, `smelt` enables the following on top of it:
* Plugging the pipeline for the other supported binary backends (Cython/mypyc) into Nuitka. Smelt will pre-build the native extensions, and hand the built files to Nuitka  (some backends like mypc generate additional runtime files that are not discovered by Nuitka automatically).
* Using the Zig compiler with Nuitka (**Note: Nuitka >=4.0.0 now supports zig compiler natively but it was not the case before**) - and thus benefit from its standalone-ness.
* Defining the flags for Nuitka from `pyproject.toml`, so that the build command is as simple as `smelt build-standalone-binary`

### Where to go next

* **[Get Started](get-started.md)** - a minimal working setup, one mypyc extension declared in `pyproject.toml`.
* **[Manifesto](manifesto.md)** - the full story behind the problems Smelt addresses, and the reasoning behind its approach.
* **[Build Hook](build-hook.md)** - the primary way of using Smelt: plugging it into your `pyproject.toml` as a Hatchling build hook so native extensions are built automatically whenever your project is built.
* **[CLI](cli.md)** - the `smelt` command-line tool, for driving builds manually, producing standalone binaries, and cleaning up build artifacts.
* **[Advanced Use Cases](advanced.md)** - cross-compilation support and its current limitations, plus a deep dive into how Smelt makes Nuitka behave as a per-module backend.
