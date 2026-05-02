from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.extensions.manifest import ExtensionManifest
import toposync.extensions.manager as ext_manager_mod
from toposync.runtime.extension_management import InstalledExtensionProbe, PipOperationResult
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


def test_extension_management_manual_install_records_desired_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = False

    async def fake_install(pip_spec: str) -> PipOperationResult:
        nonlocal installed
        installed = True
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
            json={"pip_spec": "toposync-ext-extra==0.1.0"},
        )
        settings = client.get("/api/settings").json()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    extra = _item_by_id(body["catalog"], "com.test.extra")
    assert extra["status"] == "pending_restart"
    desired = settings["core"]["extension_management"]["desired"]
    assert desired == [
        {
            "pip_spec": "toposync-ext-extra==0.1.0",
            "package": "toposync-ext-extra",
            "extension_id": "com.test.extra",
            "source": "manual",
        }
    ]
