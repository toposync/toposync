from __future__ import annotations

import time
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


def test_auth_setup_then_requires_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_event_grant_exclude_overrides_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            json={
                "payload": {"domain": "light", "service": "toggle", "service_data": {}},
                "context": {},
            },
        )
        assert res.status_code == 200


def test_cameras_pipeline_preset_requires_pipelines_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "/api/cameras/cameras/cam1/pipelines/presets",
            json={"preset": "people_simple", "pipeline_name": "cam1_people", "enabled": True},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "Permission denied"


def test_camera_settings_require_scoped_configure_grants_before_patch_or_put(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CameraEntryPoint:
        name = "cameras"
        value = "toposync_ext_cameras.plugin:CamerasExtension"

        def load(self):  # type: ignore[no-untyped-def]
            from toposync_ext_cameras.plugin import CamerasExtension

            return CamerasExtension

    initial_devices = [
        {
            "id": "front",
            "name": "Front",
            "enabled": True,
            "control": {"type": "none"},
            "sources": [],
        },
        {
            "id": "back",
            "name": "Back",
            "enabled": True,
            "control": {"type": "none"},
            "sources": [],
        },
    ]

    with _create_client(
        tmp_path,
        monkeypatch,
        entry_points=[_CameraEntryPoint()],
    ) as client:
        _setup_owner(client)
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": initial_devices},
        )
        assert response.status_code == 200, response.text

        response = client.post(
            "/api/access/users",
            json={
                "username": "camera-operator",
                "display_name": "Camera operator",
                "role": "member",
                "password": "password123",
            },
        )
        assert response.status_code == 200, response.text
        member_id = response.json()["id"]
        grants = [
            {
                "action": "core:extension:settings:write",
                "resource_type": "core:extension",
                "include": ["com.toposync.cameras"],
                "exclude": [],
            },
            {
                "action": "core:settings:write",
                "resource_type": "core:global",
                "include": [],
                "exclude": [],
            },
            {
                "action": "core:camera:configure",
                "resource_type": "core:camera",
                "include": ["front"],
                "exclude": [],
            },
        ]
        for grant in grants:
            response = client.post(
                f"/api/access/users/{member_id}/grants",
                json=grant,
            )
            assert response.status_code == 200, response.text

        response = client.post(
            "/api/auth/login",
            json={
                "username": "camera-operator",
                "password": "password123",
                "device_label": "pytest-camera-operator",
            },
        )
        assert response.status_code == 200, response.text

        front_changed = [
            {**initial_devices[0], "name": "Front door"},
            initial_devices[1],
        ]
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": front_changed},
        )
        assert response.status_code == 200, response.text

        async def stored_settings() -> dict:
            settings = await client.app.state.config_store.get_settings()
            return settings.model_dump(mode="json")

        before_denied_write = client.portal.call(stored_settings)
        back_changed = [front_changed[0], {**initial_devices[1], "name": "Back door"}]
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": back_changed},
        )
        assert response.status_code == 403, response.text
        assert client.portal.call(stored_settings) == before_denied_write

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"devices": [front_changed[0]]},
        )
        assert response.status_code == 403, response.text
        assert client.portal.call(stored_settings) == before_denied_write

        denied_put = {
            **before_denied_write,
            "extensions": {
                **before_denied_write["extensions"],
                "com.toposync.cameras": {
                    "schema_version": 4,
                    "devices": back_changed,
                },
            },
        }
        response = client.put("/api/settings", json=denied_put)
        assert response.status_code == 403, response.text
        assert client.portal.call(stored_settings) == before_denied_write

        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"devices": "invalid"},
        )
        assert response.status_code == 403, response.text
        assert client.portal.call(stored_settings) == before_denied_write


def test_camera_index_filters_each_camera_by_scoped_read_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CameraEntryPoint:
        name = "cameras"
        value = "toposync_ext_cameras.plugin:CamerasExtension"

        def load(self):  # type: ignore[no-untyped-def]
            from toposync_ext_cameras.plugin import CamerasExtension

            return CamerasExtension

    devices = [
        {
            "id": "front",
            "name": "Front",
            "enabled": True,
            "control": {"type": "onvif"},
            "onvif": {
                "xaddr": "http://front/onvif/device_service",
                "username": "front-user-secret",
                "password": "front-password-secret",
            },
            "sources": [],
        },
        {
            "id": "back",
            "name": "Back private camera",
            "enabled": True,
            "control": {"type": "none"},
            "sources": [],
        },
    ]

    with _create_client(
        tmp_path,
        monkeypatch,
        entry_points=[_CameraEntryPoint()],
    ) as client:
        _setup_owner(client)
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": devices},
        )
        assert response.status_code == 200, response.text

        response = client.post(
            "/api/access/users",
            json={
                "username": "front-viewer",
                "display_name": "Front viewer",
                "role": "member",
                "password": "password123",
            },
        )
        assert response.status_code == 200, response.text
        member_id = response.json()["id"]
        for grant in (
            {
                "action": "core:extension:use",
                "resource_type": "core:extension",
                "include": ["com.toposync.cameras"],
                "exclude": [],
            },
            {
                "action": "core:camera:read",
                "resource_type": "core:camera",
                "include": ["front"],
                "exclude": [],
            },
        ):
            response = client.post(
                f"/api/access/users/{member_id}/grants",
                json=grant,
            )
            assert response.status_code == 200, response.text

        response = client.post(
            "/api/auth/login",
            json={
                "username": "front-viewer",
                "password": "password123",
                "device_label": "pytest-front-viewer",
            },
        )
        assert response.status_code == 200, response.text

        response = client.get("/api/cameras/index")

    assert response.status_code == 200, response.text
    assert [camera["id"] for camera in response.json()["cameras"]] == ["front"]
    assert "Back private camera" not in response.text
    assert "front-user-secret" not in response.text
    assert "front-password-secret" not in response.text


def test_disabled_camera_extension_still_requires_camera_configure_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CameraEntryPoint:
        name = "cameras"
        value = "toposync_ext_cameras.plugin:CamerasExtension"

        def load(self):  # type: ignore[no-untyped-def]
            from toposync_ext_cameras.plugin import CamerasExtension

            return CamerasExtension

    monkeypatch.setattr(
        "toposync.app.disabled_extension_ids_from_settings",
        lambda _settings: {"com.toposync.cameras"},
    )
    with _create_client(
        tmp_path,
        monkeypatch,
        entry_points=[_CameraEntryPoint()],
    ) as client:
        _setup_owner(client)
        response = client.post(
            "/api/access/users",
            json={
                "username": "disabled-camera-operator",
                "display_name": "Disabled camera operator",
                "role": "member",
                "password": "password123",
            },
        )
        assert response.status_code == 200, response.text
        member_id = response.json()["id"]
        response = client.post(
            f"/api/access/users/{member_id}/grants",
            json={
                "action": "core:extension:settings:write",
                "resource_type": "core:extension",
                "include": ["com.toposync.cameras"],
                "exclude": [],
            },
        )
        assert response.status_code == 200, response.text
        response = client.post(
            "/api/auth/login",
            json={
                "username": "disabled-camera-operator",
                "password": "password123",
                "device_label": "pytest-disabled-camera-operator",
            },
        )
        assert response.status_code == 200, response.text

        async def stored_settings() -> dict:
            settings = await client.app.state.config_store.get_settings()
            return settings.model_dump(mode="json")

        before_denied_write = client.portal.call(stored_settings)
        response = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "enabled": True,
                        "control": {"type": "none"},
                        "sources": [],
                    }
                ],
            },
        )

        assert response.status_code == 403, response.text
        assert client.portal.call(stored_settings) == before_denied_write


def test_auth_store_deletes_tokens_and_grants_on_user_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        token, _ = auth.store.issue_refresh_token(
            user_id=member.id, device_label="pytest", ttl_s=3600
        )
        assert token
        assert auth.store.active_sessions_count(member.id) == 1
        assert len(auth.store.list_grants(member.id)) == 1

        auth.store.delete_user(member.id)
        assert auth.store.active_sessions_count(member.id) == 0
        assert len(auth.store.list_grants(member.id)) == 0


def test_refresh_flow_rotates_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        auth = client.app.state.auth
        current_refresh = client.cookies.get(auth.refresh_cookie_name)
        assert current_refresh

        refreshed = auth._tokens_from_refresh(str(current_refresh))
        assert refreshed is not None
        _principal, (_access, next_refresh) = refreshed

        assert next_refresh != current_refresh
        assert auth.store.get_refresh_session(str(current_refresh)) is None
        assert auth.store.get_refresh_session(next_refresh) is not None


def test_refresh_rotation_grace_allows_concurrent_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        auth = client.app.state.auth
        current_refresh = client.cookies.get(auth.refresh_cookie_name)
        assert current_refresh

        first = auth._tokens_from_refresh(str(current_refresh))
        assert first is not None
        _principal, (_access, next_refresh) = first
        assert next_refresh != current_refresh

        # Simulate concurrent requests using the same (now revoked) refresh token.
        second = auth._tokens_from_refresh(str(current_refresh))
        assert second is not None
        _principal2, (_access2, next_refresh2) = second
        assert next_refresh2 != current_refresh
        assert auth.store.get_refresh_session(next_refresh2) is not None


def test_pairing_code_exchanges_for_session_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        owner = _setup_owner(client)["user"]

        start = client.post("/api/auth/pair/start", json={"device_label": "owner-phone"})
        assert start.status_code == 200
        code = str(start.json()["code"])
        assert code

        client.cookies.clear()
        complete = client.post(
            "/api/auth/pair/complete", json={"code": code, "device_label": "owner-phone"}
        )
        assert complete.status_code == 200
        assert complete.json()["user"]["id"] == owner["id"]

        auth = client.app.state.auth
        assert client.cookies.get(auth.refresh_cookie_name)

        replay = client.post(
            "/api/auth/pair/complete", json={"code": code, "device_label": "owner-phone"}
        )
        assert replay.status_code == 401


def test_guest_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)

        res = client.post(
            "/api/access/users",
            json={
                "username": "guest1",
                "display_name": "Guest",
                "role": "guest",
                "password": "password123",
            },
        )
        assert res.status_code == 200

        res = client.post(
            "/api/auth/login",
            json={
                "username": "guest1",
                "password": "password123",
                "device_label": "pytest-guest",
            },
        )
        assert res.status_code == 200

        res = client.get("/api/compositions")
        assert res.status_code == 200

        res = client.post(
            "/api/events/device.action_requested",
            json={"payload": {"device_id": "lamp", "action": "toggle"}, "context": {}},
        )
        assert res.status_code == 403


def test_owner_can_revoke_session_by_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        setup = _setup_owner(client)
        owner = setup["user"]

        res = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "password123", "device_label": "owner-tablet"},
        )
        assert res.status_code == 200

        auth = client.app.state.auth
        tablet_refresh = client.cookies.get(auth.refresh_cookie_name)
        assert tablet_refresh

        sessions = client.get(f"/api/access/users/{owner['id']}/sessions")
        assert sessions.status_code == 200
        entries = sessions.json()["sessions"]
        assert len(entries) >= 1
        tablet_entry = next(
            (item for item in entries if item["device_label"] == "owner-tablet"), None
        )
        assert tablet_entry is not None

        revoke = client.delete(f"/api/access/users/{owner['id']}/sessions/{tablet_entry['id']}")
        assert revoke.status_code == 200
        assert revoke.json()["ok"] is True
        assert auth.store.get_refresh_session(str(tablet_refresh)) is None


def test_home_assistant_service_token_reads_embed_surface_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        auth = client.app.state.auth
        owner_access = client.cookies.get(auth.access_cookie_name)
        assert owner_access

        created = client.post(
            "/api/auth/home-assistant/token",
            json={"label": "Home Assistant Lab"},
        )
        assert created.status_code == 200
        payload = created.json()
        service_token = payload["token"]
        service_user = payload["user"]
        assert service_token.startswith("toposync_st_")
        assert payload["token_type"] == "Bearer"
        assert service_user["role"] == "service"

        client.cookies.clear()
        headers = {"Authorization": f"Bearer {service_token}"}

        status = client.get("/api/auth/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["authenticated"] is True
        assert status.json()["user"]["id"] == service_user["id"]

        settings = client.get("/api/settings", headers=headers)
        assert settings.status_code == 200

        composition = client.get("/api/composition", headers=headers)
        assert composition.status_code == 200
        saved = client.put("/api/composition", headers=headers, json=composition.json())
        assert saved.status_code == 200

        access = client.get("/api/access/users", headers=headers)
        assert access.status_code == 403

        listed = client.get(
            f"/api/access/users/{service_user['id']}/service-tokens",
            headers={"Authorization": f"Bearer {owner_access}"},
        )
        assert listed.status_code == 200
        assert any(item["id"] == payload["token_id"] for item in listed.json()["tokens"])

        revoked = client.delete(
            f"/api/access/users/{service_user['id']}/service-tokens/{payload['token_id']}",
            headers={"Authorization": f"Bearer {owner_access}"},
        )
        assert revoked.status_code == 200

        denied = client.get("/api/settings", headers=headers)
        assert denied.status_code == 401


def test_home_assistant_service_token_creates_embed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        auth = client.app.state.auth

        created = client.post("/api/auth/home-assistant/token", json={})
        assert created.status_code == 200
        service_token = created.json()["token"]

        client.cookies.clear()
        start = client.post(
            "/api/auth/embed/start",
            headers={"Authorization": f"Bearer {service_token}"},
            json={"path": "/?source=home-assistant", "ttl_seconds": 60},
        )
        assert start.status_code == 200
        embed_url = start.json()["url"]
        assert embed_url.startswith("/api/auth/embed/complete?token=")

        complete = client.get(embed_url, follow_redirects=False)
        assert complete.status_code == 303
        assert complete.headers["location"] == "/?source=home-assistant"
        refresh_cookie = client.cookies.get(auth.refresh_cookie_name)
        assert refresh_cookie
        refresh_session = auth.store.get_refresh_session(str(refresh_cookie))
        assert refresh_session is not None
        assert refresh_session.expires_at > time.time() + (30 * 60)

        settings = client.get("/api/settings")
        assert settings.status_code == 200
