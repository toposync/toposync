from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Artifact,
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
from toposync.runtime.pipelines.stats import PipelineStatsStore
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
        frame = {"seq": self._sequence}
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"frame_index": self._sequence},
            artifacts={
                "main": Artifact(
                    name="main", data=frame, mime_type="application/json"
                ),
                "aux": Artifact(
                    name="aux",
                    data=frame,
                    mime_type="application/json",
                    metadata={"derived_from": "main"},
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
        sink_counters = self._counters.setdefault("sink_counts", {})
        sink_counters[self._sink_name] = int(sink_counters.get(self._sink_name, 0)) + 1
        return []


class _SequenceDetectorBackend:
    backend_id = "fake_detector"

    def __init__(self, sequence: list[list[DetectionObject]], counters: dict[str, Any]) -> None:
        self._sequence = sequence
        self._counters = counters
        self._index = 0

    def detect(self, frame: Any, *, categories: set[str] | None = None) -> list[DetectionObject]:  # noqa: ARG002
        self._counters["detect_calls"] = int(self._counters.get("detect_calls", 0)) + 1
        if not self._sequence:
            return []
        idx = min(self._index, len(self._sequence) - 1)
        self._index += 1
        objects = self._sequence[idx]
        if not categories:
            return list(objects)
        return [item for item in objects if item.label in categories]


def _build_detection_sequence(
    sequence: list[list[tuple[str, float, tuple[float, float, float, float]]]],
) -> list[list[DetectionObject]]:
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


def _tracking_pipeline_graph(
    *, source_id: str, detect_id: str, track_id: str, sink_id: str, sink_name: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": source_id,
                "operator": "test.frame_source",
                "config": {"stream_id": "camera:test", "max_frames": 10, "interval_ms": 15},
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
            {
                "from": {"node": source_id, "port": "out"},
                "to": {"node": detect_id, "port": "in"},
                "maxsize": 1,
                "drop_policy": "latest_only",
            },
            {
                "from": {"node": detect_id, "port": "out"},
                "to": {"node": track_id, "port": "in"},
                "maxsize": 1,
                "drop_policy": "latest_only",
            },
            {
                "from": {"node": track_id, "port": "out"},
                "to": {"node": sink_id, "port": "in"},
                "maxsize": 64,
                "drop_policy": "drop_oldest",
            },
        ],
    }


def test_orchestrator_runs_local_bundle_and_shares_detect_and_track(tmp_path: Path) -> None:
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

        detection_sequence = _build_detection_sequence(
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
        stats_store = PipelineStatsStore(window_seconds=24 * 60 * 60, bucket_seconds=60)
        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(
                detection_sequence, counters
            ),
            vision_model_registry=_build_registry(),
            pipeline_stats_store=stats_store,
        )

        paths = UserDataPaths(
            data_dir=tmp_path / "data",
            config_path=tmp_path / "data" / "config.json",
            files_dir=tmp_path / "data" / "files",
        )
        store = ConfigStore(paths=paths)

        graph_a = _tracking_pipeline_graph(
            source_id="source_a",
            detect_id="detect_a",
            track_id="track_a",
            sink_id="sink_a",
            sink_name="sink_a",
        )
        graph_b = _tracking_pipeline_graph(
            source_id="source_b",
            detect_id="detect_b",
            track_id="track_b",
            sink_id="sink_b",
            sink_name="sink_b",
        )
        await store.create_pipeline(Pipeline(name="final_a", graph=graph_a))
        await store.create_pipeline(Pipeline(name="final_b", graph=graph_b))

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
        pipeline_status = {
            item["name"]: item for item in status.get("pipelines", []) if isinstance(item, dict)
        }
        assert pipeline_status.get("final_a", {}).get("mode") == "bundle"
        assert pipeline_status.get("final_b", {}).get("mode") == "bundle"

        source_frames = int(counters.get("source_frames", 0))
        detect_calls = int(counters.get("detect_calls", 0))
        assert source_frames <= 12
        assert detect_calls <= 12

        sink_counts = counters.get("sink_counts", {})
        assert int(sink_counts.get("sink_a", 0)) > 0
        assert int(sink_counts.get("sink_b", 0)) > 0

        stats_a = stats_store.snapshot("final_a")
        stats_b = stats_store.snapshot("final_b")
        assert int(stats_a["node_outputs"]["source_a"]) == int(counters.get("source_frames", 0))
        assert int(stats_b["node_outputs"]["source_b"]) == int(counters.get("source_frames", 0))
        assert int(stats_a["node_outputs"]["sink_a"]) == int(sink_counts.get("sink_a", 0))
        assert int(stats_b["node_outputs"]["sink_b"]) == int(sink_counts.get("sink_b", 0))

        runtime_snapshot = status["local_bundle"]["runtime"]
        for channel in runtime_snapshot["channels"].values():
            assert int(channel["max_depth_seen"]) <= int(channel["maxsize"])

        await orchestrator.stop()

    asyncio.run(scenario())
