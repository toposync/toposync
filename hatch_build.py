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
        source_packaged_frontend_dist = root / "src" / "toposync" / "_frontend" / "dist"
        source_packaged_frontend_index = source_packaged_frontend_dist / "index.html"
        sdist_packaged_frontend_dist = root / "toposync" / "_frontend" / "dist"
        sdist_packaged_frontend_index = sdist_packaged_frontend_dist / "index.html"

        if not frontend_index.is_file():
            if source_packaged_frontend_index.is_file() or sdist_packaged_frontend_index.is_file():
                return

            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError(
                    "Missing frontend bundle at frontend/dist and no packaged host bundle was found. "
                    "Run `npm install && npm run build:frontend` before building toposync-core."
                )
            subprocess.run([npm, "run", "build:frontend"], cwd=root, check=True)

        if not frontend_index.is_file():
            raise RuntimeError(
                "Frontend build did not produce frontend/dist/index.html and no packaged host bundle was found. "
                "Run `npm run build:frontend` and try again."
            )

        shutil.rmtree(source_packaged_frontend_dist, ignore_errors=True)
        source_packaged_frontend_dist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(frontend_dist, source_packaged_frontend_dist, dirs_exist_ok=True)
