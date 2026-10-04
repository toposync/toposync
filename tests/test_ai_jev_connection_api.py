from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry
from toposync_ext_ai.constants import EXTENSION_ID
from toposync_ext_ai.jev import CONNECTION_TEST_TEXT
from toposync_ext_ai.plugin import AiExtension


@pytest.fixture
def connection_app(tmp_path, monkeypatch):
    for key in ("TOPOSYNC_JEV_ENABLED", "TOPOSYNC_JEV_BACKEND", "TOPOSYNC_OLLAYA_URL", "TOPOSYNC_OLLAYA_MODEL"):
        monkeypatch.delenv(key, raising=False)
    store = ConfigStore(paths=UserDataPaths(
        data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files",
    ))
    app = FastAPI()
    app.state.config_store = store
    asyncio.run(AiExtension().setup(app, bus=EventBus(), services=ServiceRegistry()))
    inference = AsyncMock(return_value={"status": "evaluated", "reason": "semantic_review_only"})
    monkeypatch.setattr("toposync_ext_ai.router.evaluate_event_text", inference)
    with TestClient(app) as client:
        yield client, store, inference


def test_connection_default_read_and_disabled_test_do_not_infer(connection_app):
    client, store, inference = connection_app
    response = client.get("/api/ai/jev/connection")
    assert response.status_code == 200
    assert response.json()["source"] == "defaults"
    assert response.json()["connection"]["enabled"] is False
    saved = store.paths.config_path.read_bytes()
    assert client.post("/api/ai/jev/connection/test", json={}).json()["reason"] == "disabled"
    inference.assert_not_awaited()
    assert store.paths.config_path.read_bytes() == saved


def test_manual_test_uses_synthetic_text_and_never_saves_or_qualifies(connection_app):
    client, store, inference = connection_app
    client.get("/api/ai/jev/connection")
    saved = store.paths.config_path.read_bytes()
    draft = {"enabled": True, "url": "http://127.0.0.1:11435", "model": "laya:multilingual"}
    response = client.post("/api/ai/jev/connection/test", json=draft)
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True and result["connection"] == draft
    assert result["checked_at"] and result["semantic_qualified"] is False
    assert result["review_required"] is True and "evaluation" not in result
    inference.assert_awaited_once()
    assert inference.await_args.args == (CONNECTION_TEST_TEXT,)
    assert inference.await_args.kwargs["connection"].model_dump() == draft
    assert store.paths.config_path.read_bytes() == saved
    assert client.get("/api/ai/jev/connection").json()["source"] == "defaults"


@pytest.mark.parametrize("body", [
    {"enabled": True, "text": "Private user text"},
    {"enabled": True, "url": "https://remote.invalid"},
    {"enabled": True, "api_key": "synthetic-secret"},
])
def test_connection_test_rejects_payload_or_destination_expansion(connection_app, body):
    client, _store, inference = connection_app
    assert client.post("/api/ai/jev/connection/test", json=body).status_code == 422
    inference.assert_not_awaited()


def test_connection_test_reuses_usage_limits_and_returns_actionable_failure(connection_app):
    client, _store, inference = connection_app
    assert client.patch("/api/ai/settings", json={"limits": {"requests_per_minute": 1}}).status_code == 200
    inference.return_value = {"status": "unavailable", "reason": "service_unavailable"}
    assert client.post("/api/ai/jev/connection/test", json={"enabled": True}).json()["ok"] is False
    assert client.post("/api/ai/jev/connection/test", json={"enabled": True}).json()["reason"] == "minute_limit"
    inference.assert_awaited_once()


def test_connection_test_timeout_does_not_expose_exception(connection_app):
    client, _store, inference = connection_app
    inference.side_effect = TimeoutError("synthetic private transport detail")
    response = client.post("/api/ai/jev/connection/test", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["reason"] == "service_unavailable"
    assert "private transport" not in response.text


def test_connection_origin_and_invalid_environment_do_not_expose_secrets(connection_app, monkeypatch):
    client, store, inference = connection_app
    monkeypatch.setenv("TOPOSYNC_JEV_ENABLED", "1")
    monkeypatch.setenv("TOPOSYNC_JEV_BACKEND", "typesafe")
    assert client.get("/api/ai/jev/connection").json()["active_backend"] == "typesafe"
    monkeypatch.setenv("TOPOSYNC_JEV_BACKEND", "ollaya")
    monkeypatch.setenv("TOPOSYNC_OLLAYA_URL", "http://synthetic-secret@127.0.0.1:11435")
    result = client.get("/api/ai/jev/connection").json()
    assert result["reason"] == "invalid_environment_configuration"
    assert "synthetic-secret" not in json.dumps(result)
    asyncio.run(store.patch_extension_settings(EXTENSION_ID, {"jev": {"enabled": False}}))
    result = client.get("/api/ai/jev/connection").json()
    assert result["source"] == "settings" and result["active_backend"] is None
    inference.assert_not_awaited()
