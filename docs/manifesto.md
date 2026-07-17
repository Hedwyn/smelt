# Manifesto

## The problem with Python projects distribution

One usual headache with Python is software distribution. As an interpreted language, Python has some limitations compared to other compiled languages - first because shipping your Python code requires an interpreter on the target machine, and second because the dynamic nature of the language makes obfuscation difficult - which is a problem for closed-source projects.

Regarding the distribution issue, one simple solution is to ship an entire interpreter together with the project, in order to make it a standalone binary. That's what tools like PyInstaller do, for example. It has limitations though:

* It tends to produce bloated binaries, as the entire interpreter is shipped, even though only a tiny part of the standard library might be used by the shipped project.
* It does not obfuscate anything, as the source code is shipped directly; even if one were to only ship the compiled `.pyc` files, these still leak most of the information if it has not been pre-processed by obfuscation tools.

As a consequence, it's not uncommon to see Python-to-C transpilers such as Nuitka being used for the sake of obfuscating, or to produce less bloated binaries. Transpiling to C actually allows eliminating dead code and also provides actual obfuscation - but it also makes the platform-specific part more complex, as now the whole project needs to be compiled for the target platform - whereas methods based on interpreter bundling only have to bundle a pre-built interpreter binary for the target platform, the pure Python code itself being portable.

Another dimension to that problem appears when one starts using native code in their Python project - *native code* being usually some kind of C extension (or other languages such as Rust or Zig). That native code also needs to be built per platform - even when an interpreter is already available on the target host. Packages that have native code will be built into platform-specific wheels; for major libraries, these wheels are usually pre-built and uploaded to the PyPI index, which means you often don't have to deal with that as a user of the library. However, for smaller projects (or closed-source ones) that might not have a complex multi-platform build pipeline, people installing the library might have to compile the project locally. Compiling the native code usually implies dependency on the host system (at least a C compiler!), which might not be met all the time.

Add to that the fact there are now multiple tools that can provide native code in Python projects: standard C extensions, Rust extensions, mypyc-compiled modules, Nuitka-compiled modules, etc. These tools all have independent build tools and pipelines, which are themselves covered by multiple layers of abstraction in the Python build backend. That makes handling cross-platform distribution (and even single-platform ones!) considerably more complex than it would be with a compiled programming language.

## Smelt's key ingredients

Smelt aims to solve these problems along 4 axes:

* Making native code building completely self-contained by removing system dependencies (such as a C compiler) from the equation, thus making a simple `pip install ...` enough to install a package with native code. This is achieved by leveraging [Zig](https://ziglang.org/) as the compiler backend: Zig ships as a fully standalone, self-contained toolchain, and is itself available as a PyPI package (`ziglang`).
* Orchestrating the aforementioned native code solutions under one single interface, to allow automating the build of complex projects from a simple config file. Smelt provides a unified, modern, `pyproject.toml`-based API on top of several existing Python-to-binary tools: mypyc, Cython, Nuitka, and handwritten C/Zig extensions.
* Providing a self-contained cross-compiling solution for native code within Python projects, which is for now largely absent from the Python ecosystem.
* Providing standalone binary builds (a single executable for an entire Python application) as a first-class citizen - with all the features mentioned in the bullet points above.
