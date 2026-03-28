from __future__ import annotations

import asyncio

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.pipelines import DetectionObject, ModelRegistry, VisionDetectRuntime
from toposync_ext_vision.registry import ModelManifest


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        _ = kwargs
        return func(*args)


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


def test_vision_detect_annotate_mode_writes_contract_payload() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def detect(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [
                    DetectionObject(
                        label="Person",
                        label_id=0,
                        score=0.93,
                        bbox01=(0.1, 0.2, 0.4, 0.8),
                        model_id="",
                        metadata={"source": "fake"},
                    )
                ]

        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "annotate"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw"),
                "frame": Artifact(name="frame", data=object(), mime_type="image/raw"),
            },
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.payload.get("vision", {}).get("task") == "detection"
        assert out.payload.get("vision", {}).get("model_id") == "fake.detector"
        assert out.payload.get("vision", {}).get("runtime") == "fake"
        detections = out.payload.get("vision", {}).get("detections")
        assert isinstance(detections, list)
        assert detections[0]["label"] == "person"
        assert detections[0]["bbox01"] == [0.1, 0.2, 0.4, 0.8]
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_confidence") == 0.93
        assert out.payload.get("object_bbox01") == [0.1, 0.2, 0.4, 0.8]
        assert out.payload.get("detected_object", {}).get("category") == "person"
        assert out.payload.get("event_id") is None
        assert out.payload.get("tracking_id") is None

    asyncio.run(scenario())


def test_vision_detect_respects_frame_crop_geometry() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def detect(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        score=0.9,
                        bbox01=(0.2, 0.25, 0.8, 0.75),
                        model_id="fake.detector",
                    )
                ]

        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "annotate"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_crop": {
                    "bbox01": [0.25, 0.1, 0.75, 0.9],
                    "set_stream_frame": True,
                }
            },
            artifacts={"frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.payload.get("object_bbox01") == [0.35, 0.30000000000000004, 0.65, 0.7000000000000001]
        detections = out.payload.get("vision", {}).get("detections")
        assert isinstance(detections, list)
        assert detections[0]["bbox01"] == [0.35, 0.30000000000000004, 0.65, 0.7000000000000001]

    asyncio.run(scenario())


def test_vision_detect_filter_mode_emits_packet_when_detections_exist() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def detect(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        score=0.88,
                        bbox01=(0.1, 0.2, 0.3, 0.4),
                        model_id="fake.detector",
                    )
                ]

        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "events"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.stream_id == "camera:test"
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_confidence") == 0.88
        assert out.payload.get("object_bbox01") == [0.1, 0.2, 0.3, 0.4]

    asyncio.run(scenario())


def test_vision_detect_filter_mode_drops_packets_without_detections() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def detect(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return []

        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "events"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert out_packets == []

    asyncio.run(scenario())
