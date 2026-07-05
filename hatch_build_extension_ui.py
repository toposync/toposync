from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        extension_root = Path(self.root)
        extension_name = extension_root.name
        ui_package = extension_root / "ui" / "package.json"
        static_entry = (
            extension_root
            / "src"
            / f"toposync_ext_{extension_name}"
            / "static"
            / "remoteEntry.js"
        )

        npm = shutil.which("npm")
        repo_root = extension_root.parent.parent
        if npm is not None and ui_package.is_file() and (repo_root / "package.json").is_file():
            subprocess.run(
                [npm, "run", "build:extension-ui", "--", extension_name],
                cwd=repo_root,
                check=True,
            )

        if not static_entry.is_file():
            raise RuntimeError(
                f"Missing extension UI bundle for {extension_name}. "
                f"Run `npm run build:extension-ui -- {extension_name}` before building the wheel."
            )
