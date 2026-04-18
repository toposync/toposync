from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


def _create_ingress_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted_ips: str = "testclient,127.0.0.1,::1",
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "home_assistant_ingress")
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_TRUSTED_IPS", trusted_ips)
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def test_ingress_auth_status_uses_forwarded_user_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {
        "x-remote-user-id": "ha-user-1",
        "x-remote-user-name": "mateus",
        "x-remote-user-display-name": "Mateus Calza",
    }

    with _create_ingress_client(tmp_path, monkeypatch) as client:
        response = client.get("/api/auth/status", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "ingress"
    assert payload["authenticated"] is True
    assert payload["requires_setup"] is False
    assert payload["user"]["id"] == "ha-user-1"
    assert payload["user"]["username"] == "mateus"
    assert payload["user"]["display_name"] == "Mateus Calza"
    assert payload["user"]["role"] == "owner"


def test_ingress_mode_rejects_untrusted_requests_except_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_ingress_client(tmp_path, monkeypatch, trusted_ips="127.0.0.1") as client:
        health = client.get("/api/health")
        blocked = client.get("/")

    assert health.status_code == 200
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Ingress access is restricted to Home Assistant"
