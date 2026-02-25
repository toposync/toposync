from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync_ext_streaming.api.models import (
    StreamingExtensionSettings,
    Transmission,
    TransmissionOutput,
    list_engine_paths_for_host,
)
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager


def _create_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    # Comentário: engine pode ficar desligada nos testes; precisamos apenas do objeto.
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_transmission_path_is_sanitized_to_safe_slug() -> None:
    transmission = Transmission(id="demo_stream", name="Demo", path="  Hello @World  ")

    assert transmission.path == "hello--world"
    assert transmission.path
    assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in transmission.path)


def test_transmission_path_falls_back_to_id_when_empty_or_invalid() -> None:
    empty = Transmission(id="abc_123", name="Demo", path="")
    assert empty.path == "abc_123"

    invalid = Transmission(id="stream-1", name="Demo", path="!!!")
    assert invalid.path == "stream-1"


def test_streaming_extension_settings_roundtrip_serialization() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(id="t1", name="Demo", path="Demo Path"),
        ]
    )

    dumped = settings.model_dump(mode="json")
    loaded = StreamingExtensionSettings.model_validate(dumped)

    assert loaded.transmissions[0].id == "t1"
    assert loaded.transmissions[0].path == "demo-path"


def test_update_transmission_preserves_created_at_and_updates_updated_at(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Demo",
                "path": "demo-stream",
                "enabled": True,
                "outputs": [{"protocol": "hls", "enabled": True, "resolution": {"width": 320, "height": 180}}],
            },
        )
        assert created_res.status_code == 200
        created = created_res.json()

        transmission_id = str(created["id"])
        created_at = datetime.fromisoformat(created["created_at"])
        updated_at = datetime.fromisoformat(created["updated_at"])

        # Comentário: garantir que updated_at mude de fato (resolução de timestamp pode ser alta).
        time.sleep(0.02)

        update_payload = dict(created)
        update_payload["name"] = "Demo v2"
        update_res = client.put(f"/api/streams/transmissions/{transmission_id}", json=update_payload)
        assert update_res.status_code == 200
        updated = update_res.json()

        assert updated["id"] == transmission_id
        assert datetime.fromisoformat(updated["created_at"]) == created_at
        assert datetime.fromisoformat(updated["updated_at"]) > updated_at


def test_list_engine_paths_for_host_filters_by_host_server_id() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(
                id="local_tx",
                name="Local stream",
                host_server_id="local",
                path="local-main",
                outputs=[TransmissionOutput(id="hls_local", protocol="hls", enabled=True)],
            ),
            Transmission(
                id="edge_tx",
                name="Edge stream",
                host_server_id="edge_gpu",
                path="edge-main",
                outputs=[TransmissionOutput(id="rtsp_edge", protocol="rtsp", enabled=True)],
            ),
        ]
    )

    local_paths = list_engine_paths_for_host(settings, host_server_id="local")
    edge_paths = list_engine_paths_for_host(settings, host_server_id="edge_gpu")

    assert "test" in local_paths
    assert "local-main" in local_paths
    assert "edge-main" not in local_paths

    assert "test" in edge_paths
    assert "edge-main" in edge_paths
    assert "local-main" not in edge_paths


def test_duplicate_transmission_path_is_allowed_across_different_hosts() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(id="tx_local", name="A", host_server_id="local", path="camera-1"),
            Transmission(id="tx_edge", name="B", host_server_id="edge_gpu", path="camera-1"),
        ]
    )
    assert len(settings.transmissions) == 2
