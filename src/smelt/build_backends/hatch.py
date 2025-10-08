"""
Build hook hatchling backend.

@date: 03.09.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import fields

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl

from smelt.backend import SmeltConfig, run_backend
from smelt.utils import ModpathType


class HatchlingBuildHook(BuildHookInterface):
    PLUGIN_NAME = "smelt"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        try:
            config = SmeltConfig(**self.config)
        except Exception as exc:
            raise ValueError(
                "Smelt config is invalid:"
                f"Current config: {self.config}"
                "Valid parameters are:\n"
                f"{[f.name for f in fields(SmeltConfig)]}"
            ) from exc
        try:
            run_backend(config, strategy=ModpathType.FS, without_entrypoint=True)
        except Exception as exc:
            raise RuntimeError(f"Smelt build failed: {exc}")


@hookimpl
def hatch_register_build_hook() -> type[BuildHookInterface]:
    """
    Registers Smelt's build hook as a hatch plugin
    """
    return HatchlingBuildHook
