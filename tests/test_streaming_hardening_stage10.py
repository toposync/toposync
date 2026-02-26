from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync_ext_streaming.api.models import (
    StreamingExtensionSettings,
    StreamAuthentication,
    Transmission,
    TransmissionOutput,
    list_path_read_auth_for_host,
)
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXPathAuth, MediaMTXResolvedPorts, render_mediamtx_config
from toposync_ext_streaming.streaming.writer_bridge import (
    _parse_graph_topology,
    _resolve_chain_rtsp_source,
    _resolve_simple_chain,
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
    app.state.config_store = config_store
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_list_path_read_auth_for_host_respects_enabled_outputs() -> None:
    settings = StreamingExtensionSettings(
        transmissions=[
            Transmission(
                id="tx-auth",
                host_server_id="local",
                path="cam-main",
                outputs=[
                    TransmissionOutput(
                        id="hls_main",
                        protocol="hls",
                        enabled=True,
                        authentication=StreamAuthentication(enabled=True, username="viewer", password="secret"),
                    )
                ],
            ),
            Transmission(
                id="tx-other",
                host_server_id="edge_gpu",
                path="cam-edge",
                outputs=[
                    TransmissionOutput(
                        id="hls_edge",
                        protocol="hls",
                        enabled=True,
                        authentication=StreamAuthentication(enabled=True, username="edge", password="pass"),
                    )
                ],
            ),
        ]
    )

    auth_local = list_path_read_auth_for_host(settings, host_server_id="local")
    auth_edge = list_path_read_auth_for_host(settings, host_server_id="edge_gpu")

    assert auth_local == {"cam-main": ("viewer", "secret")}
    assert auth_edge == {"cam-edge": ("edge", "pass")}


def test_transmission_urls_include_auth_hints(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Auth stream",
                "path": "auth-main",
                "outputs": [
                    {
                        "id": "hls_main",
                        "protocol": "hls",
                        "enabled": True,
                        "authentication": {
                            "enabled": True,
                            "username": "viewer",
                            "password": "secret123",
                        },
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = created_res.json()["id"]

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")
        assert urls_res.status_code == 200
        body = urls_res.json()
        outputs = body.get("outputs")
        assert isinstance(outputs, list) and len(outputs) == 1
        output = outputs[0]
        assert output["requires_auth"] is True
        assert output["auth_username"] == "viewer"


def test_mediamtx_config_renders_path_read_and_publish_auth() -> None:
    config = render_mediamtx_config(
        bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997),
        paths=["camera-main"],
        enable_webrtc=True,
        path_auth=[
            MediaMTXPathAuth(
                path="camera-main",
                read_username="viewer",
                read_password="viewer-pass",
                publish_username="pub-internal",
                publish_password="pub-pass",
            )
        ],
    )

    assert "authInternalUsers:" in config
    assert "viewer" in config
    assert "viewer-pass" in config
    assert "pub-internal" in config
    assert "pub-pass" in config
    assert "action: read" in config
    assert "action: publish" in config


def test_bypass_simple_chain_is_detected_and_resolves_rtsp_source() -> None:
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "source",
                "operator": "camera.source",
                "config": {"camera_id": "cam1", "backend": "ffmpeg"},
            },
            {
                "id": "fps",
                "operator": "core.fps_reducer",
                "config": {"target_fps": 8},
            },
            {
                "id": "stream",
                "operator": "stream.write",
                "config": {"transmission_id": "tx1", "bypass_mode": "auto"},
            },
        ],
        "edges": [
            {"from": {"node": "source", "port": "out"}, "to": {"node": "fps", "port": "in"}},
            {"from": {"node": "fps", "port": "out"}, "to": {"node": "stream", "port": "in"}},
        ],
    }
    by_node_id, incoming_by_target, outgoing_by_source = _parse_graph_topology(graph)
    chain = _resolve_simple_chain(
        target_stream_node_id="stream",
        by_node_id=by_node_id,
        incoming_by_target=incoming_by_target,
        outgoing_by_source=outgoing_by_source,
    )
    assert chain is not None
    assert chain["fps_limit"] == 8

    source_url, source_fps, source_backend, camera_id = _resolve_chain_rtsp_source(
        camera_node=chain["camera_node"],
        camera_by_id={
            "cam1": {
                "id": "cam1",
                "rtsp_url": "rtsp://10.0.0.50/live",
                "username": "camuser",
                "password": "campass",
                "fps": 20,
            }
        },
    )
    assert source_url == "rtsp://camuser:campass@10.0.0.50/live"
    assert source_fps == 20
    assert source_backend == "ffmpeg"
    assert camera_id == "cam1"


def test_bypass_rejects_non_simple_graph() -> None:
    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "source", "operator": "camera.source", "config": {"camera_id": "cam1"}},
            {"id": "detect", "operator": "vision.object_detection_yolo", "config": {}},
            {"id": "stream", "operator": "stream.write", "config": {"transmission_id": "tx1", "bypass_mode": "auto"}},
        ],
        "edges": [
            {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
            {"from": {"node": "detect", "port": "out"}, "to": {"node": "stream", "port": "in"}},
        ],
    }
    by_node_id, incoming_by_target, outgoing_by_source = _parse_graph_topology(graph)
    chain = _resolve_simple_chain(
        target_stream_node_id="stream",
        by_node_id=by_node_id,
        incoming_by_target=incoming_by_target,
        outgoing_by_source=outgoing_by_source,
    )
    assert chain is None
