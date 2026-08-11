from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_distribution_smoke_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_distribution_install_script",
        ROOT / "scripts/test_distribution_install.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distribution_smoke_uses_shared_frontend_builds(monkeypatch) -> None:
    smoke = _load_distribution_smoke_script()
    commands: list[list[str]] = []
    monkeypatch.setattr(smoke, "_run", lambda command: commands.append(command))

    smoke._build_frontends()

    assert commands == [
        ["npm", "run", "build:frontend"],
        ["npm", "run", "build:extensions"],
    ]


def test_distribution_workflow_validates_extension_wheels() -> None:
    workflow = (ROOT / ".github/workflows/distribution-smoke.yml").read_text(encoding="utf-8")

    wheel_check = "python scripts/check_extension_wheels.py"
    distribution_smoke = "python scripts/test_distribution_install.py"
    assert wheel_check in workflow
    assert workflow.index(wheel_check) < workflow.index(distribution_smoke)


def test_ptz_attention_build_configuration_is_sdist_portable() -> None:
    extension_root = ROOT / "extensions" / "ptz_attention"
    config = tomllib.loads((extension_root / "pyproject.toml").read_text(encoding="utf-8"))
    custom_hook = (
        config.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("hooks", {})
        .get("custom", {})
        .get("path")
    )
    if custom_hook:
        hook_path = (extension_root / str(custom_hook)).resolve()
        assert hook_path.is_relative_to(extension_root.resolve())
        assert hook_path.is_file()

    package_root = extension_root / "src" / "toposync_ext_ptz_attention"
    assert (package_root / "extension.json").is_file()
    assert (package_root / "static" / "remoteEntry.js").is_file()
