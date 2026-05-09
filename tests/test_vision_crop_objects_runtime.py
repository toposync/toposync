from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.pipelines import DetectionObject, ModelManifest, ModelRegistry
from toposync_ext_vision.processing.tasks import VisionCropObjectsRuntime, VisionDetectRuntime


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        kwargs = dict(kwargs)
        kwargs.pop("concurrency_key", None)
        return func(*args, **kwargs)


class _OneObjectDetector:
    backend_id = "fake_detector"

    def detect(self, frame: Any, *, categories: set[str] | None = None) -> list[DetectionObject]:  # noqa: ARG002
        detection = DetectionObject(
            label="person",
            label_id=0,
            score=0.92,
            bbox01=(0.20, 0.30, 0.50, 0.70),
            model_id="fake.detector",
        )
        if categories and detection.label not in categories:
            return []
        return [detection]


def _model_registry() -> ModelRegistry:
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


def test_vision_detect_events_feed_object_crop_without_tracking() -> None:
    async def scenario() -> None:
        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda _manifest: _OneObjectDetector(),
            vision_model_registry=_model_registry(),
        )
        detect = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "events"}, deps)
        crop = VisionCropObjectsRuntime({"padding_ratio": 0.0, "min_crop_size_px": 1})

        packet = Packet.create(
            stream_id="camera:test",
            lifecycle=Lifecycle.UPDATE,
            payload={"frame_ts": 1.0},
            artifacts={
                "main": Artifact(
                    name="main",
                    data=np.zeros((100, 100, 3), dtype=np.uint8),
                    mime_type="image/raw",
                )
            },
        )

        detection_events = await detect.process_packet(packet, _Context())
        assert [event.lifecycle for event in detection_events] == [Lifecycle.OPEN, Lifecycle.CLOSE]

        cropped: list[Packet] = []
        for event in detection_events:
            cropped.extend(await crop.process_packet(event, _Context()))

        assert [event.lifecycle for event in cropped] == [Lifecycle.OPEN, Lifecycle.CLOSE]
        assert cropped[0].stream_id == cropped[1].stream_id
        assert cropped[0].payload.get("event_id") == cropped[1].payload.get("event_id")
        assert cropped[0].payload.get("correlation_id") == cropped[1].payload.get("correlation_id")
        assert cropped[0].payload.get("tracking_id") is None
        assert "main" in cropped[0].artifacts
        assert tuple(cropped[0].artifacts["main"].data.shape[:2]) == (40, 30)
        assert "main" not in cropped[1].artifacts

    asyncio.run(scenario())


def test_vision_crop_objects_reads_detected_object_bbox_fallback() -> None:
    async def scenario() -> None:
        crop = VisionCropObjectsRuntime(
            {
                "bbox_field": "missing_bbox",
                "output_artifact_name": "object",
                "padding_ratio": 0.0,
                "min_crop_size_px": 1,
            }
        )
        packet = Packet.create(
            stream_id="obj:camera:test:1",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "event_id": "trk-1",
                "tracking_id": "trk-1",
                "correlation_id": "corr-1",
                "detected_object": {"bbox01": [0.10, 0.20, 0.30, 0.60]},
            },
            artifacts={
                "main": Artifact(
                    name="main",
                    data=np.zeros((50, 100, 3), dtype=np.uint8),
                    mime_type="image/raw",
                )
            },
        )

        out = (await crop.process_packet(packet, _Context()))[0]

        assert out.stream_id == packet.stream_id
        assert out.payload.get("event_id") == "trk-1"
        assert out.payload.get("tracking_id") == "trk-1"
        assert out.payload.get("correlation_id") == "corr-1"
        assert "object" in out.artifacts
        assert tuple(out.artifacts["object"].data.shape[:2]) == (20, 20)

    asyncio.run(scenario())
