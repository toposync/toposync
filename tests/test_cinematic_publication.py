from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cinematic.constants import OPERATOR_ID_DIRECTOR_SOURCE
from toposync_ext_cinematic.pipelines import register_cinematic_pipeline_operators
from toposync_ext_streaming.api.models import EXTENSION_ID as STREAMING_EXTENSION_ID
from toposync_ext_streaming.pipelines import (
    StreamingRuntimeBindings,
    register_streaming_pipeline_operators,
    set_streaming_runtime_bindings,
)
from toposync_ext_streaming.pipelines.operators import PublishVideoRuntime
from toposync_ext_streaming.streaming.publisher_manager import PublisherEncodingSettings, PublisherOutput
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState
from toposync_ext_streaming.streaming.writer_bridge import StreamWriterBridge


class _ConfigStoreStub:
    def __init__(self, extension_payload: dict[str, Any]) -> None:
        self._extension_payload = extension_payload

    async def get_settings(self) -> SimpleNamespace:
        return SimpleNamespace(extensions={STREAMING_EXTENSION_ID: self._extension_payload})


class _EngineManagerStub:
    async def ensure_running(  # noqa: ANN001
        self,
        _engine_settings,
        *,
        engine_paths=None,
        path_auth=None,
        path_configs=None,
    ) -> None:
        _ = engine_paths, path_auth, path_configs

    async def get_urls_for_path(self, path_slug: str, *, host: str | None = None) -> dict[str, str]:
        _ = host
        return {
            "rtsp_url": f"rtsp://127.0.0.1:8554/{path_slug}",
            "hls_url": f"http://127.0.0.1:8888/{path_slug}/index.m3u8",
        }

    async def get_publish_url_for_path(self, path_slug: str, *, host: str | None = None) -> str:
        _ = host
        return f"rtsp://127.0.0.1:8554/{path_slug}"


class _PublisherManagerStub:
    def __init__(self) -> None:
        self.started: set[str] = set()
        self.start_calls: list[str] = []
        self.frames_by_output: dict[str, int] = {}
        self.last_frame_by_output: dict[str, numpy.ndarray] = {}

    async def start_publisher(
        self,
        *,
        output: PublisherOutput,
        engine_path: str,
        publish_url: str,
        encoding_settings: PublisherEncodingSettings,
        input_settings=None,
        encoder_policy=None,
    ) -> None:
        _ = engine_path, publish_url, encoding_settings, input_settings, encoder_policy
        self.started.add(output.output_id)
        self.start_calls.append(output.output_id)

    async def submit_frame(self, output_id: str, frame: numpy.ndarray) -> None:
        self.frames_by_output[output_id] = int(self.frames_by_output.get(output_id, 0)) + 1
        self.last_frame_by_output[output_id] = numpy.asarray(frame).copy()

    async def stop_publisher(self, output_id: str) -> None:
        self.started.discard(output_id)

    async def stop_all(self) -> None:
        for output_id in list(self.started):
            await self.stop_publisher(output_id)

    async def stop_missing(self, desired_output_ids: set[str]) -> None:
        for output_id in list(self.started):
            if output_id not in desired_output_ids:
                await self.stop_publisher(output_id)

    async def snapshot(self) -> dict[str, Any]:
        return {}


@dataclass(slots=True)
class _MediaMtxApiClientStub:
    viewers_by_path: dict[str, int]

    async def get_viewer_count_by_path(self) -> dict[str, int]:
        return dict(self.viewers_by_path)


def _registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_streaming_pipeline_operators(registry)
    register_cinematic_pipeline_operators(registry)
    return registry


def _cinematic_publication_graph() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "demand",
                "operator": "stream.demand_gate",
                "config": {"transmission_id": "tx-cinematic", "output_id": "main"},
            },
            {
                "id": "director",
                "operator": OPERATOR_ID_DIRECTOR_SOURCE,
                "config": {"camera_ids": ["front"]},
            },
            {
                "id": "publish",
                "operator": "stream.publish_video",
                "config": {"transmission_id": "tx-cinematic", "writer_priority": 5},
            },
        ],
        "edges": [
            {
                "from": {"node": "demand", "port": "out"},
                "to": {"node": "director", "port": "gate"},
                "maxsize": 1,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "director", "port": "out"},
                "to": {"node": "publish", "port": "in"},
                "maxsize": 1,
                "drop_policy": "latest_only",
            },
        ],
    }


def _cinematic_packet(frame: numpy.ndarray, *, frame_ts: float = 12.5) -> Packet:
    height, width = int(frame.shape[0]), int(frame.shape[1])
    return Packet.create(
        stream_id="cinematic:test:director",
        lifecycle=Lifecycle.OPEN,
        payload={
            "source": {
                "device_id": "front",
                "source_id": "main",
                "kind": "camera",
                "modality": "video",
                "transport": "rtsp",
            },
            "media": {"modality": "video", "ts": float(frame_ts), "width": width, "height": height},
            "frame_ts": float(frame_ts),
            "camera_id": "front",
            "camera_source_id": "main",
            "cinematic": {"mode": "idle", "cut_reason": "idle_round", "active_camera_id": "front"},
        },
        artifacts={
            MAIN_ARTIFACT_NAME: Artifact(
                name=MAIN_ARTIFACT_NAME,
                data=frame,
                mime_type="image/raw",
                metadata={"source": OPERATOR_ID_DIRECTOR_SOURCE, "width": width, "height": height},
            )
        },
    )


def test_cinematic_publication_pipeline_compiles_and_satisfies_main_artifact_contract() -> None:
    registry = _registry()
    registered = registry.get("stream.publish_video")
    assert registered is not None
    assert MAIN_ARTIFACT_NAME in registered.definition.requires_artifacts

    pipeline = Pipeline(name="cinematic_publication", graph=_cinematic_publication_graph())
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)

    edges = {
        (edge.source_node_id, edge.source_port, edge.target_node_id, edge.target_port)
        for edge in compiled.edges
    }
    assert ("demand", "out", "director", "gate") in edges
    assert ("director", "out", "publish", "in") in edges

    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)
    assert not any(alert.code == "missing_required_artifacts" and alert.node_id == "publish" for alert in alerts)


def test_publish_video_warns_when_main_artifact_is_not_produced_upstream() -> None:
    registry = _registry()
    pipeline = Pipeline(
        name="cinematic_publication_missing_main",
        graph={
            "schema_version": 1,
            "nodes": [
                {
                    "id": "demand",
                    "operator": "stream.demand_gate",
                    "config": {"transmission_id": "tx-cinematic"},
                },
                {
                    "id": "publish",
                    "operator": "stream.publish_video",
                    "config": {"transmission_id": "tx-cinematic"},
                },
            ],
            "edges": [
                {"from": {"node": "demand", "port": "out"}, "to": {"node": "publish", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)

    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "publish"
        and alert.details.get("missing_artifacts") == [MAIN_ARTIFACT_NAME]
        for alert in alerts
    )


def test_publish_video_receives_cinematic_main_artifact_and_writer_priority() -> None:
    asyncio.run(_run_publish_video_receives_cinematic_main_artifact_and_writer_priority())


async def _run_publish_video_receives_cinematic_main_artifact_and_writer_priority() -> None:
    state = TransmissionRuntimeState()
    frame = numpy.full((90, 160, 3), 180, dtype=numpy.uint8)
    runtime = PublishVideoRuntime({"transmission_id": "tx-cinematic", "writer_priority": 7})
    context = SimpleNamespace(pipeline_name="cinematic_pipeline", node_id="publish")

    set_streaming_runtime_bindings(StreamingRuntimeBindings(runtime_state=state))
    try:
        await runtime.process_packet(_cinematic_packet(frame, frame_ts=44.0), context)
    finally:
        set_streaming_runtime_bindings(None)

    selected = await state.get_selected_writer_frame("tx-cinematic", stale_after_s=5.0, placeholder_after_s=10.0)
    assert selected.writer_id == "cinematic_pipeline:publish"
    assert selected.selected_writer_id == "cinematic_pipeline:publish"
    assert selected.writer_priority == 7
    assert selected.frame_ts == 44.0
    assert selected.frame is not None
    assert numpy.array_equal(selected.frame, frame)


def test_cinematic_publication_resize_contain_through_writer_bridge() -> None:
    asyncio.run(_run_cinematic_publication_resize_contain_through_writer_bridge())


async def _run_cinematic_publication_resize_contain_through_writer_bridge() -> None:
    extension_payload = {
        "engine": {"enabled": True, "expose_to_lan": False},
        "transmissions": [
            {
                "id": "tx-cinematic",
                "path": "cinematic-path",
                "enabled": True,
                "outputs": [
                    {
                        "id": "main",
                        "protocol": "hls",
                        "enabled": True,
                        "resolution": {"width": 300, "height": 300},
                        "fps_limit": 12,
                        "resize_mode": "contain",
                    }
                ],
            }
        ],
    }
    state = TransmissionRuntimeState()
    bridge = StreamWriterBridge(
        config_store=_ConfigStoreStub(extension_payload),
        engine_manager=_EngineManagerStub(),  # type: ignore[arg-type]
        runtime_state=state,
        publisher_manager=_PublisherManagerStub(),  # type: ignore[arg-type]
        mediamtx_api_client=_MediaMtxApiClientStub({"cinematic-path": 1}),  # type: ignore[arg-type]
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),  # type: ignore[arg-type]
        viewer_refresh_s=0.2,
        on_demand_enabled=True,
    )
    publisher = bridge._publisher_manager
    frame = numpy.full((100, 200, 3), 220, dtype=numpy.uint8)
    runtime = PublishVideoRuntime({"transmission_id": "tx-cinematic", "writer_priority": 3})

    set_streaming_runtime_bindings(StreamingRuntimeBindings(runtime_state=state))
    try:
        await runtime.process_packet(_cinematic_packet(frame, frame_ts=10.0), SimpleNamespace(pipeline_name="cinematic", node_id="publish"))
    finally:
        set_streaming_runtime_bindings(None)
    await bridge._tick_once(1.0)

    output_id = "tx-cinematic:cinematic-path"
    assert publisher.frames_by_output[output_id] == 1
    output = publisher.last_frame_by_output[output_id]
    assert output.shape == (300, 300, 3)
    assert int(output[10, 10, 0]) == 0
    assert int(output[150, 150, 2]) == 220


def test_cinematic_writer_priority_wins_in_priority_latest_arbitration() -> None:
    asyncio.run(_run_cinematic_writer_priority_wins_in_priority_latest_arbitration())


async def _run_cinematic_writer_priority_wins_in_priority_latest_arbitration() -> None:
    clock = {"now": 10.0}
    state = TransmissionRuntimeState(monotonic=lambda: float(clock["now"]), wall_time=lambda: float(clock["now"]))
    await state.set_transmission_arbitration(transmission_id="tx-cinematic", arbitration_mode="priority_latest")

    await state.update_writer_frame(
        transmission_id="tx-cinematic",
        writer_id="cinematic_pipeline:publish",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=9,
        frame=numpy.full((80, 80, 3), 90, dtype=numpy.uint8),
        frame_ts=1.0,
    )
    clock["now"] = 10.4
    await state.update_writer_frame(
        transmission_id="tx-cinematic",
        writer_id="secondary_pipeline:publish",
        lifecycle_state=Lifecycle.UPDATE,
        writer_priority=1,
        frame=numpy.full((80, 80, 3), 220, dtype=numpy.uint8),
        frame_ts=2.0,
    )

    selected = await state.get_selected_writer_frame("tx-cinematic", stale_after_s=5.0, placeholder_after_s=10.0)

    assert selected.selected_writer_id == "cinematic_pipeline:publish"
    assert selected.writer_priority == 9
    assert selected.frame is not None
    assert int(selected.frame[0, 0, 0]) == 90
