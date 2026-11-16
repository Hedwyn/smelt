# Bundler: Overview

`smelt build-dist` assembles a standalone distribution for one `[project.scripts]` entrypoint: the entrypoint's own dependency closure, shipped as compiled extensions plus bytecode, optionally alongside an interpreter. This page covers what it actually does, and where it sits next to existing bundlers. For hands-on usage see [Get Started](bundler-get-started.md) and [Reference](bundler-reference.md); for the vocabulary used below (closure, discovery, payload, tailoring, keep-set) see the [Glossary](glossary.md).

## What gets shipped

Building a distribution starts by computing the entrypoint's **closure** - every module it needs, standard library and third-party alike - through static analysis, tracing, or both. The non-standard-library part of that closure becomes the payload: each module compiled by one of Smelt's own backends is shipped as the native artifact already produced for it, and everything else is shipped as bytecode (`.pyc`). Nothing here forces a whole-program compile: an application can be built entirely from a mypyc extension, a Nuitka module, and plain Python, and the distribution folder carries the three side by side, exactly as they were compiled for a regular build.

## `byo` versus `own`

The interpreter question is a separate axis from the payload. In `byo` mode ("bring your own [interpreter]", the default) the folder holds only the application - it runs on any machine with a matching CPython minor version already installed. In `own` mode, Smelt also builds a CPython interpreter (through `meta-python`) and ships it inside the folder, which then runs where nothing is installed at all.

An `own` interpreter is not simply copied wholesale: by default it is **tailored** down to the standard-library modules and native libraries the closure actually reaches, plus a small always-kept baseline (see Glossary - Minimal Viable Stdlib) that discovery cannot see on its own. Which parts of OpenSSL, SQLite, `libffi`, `bz2`, `lzma` or Tk get compiled in follows the same closure - an application that never touches `ssl` gets an interpreter built without OpenSSL at all, rather than one that has it but never loads it.

## One folder, one file, or fully static

Either mode can additionally be packed into a single file with `--onefile`: an executable zip application for `byo` (no compiler involved - `zipimport` reads it directly), or a compiled Zig launcher carrying the whole folder, interpreter included, as a compressed payload it extracts on first run for `own`. The folder is always the thing actually built; packing is a step on top of a finished one, not a second way of assembling one.

A further, more experimental axis is how tightly the `own` interpreter binds to the target's C library. The default `own` build still dynamically links `libc` and its own `libpython` - deliberately, since a portable `libc` replacement is a much harder problem than a portable Python. On `musl` targets, `--own-python-static` goes further and produces one monolithic static-PIE executable with no shared libraries of its own at all; getting there requires compiling every native module Smelt would otherwise `dlopen()` - both stdlib accelerators and Smelt's own extensions - straight into the interpreter, since a fully static binary has no working `dlopen()` left to load them with later.

## What this is not: obfuscation

`build-dist` does not transpile or obscure anything by itself. A module shipped as bytecode decompiles about as readily as its source; a module Smelt compiled with mypyc, Cython or Nuitka is exactly as opaque as that backend makes it, independent of the bundler. Anyone relying on a build for source protection should compile the sensitive modules with one of those backends rather than expect the bundling step itself to hide anything.

## Comparison with existing bundlers

| | Smelt (`build-dist`) | PyInstaller | Nuitka (standalone mode) | PyOxidizer |
| --- | --- | --- | --- | --- |
| How it assembles the app | ships bytecode + whatever native artifacts Smelt's backends already produced | freezes bytecode + collects native dependencies via its own analysis/hooks | transpiles the *whole program* to C and compiles it to native code | embeds interpreter and modules as in-memory resources in one compiled Rust binary |
| Source protection | none by itself - only as strong as the backend that compiled a given module | none - bytecode is shipped as-is | strong - source is transpiled away entirely | strong - no bytecode files on disk |
| Interpreter shipped | `byo`: none, needs a host match. `own`: built by Smelt, tailored to the app's closure | a full, generic copy of a real interpreter | Nuitka's own compiled runtime, not a copy of CPython | a statically embedded interpreter |
| Compile backend choice | per module: mypyc, Cython, Nuitka, or handwritten C/Zig | none - doesn't compile Python itself | Nuitka only, whole-program | none - doesn't compile Python itself |
| Static / libc-independent builds | experimental, musl targets only (`--own-python-static`) | not a target | not a target | one of its stated goals, though the project has seen little activity since 2023 |

Smelt also has a second, older way to produce a single native artifact: `smelt build-standalone-binary`, a thin CLI layer over Nuitka's own whole-program compile mode (see [CLI](cli.md#build-standalone-binary)). That command is the one closer in spirit to Nuitka/PyOxidizer's "transpile or embed the whole thing" approach; `build-dist` is closer in spirit to PyInstaller - assemble the app and (optionally) an interpreter, without requiring a whole-program compile.
