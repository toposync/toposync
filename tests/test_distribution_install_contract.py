from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement


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


def test_distribution_smoke_accepts_manifest_declared_frontends() -> None:
    smoke = _load_distribution_smoke_script()
    payload = [
        {
            "id": identifier,
            "frontend": {"remote_entry_url": smoke.EXPECTED_FRONTEND_REMOTES[identifier]},
        }
        if identifier in smoke.EXPECTED_FRONTEND_REMOTES
        else {"id": identifier}
        for identifier in smoke.EXPECTED_EXTENSION_IDS
    ]

    assert smoke._assert_extensions(payload) == sorted(smoke.EXPECTED_FRONTEND_REMOTES.values())
    vision = next(item for item in payload if item["id"] == "com.toposync.vision")
    vision.pop("frontend")
    with pytest.raises(RuntimeError, match="missing frontend metadata"):
        smoke._assert_extensions(payload)


def test_distribution_workflow_validates_extension_wheels() -> None:
    workflow = (ROOT / ".github/workflows/distribution-smoke.yml").read_text(encoding="utf-8")

    wheel_check = "python scripts/check_extension_wheels.py"
    distribution_smoke = "python scripts/test_distribution_install.py"
    assert wheel_check in workflow
    assert workflow.index("npm run build:extensions") < workflow.index(wheel_check)
    assert workflow.index(wheel_check) < workflow.index(distribution_smoke)


def test_distribution_workflow_uses_supported_playwright_runner() -> None:
    workflow = (ROOT / ".github/workflows/distribution-smoke.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-24.04" in workflow


@pytest.mark.parametrize("missing_vision_frontend", [False, True])
def test_distribution_smoke_requires_declared_vision_frontend(missing_vision_frontend) -> None:
    smoke = _load_distribution_smoke_script()
    payload = []
    for extension_id in smoke.EXPECTED_EXTENSION_IDS:
        item = {"id": extension_id}
        if extension_id in smoke.EXPECTED_FRONTEND_REMOTES:
            item["frontend"] = {"remote_entry_url": smoke.EXPECTED_FRONTEND_REMOTES[extension_id]}
        payload.append(item)
    vision_path = smoke.EXPECTED_FRONTEND_REMOTES["com.toposync.vision"]
    if missing_vision_frontend:
        next(item for item in payload if item["id"] == "com.toposync.vision").pop("frontend")
        with pytest.raises(RuntimeError, match="missing frontend metadata"):
            smoke._assert_extensions(payload)
    else:
        assert vision_path in smoke._assert_extensions(payload)


@pytest.mark.parametrize("bundle", sorted((ROOT / "packages").glob("*/pyproject.toml")))
def test_bundle_dependencies_accept_current_local_projects(bundle: Path) -> None:
    manifests = [ROOT / "pyproject.toml", *ROOT.glob("extensions/*/pyproject.toml"),
                 *ROOT.glob("packages/*/pyproject.toml")]
    projects = [tomllib.loads(path.read_text())["project"] for path in manifests]
    versions = {project["name"]: project["version"] for project in projects}
    project = tomllib.loads(bundle.read_text())["project"]
    for dependency in project["dependencies"]:
        requirement = Requirement(dependency)
        if requirement.name in versions:
            assert requirement.specifier.contains(versions[requirement.name], prereleases=True), (
                f"{project['name']} requires {requirement}, but the local version is "
                f"{versions[requirement.name]}"
            )


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


def test_all_extension_build_hooks_are_sdist_portable() -> None:
    for manifest in sorted((ROOT / "extensions").glob("*/pyproject.toml")):
        config = tomllib.loads(manifest.read_text(encoding="utf-8"))
        custom_hook = (
            config.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("hooks", {})
            .get("custom", {})
            .get("path")
        )
        if custom_hook:
            hook_path = (manifest.parent / custom_hook).resolve()
            assert hook_path.is_relative_to(manifest.parent.resolve()), manifest
            assert hook_path.is_file(), manifest
