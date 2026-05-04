from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
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


class _SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"
    interval_ms: int = Field(default=15, ge=1, le=1000)


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[dict[str, Any]]) -> None:
        parsed = _SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._interval_s = float(parsed.interval_ms) / 1000.0
        self._next_tick = 0.0
        self._sequence = list(sequence)
        self._index = 0

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._index >= len(self._sequence):
            return None
        if self._next_tick:
            await context.sleep(self._interval_s)
        self._next_tick += 1.0
        item = self._sequence[self._index]
        self._index += 1
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=item.get("lifecycle", Lifecycle.UPDATE),
            payload=dict(item.get("payload") or {}),
            artifacts=dict(item.get("artifacts") or {}),
            metadata={"motion_gate_open": True},
        )


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: dict[str, list[Packet]]) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.setdefault(self._sink_name, []).append(packet)
        return []


class _SequenceDetectorBackend:
    backend_id = "fake_detector"

    def __init__(self, sequence: list[list[DetectionObject]]) -> None:
        self._sequence = sequence
        self._index = 0

    def detect(self, frame: Any, *, categories: set[str] | None = None) -> list[DetectionObject]:  # noqa: ARG002
        if not self._sequence:
            return []
        idx = min(self._index, len(self._sequence) - 1)
        self._index += 1
        objects = self._sequence[idx]
        if not categories:
            return list(objects)
        return [item for item in objects if item.label in categories]


def _frame_artifacts(frame: np.ndarray) -> dict[str, Artifact]:
    return {
        "main": Artifact(name="main", data=frame, mime_type="image/raw"),
        "aux": Artifact(
            name="aux",
            data=frame,
            mime_type="image/raw",
            metadata={"derived_from": "main"},
        ),
    }


def _pipeline_runtime(
    *,
    graph: dict[str, Any],
    source_sequence: list[dict[str, Any]],
    detection_sequence: list[list[DetectionObject]],
    collector: dict[str, list[Packet]],
    tracker_backend_factory=None,
) -> PipelineRuntime:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    registry.register_operator(
        operator_id="test.sequence_source",
        config_model=_SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=_SequenceSourceConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _SequenceSourceRuntime(config, source_sequence),
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
    pipeline = Pipeline(name="vision_track_integration", graph=graph)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    return PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _SequenceDetectorBackend(detection_sequence),
            tracker_backend_factory=tracker_backend_factory,
            vision_model_registry=_build_registry(),
        ),
    )


def test_velocity_estimation_continues_working_after_vision_track_decoupling() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        source_sequence = [
            {
                "payload": {"camera_id": "camera-main", "frame_ts": 0.00},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "payload": {"camera_id": "camera-main", "frame_ts": 0.05},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "payload": {"camera_id": "camera-main", "frame_ts": 0.10},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "payload": {"camera_id": "camera-main", "frame_ts": 0.15},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "payload": {"camera_id": "camera-main", "frame_ts": 0.20},
                "artifacts": _frame_artifacts(frame),
            },
        ]
        detection_sequence = [
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.48, 0.48, 0.52, 0.52),
                    model_id="fake.detector",
                )
            ],
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.70, 0.48, 0.74, 0.52),
                    model_id="fake.detector",
                )
            ],
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.70, 0.48, 0.74, 0.52),
                    model_id="fake.detector",
                )
            ],
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.70, 0.48, 0.74, 0.52),
                    model_id="fake.detector",
                )
            ],
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.70, 0.48, 0.74, 0.52),
                    model_id="fake.detector",
                )
            ],
        ]
        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
                },
                {
                    "id": "track",
                    "operator": "vision.track",
                    "config": {
                        "tracker_id": "simple_iou_kalman",
                        "emit_mode": "events",
                        "default_interval_seconds": 0.0,
                        "close_after_seconds": 0.2,
                    },
                },
                {
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "bbox_field": "object_bbox01",
                        "control_point_sets": [
                            {
                                "id": "main",
                                "label": "Main",
                                "pose_reference": None,
                                "control_points": [
                                    {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                    {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 10.0, "z": 10.0},
                                    },
                                    {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                ],
                            }
                        ],
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
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
                {"from": {"node": "detect", "port": "out"}, "to": {"node": "track", "port": "in"}},
                {"from": {"node": "track", "port": "out"}, "to": {"node": "mapping", "port": "in"}},
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "velocity", "port": "in"},
                },
                {"from": {"node": "velocity", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            source_sequence=source_sequence,
            detection_sequence=detection_sequence,
            collector=collector,
        )
        await runtime.run_for(0.35)

        packets = collector.get("sink", [])
        assert len(packets) >= 3
        packet = packets[-1]
        velocity = packet.payload.get("velocity")
        assert isinstance(velocity, dict)
        assert velocity.get("ever_stopped") is True
        assert velocity.get("moving") is False
        assert isinstance(packet.payload.get("world"), dict)

    asyncio.run(scenario())


def test_vision_track_annotate_mode_fills_future_multicamera_fields_from_packet() -> None:
    class _FutureTrackerBackend:
        tracker_id = "future_tracker"

        def reset_stream(self, stream_key: str) -> None:  # noqa: ARG002
            return None

        def update(  # noqa: ANN001
            self,
            stream_key: str,
            frame,
            detections: list[DetectionObject],
            *,
            frame_ts: float | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> list[dict[str, Any]]:
            _ = stream_key, frame, frame_ts, metadata
            return [
                {
                    "tracking_id": "trk:future:1",
                    "source_tracking_id": "1",
                    "label": detections[0].label,
                    "label_id": detections[0].label_id,
                    "score": detections[0].score,
                    "bbox01": detections[0].bbox01,
                    "model_id": detections[0].model_id,
                    "tracker_id": "future_tracker",
                }
            ]

    async def scenario() -> None:
        frame = np.zeros((24, 24, 3), dtype=np.uint8)
        source_sequence = [
            {
                "payload": {
                    "camera_id": "camera-main",
                    "frame_ts": 0.00,
                    "world": {"x": 7.0, "z": 9.0},
                },
                "artifacts": {
                    **_frame_artifacts(frame),
                    "appearance_embedding": Artifact(
                        name="appearance_embedding",
                        data=b"emb",
                        mime_type="application/octet-stream",
                    ),
                },
            }
        ]
        detection_sequence = [
            [
                DetectionObject(
                    label="person",
                    label_id=0,
                    score=0.9,
                    bbox01=(0.1, 0.1, 0.3, 0.5),
                    model_id="fake.detector",
                )
            ]
        ]
        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
                },
                {
                    "id": "track",
                    "operator": "vision.track",
                    "config": {
                        "tracker_id": "simple_iou_kalman",
                        "emit_mode": "annotate",
                        "default_interval_seconds": 0.0,
                        "close_after_seconds": 0.2,
                        "use_world_anchor": True,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
                {"from": {"node": "detect", "port": "out"}, "to": {"node": "track", "port": "in"}},
                {"from": {"node": "track", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            source_sequence=source_sequence,
            detection_sequence=detection_sequence,
            collector=collector,
            tracker_backend_factory=lambda config: _FutureTrackerBackend(),
        )
        await runtime.run_for(0.10)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        track = packets[0].payload["vision"]["tracks"][0]
        assert track["camera_id"] == "camera-main"
        assert track["world_anchor"] == {"x": 7.0, "z": 9.0}
        assert track["appearance_embedding_artifact_name"] == "appearance_embedding"
        assert packets[0].payload["camera_id"] == "camera-main"

    asyncio.run(scenario())
