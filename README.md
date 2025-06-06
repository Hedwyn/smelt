# Smelt
Smelt is python tool that aims to simplify greatly shipping native code from Python projects. This notably covers:
* Providing a fully standalone way to compile native code in Python project without any system dependency.
* Cross-compiling Python C extensions
* Combining multiple tools building or providing native code in Python projects (e.g., mypyc, nuitka, local C/Zig extensions)
* Providing a single high-level interface to automate binary builds, either for platform-specific wheels or even for standalone fully compiled binary Python projects.

## Status
Smelt is currently under construction and is not ready-for-use -yet.

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




