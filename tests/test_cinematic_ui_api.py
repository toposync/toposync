from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cinematic.api import create_cinematic_router
from toposync_ext_cinematic.constants import OPERATOR_ID_DIRECTOR_SOURCE
from toposync_ext_cinematic.pipelines import register_cinematic_pipeline_operators
from toposync_ext_cinematic.status import get_cinematic_status_store
from toposync_ext_cinematic.wizard import build_cinematic_wizard_graph
from toposync_ext_streaming.pipelines import register_streaming_pipeline_operators


def _settings() -> AppSettings:
    return AppSettings(
        extensions={
            "com.toposync.streaming": {
                "transmissions": [
                    {
                        "id": "tx_cinematic",
                        "name": "Cinematic",
                        "path": "cinematic",
                        "enabled": True,
                        "host_server_id": "local",
                        "outputs": [{"id": "main", "protocol": "hls", "enabled": True}],
                    }
                ]
            },
            "com.toposync.cameras": {
                "devices": [
                    {
                        "id": "front",
                        "name": "Front",
                        "sources": [
                            {
                                "id": "main",
                                "kind": "video",
                                "role": "main",
                                "enabled": True,
                                "is_default": True,
                            }
                        ],
                    },
                    {
                        "id": "side",
                        "name": "Side",
                        "sources": [{"id": "main", "kind": "video", "enabled": True}],
                    },
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
    register_streaming_pipeline_operators(registry)
    register_cinematic_pipeline_operators(registry)

    services = ServiceRegistry()
    services.register("notifications.list", lambda **_kwargs: {"notifications": [], "next_cursor": None})
    services.register("cameras.catalog.list", lambda **_kwargs: {"cameras": []})
    services.register("cameras.capture.open", lambda **_kwargs: {"lease_id": "lease"})
    services.register("cameras.capture.get_latest", lambda **_kwargs: {"frame": None})
    services.register("cameras.capture.release", lambda **_kwargs: {"ok": True})
    services.register("cameras.capture.release_owner", lambda **_kwargs: {"ok": True})

    app.state.config_store = config_store
    app.state.pipeline_operator_registry = registry
    app.state.pipeline_graph_compiler = PipelineGraphCompiler(registry)
    app.state.services = services
    app.include_router(create_cinematic_router())
    return TestClient(app)


def _node_config(pipeline: dict[str, Any], operator_id: str) -> dict[str, Any]:
    graph = pipeline.get("graph") if isinstance(pipeline.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if isinstance(node, dict) and node.get("operator") == operator_id:
            config = node.get("config")
            return config if isinstance(config, dict) else {}
    return {}


def test_cinematic_wizard_graph_uses_demand_director_publish_chain() -> None:
    graph = build_cinematic_wizard_graph(
        transmission_id="tx_cinematic",
        optional_parameters={
            "cameras_mode": "include",
            "camera_ids": ["front"],
            "resize_mode": "none",
            "writer_priority": 6,
        },
    )

    assert [node["operator"] for node in graph["nodes"]] == [
        "stream.demand_gate",
        OPERATOR_ID_DIRECTOR_SOURCE,
        "stream.publish_video",
    ]
    assert graph["edges"][0]["maxsize"] == 1
    assert graph["edges"][0]["drop_policy"] == "drop_oldest"
    assert graph["edges"][1]["maxsize"] == 1
    assert graph["edges"][1]["drop_policy"] == "latest_only"
    assert graph["nodes"][0]["config"]["demand_scope"] == "transmission"
    assert graph["nodes"][0]["config"]["output_id"] == ""
    assert graph["nodes"][0]["config"]["quality_profile_id"] == ""
    assert graph["nodes"][1]["config"]["behavior"] == "rotation_with_events"
    assert graph["nodes"][1]["config"]["camera_ids"] == ["front"]
    assert graph["nodes"][2]["config"]["resize_mode"] == "none"
    assert graph["nodes"][2]["config"]["writer_priority"] == 6


def test_cinematic_wizard_endpoint_creates_pipeline(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/cinematic/wizard/create-pipeline",
            json={
                "transmission_id": "tx_cinematic",
                "optional_parameters": {
                    "pipeline_name": "cinematic_front",
                    "cameras_mode": "include",
                    "camera_ids": ["front"],
                    "priority_filter": ["high"],
                    "resize_mode": "none",
                    "writer_priority": 4,
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pipeline_name"] == "cinematic_front"
        assert body["behavior"] == "rotation_with_events"
        assert body["camera_ids"] == ["front"]

        pipeline_model = asyncio.run(client.app.state.config_store.get_pipeline("cinematic_front"))
        assert pipeline_model is not None
        pipeline = pipeline_model.model_dump(mode="json")
        director_config = _node_config(pipeline, OPERATOR_ID_DIRECTOR_SOURCE)
        publish_config = _node_config(pipeline, "stream.publish_video")
        assert director_config["cameras_mode"] == "include"
        assert director_config["behavior"] == "rotation_with_events"
        assert director_config["priority_filter"] == ["high"]
        assert publish_config["transmission_id"] == "tx_cinematic"
        assert publish_config["resize_mode"] == "none"
        assert publish_config["writer_priority"] == 4


def test_cinematic_wizard_endpoint_creates_primary_behavior_pipeline(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/cinematic/wizard/create-pipeline",
            json={
                "transmission_id": "tx_cinematic",
                "optional_parameters": {
                    "pipeline_name": "cinematic_primary",
                    "behavior": "primary_with_events",
                    "primary_camera_id": "front",
                    "cameras_mode": "include",
                    "camera_ids": ["side"],
                    "priority_filter": ["high", "medium"],
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["behavior"] == "primary_with_events"
        assert body["primary_camera_id"] == "front"
        assert body["camera_ids"] == ["front", "side"]

        pipeline_model = asyncio.run(client.app.state.config_store.get_pipeline("cinematic_primary"))
        assert pipeline_model is not None
        pipeline = pipeline_model.model_dump(mode="json")
        director_config = _node_config(pipeline, OPERATOR_ID_DIRECTOR_SOURCE)
        assert director_config["behavior"] == "primary_with_events"
        assert director_config["primary_camera_id"] == "front"
        assert director_config["camera_ids"] == ["front", "side"]
        assert director_config["priority_filter"] == ["high", "medium"]


def test_cinematic_wizard_endpoint_rejects_unknown_camera(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/cinematic/wizard/create-pipeline",
            json={
                "transmission_id": "tx_cinematic",
                "optional_parameters": {
                    "pipeline_name": "cinematic_missing",
                    "cameras_mode": "include",
                    "camera_ids": ["missing"],
                },
            },
        )

        assert response.status_code == 404
        assert "Camera not found" in response.text


def test_cinematic_wizard_endpoint_rejects_unknown_primary_camera(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.post(
            "/api/cinematic/wizard/create-pipeline",
            json={
                "transmission_id": "tx_cinematic",
                "optional_parameters": {
                    "pipeline_name": "cinematic_missing_primary",
                    "behavior": "primary_with_events",
                    "primary_camera_id": "missing",
                },
            },
        )

        assert response.status_code == 404
        assert "Camera not found" in response.text


def test_cinematic_wizard_endpoint_rejects_duplicate_pipeline_name(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        payload = {
            "transmission_id": "tx_cinematic",
            "optional_parameters": {
                "pipeline_name": "cinematic_duplicate",
                "cameras_mode": "all",
            },
        }
        first = client.post("/api/cinematic/wizard/create-pipeline", json=payload)
        assert first.status_code == 200, first.text
        second = client.post("/api/cinematic/wizard/create-pipeline", json=payload)
        assert second.status_code == 409
        assert "Pipeline already exists" in second.text


def test_cinematic_status_endpoint_returns_store_snapshot(tmp_path: Path) -> None:
    store = get_cinematic_status_store()
    store.clear()
    store.update(
        pipeline_name="cinematic_front",
        node_id="director",
        payload={
            "demand_active": True,
            "mode": "event",
            "cut_reason": "event_priority",
            "active_camera_id": "front",
        },
    )

    try:
        with _create_client(tmp_path) as client:
            response = client.get("/api/cinematic/status")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["items"][0]["pipeline_name"] == "cinematic_front"
            assert body["items"][0]["cut_reason"] == "event_priority"
    finally:
        store.clear()


def test_cinematic_diagnostics_reports_basic_counts(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        response = client.get("/api/cinematic/diagnostics")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["counts"]["transmissions"] == 1
        assert body["counts"]["cameras"] == 2
        assert body["operators"][OPERATOR_ID_DIRECTOR_SOURCE] is True
