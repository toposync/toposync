from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import numpy as np
import pytest
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
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_cameras.processing.mapping import ControlPointMapper, ControlPointPair
from toposync_ext_cameras.pipelines.postprocess import (
    CameraMappingRuntime,
    VelocityEstimationRuntime,
)


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


def test_camera_mapping_annotates_detection_world_anchors_before_tracking() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "control_point_sets": [
                    {
                        "id": "main",
                        "label": "Main",
                        "control_points": [
                            {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                            {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                        ],
                    }
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "object_bbox01": [0.10, 0.10, 0.30, 0.50],
                "detected_object": {
                    "label": "person",
                    "score": 0.9,
                    "bbox01": [0.10, 0.10, 0.30, 0.50],
                },
                "detected_objects": [
                    {
                        "label": "person",
                        "score": 0.9,
                        "bbox01": [0.10, 0.10, 0.30, 0.50],
                    },
                    {
                        "label": "person",
                        "score": 0.8,
                        "bbox01": [0.60, 0.60, 0.80, 0.80],
                    },
                ],
                "vision": {
                    "task": "detection",
                    "detections": [
                        {
                            "label": "person",
                            "label_id": 0,
                            "score": 0.9,
                            "bbox01": [0.10, 0.10, 0.30, 0.50],
                            "model_id": "fake.detector",
                        },
                        {
                            "label": "person",
                            "label_id": 0,
                            "score": 0.8,
                            "bbox01": [0.60, 0.60, 0.80, 0.80],
                            "model_id": "fake.detector",
                        },
                    ],
                },
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload
        detections = payload["vision"]["detections"]

        assert payload["world"] == pytest.approx({"x": 2.0, "z": 5.0}, abs=1e-6)
        assert payload["world_anchor"]["confidence"] > 0.0
        assert detections[0]["world_anchor"]["x"] == pytest.approx(2.0, abs=1e-6)
        assert detections[0]["world_anchor"]["z"] == pytest.approx(5.0, abs=1e-6)
        assert detections[1]["world_anchor"]["x"] == pytest.approx(7.0, abs=1e-6)
        assert detections[1]["world_anchor"]["z"] == pytest.approx(8.0, abs=1e-6)
        assert payload["detected_objects"][1]["world_anchor"]["x"] == pytest.approx(7.0, abs=1e-6)

    asyncio.run(scenario())


def test_camera_mapping_runtime_applies_calibrated_view_refinement_to_world_payload() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    {
                        "id": "main",
                        "label": "Main",
                        "projection_model": {
                            "type": "image_quad_on_world",
                            "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
                            "world_quad": {
                                "top_left": {"x": 0.0, "z": 0.0},
                                "top_right": {"x": 10.0, "z": 0.0},
                                "bottom_right": {"x": 10.0, "z": 10.0},
                                "bottom_left": {"x": 0.0, "z": 10.0},
                            },
                            "refinement": {
                                "model": "local_rbf_v1",
                                "points": [
                                    {
                                        "id": "center",
                                        "image": {"x": 0.5, "y": 0.5},
                                        "world": {"x": 7.0, "z": 3.0},
                                    }
                                ],
                            },
                        },
                    }
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "object_bbox01": [0.40, 0.10, 0.60, 0.50],
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert payload["world"]["x"] == pytest.approx(7.0, abs=1e-6)
        assert payload["world"]["z"] == pytest.approx(3.0, abs=1e-6)
        assert payload["mapping"]["quality"]["number_of_points"] == 4

    asyncio.run(scenario())


def _pipeline_runtime(
    *,
    graph: dict[str, Any],
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
    dependencies: PipelineRuntimeDependencies | None = None,
) -> PipelineRuntime:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    _register_test_source_and_sink(registry, sequence, collector)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="stage6_postprocess_test", graph=graph),
    )
    return PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=dependencies or PipelineRuntimeDependencies(),
    )


def _frame_artifacts(frame: Any) -> dict[str, Artifact]:
    return {
        "main": Artifact(name="main", data=frame, mime_type="image/raw"),
    }


def _main_frame_artifacts(frame: Any) -> dict[str, Artifact]:
    return {
        "main": Artifact(name="main", data=frame, mime_type="image/raw"),
    }


def test_object_crop_reprojects_bbox_for_cropped_stream_frame() -> None:
    async def scenario() -> None:
        main = np.zeros((100, 100, 3), dtype=np.uint8)
        main[30:50, 30:50] = 123

        # Simulates a stream crop of the center area [0.25..0.75] applied as the stream frame.
        stream_frame = main[25:75, 25:75].copy()

        sequence: list[dict[str, Any]] = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-1",
                    # Use exact binary fractions to avoid borderline rounding in int/ceil conversions.
                    "object_bbox01": [0.3125, 0.3125, 0.50, 0.50],
                    "frame_crop": {
                        "bbox01": [0.25, 0.25, 0.75, 0.75],
                        "output_artifact_name": "main",
                    },
                },
                "artifacts": _main_frame_artifacts(stream_frame),
            },
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
                    "id": "crop",
                    "operator": "vision.crop_objects",
                    "config": {
                        "output_artifact_name": "main",
                        "bbox_field": "object_bbox01",
                        "padding_ratio": 0.0,
                        "min_crop_size_px": 1,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "crop", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "crop", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        main = packet.artifacts["main"].data
        assert main is not None

        # The crop metadata is applied only because it explicitly targets the selected main artifact.
        assert tuple(main.shape[:2]) == (19, 19)
        assert int(main[0, 0, 0]) == 123

    asyncio.run(scenario())


def test_object_crop_reprojects_bbox_for_perspective_warped_stream_frame() -> None:
    async def scenario() -> None:
        try:
            import cv2  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("opencv-python-headless is required for this test") from exc

        main = np.zeros((100, 100, 3), dtype=np.uint8)
        main[30:50, 30:50] = 123

        src = np.asarray([[25, 25], [75, 25], [75, 75], [25, 75]], dtype=np.float32)
        dst_w = 51
        dst_h = 51
        dst = np.asarray([[0, 0], [50, 0], [50, 50], [0, 50]], dtype=np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        stream_frame = cv2.warpPerspective(main, H, (dst_w, dst_h))

        sequence: list[dict[str, Any]] = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-1",
                    "object_bbox01": [0.30, 0.30, 0.50, 0.50],
                    "frame_warp": {
                        "kind": "perspective",
                        "source_frame_width": 100,
                        "source_frame_height": 100,
                        "dest_frame_width": dst_w,
                        "dest_frame_height": dst_h,
                        "homography": [[float(v) for v in row] for row in H.tolist()],
                        "output_artifact_name": "main",
                    },
                },
                "artifacts": _main_frame_artifacts(stream_frame),
            },
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
                    "id": "crop",
                    "operator": "vision.crop_objects",
                    "config": {
                        "output_artifact_name": "main",
                        "bbox_field": "object_bbox01",
                        "padding_ratio": 0.0,
                        "min_crop_size_px": 1,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "crop", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "crop", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        main = packet.artifacts["main"].data
        assert main is not None

        meta = packet.artifacts["main"].metadata
        assert isinstance(meta, dict)
        assert "reproject:frame_warp" in str(meta.get("bbox_source", ""))

        assert int(main.max()) == 123

    asyncio.run(scenario())


def test_image_resize_downscales_selected_artifacts_in_place() -> None:
    async def scenario() -> None:
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-resize",
                },
                "artifacts": _frame_artifacts(frame),
            },
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
                    "id": "resize",
                    "operator": "camera.image_resize",
                    "config": {
                        "max_edge_px": 50,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "resize", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "resize", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        assert "main" in packet.artifacts
        image = packet.artifacts["main"].data
        assert image is not None
        assert tuple(image.shape[:2]) == (25, 50)
        meta = packet.artifacts["main"].metadata
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
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 1.0,
                    "object_bbox01": [0.48, 0.48, 0.52, 0.52],
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 2.0,
                    "object_bbox01": [0.70, 0.48, 0.74, 0.52],
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 3.0,
                    "object_bbox01": [0.70, 0.48, 0.74, 0.52],
                },
                "artifacts": _frame_artifacts(frame),
            },
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
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "area", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "area", "port": "out"},
                    "to": {"node": "velocity", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "velocity", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
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
        assert round(float(world.get("z")), 1) == 5.2
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "main"
        quality = mapping.get("quality")
        assert isinstance(quality, dict)
        assert quality.get("number_of_points") == 4

    asyncio.run(scenario())


def test_velocity_stopped_now_drops_close_when_first_valid_world_sample_arrives_on_close() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-close-only",
                    "tracking_id": "velocity-close-only",
                    "frame_ts": 1.0,
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-close-only",
                    "tracking_id": "velocity-close-only",
                    "frame_ts": 9.0,
                    "world": {"x": 2.0, "z": 3.0},
                },
                "artifacts": _frame_artifacts(frame),
            },
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
                    "id": "velocity",
                    "operator": "camera.velocity_estimation",
                    "config": {
                        "filter_mode": "stopped_now",
                        "min_elapsed_seconds": 0.05,
                        "stopped_speed_threshold": 0.07,
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "velocity", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "velocity", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        assert collector.get("sink", []) == []

    asyncio.run(scenario())


def test_mapping_selects_pose_bound_set_when_ptz_state_matches() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                    "pan_tilt_zoom_state": {
                        "pan": 0.12,
                        "tilt": -0.08,
                        "zoom": 0.33,
                        "move_status": "IDLE",
                    },
                },
                "artifacts": _frame_artifacts(frame),
            }
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
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
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
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ]
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packet = collector["sink"][-1]
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 105.0
        assert round(float(world.get("z")), 1) == 105.0
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "door_zoom"
        assert round(float(mapping.get("pose_distance")), 3) == 0.0
        assert mapping.get("move_status") == "idle"

    asyncio.run(scenario())


def test_mapping_fetches_ptz_state_from_service_when_payload_missing() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                },
                "artifacts": _frame_artifacts(frame),
            }
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
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
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
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ]
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        services = ServiceRegistry()
        call_count = {"value": 0}

        async def _get_status(*, camera_id: str) -> dict[str, Any]:
            assert camera_id == "camera-main"
            call_count["value"] += 1
            return {"pan": 0.12, "tilt": -0.08, "zoom": 0.33, "move_status": "IDLE"}

        services.register("cameras.ptz.get_status", _get_status)

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            sequence=sequence,
            collector=collector,
            dependencies=PipelineRuntimeDependencies(services=services),
        )
        await runtime.run_for(0.2)

        assert call_count["value"] == 1
        packet = collector["sink"][-1]
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 105.0
        assert round(float(world.get("z")), 1) == 105.0
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "door_zoom"
        pose_state = packet.payload.get("pan_tilt_zoom_state")
        assert isinstance(pose_state, dict)
        assert pose_state.get("source") == "cameras.ptz.get_status"

    asyncio.run(scenario())


def test_mapping_caches_fetched_ptz_state_between_packets() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {"camera_id": "camera-main", "image_uv": {"u": 0.5, "v": 0.5}},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {"camera_id": "camera-main", "image_uv": {"u": 0.5, "v": 0.5}},
                "artifacts": _frame_artifacts(frame),
            },
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
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "ptz_state_fetch": {"cache_ttl_seconds": 60.0},
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
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
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ],
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        services = ServiceRegistry()
        call_count = {"value": 0}

        async def _get_status(*, camera_id: str) -> dict[str, Any]:
            assert camera_id == "camera-main"
            call_count["value"] += 1
            return {"pan": 0.12, "tilt": -0.08, "zoom": 0.33, "move_status": "IDLE"}

        services.register("cameras.ptz.get_status", _get_status)

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            sequence=sequence,
            collector=collector,
            dependencies=PipelineRuntimeDependencies(services=services),
        )
        await runtime.run_for(0.2)

        assert call_count["value"] == 1
        packets = collector["sink"]
        assert len(packets) == 2
        assert all(isinstance(packet.payload.get("mapping"), dict) for packet in packets)

    asyncio.run(scenario())


def test_mapping_skips_when_ptz_state_reports_moving() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                    "pan_tilt_zoom_state": {
                        "pan": 0.12,
                        "tilt": -0.08,
                        "zoom": 0.33,
                        "move_status": "MOVING",
                    },
                },
                "artifacts": _frame_artifacts(frame),
            }
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
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            }
                        ]
                    },
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"sink_name": "sink"}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packet = collector["sink"][-1]
        assert "world" not in packet.payload
        assert "mapping" not in packet.payload

    asyncio.run(scenario())


def test_control_point_mapper_rejects_single_outlier_with_robust_homography() -> None:
    mapper = ControlPointMapper(
        [
            ControlPointPair(image_u=0.0, image_v=0.0, world_x=0.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.0, world_x=10.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=1.0, world_x=10.0, world_z=10.0),
            ControlPointPair(image_u=0.0, image_v=1.0, world_x=0.0, world_z=10.0),
            ControlPointPair(image_u=0.5, image_v=0.0, world_x=5.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.5, world_x=10.0, world_z=5.0),
            ControlPointPair(image_u=0.5, image_v=1.0, world_x=5.0, world_z=10.0),
            ControlPointPair(image_u=0.0, image_v=0.5, world_x=0.0, world_z=5.0),
            ControlPointPair(image_u=0.25, image_v=0.75, world_x=2.5, world_z=7.5),
            ControlPointPair(image_u=0.9, image_v=0.1, world_x=42.0, world_z=17.0),
        ]
    )
    mapped = mapper.map(0.5, 0.5)
    assert mapped is not None
    assert mapped[0] == pytest.approx(5.0, abs=0.25)
    assert mapped[1] == pytest.approx(5.0, abs=0.25)
    assert mapper.quality.number_of_inliers >= 8


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
                    "event_id": "velocity-track",
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


def test_velocity_state_is_namespaced_when_tracking_id_repeats_across_streams() -> None:
    async def scenario() -> None:
        runtime = VelocityEstimationRuntime(
            {
                "stopped_speed_threshold": 0.2,
                "filter_mode": "annotate",
            },
        )

        def make_packet(*, stream_id: str, frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id=stream_id,
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "event_id": "1",
                    "tracking_id": "1",
                    "frame_ts": frame_ts,
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        # If state was keyed only by tracking_id ("1"), packet2 would see a large speed from packet1.
        packet1 = make_packet(stream_id="cam:one", frame_ts=1.0, world_x=0.0)
        packet2 = make_packet(stream_id="cam:two", frame_ts=2.0, world_x=10.0)

        out1 = (await runtime.process_packet(packet1, context=None))[0]
        out2 = (await runtime.process_packet(packet2, context=None))[0]

        v1 = out1.payload.get("velocity")
        v2 = out2.payload.get("velocity")
        assert isinstance(v1, dict)
        assert isinstance(v2, dict)
        assert v1.get("valid") is False
        assert v2.get("valid") is False

    asyncio.run(scenario())
