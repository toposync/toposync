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
    mode: str = "home_assistant_ingress",
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", mode)
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_TRUSTED_IPS", trusted_ips)
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])
    return TestClient(create_app())


def test_ingress_auth_status_uses_forwarded_user_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {
        "x-remote-user-id": "ha-user-1",
        "x-remote-user-name": "test-user",
        "x-remote-user-display-name": "Test User",
    }

    with _create_ingress_client(tmp_path, monkeypatch) as client:
        response = client.get("/api/auth/status", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "ingress"
    assert payload["authenticated"] is True
    assert payload["requires_setup"] is False
    assert payload["user"]["id"] == "ha-user-1"
    assert payload["user"]["username"] == "test-user"
    assert payload["user"]["display_name"] == "Test User"
    assert payload["user"]["role"] == "owner"


def test_ingress_mode_rejects_untrusted_requests_except_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_ingress_client(tmp_path, monkeypatch, trusted_ips="127.0.0.1") as client:
        health = client.get("/api/health")
        blocked = client.get("/")

    assert health.status_code == 200
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Ingress access is restricted to Home Assistant"


def test_hybrid_auth_uses_ingress_headers_for_trusted_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {
        "x-remote-user-id": "ha-user-1",
        "x-remote-user-name": "test-user",
        "x-remote-user-display-name": "Test User",
    }

    with _create_ingress_client(tmp_path, monkeypatch, mode="home_assistant_hybrid") as client:
        response = client.get("/api/auth/status", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "hybrid"
    assert payload["authenticated"] is True
    assert payload["requires_setup"] is False
    assert payload["user"]["id"] == "ha-user-1"


def test_hybrid_auth_blocks_first_setup_from_direct_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _create_ingress_client(tmp_path, monkeypatch, mode="home_assistant_hybrid") as client:
        status = client.get("/api/auth/status")
        setup = client.post(
            "/api/auth/setup",
            json={
                "username": "owner",
                "display_name": "Owner",
                "password": "password123",
                "device_label": "pytest",
            },
        )
        pipelines = client.get("/api/pipelines")

    assert status.status_code == 200
    assert status.json()["mode"] == "hybrid"
    assert status.json()["requires_setup"] is False
    assert status.json()["authenticated"] is False
    assert setup.status_code == 400
    assert setup.json()["detail"] == "Setup is disabled in hybrid mode"
    assert pipelines.status_code == 401


@pytest.mark.parametrize("proxy_token", ["", "remote-proxy-token-" + "a" * 32])
def test_hybrid_auth_allows_direct_login_when_local_user_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy_token: str
) -> None:
    from toposync.runtime.auth import AuthRuntime

    data_dir = tmp_path / "data"
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "enforced")
    AuthRuntime(data_dir=data_dir).setup_owner(
        username="owner",
        display_name="Owner",
        password="password123",
    )

    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_PROXY_TOKEN", proxy_token)
    with _create_ingress_client(tmp_path, monkeypatch, mode="home_assistant_hybrid") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "owner", "password": "password123", "device_label": "pytest"},
        )
        pipelines = client.get("/api/pipelines")

    assert login.status_code == 200
    assert login.json()["user"]["username"] == "owner"
    assert pipelines.status_code == 200


@pytest.mark.parametrize("mode", ["home_assistant_hybrid", "home_assistant_ingress"])
def test_remote_proxy_preserves_each_ingress_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    token = "remote-proxy-token-" + "a" * 32
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_PROXY_TOKEN", token)
    with _create_ingress_client(tmp_path, monkeypatch, mode=mode) as client:
        for user_id in ("first-user", "second-user"):
            headers = {"x-toposync-ingress-token": token, "x-remote-user-id": user_id}
            assert client.get("/api/auth/ingress-proxy", headers=headers).status_code == 204
            response = client.get("/api/auth/status", headers=headers)
            assert response.json()["user"]["id"] == user_id
            assert token not in response.text
            assert not client.cookies


@pytest.mark.parametrize("supplied_token", ["", "wrong-token", "non-ascii-\N{SNOWMAN}"])
def test_remote_proxy_rejects_forged_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied_token: str
) -> None:
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_PROXY_TOKEN", "a" * 48)
    headers = [
        (b"x-toposync-ingress-token", supplied_token.encode("utf-8")),
        (b"x-remote-user-id", b"forged-owner"),
    ]
    with _create_ingress_client(tmp_path, monkeypatch, mode="home_assistant_hybrid") as client:
        assert client.get("/api/auth/ingress-proxy", headers=headers).status_code == 403
        assert client.get("/api/pipelines", headers=headers).status_code == 401
        assert client.get("/api/auth/status", headers=headers).json()["authenticated"] is False


def test_remote_proxy_token_does_not_replace_origin_or_identity_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "a" * 48
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_PROXY_TOKEN", token)
    headers = {"x-toposync-ingress-token": token, "x-remote-user-id": "ha-owner"}
    with _create_ingress_client(
        tmp_path, monkeypatch, mode="home_assistant_hybrid", trusted_ips="192.0.2.5"
    ) as client:
        assert client.get("/api/auth/ingress-proxy", headers=headers).status_code == 403
        assert client.get("/api/pipelines", headers=headers).status_code == 401
    with _create_ingress_client(tmp_path, monkeypatch, mode="home_assistant_hybrid") as client:
        headers.pop("x-remote-user-id")
        assert client.get("/api/auth/ingress-proxy", headers=headers).status_code == 403


def test_existing_ingress_does_not_advertise_remote_proxy_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _create_ingress_client(tmp_path, monkeypatch) as client:
        headers = {"x-remote-user-id": "ha-owner"}
        assert client.get("/api/auth/status", headers=headers).json()["authenticated"] is True
        assert client.get("/api/auth/ingress-proxy", headers=headers).status_code == 403


@pytest.mark.parametrize(
    ("token", "trusted_ips", "mode"),
    [
        ("short", "testclient", "hybrid"),
        ("a" * 48, "*", "hybrid"),
        ("a" * 48, "", "hybrid"),
        ("a" * 48, ", ,", "hybrid"),
        ("a" * 48, "testclient", "enforced"),
    ],
)
def test_remote_proxy_rejects_unsafe_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str, trusted_ips: str, mode: str
) -> None:
    from toposync.runtime.auth import AuthRuntime

    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", mode)
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_PROXY_TOKEN", token)
    monkeypatch.setenv("TOPOSYNC_AUTH_INGRESS_TRUSTED_IPS", trusted_ips)
    with pytest.raises(ValueError, match="Ingress proxy"):
        AuthRuntime(data_dir=tmp_path / "data")


def test_ingress_owner_can_pair_local_access_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {
        "x-remote-user-id": "ha-user-1",
        "x-remote-user-name": "test-user",
        "x-remote-user-display-name": "Test User",
    }

    with _create_ingress_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/access/users",
            headers=headers,
            json={
                "username": "mobile_owner",
                "display_name": "Mobile Owner",
                "role": "owner",
                "password": "password123",
            },
        )
        assert created.status_code == 200
        user_id = created.json()["id"]

        start = client.post(
            f"/api/access/users/{user_id}/pair/start",
            headers=headers,
            json={"device_label": "native app"},
        )
        assert start.status_code == 200
        code = str(start.json()["code"])
        assert code

        complete = client.post(
            "/api/auth/pair/complete",
            json={"code": code, "device_label": "native app"},
        )
        assert complete.status_code == 200
        assert complete.json()["user"]["id"] == user_id
