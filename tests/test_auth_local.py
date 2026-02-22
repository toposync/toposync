from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


def _create_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry_points: list[object] | None = None,
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "enforced")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: entry_points or [])
    return TestClient(create_app())


def _setup_owner(client: TestClient) -> dict:
    res = client.post(
        "/api/auth/setup",
        json={
            "username": "owner",
            "display_name": "Owner",
            "password": "password123",
            "device_label": "pytest",
        },
    )
    assert res.status_code == 200
    return res.json()


def test_auth_requires_setup_blocks_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        res = client.get("/api/auth/status")
        assert res.status_code == 200
        assert res.json()["requires_setup"] is True
        assert res.json()["authenticated"] is False

        res = client.get("/api/pipelines")
        assert res.status_code == 503
        assert res.json()["detail"] == "Auth setup is required"


def test_auth_setup_then_requires_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)

        client.cookies.clear()
        res = client.get("/api/pipelines")
        assert res.status_code == 401
        assert res.json()["detail"] == "Authentication required"


def test_auth_login_logout_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)

        res = client.get("/api/auth/status")
        assert res.status_code == 200
        assert res.json()["authenticated"] is True
        assert res.json()["user"]["username"] == "owner"

        res = client.get("/api/pipelines")
        assert res.status_code == 200

        res = client.post("/api/auth/logout")
        assert res.status_code == 200
        assert res.json()["ok"] is True

        res = client.get("/api/auth/status")
        assert res.status_code == 200
        assert res.json()["authenticated"] is False

        res = client.get("/api/pipelines")
        assert res.status_code == 401

        res = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "password123", "device_label": "pytest-2"},
        )
        assert res.status_code == 200
        assert res.json()["user"]["username"] == "owner"

        res = client.get("/api/auth/status")
        assert res.status_code == 200
        assert res.json()["authenticated"] is True
        assert res.json()["user"]["username"] == "owner"

        res = client.get("/api/pipelines")
        assert res.status_code == 200


def test_event_grant_exclude_overrides_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)

        res = client.post(
            "/api/access/users",
            json={
                "username": "member1",
                "display_name": "Member",
                "role": "member",
                "password": "password123",
            },
        )
        assert res.status_code == 200
        member_id = res.json()["id"]

        res = client.post(
            f"/api/access/users/{member_id}/grants",
            json={
                "action": "core:events:emit",
                "resource_type": "core:event",
                "include": [],
                "exclude": ["device.action_requested"],
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/auth/login",
            json={
                "username": "member1",
                "password": "password123",
                "device_label": "pytest-member",
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/events/device.action_requested",
            json={"payload": {"device_id": "lamp", "action": "toggle"}, "context": {}},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "Permission denied"

        res = client.post(
            "/api/events/home_assistant.service_call",
            json={"payload": {"domain": "light", "service": "toggle", "service_data": {}}, "context": {}},
        )
        assert res.status_code == 200


def test_cameras_pipeline_wizard_requires_pipelines_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _EP:
        name = "cameras"
        value = "toposync_ext_cameras.plugin:CamerasExtension"

        def load(self):  # type: ignore[no-untyped-def]
            from toposync_ext_cameras.plugin import CamerasExtension

            return CamerasExtension

    with _create_client(tmp_path, monkeypatch, entry_points=[_EP()]) as client:
        _setup_owner(client)

        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"cameras": [{"id": "cam1", "name": "Front"}]},
        )
        assert res.status_code == 200

        res = client.post(
            "/api/access/users",
            json={
                "username": "member1",
                "display_name": "Member",
                "role": "member",
                "password": "password123",
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/auth/login",
            json={
                "username": "member1",
                "password": "password123",
                "device_label": "pytest-member",
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/cameras/cameras/cam1/pipeline-wizard",
            json={"preset": "people", "pipeline_name": "cam1_people", "enabled": True},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "Permission denied"


def test_auth_store_deletes_tokens_and_grants_on_user_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)

        auth = client.app.state.auth
        member = auth.store.create_user(
            username="member1",
            display_name="Member",
            role="member",
            password="password123",
        )

        auth.store.upsert_grant(
            user_id=member.id,
            action="core:events:emit",
            resource_type="core:event",
            include=[],
            exclude=["device.action_requested"],
        )
        token, _ = auth.store.issue_refresh_token(user_id=member.id, device_label="pytest", ttl_s=3600)
        assert token
        assert auth.store.active_sessions_count(member.id) == 1
        assert len(auth.store.list_grants(member.id)) == 1

        auth.store.delete_user(member.id)
        assert auth.store.active_sessions_count(member.id) == 0
        assert len(auth.store.list_grants(member.id)) == 0
