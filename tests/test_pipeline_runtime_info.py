from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    CompilationReport,
    DropPolicy,
    OperatorRegistry,
    Packet,
    PipelineBundleRuntime,
    PipelineGraphCompiler,
    PipelineRuntime,
    TransformOperatorRuntime,
)
import toposync.extensions.manager as ext_manager_mod


def _runtime_factory(_config: dict[str, Any], _deps: Any) -> TransformOperatorRuntime:
    return TransformOperatorRuntime()


def _registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.camera_source",
        outputs=[{"name": "out"}],
        resource_kind="camera",
        pressure_behavior="reduce_source_rate",
        runtime_factory=_runtime_factory,
    )
    registry.register_operator(
        operator_id="test.vision_detect",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        resource_kind="vision_model",
        pressure_behavior="skip_before_compute",
        runtime_factory=_runtime_factory,
    )
    registry.register_operator(
        operator_id="test.sink",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        share_strategy="never",
        pressure_behavior="block",
        runtime_factory=_runtime_factory,
    )
    return registry


def test_graph_runtime_info_exposes_nodes_edges_resources_pressure_and_progress() -> None:
    async def scenario() -> None:
        registry = _registry()
        pipeline = Pipeline(
            name="runtime_info_probe",
            graph={
                "schema_version": 2,
                "uid": "runtime_info_probe",
                "nodes": [
                    {
                        "uid": "camera",
                        "id": "camera",
                        "operator": "test.camera_source",
                        "config": {"camera_id": "front"},
                    },
                    {
                        "uid": "detect",
                        "id": "detect",
                        "operator": "test.vision_detect",
                        "config": {"model_id": "people"},
                    },
                    {"uid": "sink", "id": "sink", "operator": "test.sink", "config": {}},
                ],
                "edges": [
                    {
                        "uid": "camera.out->detect.in",
                        "from": {"node": "camera", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                        "queue": {"max_items": 1, "drop_policy": DropPolicy.DROP_NEWEST.value},
                    },
                    {
                        "uid": "detect.out->sink.in",
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        channel = runtime.channel_map["camera.out->detect.in"]

        assert (await channel.put(Packet.create(stream_id="front", payload={"seq": 1}))).accepted
        dropped = await channel.put(Packet.create(stream_id="front", payload={"seq": 2}))
        assert not dropped.accepted

        runtime.node_metrics["detect"].processed_packets = 2
        runtime.node_metrics["detect"].emitted_packets = 1
        info = runtime.graph_runtime_info()

        assert info["graph_id"] == "runtime_info_probe"
        assert info["running"] is False
        assert info["progress"]["processed_packets"] == 2
        assert info["progress"]["emitted_packets"] == 1
        assert info["pressure"]["active"] is True
        assert info["pressure"]["cause"] == "drop_policy"
        assert info["resources"]["camera:front"]["state"] == "pressured"
        assert info["resources"]["vision_model:people"]["state"] == "pressured"

        camera_node = next(item for item in info["nodes"].values() if item["node_id"] == "camera")
        detect_node = next(item for item in info["nodes"].values() if item["node_id"] == "detect")
        edge = next(item for item in info["edges"].values() if item["source"]["node"] == "camera")
        assert camera_node["resource_kind"] == "camera"
        assert detect_node["pressure_behavior"] == "skip_before_compute"
        assert edge["pressure_cause"] == "drop_policy"
        assert edge["progress"]["dropped_total"] == 1

    asyncio.run(scenario())


def test_graph_v1_is_rejected() -> None:
    with pytest.raises(ValueError, match="graph v1 is no longer supported"):
        Pipeline(
            name="legacy",
            graph={
                "schema_version": 1,
                "nodes": [{"id": "source", "operator": "test.camera_source", "config": {}}],
                "edges": [],
            },
        )


def test_bundle_graph_runtime_info_exposes_shared_node_occurrences() -> None:
    registry = _registry()
    compiler = PipelineGraphCompiler(registry)

    def pipeline(name: str, source_id: str, sink_id: str) -> Pipeline:
        return Pipeline(
            name=name,
            graph={
                    "schema_version": 2,
                    "uid": name,
                    "nodes": [
                        {
                            "uid": source_id,
                            "id": source_id,
                            "operator": "test.camera_source",
                            "config": {"camera_id": "front"},
                        },
                        {"uid": sink_id, "id": sink_id, "operator": "test.sink", "config": {}},
                    ],
                    "edges": [
                        {
                            "uid": f"{source_id}.out->{sink_id}.in",
                            "from": {"node": source_id, "port": "out"},
                            "to": {"node": sink_id, "port": "in"},
                        }
                ],
            },
        )

    report = CompilationReport(
        pipelines=(
            compiler.compile_pipeline(pipeline("a", "source_a", "sink_a")),
            compiler.compile_pipeline(pipeline("b", "source_b", "sink_b")),
        ),
        shared_signatures={},
    )
    bundle = PipelineBundleRuntime(report=report, registry=registry, bundle_name="local_bundle")
    info = bundle.graph_runtime_info()

    assert info["graph_id"] == "local_bundle"
    assert info["pipelines"] == ["a", "b"]
    assert any(len(items) == 2 for items in info["shared_nodes"].values())


def test_runtime_graph_info_endpoint_returns_graphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])

    class _FakeOrchestrator:
        def status(self) -> dict[str, Any]:
            return {
                "running": True,
                "graph_info": {"graphs": [{"graph_id": "g", "pressure": {"active": False}}]},
            }

    with TestClient(create_app()) as client:
        client.app.state.pipelines_orchestrator = _FakeOrchestrator()
        response = client.get("/api/pipelines/runtime/graph-info")
        status_response = client.get("/api/pipelines/runtime/status")

    assert response.status_code == 200
    assert response.json()["graphs"][0]["graph_id"] == "g"
    assert status_response.status_code == 200
    assert status_response.json()["status"]["graph_info"]["graphs"][0]["graph_id"] == "g"
