from __future__ import annotations

import asyncio
import logging
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
from toposync.runtime.pipelines.execution import (
    NodeExecutionContext,
    NodeRuntimeMetrics,
    SourceOperatorRuntime,
)
from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync.runtime.pipelines.runtime import BoundedChannel


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
        runtime.node_metrics["detect"].record_completion()
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
        completed_at = runtime.node_metrics["detect"].last_completed_at
        assert detect_node["last_completed_at"] == completed_at
        assert detect_node["last_received_at"] is None
        assert detect_node["progress"]["completed_packets"] == 1
        assert camera_node["last_completed_at"] is None
        assert runtime.graph_runtime_info()["nodes"]["detect"]["last_completed_at"] == completed_at
        assert edge["pressure_cause"] == "drop_policy"
        assert edge["progress"]["dropped_total"] == 1

        from toposync.runtime.pipelines.runtime_info import build_graph_runtime_info

        for issue in ("ext.cameras.cat.regionReconfirm", None):
            runtime.node_metrics["detect"].unavailable_reason = issue
            current = build_graph_runtime_info(
                compiled=compiled, registry=registry, node_metrics=runtime.node_metrics,
                channel_metrics={}, task_states={"detect": "running"}, running=True,
            ).to_dict()["nodes"]["detect"]
            assert current["unavailable_reason"] == issue
            assert current["runtime_state"] == ("degraded" if issue else "active")
            assert current["progress"]["error_count"] == 0

    asyncio.run(scenario())


def _activity_context() -> NodeExecutionContext:
    return NodeExecutionContext(
        node_id="probe",
        operator_id="test.probe",
        execution_mode="in_event_loop",
        max_concurrency=1,
        scheduler=ExecutionScheduler(),
        pipeline_name="activity_probe",
        inputs={"in": BoundedChannel(name="input", maxsize=1)},
        outputs={"out": [BoundedChannel(
            name="output", maxsize=1, drop_policy=DropPolicy.DROP_NEWEST,
        )]},
        cancel_event=asyncio.Event(),
        metrics=NodeRuntimeMetrics(),
        logger=logging.getLogger("test.activity_probe"),
    )


@pytest.mark.parametrize("outcome", ["empty", "error", "canceled", "emitted", "dropped"])
def test_activity_distinguishes_receipt_completion_and_delivery(outcome: str) -> None:
    async def scenario() -> None:
        context = _activity_context()
        entered, release = asyncio.Event(), asyncio.Event()
        packet = Packet.create(stream_id="activity")
        await context.inputs["in"].put(packet)
        if outcome == "dropped":
            await context.outputs["out"][0].put(packet)

        class ControlledTransform(TransformOperatorRuntime):
            async def process_packet(self, packet, context):
                entered.set()
                await release.wait()
                if outcome in {"empty", "error", "canceled"}:
                    context.cancel_event.set()
                if outcome == "error":
                    raise ValueError("controlled failure")
                if outcome == "canceled":
                    raise asyncio.CancelledError
                return [] if outcome == "empty" else [packet]

        task = asyncio.create_task(ControlledTransform().run(context))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            receiving = context.metrics.snapshot()
            assert receiving["processed_packets"] == 1
            assert receiving["last_received_at"] is not None
            assert receiving["completed_packets"] == 0
            assert receiving["last_completed_at"] is None
            assert receiving["last_emitted_at"] is None
            release.set()
            if outcome in {"emitted", "dropped"}:
                async def wait_for_delivery_attempt():
                    while not (context.metrics.emitted_packets or context.metrics.dropped_packets):
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_for_delivery_attempt(), timeout=1)
                context.cancel_event.set()
            if outcome == "canceled":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(task, timeout=1)
            finished = context.metrics.snapshot()
            assert finished["last_received_at"] == receiving["last_received_at"]
            completed = outcome not in {"error", "canceled"}
            assert finished["completed_packets"] == int(completed)
            assert (finished["last_completed_at"] is not None) == completed
            assert (finished["last_emitted_at"] is not None) == (outcome == "emitted")
            assert finished["error_count"] == int(outcome == "error")
            assert context.metrics.snapshot() == finished
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("produces_packet", [False, True])
def test_source_completion_requires_a_produced_packet(produces_packet: bool) -> None:
    async def scenario() -> None:
        context = _activity_context()

        class OneSource(SourceOperatorRuntime):
            async def produce(self, context):
                context.cancel_event.set()
                return Packet.create(stream_id="source") if produces_packet else None

        await OneSource().run(context)
        assert context.metrics.completed_packets == int(produces_packet)
        assert (context.metrics.last_completed_at is not None) == produces_packet
        assert context.metrics.last_received_at is None

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
                "activation_errors": {"blocked": "Model unavailable"},
                "pipelines": [
                    {"name": "entrance", "processing_server_id": "local"},
                    {"name": "remote", "processing_server_id": "worker"},
                    {"name": "legacy", "processing_server_id": "local"},
                ],
                "graph_info": {"graphs": [{
                    "graph_id": "g", "pipeline_name": "entrance", "running": True,
                    "pressure": {"active": False},
                    "nodes": {"detect": {"last_received_at": 90, "last_completed_at": 91}},
                }]},
            }

    with TestClient(create_app()) as client:
        client.app.state.pipelines_orchestrator = _FakeOrchestrator()
        response = client.get("/api/pipelines/runtime/graph-info")
        status_response = client.get("/api/pipelines/runtime/status")

    assert response.status_code == 200
    assert response.json()["graphs"][0]["graph_id"] == "g"
    assert response.json()["status"]["activation_errors"] == {"blocked": "Model unavailable"}
    assert response.json()["execution_observations"] == {
        "entrance": {"state": "observed", "last_received_at": 90, "last_completed_at": 91},
        "remote": None,
        "legacy": None,
    }
    assert status_response.status_code == 200
    assert status_response.json()["status"]["graph_info"]["graphs"][0]["graph_id"] == "g"
