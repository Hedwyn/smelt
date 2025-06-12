"""
Command-line interface for Smelt
"""

from __future__ import annotations

import os

import click
import tomllib

from smelt.backend import SmeltConfig, SmeltError, run_backend


class SmeltConfigError(SmeltError): ...


type TomlData = dict[str, str | list[str] | TomlData]


def parse_config(toml_data: TomlData) -> SmeltConfig:
    """ """
    c_extensions = toml_data.get("c_extensions", [])
    assert isinstance(c_extensions, list) and all(
        (isinstance(elem, str) for elem in c_extensions)
    ), c_extensions
    mypyc = toml_data.get("mypyc", [])
    assert isinstance(mypyc, list) and all(
        (isinstance(elem, str) for elem in c_extensions)
    ), mypyc
    entrypoint = toml_data.get("entrypoint", None)
    if entrypoint is None:
        # for now, raising
        raise SmeltConfigError("Defining an entrypoint for smelt is mandatory")
    assert isinstance(entrypoint, str), entrypoint
    return SmeltConfig(
        c_extensions=c_extensions,
        mypyc=mypyc,
        entrypoint=entrypoint,
    )


@click.group()
def smelt() -> None:
    pass


@smelt.command()
@click.option(
    "-p",
    "--path",
    default=".",
    type=str,
)
def show_config(path: str) -> None:
    try:
        with open(os.path.join(path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    smelt_config = toml_data.get("tool", {}).get("smelt", {})
    print(parse_config(smelt_config))


@smelt.command()
@click.option(
    "-p",
    "--package-path",
    default=".",
    type=str,
)
def build_standalone_binary(package_path: str) -> None:
    try:
        with open(os.path.join(package_path, "pyproject.toml"), "rb") as f:
            toml_data = tomllib.load(f)
    except FileNotFoundError:
        click.echo("No pyproject.toml not found.")
        return
    smelt_config = toml_data.get("tool", {}).get("smelt", {})
    if smelt_config == {}:
        click.echo("Targeted prject does not seem to use smelt: no smelt config found")
        return
    try:
        config = parse_config(smelt_config)
    except SmeltConfigError as exc:
        click.echo(f"Smelt config is incorrect: {exc}")
        return

    run_backend(config, stdout="stdout")
