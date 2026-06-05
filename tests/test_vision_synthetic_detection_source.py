from __future__ import annotations

import asyncio
from typing import Any

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    register_builtin_operators,
)
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_vision.pipelines import VisionSyntheticDetectionSourceRuntime


class _SourceContext:
    async def sleep(self, _seconds: float) -> None:
        return None


def _control_point_sets() -> list[dict[str, Any]]:
    return [
        {
            "id": "image_360_fake_quad",
            "label": "Mapeamento fake da imagem",
            "control_points": [
                {"image": {"x": 0.0, "y": 0.0}, "world": {"x": -4.05, "z": 0.15}},
                {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 4.05, "z": 0.15}},
                {"image": {"x": 1.0, "y": 1.0}, "world": {"x": 4.05, "z": 2.55}},
                {"image": {"x": 0.0, "y": 1.0}, "world": {"x": -4.05, "z": 2.55}},
            ],
        }
    ]


def _person_bbox_for_image_point() -> list[float]:
    center_u = 494 / 1280
    foot_v = 511 / 720
    return [center_u - 0.02, 0.54, center_u + 0.02, foot_v]


def test_synthetic_detection_source_emits_frame_and_detection_payload() -> None:
    async def scenario() -> None:
        runtime = VisionSyntheticDetectionSourceRuntime(
            {
                "stream_id": "camera:image_360_rtsp_camera:main",
                "camera_id": "image_360_rtsp_camera",
                "camera_name": "Camera imagem RTSP 360",
                "source_id": "main",
                "source_name": "Imagem RTSP principal",
                "model_id": "synthetic.person",
                "width": 1280,
                "height": 720,
                "frames": 2,
                "interval_seconds": 0.0,
                "detections": [
                    {
                        "label": " Person ",
                        "label_id": 0,
                        "score": 0.99,
                        "bbox01": _person_bbox_for_image_point(),
                    }
                ],
            }
        )

        first = await runtime.produce(_SourceContext())
        second = await runtime.produce(_SourceContext())
        done = await runtime.produce(_SourceContext())

        assert first is not None
        assert second is not None
        assert done is None
        assert first.lifecycle == Lifecycle.OPEN
        assert second.lifecycle == Lifecycle.UPDATE
        assert first.stream_id == "camera:image_360_rtsp_camera:main"
        assert first.payload["camera_id"] == "image_360_rtsp_camera"
        assert first.payload["media"]["width"] == 1280
        assert first.payload["source"]["source_id"] == "main"
        assert first.artifacts[MAIN_ARTIFACT_NAME].mime_type == "image/raw"
        assert first.artifacts[MAIN_ARTIFACT_NAME].data.shape == (720, 1280, 3)
        detections = first.payload["vision"]["detections"]
        assert detections[0]["label"] == "person"
        assert detections[0]["model_id"] == "synthetic.person"
        assert detections[0]["score"] == 0.99

    asyncio.run(scenario())


def test_synthetic_detection_source_maps_tracks_and_notifies_world_pin(tmp_path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "notifications")
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "vision.synthetic_detection_source",
                    "config": {
                        "stream_id": "camera:image_360_rtsp_camera:main",
                        "camera_id": "image_360_rtsp_camera",
                        "camera_name": "Camera imagem RTSP 360",
                        "source_id": "main",
                        "source_name": "Imagem RTSP principal",
                        "model_id": "synthetic.person",
                        "width": 1280,
                        "height": 720,
                        "frames": 4,
                        "interval_seconds": 0.01,
                        "detections": [
                            {
                                "label": "person",
                                "label_id": 0,
                                "score": 0.99,
                                "bbox01": _person_bbox_for_image_point(),
                            }
                        ],
                    },
                },
                {
                    "id": "map",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "camera_id": "image_360_rtsp_camera",
                        "composition_id": "image_360_onboarding_lab",
                        "control_point_sets": _control_point_sets(),
                    },
                },
                {
                    "id": "track",
                    "operator": "vision.track",
                    "config": {
                        "tracker_id": "simple_iou_kalman",
                        "default_interval_seconds": 0.0,
                        "close_after_seconds": 10.0,
                        "use_world_anchor": "auto",
                    },
                },
                {
                    "id": "notify",
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.tracking",
                        "title": "{{camera_name}}: Synthetic person mapped",
                        "description": "{{subject.category}} - deterministic onboarding smoke",
                        "priority": "high",
                        "update_interval_seconds": 0.0,
                        "dedupe_key_template": "{{subject.id}}",
                    },
                },
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "map", "port": "in"}},
                {"from": {"node": "map", "port": "out"}, "to": {"node": "track", "port": "in"}},
                {"from": {"node": "track", "port": "out"}, "to": {"node": "notify", "port": "in"}},
            ],
        }
        pipeline = Pipeline(name="lab_synthetic_person_pin_360", graph=graph)
        runtime = PipelineRuntime(
            compiled=PipelineGraphCompiler(registry).compile_pipeline(pipeline),
            registry=registry,
            dependencies=PipelineRuntimeDependencies(notifications_upsert=notifications.upsert),
        )

        await runtime.start()
        try:
            await asyncio.sleep(0.2)
            items, _cursor = await notifications.list(limit=20)
        finally:
            await runtime.stop()

        assert len(items) == 1
        payload = items[0]["payload"]
        assert payload["pipeline_name"] == "lab_synthetic_person_pin_360"
        assert payload["subject"]["category"] == "person"
        assert payload["status"] == "open"
        trail = payload.get("trail")
        assert isinstance(trail, list)
        assert trail
        last = trail[-1]
        assert last["composition_id"] == "image_360_onboarding_lab"
        assert last["x"] == pytest.approx(-0.923906, abs=0.05)
        assert last["z"] == pytest.approx(1.853333, abs=0.05)

    asyncio.run(scenario())
