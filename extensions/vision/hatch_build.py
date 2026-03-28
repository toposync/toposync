from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        root = Path(self.root)
        manifests_dir = root / "manifests"
        if not manifests_dir.is_dir():
            raise RuntimeError("Missing built-in manifest directory: extensions/vision/manifests")

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("build_data.force_include must be a dictionary")

        force_include[str(manifests_dir)] = "toposync_ext_vision/manifests"

        models_dir = root / "models"
        if models_dir.is_dir():
            force_include[str(models_dir)] = "toposync_ext_vision/models"
