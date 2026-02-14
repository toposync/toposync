from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineBundleRuntime,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync_ext_cameras.pipelines import YoloObject, register_camera_pipeline_operators


class _FrameSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"
    max_frames: int = Field(default=8, ge=1, le=1000)
    interval_ms: int = Field(default=20, ge=1, le=1000)


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


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
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"frame": {"seq": self._sequence}, "frame_index": self._sequence},
            metadata={"source": "test.frame_source"},
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


class _SequenceYoloBackend:
    def __init__(self, sequence: list[list[YoloObject]], counters: dict[str, Any]) -> None:
        self._sequence = sequence
        self._counters = counters
        self._index_track = 0
        self._index_detect = 0

    def track_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        self._counters["track_calls"] = int(self._counters.get("track_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index_track, len(self._sequence) - 1)
        self._index_track += 1
        objects = self._sequence[idx]
        return _filter_categories(objects, categories)

    def detect_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        self._counters["detect_calls"] = int(self._counters.get("detect_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index_detect, len(self._sequence) - 1)
        self._index_detect += 1
        objects = self._sequence[idx]
        return _filter_categories(objects, categories)


def _register_test_source_and_sink(registry: OperatorRegistry, counters: dict[str, Any], *, source_shareable: bool) -> None:
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
        operator_id="test.collect_sink",
        config_model=_CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=_CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, counters),
    )


def _tracking_pipeline_graph(*, source_id: str, yolo_id: str, sink_id: str, sink_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": source_id,
                "operator": "test.frame_source",
                "config": {"stream_id": "camera:test", "max_frames": 10, "interval_ms": 15},
            },
            {
                "id": yolo_id,
                "operator": "vision.object_tracking_yolo",
                "config": {
                    "categories": ["person"],
                    "default_interval_seconds": 0.0,
                    "close_after_seconds": 0.05,
                    "emit_open_on_first": True,
                    "emit_close_on_lost": True,
                },
            },
            {"id": sink_id, "operator": "test.collect_sink", "config": {"sink_name": sink_name}},
        ],
        "edges": [
            {"from": {"node": source_id, "port": "out"}, "to": {"node": yolo_id, "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
            {"from": {"node": yolo_id, "port": "out"}, "to": {"node": sink_id, "port": "in"}, "maxsize": 64, "drop_policy": "drop_oldest"},
        ],
    }


def _build_backend_factory(sequence: list[list[YoloObject]], counters: dict[str, Any]):
    def _factory(config: Any) -> _SequenceYoloBackend:  # noqa: ANN401
        _ = config
        return _SequenceYoloBackend(sequence=sequence, counters=counters)

    return _factory


def test_object_tracking_yolo_splits_two_objects_and_closes_lifecycle() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = [
            [
                YoloObject(tracking_id="17", category="person", confidence=0.98, bbox01=(0.1, 0.1, 0.2, 0.4)),
                YoloObject(tracking_id="42", category="person", confidence=0.95, bbox01=(0.5, 0.2, 0.7, 0.6)),
            ],
            [
                YoloObject(tracking_id="17", category="person", confidence=0.96, bbox01=(0.11, 0.1, 0.22, 0.4)),
                YoloObject(tracking_id="42", category="person", confidence=0.92, bbox01=(0.51, 0.2, 0.71, 0.61)),
            ],
            [
                YoloObject(tracking_id="17", category="person", confidence=0.91, bbox01=(0.12, 0.1, 0.23, 0.41)),
            ],
            [
                YoloObject(tracking_id="17", category="person", confidence=0.89, bbox01=(0.13, 0.1, 0.24, 0.42)),
            ],
            [],
            [],
            [],
        ]
        dependencies = PipelineRuntimeDependencies(
            yolo_backend_factory=_build_backend_factory(sequence, counters),
        )

        graph = _tracking_pipeline_graph(source_id="source", yolo_id="yolo", sink_id="sink", sink_name="tracking_sink")
        pipeline = Pipeline(name="stage5_tracking_split", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.45)

        packets = [record["packet"] for record in counters.get("packets", [])]
        assert packets
        grouped_by_tracking: dict[str, list[Packet]] = defaultdict(list)
        for packet in packets:
            tracking_id = str(packet.payload.get("tracking_id") or "")
            grouped_by_tracking[tracking_id].append(packet)

        assert "17" in grouped_by_tracking
        assert "42" in grouped_by_tracking
        for tracking_id, tracking_packets in grouped_by_tracking.items():
            assert tracking_packets[0].lifecycle == Lifecycle.OPEN
            assert any(item.lifecycle == Lifecycle.CLOSE for item in tracking_packets), tracking_id
            stream_ids = {item.stream_id for item in tracking_packets}
            assert len(stream_ids) == 1
            correlation_ids = {str(item.payload.get("correlation_id") or "") for item in tracking_packets}
            assert len(correlation_ids) == 1

        source_frames = int(counters.get("source_frames", 0))
        track_calls = int(counters.get("track_calls", 0))
        assert 0 <= (source_frames - track_calls) <= 1

    asyncio.run(scenario())


def test_object_detection_yolo_emits_open_close_pairs_with_category_throttle() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=False)

        sequence = [
            [
                YoloObject(tracking_id=None, category="person", confidence=0.9, bbox01=(0.1, 0.1, 0.3, 0.5)),
            ],
            [
                YoloObject(tracking_id=None, category="person", confidence=0.8, bbox01=(0.11, 0.1, 0.31, 0.5)),
            ],
            [
                YoloObject(tracking_id=None, category="cat", confidence=0.95, bbox01=(0.4, 0.3, 0.6, 0.7)),
            ],
        ]
        dependencies = PipelineRuntimeDependencies(
            yolo_backend_factory=_build_backend_factory(sequence, counters),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.frame_source",
                    "config": {"stream_id": "camera:test", "max_frames": 5, "interval_ms": 15},
                },
                {
                    "id": "detector",
                    "operator": "vision.object_detection_yolo",
                    "config": {
                        "categories": ["person"],
                        "default_interval_seconds": 0.05,
                        "emit_open_and_close": True,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "detection_sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detector", "port": "in"}},
                {
                    "from": {"node": "detector", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 64,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="stage5_detection", type="final", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=dependencies)
        await runtime.run_for(0.25)

        packets = [record["packet"] for record in counters.get("packets", [])]
        assert packets
        stream_groups: dict[str, list[Packet]] = defaultdict(list)
        for packet in packets:
            stream_groups[packet.stream_id].append(packet)
        for items in stream_groups.values():
            lifecycles = [packet.lifecycle for packet in items]
            assert lifecycles == [Lifecycle.OPEN, Lifecycle.CLOSE]
            categories = {str(packet.payload.get("object_category_label") or "") for packet in items}
            assert categories == {"person"}

        source_frames = int(counters.get("source_frames", 0))
        detect_calls = int(counters.get("detect_calls", 0))
        assert 0 <= (source_frames - detect_calls) <= 1

    asyncio.run(scenario())


def test_bundle_runtime_shares_single_yolo_across_two_final_pipelines() -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        _register_test_source_and_sink(registry, counters, source_shareable=True)

        sequence = [
            [
                YoloObject(tracking_id="17", category="person", confidence=0.95, bbox01=(0.1, 0.1, 0.2, 0.4)),
                YoloObject(tracking_id="42", category="person", confidence=0.90, bbox01=(0.5, 0.2, 0.7, 0.6)),
            ],
            [
                YoloObject(tracking_id="17", category="person", confidence=0.90, bbox01=(0.11, 0.1, 0.22, 0.4)),
            ],
            [],
            [],
        ]
        dependencies = PipelineRuntimeDependencies(
            yolo_backend_factory=_build_backend_factory(sequence, counters),
        )

        graph_one = _tracking_pipeline_graph(source_id="source_a", yolo_id="yolo_a", sink_id="sink_a", sink_name="sink_a")
        graph_two = _tracking_pipeline_graph(source_id="source_b", yolo_id="yolo_b", sink_id="sink_b", sink_name="sink_b")
        report = PipelineGraphCompiler(registry).compile_many(
            [
                Pipeline(name="final_a", type="final", graph=graph_one),
                Pipeline(name="final_b", type="final", graph=graph_two),
            ],
        )
        bundle_runtime = PipelineBundleRuntime(report=report, registry=registry, dependencies=dependencies)
        snapshot = await bundle_runtime.run_for(0.35)

        yolo_node_count = sum(
            1
            for node in bundle_runtime.plan.merged_pipeline.nodes
            if node.operator_id == "vision.object_tracking_yolo"
        )
        assert yolo_node_count == 1
        source_frames = int(counters.get("source_frames", 0))
        track_calls = int(counters.get("track_calls", 0))
        assert 0 <= (source_frames - track_calls) <= 1
        sink_counts = counters.get("sink_counts", {})
        assert int(sink_counts.get("sink_a", 0)) > 0
        assert int(sink_counts.get("sink_b", 0)) > 0

        runtime_snapshot = snapshot["runtime"]
        for channel in runtime_snapshot["channels"].values():
            assert int(channel["max_depth_seen"]) <= int(channel["maxsize"])

    asyncio.run(scenario())


def _filter_categories(objects: list[YoloObject], categories: set[str] | None) -> list[YoloObject]:
    if not categories:
        return list(objects)
    normalized = {str(item or "").strip().lower() for item in categories}
    return [obj for obj in objects if obj.category in normalized]
