from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineBundleRuntime,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_vision.pipelines import DetectionObject, ModelManifest, ModelRegistry


def _build_registry() -> ModelRegistry:
    return ModelRegistry(
        [
            ModelManifest(
                model_id="fake.detector",
                display_name="Fake Detector",
                task="detection",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://detector",
            )
        ]
    )


class _FrameSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"
    max_frames: int = Field(default=8, ge=1, le=1000)
    interval_ms: int = Field(default=20, ge=1, le=1000)


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class _IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FrameSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any]) -> None:
        parsed = _FrameSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._max_frames = int(parsed.max_frames)
        self._interval_s = float(parsed.interval_ms) / 1000.0
        self._next_tick = time.monotonic()
        self._sequence = 0
        self._counters = counters

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._sequence >= self._max_frames:
            return None
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())
        self._counters["source_frames"] = int(self._counters.get("source_frames", 0)) + 1
        frame = {"seq": self._sequence}
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"frame_index": self._sequence},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="application/json"),
                "frame": Artifact(
                    name="frame",
                    data=frame,
                    mime_type="application/json",
                    metadata={"derived_from": "frame_original"},
                ),
            },
            metadata={"source": "test.frame_source", "motion_gate_open": True},
        )
        self._sequence += 1
        return packet


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any]) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._counters = counters

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packets = self._counters.setdefault("packets", [])
        packets.append({"sink": self._sink_name, "packet": packet})
        sink_counters = self._counters.setdefault("sink_counts", {})
        sink_counters[self._sink_name] = int(sink_counters.get(self._sink_name, 0)) + 1
        return []


class _IdentityRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        _IdentityConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        return [packet]


class _SequenceDetectorBackend:
    backend_id = "fake_detector"

    def __init__(self, sequence: list[list[DetectionObject]], counters: dict[str, Any]) -> None:
        self._sequence = sequence
        self._counters = counters
        self._index = 0

    def detect(
        self, frame: Any, *, categories: set[str] | None = None
    ) -> list[DetectionObject]:  # noqa: ARG002
        self._counters["detect_calls"] = int(self._counters.get("detect_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index, len(self._sequence) - 1)
        self._index += 1
        objects = self._sequence[idx]
        if not categories:
            return list(objects)
        return [item for item in objects if item.label in categories]


def _register_test_source_and_sink(
    registry: OperatorRegistry,
    counters: dict[str, Any],
    *,
    source_shareable: bool,
) -> None:
    registry.register_operator(
        operator_id="test.frame_source",
        config_model=_FrameSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=_FrameSourceConfig().model_dump(),
        share_strategy="by_signature" if source_shareable else "never",
        runtime_factory=lambda config, _deps: _FrameSourceRuntime(config, counters),
    )
    registry.register_operator(
        operator_id="test.identity",
        config_model=_IdentityConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults=_IdentityConfig().model_dump(),
        share_strategy="by_signature",
        runtime_factory=lambda config, _deps: _IdentityRuntime(config),
    )
    registry.register_operator(
        operator_id="test.collect_sink",
        config_model=_CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=_CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, counters),
    )


def _tracking_pipeline_graph(*, source_id: str, detect_id: str, track_id: str, sink_id: str, sink_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": source_id,
                "operator": "test.frame_source",
                "config": {"stream_id": "camera:test", "max_frames": 12, "interval_ms": 20},
            },
            {
                "id": detect_id,
                "operator": "vision.detect",
                "config": {
                    "model_id": "fake.detector",
                    "emit_mode": "annotate",
                    "categories": ["person"],
                },
            },
            {
                "id": track_id,
                "operator": "vision.track",
                "config": {
                    "tracker_id": "simple_iou_kalman",
                    "default_interval_seconds": 0.0,
                    "close_after_seconds": 0.05,
                    "emit_mode": "events",
                },
            },
            {"id": sink_id, "operator": "test.collect_sink", "config": {"sink_name": sink_name}},
        ],
        "edges": [
            {"from": {"node": source_id, "port": "out"}, "to": {"node": detect_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {"from": {"node": detect_id, "port": "out"}, "to": {"node": track_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {"from": {"node": track_id, "port": "out"}, "to": {"node": sink_id, "port": "in"}, "maxsize": 64, "drop_policy": "drop_oldest"},
        ],
    }


def _tracking_pipeline_graph_with_shareable_transform(
    *,
    source_id: str,
    detect_id: str,
    track_id: str,
    transform_id: str,
    sink_id: str,
    sink_name: str,
    track_to_transform_maxsize: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": source_id,
                "operator": "test.frame_source",
                "config": {"stream_id": "camera:test", "max_frames": 12, "interval_ms": 20},
            },
            {
                "id": detect_id,
                "operator": "vision.detect",
                "config": {
                    "model_id": "fake.detector",
                    "emit_mode": "annotate",
                    "categories": ["person"],
                },
            },
            {
                "id": track_id,
                "operator": "vision.track",
                "config": {
                    "tracker_id": "simple_iou_kalman",
                    "default_interval_seconds": 0.0,
                    "close_after_seconds": 0.05,
                    "emit_mode": "events",
                },
            },
            {"id": transform_id, "operator": "test.identity", "config": {}},
            {"id": sink_id, "operator": "test.collect_sink", "config": {"sink_name": sink_name}},
        ],
        "edges": [
            {"from": {"node": source_id, "port": "out"}, "to": {"node": detect_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {"from": {"node": detect_id, "port": "out"}, "to": {"node": track_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {
                "from": {"node": track_id, "port": "out"},
                "to": {"node": transform_id, "port": "in"},
                "maxsize": int(track_to_transform_maxsize),
                "drop_policy": "drop_oldest",
            },
            {"from": {"node": transform_id, "port": "out"}, "to": {"node": sink_id, "port": "in"}, "maxsize": 64, "drop_policy": "drop_oldest"},
        ],
    }


def _build_detection_sequence(sequence: list[list[tuple[str, float, tuple[float, float, float, float]]]]) -> list[list[DetectionObject]]:
    return [
        [
            DetectionObject(
                label=label,
                label_id=0,
                score=score,
                bbox01=bbox01,
                model_id="fake.detector",
            )
            for label, score, bbox01 in frame
        ]
        for frame in sequence
    ]


def test_vision_track_splits_two_objects_and_closes_lifecycle() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = _build_detection_sequence(
            [
                [
                    ("person", 0.98, (0.1, 0.1, 0.2, 0.4)),
                    ("person", 0.95, (0.5, 0.2, 0.7, 0.6)),
                ],
                [
                    ("person", 0.96, (0.11, 0.1, 0.22, 0.4)),
                    ("person", 0.92, (0.51, 0.2, 0.71, 0.61)),
                ],
                [("person", 0.91, (0.12, 0.1, 0.23, 0.41))],
                [("person", 0.89, (0.13, 0.1, 0.24, 0.42))],
                [],
                [],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph = _tracking_pipeline_graph(
            source_id="source",
            detect_id="detect",
            track_id="track",
            sink_id="sink",
            sink_name="tracking_sink",
        )
        pipeline = Pipeline(name="stage5_tracking_split", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.6)

        packets = [record["packet"] for record in counters.get("packets", [])]
        assert packets
        grouped_by_tracking: dict[str, list[Packet]] = defaultdict(list)
        for packet in packets:
            tracking_id = str(packet.payload.get("tracking_id") or "")
            grouped_by_tracking[tracking_id].append(packet)

        grouped_by_tracking.pop("", None)
        assert len(grouped_by_tracking) == 2
        source_tracking_ids = {
            str(items[0].payload.get("tracker_track_id") or "") for items in grouped_by_tracking.values()
        }
        assert len(source_tracking_ids) == 2
        for tracking_id, tracking_packets in grouped_by_tracking.items():
            assert tracking_id.startswith("trk:camera:test:")
            assert tracking_packets[0].lifecycle == Lifecycle.OPEN
            assert any(item.lifecycle == Lifecycle.CLOSE for item in tracking_packets), tracking_id
            stream_ids = {item.stream_id for item in tracking_packets}
            assert len(stream_ids) == 1
            correlation_ids = {str(item.payload.get("correlation_id") or "") for item in tracking_packets}
            assert len(correlation_ids) == 1
            assert all(item.payload.get("event_id") == tracking_id for item in tracking_packets)

        source_frames = int(counters.get("source_frames", 0))
        detect_calls = int(counters.get("detect_calls", 0))
        assert 0 < detect_calls <= source_frames

    asyncio.run(scenario())


def test_vision_track_matches_fast_non_overlapping_boxes() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = _build_detection_sequence(
            [
                [("person", 0.52, (0.70, 0.47, 0.73, 0.61))],
                [("person", 0.55, (0.74, 0.49, 0.77, 0.62))],
                [("person", 0.51, (0.78, 0.51, 0.81, 0.64))],
                [("person", 0.50, (0.82, 0.53, 0.85, 0.66))],
                [],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph = _tracking_pipeline_graph(
            source_id="source",
            detect_id="detect",
            track_id="track",
            sink_id="sink",
            sink_name="tracking_sink",
        )
        pipeline = Pipeline(name="stage5_tracking_non_overlap", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.35)

        packets = [record["packet"] for record in counters.get("packets", [])]
        tracking_ids = {str(packet.payload.get("tracking_id") or "") for packet in packets}
        tracking_ids.discard("")
        assert len(tracking_ids) == 1

    asyncio.run(scenario())


def test_vision_track_keeps_same_identity_across_a_short_gap() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = _build_detection_sequence(
            [
                [("person", 0.98, (0.10, 0.10, 0.20, 0.40))],
                [],
                [("person", 0.95, (0.12, 0.11, 0.22, 0.42))],
                [],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph = _tracking_pipeline_graph(
            source_id="source",
            detect_id="detect",
            track_id="track",
            sink_id="sink",
            sink_name="tracking_sink",
        )
        pipeline = Pipeline(
            name="stage5_tracking_gap",
            type="final",
            graph={
                **graph,
                "nodes": [
                    *graph["nodes"][:-2],
                    {
                        "id": "track",
                        "operator": "vision.track",
                        "config": {
                            "tracker_id": "simple_iou_kalman",
                            "default_interval_seconds": 0.0,
                            "close_after_seconds": 0.08,
                            "emit_mode": "events",
                        },
                    },
                    graph["nodes"][-1],
                ],
            },
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.35)

        packets = [record["packet"] for record in counters.get("packets", [])]
        tracking_ids = [str(packet.payload.get("tracking_id") or "") for packet in packets if packet.payload.get("tracking_id")]
        assert tracking_ids
        assert len(set(tracking_ids)) == 1

    asyncio.run(scenario())


def test_vision_track_annotate_mode_passes_through_frames_with_tracks() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = _build_detection_sequence(
            [
                [("person", 0.9, (0.1, 0.2, 0.3, 0.4))],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.frame_source",
                    "config": {"stream_id": "camera:test", "max_frames": 2, "interval_ms": 15},
                },
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
                },
                {
                    "id": "track",
                    "operator": "vision.track",
                    "config": {"tracker_id": "simple_iou_kalman", "emit_mode": "annotate"},
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "tracking_sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
                {"from": {"node": "detect", "port": "out"}, "to": {"node": "track", "port": "in"}},
                {"from": {"node": "track", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="stage5_tracking_annotate", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.25)

        packets = [record["packet"] for record in counters.get("packets", [])]
        assert packets
        out = packets[0]
        assert out.stream_id == "camera:test"
        assert out.payload.get("event_id") is None
        assert out.payload.get("tracking_id") is None
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_confidence") == 0.9
        assert out.payload.get("object_bbox01") == pytest.approx([0.1, 0.2, 0.3, 0.4])
        assert out.payload.get("vision", {}).get("task") == "tracking"
        tracks = out.payload.get("vision", {}).get("tracks")
        assert isinstance(tracks, list)
        assert len(tracks) == 1
        assert tracks[0].get("tracker_id") == "simple_iou_kalman"
        assert str(tracks[0].get("tracking_id") or "").startswith("trk:camera:test:")

    asyncio.run(scenario())


def test_bundle_runtime_shares_single_detect_and_track_across_two_final_pipelines() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=True)

        sequence = _build_detection_sequence(
            [
                [
                    ("person", 0.95, (0.1, 0.1, 0.2, 0.4)),
                    ("person", 0.90, (0.5, 0.2, 0.7, 0.6)),
                ],
                [("person", 0.90, (0.11, 0.1, 0.22, 0.4))],
                [],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph_one = _tracking_pipeline_graph(
            source_id="source_a",
            detect_id="detect_a",
            track_id="track_a",
            sink_id="sink_a",
            sink_name="sink_a",
        )
        graph_two = _tracking_pipeline_graph(
            source_id="source_b",
            detect_id="detect_b",
            track_id="track_b",
            sink_id="sink_b",
            sink_name="sink_b",
        )
        report = PipelineGraphCompiler(registry).compile_many(
            [
                Pipeline(name="final_a", type="final", graph=graph_one),
                Pipeline(name="final_b", type="final", graph=graph_two),
            ],
        )
        bundle_runtime = PipelineBundleRuntime(report=report, registry=registry, dependencies=dependencies)
        snapshot = await bundle_runtime.run_for(0.35)

        detect_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "vision.detect"
        )
        track_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "vision.track"
        )
        assert detect_node_count == 1
        assert track_node_count == 1
        source_frames = int(counters.get("source_frames", 0))
        detect_calls = int(counters.get("detect_calls", 0))
        assert 0 < detect_calls <= source_frames
        sink_counts = counters.get("sink_counts", {})
        assert int(sink_counts.get("sink_a", 0)) > 0
        assert int(sink_counts.get("sink_b", 0)) > 0

        runtime_snapshot = snapshot["runtime"]
        for channel in runtime_snapshot["channels"].values():
            assert int(channel["max_depth_seen"]) <= int(channel["maxsize"])

    asyncio.run(scenario())


def test_bundle_runtime_shares_detect_and_track_even_when_downstream_channel_policies_differ() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=True)

        sequence = _build_detection_sequence(
            [
                [
                    ("person", 0.95, (0.1, 0.1, 0.2, 0.4)),
                    ("person", 0.90, (0.5, 0.2, 0.7, 0.6)),
                ],
                [("person", 0.90, (0.11, 0.1, 0.22, 0.4))],
                [],
                [],
            ]
        )
        dependencies = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(sequence, counters),
            vision_model_registry=_build_registry(),
        )

        graph_one = _tracking_pipeline_graph_with_shareable_transform(
            source_id="source_a",
            detect_id="detect_a",
            track_id="track_a",
            transform_id="identity_a",
            sink_id="sink_a",
            sink_name="sink_a",
            track_to_transform_maxsize=16,
        )
        graph_two = _tracking_pipeline_graph_with_shareable_transform(
            source_id="source_b",
            detect_id="detect_b",
            track_id="track_b",
            transform_id="identity_b",
            sink_id="sink_b",
            sink_name="sink_b",
            track_to_transform_maxsize=64,
        )
        report = PipelineGraphCompiler(registry).compile_many(
            [
                Pipeline(name="final_a", type="final", graph=graph_one),
                Pipeline(name="final_b", type="final", graph=graph_two),
            ],
        )
        bundle_runtime = PipelineBundleRuntime(report=report, registry=registry, dependencies=dependencies)
        snapshot = await bundle_runtime.run_for(0.35)

        detect_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "vision.detect"
        )
        track_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "vision.track"
        )
        assert detect_node_count == 1
        assert track_node_count == 1

        identity_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "test.identity"
        )
        assert identity_node_count == 2

        source_frames = int(counters.get("source_frames", 0))
        detect_calls = int(counters.get("detect_calls", 0))
        assert 0 < detect_calls <= source_frames
        sink_counts = counters.get("sink_counts", {})
        assert int(sink_counts.get("sink_a", 0)) > 0
        assert int(sink_counts.get("sink_b", 0)) > 0

        runtime_snapshot = snapshot["runtime"]
        for channel in runtime_snapshot["channels"].values():
            assert int(channel["max_depth_seen"]) <= int(channel["maxsize"])

    asyncio.run(scenario())
