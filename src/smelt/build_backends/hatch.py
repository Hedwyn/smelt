"""
Build hook hatchling backend.

@date: 03.09.2025
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
from functools import cached_property
from dataclasses import fields

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl

from smelt.backend import SmeltConfig, run_backend
from smelt.utils import ModpathType


class HatchlingBuildHook(BuildHookInterface):
    PLUGIN_NAME = "smelt"

    @cached_property
    def is_debug(self) -> bool:
        if not self.smelt_config.debug:
            return False
        print("Smelt: SMELT_DEBUG is set, enabling debug mode")
        return True

    @cached_property
    def smelt_config(self) -> SmeltConfig:
        try:
            # config = SmeltConfig(**self.config)
            config = SmeltConfig.from_toml_data(self.config)
        except Exception as exc:
            raise ValueError(
                "Smelt config is invalid:"
                f"Current config: {self.config}"
                "Valid parameters are:\n"
                f"{[f.name for f in fields(SmeltConfig)]}"
            ) from exc
        config.load_env()
        return config

    def debug_log(self, message: str) -> None:
        """
        Prints `message` if debug mode is set.
        """
        if not self.is_debug:
            return
        print(message)

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        self.debug_log(f"Smelt: Calling build hook with config:\n{self.config}")
        try:
            run_backend(
                self.smelt_config, strategy=ModpathType.FS, without_entrypoint=True
            )
        except Exception as exc:
            raise RuntimeError(f"Smelt build failed: {exc}")


@hookimpl
def hatch_register_build_hook() -> type[BuildHookInterface]:
    """
    Registers Smelt's build hook as a hatch plugin
    """
    return HatchlingBuildHook
