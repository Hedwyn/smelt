# Advanced Use Cases

## Cross-compilation

Smelt provides experimental support for cross-compiling native extensions, leveraging the fact that [Zig](https://ziglang.org/) ships as a single, fully standalone toolchain capable of targeting other platforms without a separate sysroot - unlike a traditional C toolchain, which typically needs a matching cross-compiler and sysroot per target.

### Building for another platform

Cross-compilation is driven by the `-cp/--crosscompile` option of [`smelt compile-module`](cli.md#other-commands):

```
smelt compile-module my_project.hello --backend nuitka --crosscompile aarch64-linux
```

Under the hood this passes `-target <triple>` to `zig cc`/`zig build-lib`, the same mechanism Zig itself uses for cross-compiling C/C++ code.

### Current limitations

Cross-compilation in Smelt is still an experimental, narrow feature - keep the following in mind before relying on it:

* **Only the handwritten C/Zig extension path cross-compiles today.** `c_extensions` and `zig_modules` are cross-compiled correctly. The `mypyc`, `cython` and `nuitka` backends don't participate in cross-compilation yet: passing `--crosscompile` alongside `--backend mypyc|cython|nuitka` on `compile-module` is currently a no-op for those backends.
* **Linux-only targets, for now.** Only three target triples are currently supported: `aarch64-linux`, `arm-linux-gnueabihf` (armv7l) and `x86_64-linux`. There is no cross-compiling target for Windows or macOS yet.
* **A matching `pyconfig.h` is required for the target.** Building a Python C extension needs a `pyconfig.h` matching the target platform's Python ABI. Smelt bundles pre-generated ones for the triples above, but currently only for Python 3.12 - cross-compiling against a different interpreter version isn't supported out of the box.
* **Stability isn't guaranteed.** Smelt emits a runtime warning whenever cross-compilation is used; treat produced artifacts as experimental rather than production-ready until this matures.

## Nuitka internals

Nuitka is one of the binary backends Smelt orchestrates, but it fits the model differently from the others, and that difference drives a fair amount of machinery worth understanding if you rely on `nuitka_modules` heavily.

### The problem: Nuitka is whole-program, not per-module

`mypyc` and `cython` treat each module as an independent compilation unit: every module becomes its own extension `.so`, and any code they all need is factored into a single small shared runtime library (e.g. mypyc ships a `…__mypyc` runtime the modules link against). Compiling ten modules produces ten thin `.so`s plus one shared runtime.

Nuitka's primary mode of operation is the opposite: it is a *whole-program* compiler. You point it at an entrypoint and it produces a single artifact (one binary, or one extension module) that embeds everything it needs, including Nuitka's own C "static runtime" - the large body of C code that implements compiled functions, generators, calling conventions, the constants system, and so on.

Smelt also exposes Nuitka at the *module* level, so it can act like the other backends (one native `.so` per Python module). But because Nuitka has no notion of a shared runtime, naively compiling N modules this way re-embeds that entire static runtime N times - N copies of several megabytes of identical C code. The result is bloated binaries and much longer build times. To be a good per-module backend, Smelt has to build that runtime **once** and have every module `.so` link against it - the shared runtime library `lib__nuitka_runtime.so`.

### The constants blob, and why it complicates sharing

The one thing that stops the static runtime from being trivially shareable is how Nuitka handles **constants**.

A Python constant - a string literal, a number, a tuple, an interned name like `__name__` - is a live `PyObject` on the heap; it cannot be written as a C static literal, because it only exists relative to a running interpreter. So Nuitka *serializes* every constant a program uses into a compact binary **blob** that is embedded in the artifact, and materializes those objects at startup by decoding the blob through the CPython C-API. This is a deliberate optimization over emitting C code that builds each constant: the blob is far smaller, costs nothing to C-compile, and decodes in bulk at import for fast startup.

Crucially, this blob is a *whole-program* structure. It is a set of named sections: one universal section holding the constants every compiled module shares (empty tuple/dict, small integers, common interned names…), plus one section per module holding that module's own literals. At startup each module asks the runtime loader for its own section by name, while the universal constants are decoded once and shared. In a normal Nuitka build there is exactly one blob for the whole artifact, so a single loader that reaches for "the" blob is all that is needed.

That single-blob assumption is what breaks when modules become independent units. Each module `.so` must remain independently importable, so each carries **its own** blob (its own literals plus a copy of the universal constants). But the runtime code that *reads* the blob is written around one global accessor - so a single shared runtime would bind to whichever module happened to load first, and every other module would then be handed the wrong module's constants. Merging all modules into one blob is not an option either: that would fuse the modules back into a single unit and defeat the whole point of having independently-importable `.so`s.

### How Smelt handles it

The insight is that the coupling is only about *data*, not code: what must stay per-module is each module's blob and *which blob a module reads*; what can be shared is the (large) body of code that reads and uses constants.

So Smelt splits the build in two:

* **The shared runtime (`lib__nuitka_runtime.so`)** holds the generic static runtime and the universal constants that are identical across all modules. It is compiled once per build and every module links against it (found at load time via an `$ORIGIN` rpath, so it sits next to the modules in the package).
* **Each module `.so`** keeps its own constants blob and its own generated code, and simply *passes its blob* into the shared runtime's constants loader. The loader is made blob-agnostic - instead of pulling a single global blob, it receives the caller's blob - so every module reads its own constants while sharing all the surrounding machinery.

Concretely this touches only Nuitka's constants entrypoints: the loader (`loadConstantsBlob`) is given the blob pointer as an argument rather than fetching it from the per-module `getConstantBlobData`, and the module's generated call sites are rewritten to pass their own blob. Everything else - the bulk of the runtime - is compiled once and shared. The net effect matches the other backends: N thin module `.so`s plus one shared runtime, with the constants of each module kept private to it.

### Current limitation: packages themselves are not auto-compiled

`auto_mode` (`"package"`/`"all"`) walks a package's filesystem tree and proposes every `.py` file it finds as a compile target - except a package's own `__init__.py`, at any depth. This isn't a discovery oversight; Nuitka has no way to do it that fits Smelt's model.

Nuitka draws a hard line between compiling a *module* (point it at a `.py` file, get a `.so`) and compiling a *package* (point it at the package's *directory*, which transpiles the whole package - `__init__.py` and everything Nuitka can see under it - into a single artifact meant to *replace* that directory as the importable module). Smelt's per-module model, described above, keeps every original source file in place and drops a same-named `.so` next to it - there is no step where the source is removed. Applying that same model to a package's `__init__.py` would produce an `automode.cpython-*.so` sitting right next to the still-present `automode/` directory. Python's import system always resolves a name to a package directory before it considers a same-named extension module in that directory, so the freshly compiled `.so` would simply never be imported - dead weight, silently never taking effect.

Until Nuitka's whole-package compilation mode gets its own place in Smelt's model (package in, single replacement artifact out - a different shape from the shared-runtime scheme above), `auto_mode` skips packages entirely and only ever proposes their concrete submodules.
