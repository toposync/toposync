from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from toposync.app import create_app
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

        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _FakeResponse:
            calls.append({"method": "GET", "url": url, "headers": headers or {}})
            if url.endswith("/services"):
                return _FakeResponse(
                    payload=[
                        {
                            "domain": "notify",
                            "services": {
                                "mobile_app_pixel_9": {
                                    "name": "Pixel 9",
                                    "description": "Main phone",
                                }
                            },
                        }
                    ]
                )
            return _FakeResponse(status_code=404, payload={})

        async def post(self, url: str, *, headers: dict[str, str] | None = None, json: Any = None) -> _FakeResponse:
            calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
            return _FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
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


def test_supervisor_mode_exposes_managed_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _create_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/home_assistant/servers")

    assert response.status_code == 200
    payload = response.json()
    assert payload == [
        {
            "id": "supervisor",
            "name": "Home Assistant",
            "host": "http://supervisor/core/api",
            "managed": True,
            "source": "supervisor",
        }
    ]


def test_supervisor_mode_uses_internal_core_api_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _create_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/home_assistant/supervisor/services")

    assert response.status_code == 200
    assert response.json()[0]["domain"] == "notify"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://supervisor/core/api/services"
    assert calls[0]["headers"]["Authorization"] == "Bearer super-secret"
