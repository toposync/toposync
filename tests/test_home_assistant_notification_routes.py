from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from toposync.app import create_app
from toposync_ext_home_assistant.plugin import EXTENSION_ID
import toposync.extensions.manager as ext_manager_mod


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self) -> Any:
        return self._payload


def _create_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def post(self, url: str, *, headers: dict[str, str] | None = None, json: Any = None) -> _FakeResponse:
            calls.append({"url": url, "headers": headers or {}, "json": json})
            return _FakeResponse()

        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _FakeResponse:
            del headers
            if url.endswith("/api/services"):
                return _FakeResponse(
                    payload=[
                        {
                            "domain": "notify",
                            "services": {
                                "mobile_app_pixel_9": {
                                    "name": "Pixel 9",
                                    "description": "Main phone",
                                },
                                "family": {
                                    "name": "Family",
                                    "description": "Group notify",
                                },
                            },
                        },
                        {
                            "domain": "light",
                            "services": {
                                "turn_on": {
                                    "name": "Turn on",
                                    "description": "Turn on light",
                                }
                            },
                        },
                    ]
                )
            return _FakeResponse(status_code=404, payload={})

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    def _eps(_group: str):
        return [
            EntryPoint(
                name="home_assistant",
                value="toposync_ext_home_assistant.plugin:HomeAssistantExtension",
                group="toposync.extensions",
            ),
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _eps)
    return TestClient(create_app()), calls


async def _upsert_notification(notifications: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return await notifications.upsert(**payload)


def test_notification_routes_forward_pipeline_notifications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _create_client(tmp_path, monkeypatch)

    with client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.patch_extension_settings,
            EXTENSION_ID,
            {
                "servers": [
                    {
                        "id": "ha-main",
                        "name": "Casa",
                        "host": "http://ha.local:8123",
                        "apiKey": "secret-token",
                    }
                ],
                "notificationRoutes": [
                    {
                        "id": "route-main",
                        "name": "Pipeline alerts",
                        "enabled": True,
                        "serverId": "ha-main",
                        "notifyService": "notify.mobile_app_pixel_9",
                        "notificationTypes": ["pipelines.event"],
                        "closeAction": "clear",
                        "sendUpdates": False,
                    }
                ],
            },
        )

        notifications = client.app.state.notifications
        opened = client.portal.call(
            _upsert_notification,
            notifications,
            {
                "type": "pipelines.event",
                "title": "Pessoa detectada",
                "description": "Portao da frente",
                "payload": {"source": "pipelines", "status": "open", "lifecycle": "open"},
                "dedupe_key": "camera:event:1",
            },
        )
        time.sleep(0.05)

        client.portal.call(
            _upsert_notification,
            notifications,
            {
                "type": "pipelines.event",
                "title": "Pessoa detectada",
                "description": "Portao da frente atualizado",
                "payload": {"source": "pipelines", "status": "open", "lifecycle": "update"},
                "dedupe_key": "camera:event:1",
            },
        )
        time.sleep(0.05)

        client.portal.call(
            _upsert_notification,
            notifications,
            {
                "type": "pipelines.event",
                "title": "Pessoa detectada",
                "description": "Portao da frente atualizado",
                "payload": {"source": "pipelines", "status": "closed", "lifecycle": "close"},
                "dedupe_key": "camera:event:1",
            },
        )
        time.sleep(0.05)

        assert len(calls) == 2

        first = calls[0]
        assert first["url"] == "http://ha.local:8123/api/services/notify/mobile_app_pixel_9"
        assert first["json"]["title"] == "Pessoa detectada"
        assert first["json"]["message"] == "Portao da frente"
        tag = first["json"]["data"]["tag"]
        assert tag == f"toposync:{opened['id']}"

        second = calls[1]
        assert second["url"] == "http://ha.local:8123/api/services/notify/mobile_app_pixel_9"
        assert second["json"] == {
            "message": "clear_notification",
            "data": {"tag": tag},
        }


def test_home_assistant_notify_operator_is_registered_and_lists_notify_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _calls = _create_client(tmp_path, monkeypatch)

    with client:
        config_store = client.app.state.config_store
        client.portal.call(
            config_store.patch_extension_settings,
            EXTENSION_ID,
            {
                "servers": [
                    {
                        "id": "ha-main",
                        "name": "Casa",
                        "host": "http://ha.local:8123",
                        "apiKey": "secret-token",
                    }
                ],
            },
        )

        operators_res = client.get("/api/pipelines/operators")
        assert operators_res.status_code == 200
        operators = operators_res.json()["operators"]
        operator_ids = {str(item.get("id") or "") for item in operators}
        assert "home_assistant.notify" in operator_ids

        notify_operator = next(item for item in operators if item.get("id") == "home_assistant.notify")
        assert notify_operator["defaults"]["notify_when"] == "open"
        assert notify_operator["defaults"]["close_behavior"] == "ignore"

        services_res = client.get("/api/home_assistant/ha-main/services?domain=notify")
        assert services_res.status_code == 200
        assert services_res.json() == [
            {
                "domain": "notify",
                "service": "family",
                "name": "Family",
                "description": "Group notify",
            },
            {
                "domain": "notify",
                "service": "mobile_app_pixel_9",
                "name": "Pixel 9",
                "description": "Main phone",
            },
        ]
