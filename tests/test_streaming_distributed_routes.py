from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
import toposync_ext_streaming.api.routes as streaming_routes
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager


def _create_client(tmp_path: Path, *, server_id: str = "local") -> tuple[TestClient, ConfigStore]:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )
    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.state.streaming_server_id = server_id
    app.include_router(create_streaming_router())
    return TestClient(app), config_store


def _set_processing_servers(config_store: ConfigStore, servers: list[dict]) -> None:
    settings = asyncio.run(config_store.get_settings())
    core = dict(settings.core)
    core["processing_servers"] = servers
    asyncio.run(
        config_store.replace_settings(
            AppSettings(core=core, extensions=dict(settings.extensions)),
        )
    )


def test_distributed_settings_endpoint_filters_by_server_id(tmp_path: Path) -> None:
    client, config_store = _create_client(tmp_path, server_id="local")
    _set_processing_servers(
        config_store,
        [
            {"id": "edge_gpu", "name": "Edge GPU", "kind": "http", "url": "http://10.0.0.55:9001"},
        ],
    )

    with client:
        local_create = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Local stream",
                "path": "cam-local",
                "host_server_id": "local",
                "outputs": [{"protocol": "hls", "enabled": True}],
            },
        )
        assert local_create.status_code == 200

        remote_create = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Edge stream",
                "path": "cam-edge",
                "host_server_id": "edge_gpu",
                "outputs": [{"protocol": "rtsp", "enabled": True}],
            },
        )
        assert remote_create.status_code == 200

        res = client.get("/api/streams/distributed/settings/edge_gpu")
        assert res.status_code == 200
        body = res.json()
        transmissions = body.get("transmissions") if isinstance(body.get("transmissions"), list) else []
        assert len(transmissions) == 1
        assert transmissions[0]["host_server_id"] == "edge_gpu"
        assert transmissions[0]["path"] == "cam-edge"


def test_transmission_urls_proxy_remote_processing_server(tmp_path: Path, monkeypatch) -> None:
    client, config_store = _create_client(tmp_path, server_id="local")
    _set_processing_servers(
        config_store,
        [
            {
                "id": "edge_gpu",
                "name": "Edge GPU",
                "kind": "http",
                "url": "http://10.0.0.55:9001",
                "username": "proc",
                "password": "secret",
            },
        ],
    )

    captured: dict[str, str] = {}
    remote_transmission_id = {"value": ""}

    async def _fake_fetch_json(*, url: str, timeout_s: float = 6.0, username: str = "", password: str = "") -> dict:
        captured["url"] = url
        captured["username"] = username
        captured["password"] = password
        return {
            "transmission_id": remote_transmission_id["value"] or "tx_remote",
            "engine_running": True,
            "outputs": [
                {
                    "output_id": "hls_a",
                    "protocol": "hls",
                    "resolved_engine_path": "cam-edge",
                    "url": "http://127.0.0.1:8899/cam-edge/index.m3u8",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(streaming_routes, "_fetch_json", _fake_fetch_json)

    with client:
        created = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Edge stream",
                "path": "cam-edge",
                "host_server_id": "edge_gpu",
                "outputs": [{"id": "hls_a", "protocol": "hls", "enabled": True}],
            },
        )
        assert created.status_code == 200
        transmission_id = created.json()["id"]
        remote_transmission_id["value"] = transmission_id

        res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")
        assert res.status_code == 200
        body = res.json()
        outputs = body.get("outputs") if isinstance(body.get("outputs"), list) else []
        assert len(outputs) == 1
        assert outputs[0]["url"] == "http://10.0.0.55:8899/cam-edge/index.m3u8"
        assert "Resolved via processing server 'edge_gpu'." in body.get("warnings", [])
        assert captured["url"].endswith(f"/api/streams/internal/transmissions/{transmission_id}/urls")
        assert captured["username"] == "proc"
        assert captured["password"] == "secret"
