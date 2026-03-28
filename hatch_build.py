from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        root = Path(self.root)
        frontend_dist = root / "frontend" / "dist"
        frontend_index = frontend_dist / "index.html"

        if not frontend_index.is_file():
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError(
                    "Missing frontend bundle at frontend/dist and npm is not available. "
                    "Run `npm install && npm run build:frontend` before building toposync-core."
                )
            subprocess.run([npm, "run", "build:frontend"], cwd=root, check=True)

        if not frontend_index.is_file():
            raise RuntimeError(
                "Frontend build did not produce frontend/dist/index.html. "
                "Run `npm run build:frontend` and try again."
            )

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("build_data.force_include must be a dictionary")
        force_include[str(frontend_dist)] = "src/toposync/_frontend/dist"
