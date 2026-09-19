from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        root = Path(self.root)
        static_entry = root / "src/toposync_ext_vision/static/remoteEntry.js"
        npm = shutil.which("npm")
        if (
            npm
            and (root / "ui/package.json").is_file()
            and (root.parent.parent / "package.json").is_file()
        ):
            subprocess.run(
                [npm, "run", "build:extension-ui", "--", "vision"],
                cwd=root.parent.parent,
                check=True,
            )
        if not static_entry.is_file():
            raise RuntimeError("Missing vision UI bundle; build:extension-ui -- vision is required")
        manifests_dir = root / "manifests"
        if not manifests_dir.is_dir():
            manifests_dir = root / "toposync_ext_vision" / "manifests"
        if not manifests_dir.is_dir():
            raise RuntimeError("Missing built-in manifest directory for toposync-ext-vision")

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("build_data.force_include must be a dictionary")

        force_include[str(manifests_dir)] = "toposync_ext_vision/manifests"
