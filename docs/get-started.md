# Get Started

Install Smelt and Hatchling as build requirements, and declare a mypyc module to compile - here, a plain recursive `fib` function:

`src/fib_demo/fib.py`:

```python
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 2) + fib(n - 1)
```

`pyproject.toml`:

```toml
[build-system]
requires = ["hatchling", "smelt"]
build-backend = "hatchling.build"

[project]
name = "fib-demo"
version = "0.1.0"
requires-python = ">=3.12"

[tool.hatch.build.targets.wheel]
packages = ["src/fib_demo"]

[tool.hatch.build.hooks.smelt]
packages_location = { "fib_demo" = "src/fib_demo" }

[[tool.hatch.build.hooks.smelt.mypyc_modules]]
import_path = "fib_demo.fib"
```

That's it - no `hello.c`, no manual `setup.py`. Building the project (e.g. `pip install .`, or `python -m build`) compiles `fib.py` with mypyc and drops a native `fib*.so` right next to it, which then shadows the pure-Python source: `import fib_demo.fib` transparently loads the compiled extension.

See [Build Hook](build-hook.md) for the full configuration reference (mypyc, Cython, Nuitka, and handwritten C/Zig extensions), and [CLI](cli.md) for driving builds outside of a Hatchling build.

## Basic commands

Installing Smelt with the `cli` extra (`pip install "smelt[cli]"`) also gives you the `smelt` command, which can drive the same `fib-demo` project by hand - handy to inspect or rebuild extensions without going through a full `pip install`/`build`. `smelt --help` lists every available command; see [CLI](cli.md) for the full reference. A few of them, run from the `fib-demo` project root:

**`smelt show-config`** parses `pyproject.toml` and prints the config Smelt actually resolved - useful to confirm your `mypyc_modules`/`c_extensions`/... declarations were picked up correctly:

```
$ smelt show-config
SmeltConfig(packages_location={'fib_demo': 'src/fib_demo'},
            ...
            mypyc_modules=[MypycModule(import_path='fib_demo.fib',
                                       source=None,
                                       extras=[])],
            ...)
```

**`smelt build-extensions`** runs the same compilation step the build hook runs during `pip install`/`build`, without needing a full build. For `fib-demo` this compiles `fib.py` with mypyc and drops two files next to it: the compiled module itself, and its small mypyc runtime:

```
$ smelt build-extensions
$ ls src/fib_demo
__init__.py  fib.cpython-312-x86_64-linux-gnu.so  fib__mypyc.cpython-312-x86_64-linux-gnu.so  fib.py
```

`fib.py` is still there, but it's now shadowed: `import fib_demo.fib` loads the compiled `.so`, not the source file.

**`smelt clean-artifacts`** reverses that: it deletes the compiled `.so`s (and the mypyc runtime alongside them), unshadowing `fib.py` back to being the one Python imports - handy to reset a checkout to pure Python:

```
$ smelt clean-artifacts
Deleted following artifacts:
fib_demo.fib:
  - src/fib_demo/fib.cpython-312-x86_64-linux-gnu.so (was shadowing src/fib_demo/fib.py)
  - src/fib_demo/fib__mypyc.cpython-312-x86_64-linux-gnu.so
```
