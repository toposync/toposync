from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync_ext_cameras.pipelines import YoloObject, register_camera_pipeline_operators


class _FrameSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"
    max_frames: int = Field(default=10, ge=1, le=1000)
    interval_ms: int = Field(default=15, ge=1, le=1000)


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
        sink_counters = self._counters.setdefault("sink_counts", {})
        sink_counters[self._sink_name] = int(sink_counters.get(self._sink_name, 0)) + 1
        return []


class _SequenceYoloBackend:
    def __init__(self, sequence: list[list[YoloObject]], counters: dict[str, Any]) -> None:
        self._sequence = sequence
        self._counters = counters
        self._index_track = 0

    def track_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        self._counters["track_calls"] = int(self._counters.get("track_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index_track, len(self._sequence) - 1)
        self._index_track += 1
        objects = self._sequence[idx]
        if not categories:
            return list(objects)
        normalized = {str(item or "").strip().lower() for item in categories}
        return [obj for obj in objects if obj.category in normalized]

    def detect_objects(self, frame: Any, *, categories: set[str] | None = None) -> list[YoloObject]:  # noqa: ARG002
        return [
            YoloObject(
                tracking_id=None,
                category=item.category,
                confidence=item.confidence,
                bbox01=item.bbox01,
            )
            for item in self.track_objects(frame, categories=categories)
        ]


def _build_backend_factory(sequence: list[list[YoloObject]], counters: dict[str, Any]):
    def _factory(config: Any) -> _SequenceYoloBackend:  # noqa: ANN401
        _ = config
        return _SequenceYoloBackend(sequence=sequence, counters=counters)

    return _factory


def _tracking_pipeline_graph(*, source_id: str, yolo_id: str, sink_id: str, sink_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": source_id, "operator": "test.frame_source", "config": {"stream_id": "camera:test", "max_frames": 10, "interval_ms": 15}},
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


def test_orchestrator_runs_local_bundle_and_shares_yolo(tmp_path: Path) -> None:
    async def scenario() -> None:
        counters: dict[str, Any] = {}

        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        registry.register_operator(
            operator_id="test.frame_source",
            config_model=_FrameSourceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            defaults=_FrameSourceConfig().model_dump(),
            share_strategy="by_signature",
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

        yolo_sequence = [
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
        deps = PipelineRuntimeDependencies(yolo_backend_factory=_build_backend_factory(yolo_sequence, counters))

        paths = UserDataPaths(
            data_dir=tmp_path / "data",
            config_path=tmp_path / "data" / "config.json",
            files_dir=tmp_path / "data" / "files",
        )
        store = ConfigStore(paths=paths)
        await store.set_pipelines_feature_flag(enabled=True)

        graph_a = _tracking_pipeline_graph(source_id="source_a", yolo_id="yolo_a", sink_id="sink_a", sink_name="sink_a")
        graph_b = _tracking_pipeline_graph(source_id="source_b", yolo_id="yolo_b", sink_id="sink_b", sink_name="sink_b")
        await store.create_pipeline(Pipeline(name="final_a", type="final", graph=graph_a))
        await store.create_pipeline(Pipeline(name="final_b", type="final", graph=graph_b))

        notifications = NotificationsRuntime(data_dir=tmp_path / "data" / "notifications")
        compiler = PipelineGraphCompiler(registry)
        orchestrator = PipelinesOrchestrator(
            config_store=store,
            operator_registry=registry,
            compiler=compiler,
            notifications=notifications,
            files_dir=paths.files_dir,
            poll_interval_s=999.0,
            runtime_dependencies=deps,
        )

        await orchestrator._reconcile()
        await asyncio.sleep(0.45)
        status = orchestrator.status()

        assert status.get("local_bundle") is not None
        pipeline_status = {item["name"]: item for item in status.get("pipelines", []) if isinstance(item, dict)}
        assert pipeline_status.get("final_a", {}).get("mode") == "bundle"
        assert pipeline_status.get("final_b", {}).get("mode") == "bundle"

        source_frames = int(counters.get("source_frames", 0))
        track_calls = int(counters.get("track_calls", 0))
        assert source_frames <= 12
        assert track_calls <= 12

        sink_counts = counters.get("sink_counts", {})
        assert int(sink_counts.get("sink_a", 0)) > 0
        assert int(sink_counts.get("sink_b", 0)) > 0

        runtime_snapshot = status["local_bundle"]["runtime"]
        for channel in runtime_snapshot["channels"].values():
            assert int(channel["max_depth_seen"]) <= int(channel["maxsize"])

        await orchestrator.stop()

    asyncio.run(scenario())

