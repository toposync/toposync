from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_cameras.pipelines.postprocess import BestFrameSelectorRuntime, VelocityEstimationRuntime


class _SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[dict[str, Any]]) -> None:
        parsed = _SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._sequence = deque(sequence)

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if not self._sequence:
            return None
        item = self._sequence.popleft()
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=item["lifecycle"],
            payload=dict(item["payload"]),
            artifacts=dict(item.get("artifacts", {})),
            metadata=dict(item.get("metadata", {})),
        )


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: dict[str, list[Packet]]) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packets = self._collector.setdefault(self._sink_name, [])
        packets.append(packet)
        return []


def _register_test_source_and_sink(
    registry: OperatorRegistry,
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
) -> None:
    registry.register_operator(
        operator_id="test.sequence_source",
        config_model=_SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=_SequenceSourceConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _SequenceSourceRuntime(config, sequence),
    )
    registry.register_operator(
        operator_id="test.collect_sink",
        config_model=_CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=_CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, collector),
    )


def _pipeline_runtime(
    *,
    graph: dict[str, Any],
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
) -> PipelineRuntime:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    _register_test_source_and_sink(registry, sequence, collector)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="stage6_postprocess_test", type="final", graph=graph),
    )
    return PipelineRuntime(compiled=compiled, registry=registry)


def test_segmentation_and_best_frame_selection_are_deterministic() -> None:
    async def scenario() -> None:
        sequence: list[dict[str, Any]] = []
        for index, (lifecycle, confidence) in enumerate(
            [
                (Lifecycle.OPEN, 0.95),
                (Lifecycle.UPDATE, 0.30),
                (Lifecycle.UPDATE, 0.40),
                (Lifecycle.CLOSE, 0.10),
            ],
            start=1,
        ):
            frame_value = index
            frame = np.full((60, 60, 3), frame_value, dtype=np.uint8)
            face = np.full((30, 30, 3), frame_value + 10, dtype=np.uint8)
            sequence.append(
                {
                    "lifecycle": lifecycle,
                    "payload": {
                        "frame": frame,
                        "frame_ts": 100.0 + float(index),
                        "tracking_id": "trk-1",
                        "object_confidence": confidence,
                        "object_bbox01": [0.2, 0.2, 0.8, 0.8],
                    },
                    "artifacts": {
                        "face": Artifact(name="face", data=face, mime_type="image/raw"),
                    },
                },
            )

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "segment",
                    "operator": "camera.object_segmentation",
                    "config": {
                        "input_artifact_names": ["face", "frame_original"],
                        "output_artifact_name": "segmented",
                        "bbox_field": "object_bbox01",
                    },
                },
                {
                    "id": "best",
                    "operator": "camera.best_frame_selector",
                    "config": {
                        "input_artifact_names": ["segmented", "frame_original"],
                        "buffer_size": 2,
                        "emit_on_update": False,
                        "emit_on_close": True,
                        "output_artifact_name": "best_frame",
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "segment", "port": "in"}, "maxsize": 4, "drop_policy": "drop_oldest"},
                {"from": {"node": "segment", "port": "out"}, "to": {"node": "best", "port": "in"}, "maxsize": 4, "drop_policy": "drop_oldest"},
                {"from": {"node": "best", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        close_packets = [packet for packet in packets if packet.lifecycle == Lifecycle.CLOSE]
        assert len(close_packets) == 1
        close_packet = close_packets[0]
        assert "best_frame" in close_packet.artifacts
        assert "segmented" in close_packet.artifacts
        assert "frame_original" in close_packet.artifacts

        best_frame = close_packet.artifacts["best_frame"].data
        assert best_frame is not None
        assert int(best_frame[0, 0, 0]) == 13
        assert close_packet.artifacts["segmented"].metadata.get("source_artifact_name") == "face"

        artifact_contract = close_packet.payload.get("artifact_contract")
        assert isinstance(artifact_contract, dict)
        assert artifact_contract.get("selected_input_artifact_name") == "segmented"
        assert close_packet.payload.get("artifact_names") == [
            "best_frame",
            "face",
            "frame_original",
            "segmented",
        ]

    asyncio.run(scenario())


def test_image_resize_downscales_selected_artifacts_in_place() -> None:
    async def scenario() -> None:
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "frame_ts": 1.0,
                    "tracking_id": "trk-resize",
                },
            },
        ]

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "resize",
                    "operator": "camera.image_resize",
                    "config": {
                        "artifact_names": ["frame_original"],
                        "max_edge_px": 50,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "resize", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "resize", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        assert "frame_original" in packet.artifacts
        image = packet.artifacts["frame_original"].data
        assert image is not None
        assert tuple(image.shape[:2]) == (25, 50)
        meta = packet.artifacts["frame_original"].metadata
        assert meta.get("resized_from") == {"width": 200, "height": 100}
        assert meta.get("resized_to") == {"width": 50, "height": 25}

    asyncio.run(scenario())


def test_mapping_area_and_velocity_chain_filters_on_stopped_object() -> None:
    async def scenario() -> None:
        frame = np.zeros((50, 50, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "camera_id": "camera-main",
                    "tracking_id": "velocity-1",
                    "frame_ts": 1.0,
                    "object_bbox01": [0.48, 0.48, 0.52, 0.52],
                },
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "camera_id": "camera-main",
                    "tracking_id": "velocity-1",
                    "frame_ts": 2.0,
                    "object_bbox01": [0.70, 0.48, 0.74, 0.52],
                },
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame": frame,
                    "camera_id": "camera-main",
                    "tracking_id": "velocity-1",
                    "frame_ts": 3.0,
                    "object_bbox01": [0.70, 0.48, 0.74, 0.52],
                },
            },
        ]
        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {"stream_id": "camera:test"}},
                {
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "camera_id_field": "camera_id",
                        "bbox_field": "object_bbox01",
                        "control_points": [
                            {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                            {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                        ],
                    },
                },
                {
                    "id": "area",
                    "operator": "camera.area_restriction",
                    "config": {
                        "areas": [
                            {
                                "name": "front",
                                "points": [
                                    {"x": 0.0, "z": 0.0},
                                    {"x": 10.0, "z": 0.0},
                                    {"x": 10.0, "z": 10.0},
                                    {"x": 0.0, "z": 10.0},
                                ],
                            },
                        ],
                        "include_area_names": ["front"],
                    },
                },
                {
                    "id": "velocity",
                    "operator": "camera.velocity_estimation",
                    "config": {
                        "stopped_speed_threshold": 0.15,
                        "filter_mode": "annotate",
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "mapping", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "mapping", "port": "out"}, "to": {"node": "area", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "area", "port": "out"}, "to": {"node": "velocity", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                {"from": {"node": "velocity", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 3
        packet = packets[-1]
        assert packet.payload.get("area_label") == "front"
        velocity = packet.payload.get("velocity")
        assert isinstance(velocity, dict)
        assert velocity.get("ever_stopped") is True
        assert velocity.get("moving") is False
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 7.2
        assert round(float(world.get("z")), 1) == 5.0

    asyncio.run(scenario())


def test_best_frame_selector_keeps_bounded_buffer_per_tracking_id() -> None:
    async def scenario() -> None:
        runtime = BestFrameSelectorRuntime(
            {
                "input_artifact_names": ["segmented"],
                "fallback_to_payload_frame": False,
                "output_artifact_name": "best_frame",
                "buffer_size": 2,
                "emit_on_update": False,
                "emit_on_close": True,
            },
        )

        def make_packet(
            *,
            tracking_id: str,
            lifecycle: Lifecycle,
            frame_value: int,
            confidence: float,
        ) -> Packet:
            frame = np.full((12, 12, 3), frame_value, dtype=np.uint8)
            return Packet.create(
                stream_id=f"obj:{tracking_id}",
                lifecycle=lifecycle,
                payload={
                    "tracking_id": tracking_id,
                    "object_confidence": confidence,
                    "object_bbox01": [0.2, 0.2, 0.8, 0.8],
                },
                artifacts={"segmented": Artifact(name="segmented", data=frame, mime_type="image/raw")},
            )

        sequence = [
            make_packet(tracking_id="a", lifecycle=Lifecycle.OPEN, frame_value=1, confidence=0.95),
            make_packet(tracking_id="b", lifecycle=Lifecycle.OPEN, frame_value=10, confidence=0.90),
            make_packet(tracking_id="a", lifecycle=Lifecycle.UPDATE, frame_value=2, confidence=0.20),
            make_packet(tracking_id="b", lifecycle=Lifecycle.UPDATE, frame_value=11, confidence=0.80),
            make_packet(tracking_id="a", lifecycle=Lifecycle.CLOSE, frame_value=3, confidence=0.10),
            make_packet(tracking_id="b", lifecycle=Lifecycle.CLOSE, frame_value=12, confidence=0.10),
        ]

        outputs: list[Packet] = []
        for packet in sequence:
            output_packets = await runtime.process_packet(packet, context=None)
            outputs.extend(output_packets)

        close_outputs = [packet for packet in outputs if packet.lifecycle == Lifecycle.CLOSE]
        assert len(close_outputs) == 2
        close_by_tracking = {
            str(packet.payload.get("tracking_id")): packet
            for packet in close_outputs
        }
        assert int(close_by_tracking["a"].artifacts["best_frame"].data[0, 0, 0]) == 2
        assert int(close_by_tracking["b"].artifacts["best_frame"].data[0, 0, 0]) == 11

    asyncio.run(scenario())


def test_velocity_filter_mode_stopped_once_emits_only_after_object_stops() -> None:
    async def scenario() -> None:
        runtime = VelocityEstimationRuntime(
            {
                "stopped_speed_threshold": 0.2,
                "filter_mode": "stopped_once",
            },
        )

        def make_packet(frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="obj:velocity",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "tracking_id": "velocity-track",
                    "frame_ts": frame_ts,
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [make_packet(1.0, 0.0), make_packet(2.0, 1.0), make_packet(3.0, 1.0)]:
            output_packets = await runtime.process_packet(packet, context=None)
            outputs.extend(output_packets)

        assert len(outputs) == 1
        velocity = outputs[0].payload.get("velocity")
        assert isinstance(velocity, dict)
        assert velocity.get("ever_stopped") is True
        assert velocity.get("moving") is False

    asyncio.run(scenario())
