"""
Isolated functional tests, based on Nox.
Spawns packages with various config and verifies
that the Smelt build hook processes them properly.

@date: 11.12.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

import nox

if TYPE_CHECKING:
    from nox import Session


@dataclass
class PackageConfig:
    """
    Defines which type of extensions should be generated
    in the temporary package.
    """

    has_nuitka: bool = True
    has_mypyc: bool = True
    has_cython_pyx: bool = True
    has_cython_py: bool = True

    def build_pyproject(self, project_name: str) -> str:
        """
        Generates the `pyproject.toml` content based
        on the defined extensions types.
        """
        sections: list[str] = [PYPROJECT_TEMPLATE]
        if self.has_nuitka:
            sections.append(SMELT_NUITKA_SECTION)
        if self.has_mypyc:
            sections.append(SMELT_MYPYC_SECTION)
        if self.has_cython_py:
            sections.append(SMELT_CYTHON_SECTION)

        return "\n".join([h.format(project_name=project_name) for h in sections])


FIB_CYTHON = """\
def fibx(int n):
    cdef int i, a, b
    a, b = 0, 1
    for i in range(n):
        a, b = a + b, a
    return a\
"""

FIB_PYTHON = """\
def {fib_name}(n: int) -> int:
    if n <= 1:
        return n
    else:
        return {fib_name}(n - 2) + fib(n - 1)\
"""

PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling", "smelt", "mypy[mypyc]", "Cython", "Nuitka"]
build-backend = "hatchling.build"

[project]
name = "{project_name}"
version = "0.1.0"
dependencies = ["click"]

[project.scripts]
compute-fib = "{project_name}.cli:compute_fib"

[tool.hatch.build.targets.wheel]
packages = ["src/{project_name}"]
"""

SMELT_NUITKA_SECTION = """\
[tool.hatch.build.hooks.smelt]
entrypoint = "{project_name}.cli"

"""

SMELT_CYTHON_SECTION = """\
[tool.hatch.build.hooks.smelt.cython.remapped_modules]
"src/{project_name}/fib_cython.pyx" = "fib_cython"\
"""

SMELT_MYPYC_SECTION = """\
[tool.hatch.build.hooks.smelt.mypyc]
"{project_name}.fib" = "src/{project_name}/fib.py"
"""

CLI_ENTRYPOINT = """\
import click
from {project_name}.fib_pure_python import fib_pure_python
@click.command()
@click.argument("n", type=int)
def compute_fib(n: int) -> None:
    click.echo(fib_pure_python(n))
"""

NUITKA_ONLY_CONFIG = PackageConfig(
    has_nuitka=True,
    has_cython_py=False,
    has_cython_pyx=False,
    has_mypyc=False,
)


@contextmanager
def spawn_package(
    project_name: str, config: PackageConfig | None = None
) -> Generator[Path, None, None]:
    """
    Generates a package in a temporary folder,
    with the `pyproject.toml` and source files generated
    as defined by `config`.
    """
    config = config or PackageConfig()
    with tempfile.TemporaryDirectory() as dir:
        root = Path(dir)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(config.build_pyproject(project_name=project_name))

        src_dir: Path = root / "src" / project_name
        os.makedirs(src_dir)

        (src_dir / "fib_pure_python.py").write_text(
            FIB_PYTHON.format(fib_name="fib_pure_python")
        )
        if config.has_mypyc:
            (src_dir / "fib_mypyc.py").write_text(
                FIB_PYTHON.format(fib_name="fib_mypyc")
            )
        if config.has_cython_py:
            (src_dir / "fib_cython.py").write_text(
                FIB_PYTHON.format(fib_name="fib_cython")
            )
        if config.has_cython_pyx:
            (src_dir / "fib_cython.pyx").write_text(
                FIB_CYTHON.format(fib_name="fib_cython_pyx")
            )
        if config.has_nuitka:
            (src_dir / "cli.py").write_text(
                CLI_ENTRYPOINT.format(project_name=project_name)
            )
        yield root


def test_fib_sanity_check() -> None:
    """
    Checks the validaity of the fibonacci code template.
    """
    namespace: dict[str, Any] = {}
    exec(FIB_PYTHON.format(fib_name="fib"), locals=namespace, globals=namespace)
    assert "fib" in namespace
    assert eval("fib(10)", locals=namespace, globals=namespace) == 55


@nox.session
def spawn_package_sanity_check(session: Session) -> None:
    """
    Checks that a basic package can be generated and installed.
    """
    project_name = "testproject"
    with spawn_package(
        project_name=project_name, config=NUITKA_ONLY_CONFIG
    ) as proj_dir:
        session.install(str(proj_dir))
        # sanity check: importing the package we jsut installed
        session.run("python", "-c", f'"import {project_name}"')
        session.install("pytest")
        session.run("python", "-m", "pytest", "--package", project_name, "--pdb")
