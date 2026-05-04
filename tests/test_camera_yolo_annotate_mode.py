from __future__ import annotations

import asyncio

import pytest


def test_yolo_detection_operator_annotate_mode_passes_through_frames() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectDetectionYOLORuntime, YoloObject

        class _Backend:
            def detect_objects(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [YoloObject(tracking_id=None, category="person", confidence=0.9, bbox01=(0.1, 0.2, 0.3, 0.4))]

            def track_objects(self, frame, *, categories=None):  # noqa: ANN001
                raise NotImplementedError

        class _Context:
            async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
                _ = kwargs
                return func(*args)

        deps = PipelineRuntimeDependencies()
        runtime = ObjectDetectionYOLORuntime(
            {"emit_mode": "annotate"},
            deps,
            backend_factory=lambda _config: _Backend(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={
                "main": Artifact(name="main", data=object(), mime_type="image/raw"),
                "aux": Artifact(name="aux", data=object(), mime_type="image/raw"),
            },
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.stream_id == "camera:test"
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_confidence") == 0.9
        assert out.payload.get("object_bbox01") == [0.1, 0.2, 0.3, 0.4]
        assert out.payload.get("detected_object", {}).get("category") == "person"
        assert out.payload.get("event_id") is None
        assert out.payload.get("tracking_id") is None

    asyncio.run(scenario())


def test_yolo_detection_operator_annotate_mode_emits_even_without_detections() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectDetectionYOLORuntime

        class _Backend:
            def detect_objects(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return []

            def track_objects(self, frame, *, categories=None):  # noqa: ANN001
                raise NotImplementedError

        class _Context:
            async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
                _ = kwargs
                return func(*args)

        deps = PipelineRuntimeDependencies()
        runtime = ObjectDetectionYOLORuntime(
            {"emit_mode": "annotate"},
            deps,
            backend_factory=lambda _config: _Backend(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.payload.get("object_category_label") is None
        assert out.payload.get("object_confidence") == 0.0
        assert out.payload.get("object_bbox01") is None
        assert out.payload.get("detected_object") is None
        assert out.payload.get("detected_objects") == []

    asyncio.run(scenario())


def test_yolo_tracking_operator_annotate_mode_passes_through_frames() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectTrackingYOLORuntime, YoloObject

        class _Backend:
            def detect_objects(self, frame, *, categories=None):  # noqa: ANN001
                raise NotImplementedError

            def track_objects(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [YoloObject(tracking_id="1", category="person", confidence=0.9, bbox01=(0.1, 0.2, 0.3, 0.4))]

        class _Context:
            async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
                _ = kwargs
                return func(*args)

        deps = PipelineRuntimeDependencies()
        runtime = ObjectTrackingYOLORuntime(
            {"emit_mode": "annotate"},
            deps,
            backend_factory=lambda _config: _Backend(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
            metadata={"motion_gate_open": True},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.stream_id == "camera:test"
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_confidence") == 0.9
        assert out.payload.get("object_bbox01") == [0.1, 0.2, 0.3, 0.4]
        assert out.payload.get("event_id") is None
        assert out.payload.get("tracking_id") is None
        assert out.payload.get("detected_object", {}).get("category") == "person"

        objects = out.payload.get("detected_objects")
        assert isinstance(objects, list)
        assert len(objects) == 1
        assert objects[0].get("tracker_track_id") == "1"

    asyncio.run(scenario())


def test_legacy_yolo_runtime_requires_explicit_backend_factory() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectDetectionYOLORuntime

        class _Context:
            async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
                _ = kwargs
                return func(*args)

        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )
        runtime = ObjectDetectionYOLORuntime({"emit_mode": "annotate"}, PipelineRuntimeDependencies())
        with pytest.raises(RuntimeError, match="no longer ship with a first-party Ultralytics backend"):
            await runtime.process_packet(packet, _Context())

    asyncio.run(scenario())
