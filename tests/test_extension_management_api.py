from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.extensions.manifest import ExtensionManifest
import toposync.extensions.manager as ext_manager_mod
from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.extension_management import (
    EXTENSION_MANAGEMENT_CORE_KEY,
    InstalledExtensionProbe,
    PipOperationResult,
)
import toposync.runtime.extension_management as extension_management


class _FakeDist:
    version = "0.1.0"
    metadata = {"Name": "toposync-ext-active"}


class _FakeEntryPoint:
    name = "active"
    value = "fake:ActiveExtension"
    dist = _FakeDist()

    def load(self):
        return _ActiveExtension


class _ActiveExtension:
    def manifest(self) -> ExtensionManifest:
        return ExtensionManifest(
            id="com.test.active",
            name="Active",
            version="0.1.0",
        )


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, entry_points: list[object]) -> None:
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: entry_points)
    monkeypatch.setattr(extension_management, "_iter_entry_points", lambda _group: entry_points)


def _create_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry_points: list[object] | None = None,
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_EXTENSION_AUTO_INSTALL_ON_STARTUP", "0")
    _patch_entry_points(monkeypatch, list(entry_points or []))
    return TestClient(create_app())


def _item_by_id(catalog: dict, extension_id: str) -> dict:
    for item in catalog["items"]:
        if item["extension_id"] == extension_id:
            return item
    raise AssertionError(f"missing item {extension_id}")


def test_extension_management_catalog_includes_loaded_and_recommended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch, entry_points=[_FakeEntryPoint()]) as client:
        response = client.get("/api/extensions/manage")

    assert response.status_code == 200
    catalog = response.json()
    active = _item_by_id(catalog, "com.test.active")
    streaming = _item_by_id(catalog, "com.toposync.streaming")

    assert active["status"] == "active"
    assert active["loaded"] is True
    assert active["installed"] is True
    assert streaming["recommended"] is True
    assert streaming["pip_spec"] == "toposync-ext-streaming"
    assert streaming["status"] in {"not_installed", "pending_restart", "active"}


def test_extension_management_disable_takes_effect_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch, entry_points=[_FakeEntryPoint()]) as client:
        response = client.post("/api/extensions/manage/com.test.active/disable")
        assert response.status_code == 200
        body = response.json()
        active = _item_by_id(body["catalog"], "com.test.active")
        assert active["status"] == "pending_restart"
        assert body["catalog"]["restart_required"] is True

    with _create_client(tmp_path, monkeypatch, entry_points=[_FakeEntryPoint()]) as client:
        loaded = client.get("/api/extensions").json()
        catalog = client.get("/api/extensions/manage").json()

    assert [item["id"] for item in loaded] == []
    active = _item_by_id(catalog, "com.test.active")
    assert active["status"] == "disabled"
    assert active["loaded"] is False
    assert active["installed"] is True


def test_extension_management_manual_install_requires_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/extensions/manage/install", json={"pip_spec": "requests"})

    assert response.status_code == 400
    assert "toposync-ext-" in response.json()["detail"]


def test_extension_management_disable_rejects_unknown_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/extensions/manage/com.test.unknown/disable")

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown extension"


@pytest.mark.parametrize(
    ("pip_spec", "expected_upgrade"),
    [
        ("toposync-ext-extra", True),
        ("toposync-ext-extra>=0.1,<1", True),
        ("toposync-ext-extra==0.1.0", False),
    ],
)
def test_extension_management_manual_install_records_desired_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pip_spec: str,
    expected_upgrade: bool,
) -> None:
    installed = False
    install_calls: list[tuple[str, bool]] = []

    async def fake_install(pip_spec: str, *, upgrade: bool = False) -> PipOperationResult:
        nonlocal installed
        installed = True
        install_calls.append((pip_spec, upgrade))
        return PipOperationResult(ok=True, command=["pip", "install", pip_spec])

    def fake_discover() -> dict[str, InstalledExtensionProbe]:
        if not installed:
            return {}
        return {
            "com.test.extra": InstalledExtensionProbe(
                extension_id="com.test.extra",
                name="Extra",
                version="0.1.0",
                package="toposync-ext-extra",
                package_version="0.1.0",
                entry_point_name="extra",
                entry_point_value="toposync_ext_extra.plugin:ExtraExtension",
            )
        }

    monkeypatch.setattr(extension_management, "run_pip_install", fake_install)
    monkeypatch.setattr(extension_management, "discover_installed_extensions", fake_discover)

    with _create_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/extensions/manage/install",
            json={"pip_spec": pip_spec},
        )
        settings = client.get("/api/settings").json()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert install_calls == [(pip_spec, expected_upgrade)]
    extra = _item_by_id(body["catalog"], "com.test.extra")
    assert extra["status"] == "pending_restart"
    desired = settings["core"]["extension_management"]["desired"]
    assert desired == [
        {
            "pip_spec": pip_spec,
            "package": "toposync-ext-extra",
            "extension_id": "com.test.extra",
            "source": "manual",
        }
    ]


def test_extension_management_startup_updates_unpinned_installed_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_calls: list[tuple[str, bool]] = []

    async def fake_install(pip_spec: str, *, upgrade: bool = False) -> PipOperationResult:
        install_calls.append((pip_spec, upgrade))
        return PipOperationResult(ok=True, command=["pip", "install", pip_spec])

    monkeypatch.setattr(extension_management, "run_pip_install", fake_install)
    monkeypatch.setattr(
        extension_management, "_installed_distribution_version", lambda _pkg: "0.1.0"
    )

    paths = UserDataPaths(
        data_dir=tmp_path / "data",
        config_path=tmp_path / "data" / "config.json",
        files_dir=tmp_path / "data" / "files",
    )
    store = ConfigStore(paths=paths)

    async def run_scenario() -> None:
        await store.replace_settings(
            AppSettings(
                core={
                    EXTENSION_MANAGEMENT_CORE_KEY: {
                        "desired": [
                            {
                                "pip_spec": "toposync-ext-extra",
                                "package": "toposync-ext-extra",
                                "extension_id": "com.test.extra",
                                "source": "manual",
                            },
                            {
                                "pip_spec": "toposync-ext-pinned==0.1.0",
                                "package": "toposync-ext-pinned",
                                "extension_id": "com.test.pinned",
                                "source": "manual",
                            },
                        ]
                    }
                }
            )
        )
        results = await extension_management.ensure_desired_extensions_installed(store)
        assert len(results) == 1
        assert results[0].ok is True

    asyncio.run(run_scenario())

    assert install_calls == [("toposync-ext-extra", True)]


def test_extension_management_pip_install_constrains_current_core_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[list[str]] = []
    constraint_path: Path | None = None

    async def fake_run_pip(args: list[str]) -> PipOperationResult:
        nonlocal constraint_path
        captured_args.append(args)
        constraint_path = Path(args[args.index("--constraint") + 1])
        assert constraint_path.read_text(encoding="utf-8") == "toposync-core==0.3.6\n"
        return PipOperationResult(ok=True, command=["pip", *args])

    monkeypatch.setattr(extension_management, "_run_pip", fake_run_pip)
    monkeypatch.setattr(extension_management, "_current_core_version", lambda: "0.3.6")

    result = asyncio.run(extension_management.run_pip_install("toposync-ext-extra", upgrade=True))

    assert result.ok is True
    assert constraint_path is not None
    assert captured_args == [
        [
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            "--constraint",
            str(constraint_path),
            "toposync-ext-extra",
        ]
    ]
    assert not constraint_path.exists()
