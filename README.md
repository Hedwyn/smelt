# Smelt

Smelt is python tool that aims to simplify greatly shipping native code from Python projects. This notably covers:

* Providing a fully standalone way to compile native code in Python project without any system dependency.
* Cross-compiling Python C extensions
* Combining multiple tools building or providing native code in Python projects (e.g., mypyc, nuitka, local C/Zig extensions)
* Providing a single high-level interface to automate binary builds, either for platform-specific wheels or even for standalone fully compiled binary Python projects.

Documentation is available [here](https://hedwyn.github.io/smelt).

## Why Smelt

One usual headache with Python is to distribute the software. As an interpreted language, Python has some limitations compared to other compiled languages - first because shipping your Python code requires an interpreter on the target machine, and second because the whole nature of the language makes obfuscation difficult - which is a problem for close-source projects.
For the first problem, one simple solution is to ship an entire interpret toegether with the project, in order to make it a standalone binary. That's what a tool like PyInstaller does. It has limitations though:

* It tends to produced bloated binaries, as the entire interpreter is shipped, even though only a tiny part of the standard library might be used by the shipped project
* It does not obfuscate anything, as the source code is shipped directly; even if one were to only shipped the compiled `.pyc` files, theses ones still leak most of the information if it has not been pre-processed by tools such as PyArmor.

As a consequence, it's not uncommon to see Python-to-C transpilers such as Nuitka being used the sake of obfuscating, or to produce less bloated binaries. Transpiling to C actually allows eliminating dead code and also provides actual obfuscation - but it also makes the platform specific part more complex; as now the whole project needs to be compiled to the target platform - whereas methods based on interpreter building only have to bundle a pre-built interpreter binary for the target platform, the pure Python code itself being portable.<br>
Another dimension to that problem appears when one start using native code in their Python project- *Native code* being usually some kind of C extension (or other languages such as Rust or Zig). That native code also needs to be built per platform - even when an interpeter is already available on the target host. Packages that have native code will be built to platform-specific wheels; for major libraries, these wheels are usually pre-built and uploaded to the PyPi index, which means you often don't have to deal with that as a user of the library. However, for smaller projects (or closed-source ones) that might not a complex multi-platform build pipeline, people installing the library might have to compile the project locally. Compiling the native code usually implies dependency on the host system (at least a C compiler !), which might not be met all the times.<br>
Add to that that are now multiple tools that can provide native code in Python projects: standard C extensions, Rust extensions, mypyc compiled modules, nuitka-compiled modules, etc. These tools all have independant build tools and pipelines, which are themselves covered by multiple layers of abstraction in the Python build backend. That makes handling cross-platform distrubution (and even single-platform ones!) largely more complex that they would on a compiled programming language.<br><br>
Smelt aims to solve these problems with 4 axes:

* Making native code building completely self-contained by removing system dependency (such as C compiler) out of the equation, thus making a simple `pip install ...` enough to install your package with native code.
* Orchestrating the aforementioned native code solutions under one single interface, to allow automating the building of complex projects from a simple config file.
* Providing a self-contained cross-compiling solution for native code within Python projects, which is for now largely absent from the Python ecosystem.
* Providing standalone binary builds (=a single exe for an entire Python application) as a first-class citizen - with all the features mentioned in the bullet points above.

## Nuitkaify

Nuitka is one of the binary backends Smelt orchestrates, but it fits the model
differently from the others, and that difference drives a fair amount of machinery.

### The problem: Nuitka is whole-program, not per-module

`mypyc` and `cython` treat each module as an independent compilation unit: every
module becomes its own extension `.so`, and any code they all need is factored into a
single small shared runtime library (e.g. mypyc ships a `…__mypyc` runtime the modules
link against). Compiling ten modules produces ten thin `.so`s plus one shared runtime.

Nuitka's primary mode of operation is the opposite: it is a *whole-program* compiler.
You point it at an entrypoint and it produces a single artifact (one binary, or one
extension module) that embeds everything it needs, including Nuitka's own C "static
runtime" — the large body of C code that implements compiled functions, generators,
calling conventions, the constants system, and so on.

Smelt also exposes Nuitka at the *module* level, so it can act like the other backends
(one native `.so` per Python module). But because Nuitka has no notion of a shared
runtime, naively compiling N modules this way re-embeds that entire static runtime N
times — N copies of several megabytes of identical C code. The result is bloated
binaries and much longer build times. To be a good per-module backend, Smelt has to
build that runtime **once** and have every module `.so` link against it — the shared
runtime library `lib__nuitka_runtime.so`.

### The constants blob, and why it complicates sharing

The one thing that stops the static runtime from being trivially shareable is how
Nuitka handles **constants**.

A Python constant — a string literal, a number, a tuple, an interned name like
`__name__` — is a live `PyObject` on the heap; it cannot be written as a C static
literal, because it only exists relative to a running interpreter. So Nuitka
*serializes* every constant a program uses into a compact binary **blob** that is
embedded in the artifact, and materializes those objects at startup by decoding the
blob through the CPython C-API. This is a deliberate optimization over emitting C code
that builds each constant: the blob is far smaller, costs nothing to C-compile, and
decodes in bulk at import for fast startup.

Crucially, this blob is a *whole-program* structure. It is a set of named sections: one
universal section holding the constants every compiled module shares (empty tuple/dict,
small integers, common interned names…), plus one section per module holding that
module's own literals. At startup each module asks the runtime loader for its own
section by name, while the universal constants are decoded once and shared. In a normal
Nuitka build there is exactly one blob for the whole artifact, so a single loader that
reaches for "the" blob is all that is needed.

That single-blob assumption is what breaks when modules become independent units. Each
module `.so` must remain independently importable, so each carries **its own** blob
(its own literals plus a copy of the universal constants). But the runtime code that
*reads* the blob is written around one global accessor — so a single shared runtime
would bind to whichever module happened to load first, and every other module would
then be handed the wrong module's constants. Merging all modules into one blob is not
an option either: that would fuse the modules back into a single unit and defeat the
whole point of having independently-importable `.so`s.

### How Smelt handles it

The insight is that the coupling is only about *data*, not code: what must stay
per-module is each module's blob and *which blob a module reads*; what can be shared is
the (large) body of code that reads and uses constants.

So Smelt splits the build in two:

* **The shared runtime (`lib__nuitka_runtime.so`)** holds the generic static runtime
  and the universal constants that are identical across all modules. It is compiled
  once per build and every module links against it (found at load time via an
  `$ORIGIN` rpath, so it sits next to the modules in the package).
* **Each module `.so`** keeps its own constants blob and its own generated code, and
  simply *passes its blob* into the shared runtime's constants loader. The loader is
  made blob-agnostic — instead of pulling a single global blob, it receives the caller's
  blob — so every module reads its own constants while sharing all the surrounding
  machinery.

Concretely this touches only Nuitka's constants entrypoints: the loader
(`loadConstantsBlob`) is given the blob pointer as an argument rather than fetching it
from the per-module `getConstantBlobData`, and the module's generated call sites are
rewritten to pass their own blob. Everything else — the bulk of the runtime — is
compiled once and shared. The net effect matches the other backends: N thin module
`.so`s plus one shared runtime, with the constants of each module kept private to it.

### Current limitation: packages themselves are not auto-compiled

`auto_mode` (`"package"`/`"all"`) walks a package's filesystem tree and proposes every
`.py` file it finds as a compile target — except a package's own `__init__.py`, at any
depth. This isn't a discovery oversight; Nuitka has no way to do it that fits Smelt's
model.

Nuitka draws a hard line between compiling a *module* (point it at a `.py` file, get a
`.so`) and compiling a *package* (point it at the package's *directory*, which
transpiles the whole package — `__init__.py` and everything Nuitka can see under it —
into a single artifact meant to *replace* that directory as the importable module).
Smelt's per-module model, described above, keeps every original source file in place
and drops a same-named `.so` next to it — there is no step where the source is removed.
Applying that same model to a package's `__init__.py` would produce an
`automode.cpython-*.so` sitting right next to the still-present `automode/` directory.
Python's import system always resolves a name to a package directory before it
considers a same-named extension module in that directory, so the freshly compiled
`.so` would simply never be imported — dead weight, silently never taking effect.

Until Nuitka's whole-package compilation mode gets its own place in Smelt's model
(package in, single replacement artifact out — a different shape from the shared-runtime
scheme above), `auto_mode` skips packages entirely and only ever proposes their concrete
submodules.
