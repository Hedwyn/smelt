# Glossary

The vocabulary used by Smelt's application bundler (`smelt build-dist`) and by the
code that implements it. Defined here once; the modules involved point back at this
page rather than re-explaining the terms.

## Closure

**The set of Python modules an entrypoint needs at runtime, computed as the transitive
closure of the "imports" relation starting from that entrypoint.**

That is the mathematical sense of the word: start with the entrypoint module, add
everything it imports, then everything *those* import, and repeat until nothing new
appears. Implemented by `smelt.dist.collect_closure`, which returns
`{import_path: ResolvedModule}`.

What it contains:

* **every module, not every distribution.** A closure is a set of import paths
  (`click.core`, `json.decoder`, `_ssl`), not a list of packages from
  `pyproject.toml`. A dependency list says what to install; a closure says what is
  imported. The two differ a lot: a distribution with no dependencies of its own still
  reaches deep into the standard library.
* **the standard library and third-party code alike**, each entry classified by
  `smelt.explorer.ResolvedModule` — its kind (source, extension module, namespace
  package, builtin, frozen, unresolvable), its file on disk, and whether it belongs to
  the standard library.
* **the packages its members live under.** Importing `a.b.c` also imports `a` and
  `a.b`, so those are in the closure even when nothing names them directly.

How it is computed — `DiscoveryMode`, one of:

* `static`: parse each module and follow the import statements found anywhere in its
  AST, including inside functions and conditional branches;
* `trace`: import the entrypoint in a subprocess and record what actually landed in
  `sys.modules`;
* `both` (the default): the union of the two.

### A closure is an estimate, in both directions

This matters more than the definition, and it is why `include-modules` and
`exclude-modules` exist:

* **it can miss modules.** An import performed on a computed name
  (`importlib.import_module("plugins." + name)`), from a plugin registry, or lazily on
  a code path not taken during tracing, is invisible. Name those with
  `include-modules` / `include-package`, exactly as Nuitka requires for the same
  reason.
* **it can include modules that are never used.** The static walk follows imports
  written inside functions, which is correct for application code (a lazy import is a
  real dependency) but pulls in far too much from the standard library, whose modules
  reference each other from rarely-taken helpers. One import statement inside
  `difflib._test()` is enough to drag `doctest`, `unittest`, `asyncio` and `ssl` into
  the closure of an application that uses none of them.

So a closure is neither sound nor complete: it is a best-effort estimate with manual
corrections available on both sides.

### Two consumers, one closure

The same closure is used for two different purposes, which is worth keeping straight:

1. **assembling the payload** — its non-standard-library members become the `.pyc`
   files and native artifacts shipped in the distribution's `app/` directory;
2. **tailoring the interpreter** (mode `own` only) — its *standard-library* members
   decide which parts of the shipped CPython to keep. See **keep-set** below.

## Discovery

The act of computing a closure, and the `discovery` option that selects how (see
`DiscoveryMode` above). "Discovery reached X" means "X is in the closure".

## Distribution folder, payload

The folder `smelt build-dist` produces. Its layout:

```
myapp.dist/
  smelt-dist.json        manifest: what went in, and which interpreter it needs
  HOW_TO_RUN.txt
  app/                   the payload
  bin/  lib/             the interpreter, in mode `own` only
  myapp  myapp.cmd       launcher shim, in mode `own` only
```

The **payload** is the `app/` subdirectory: the application's own modules, its
dependencies, native artifacts and their bundled shared libraries. It is the only
directory placed on `sys.path`, which is why it is kept out of the distribution root —
a top-level module named `lib` or `bin` would otherwise collide with the interpreter's
own directories.

## Mode `byo` / mode `own`

Which interpreter a distribution runs on, selected by `--python`:

* **`byo`** ("bring your own python", the default): the application only. Running it
  needs a CPython of the same minor version already installed on the target.
* **`own`**: the same folder plus an interpreter Smelt built itself (through
  `meta-python`), so nothing needs to be installed on the target.

## Tailoring

Mode `own` only: choosing the *contents* of the shipped interpreter from the closure,
rather than shipping all of it. Two levers, both driven by the same closure:

* standard-library trees and extension modules the closure does not reach are pruned
  when the interpreter is staged into the distribution;
* third-party libraries no kept extension module needs (OpenSSL, SQLite, libffi, bz2,
  lzma, Tk) are turned off at build time, so the interpreter is compiled without them.

Controlled by `--tailor-interpreter` / `--no-tailor-interpreter`.

## Keep-set, bootstrap set, Minimal Viable Stdlib

When tailoring, the **keep-set** is what the staged interpreter is allowed to contain.
It is the union of three sources:

1. the **closure**'s standard-library members — what the application was found to need;
2. the **bootstrap set** — what the interpreter itself imports before any application
   code runs, measured by asking it (`bin/python -I -S -c "import sys;
   print(sorted(sys.modules))"`) rather than hardcoded, since it is version-dependent;
3. the **Minimal Viable Stdlib** — see below.

Anything outside the keep-set is pruned. After pruning, every member of the keep-set
must still resolve in the staged interpreter, or the build fails: shipping a folder
that raises `ImportError` on the target is worse than not shipping one.

### Minimal Viable Stdlib

What a shipped interpreter keeps whatever the closure says, because **a closure is a
lower bound on what a running Python needs**. Two kinds of import can never appear in
one: those resolved by name at runtime, and those made only after something has
already failed. Both are needed exactly when they are missing.

It is a module-level constant — `MINIMAL_VIABLE_STDLIB` in
`src/smelt/own_python.py`, a tuple of `StdlibGroup` — **not** an environment variable
and not a configuration key. Changing its membership is a code change, deliberately:
it encodes a correctness invariant, not a preference. Each group's members include
their own transitive imports, measured by importing the group's seeds in a built
interpreter and reading `sys.modules` (keeping `traceback` while pruning `textwrap`
would leave the safety net raising on its way to reporting an error).

| group | holds | droppable |
|---|---|---|
| `interpreter_core` | the import system, path handling, `runpy`, `zipimport`+`zlib` and their transitive imports | no — nothing would start |
| `exception_handling` | `traceback`, `linecache`, `warnings`, `tokenize`, `token`, `textwrap` | no — 110 KB does not justify the footgun |
| `text_codecs` | `encodings`, as a whole package | no — codecs are looked up by name at runtime |
| `international_hostnames` | `stringprep`, `unicodedata` | **yes** — 28 KB today, 1.1 MB later (see below) |

Groups exist so the reasoning is legible, and so the one boundary worth choosing can
be chosen. A group is only offered as a knob when dropping it saves something real
*and* the loss can be stated in a sentence; `StdlibGroup.__post_init__` refuses an
optional group with no stated consequence.

Dropping `international_hostnames` is measured, and the number is smaller than it
looks: **28 KB today**, not the 1128 KB `unicodedata` weighs. The reason is worth
knowing, because it is the same one behind the over-collection described under
*Closure*: `traceback` imports `unicodedata` from inside `_display_width()`, and the
static walk follows imports written in function bodies — so the closure asks for
`unicodedata` on its own account and this group cannot decide to drop it. The full
1128 KB becomes available once deferred imports inside the standard library stop being
followed.

The consequence, verified either way: every other codec and `socket`/`urllib`/`email`
are unaffected, and the single loss is that `"héllo.example".encode("idna")` — and
anything resolving an internationalised hostname — raises
`LookupError: unknown encoding: idna`. Where `unicodedata` goes too, a traceback
quoting a source line with wide characters also loses its caret alignment.

```
smelt build-dist --python own --drop-stdlib-group international_hostnames
```

or per entrypoint:

```toml
[tool.smelt.entrypoints."pkg.cli:main"]
python = "own"
drop-stdlib-groups = ["international_hostnames"]
```

Naming a group that is not droppable, or one that does not exist, is an error that
names the droppable ones. To *add* to the keep-set instead, use `include-modules` /
`--include-module`; to skip tailoring altogether, `--no-tailor-interpreter`.
