from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.pipelines import register_streaming_pipeline_operators
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


def _camera_source(source_id: str, *, rtsp_url: str, is_default: bool = False, enabled: bool = True) -> dict:
    return {
        "id": source_id,
        "name": source_id.title(),
        "enabled": enabled,
        "is_default": is_default,
        "kind": "video",
        "role": "main" if source_id == "main" else "sub",
        "view_id": "front",
        "origin": {"type": "rtsp", "rtsp_url": rtsp_url},
        "ingest": {"mode": "centralized", "host_server_id": "local"},
    }


def _settings() -> AppSettings:
    return AppSettings(
        extensions={
            "com.toposync.streaming": {
                "transmissions": [
                    {
                        "id": "tx_front",
                        "name": "Front stream",
                        "path": "front-stream",
                        "enabled": True,
                        "host_server_id": "local",
                        "outputs": [{"id": "hls", "protocol": "hls", "enabled": True}],
                    }
                ]
            },
            "com.toposync.cameras": {
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "control": {"type": "none"},
                        "sources": [
                            _camera_source("main", rtsp_url="rtsp://10.0.0.10/high", is_default=True),
                            _camera_source("sub", rtsp_url="rtsp://10.0.0.10/low"),
                            _camera_source("disabled", rtsp_url="rtsp://10.0.0.10/off", enabled=False),
                        ],
                    }
                ]
            },
        }
    )


def _create_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)

    async def _seed() -> None:
        await config_store.load()
        await config_store.replace_settings(_settings())

    asyncio.run(_seed())

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_streaming_pipeline_operators(registry)

    app.state.config_store = config_store
    app.state.pipeline_graph_compiler = PipelineGraphCompiler(registry)
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.state.streaming_runtime_state = TransmissionRuntimeState()
    app.state.streaming_publisher_manager = PublisherManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def _camera_source_config(pipeline: dict) -> dict:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("operator") == "camera.source":
            config = node.get("config")
            return config if isinstance(config, dict) else {}
    return {}


def _stream_publish_config(pipeline: dict) -> dict:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("operator") == "stream.publish_video":
            config = node.get("config")
            return config if isinstance(config, dict) else {}
    return {}


def test_streaming_wizard_creates_pipeline_from_selected_camera_source(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/streams/wizard/create-pipeline",
            json={
                "transmission_id": "tx_front",
                "camera_id": "front",
                "camera_source_id": "sub",
                "preset_id": "simple_stream",
                "optional_parameters": {
                    "pipeline_name": "front_sub_stream",
                    "source_backend": "ffmpeg",
                    "enabled": True,
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pipeline_name"] == "front_sub_stream"
        assert body["camera_id"] == "front"
        assert body["camera_source_id"] == "sub"

        pipeline_model = asyncio.run(client.app.state.config_store.get_pipeline("front_sub_stream"))
        assert pipeline_model is not None
        pipeline = pipeline_model.model_dump(mode="json")
        source_config = _camera_source_config(pipeline)
        assert source_config["camera_id"] == "front"
        assert source_config["source_id"] == "sub"
        assert source_config["backend"] == "ffmpeg"
        assert _stream_publish_config(pipeline)["transmission_id"] == "tx_front"


def test_streaming_wizard_uses_default_camera_source_when_frontend_omits_source_id(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/streams/wizard/create-pipeline",
            json={
                "transmission_id": "tx_front",
                "camera_id": "front",
                "preset_id": "simple_stream",
                "optional_parameters": {"pipeline_name": "front_default_stream"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["camera_source_id"] == "main"

        pipeline_model = asyncio.run(client.app.state.config_store.get_pipeline("front_default_stream"))
        assert pipeline_model is not None
        pipeline = pipeline_model.model_dump(mode="json")
        assert _camera_source_config(pipeline)["source_id"] == "main"


def test_streaming_wizard_rejects_disabled_camera_source(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/streams/wizard/create-pipeline",
            json={
                "transmission_id": "tx_front",
                "camera_id": "front",
                "camera_source_id": "disabled",
                "preset_id": "simple_stream",
                "optional_parameters": {"pipeline_name": "front_disabled_stream"},
            },
        )
        assert response.status_code == 409
        assert "Camera source" in response.text
