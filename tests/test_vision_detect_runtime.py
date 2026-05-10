from __future__ import annotations

import asyncio
from pathlib import Path

from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.pipelines import DetectionObject, ModelRegistry, VisionDetectRuntime
from toposync_ext_vision.registry import ModelManifest


class _Context:
    pipeline_name = "test_pipeline"
    node_id = "detect"

    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        _ = kwargs
        return func(*args)


class _NotifyContext:
    pipeline_name = "test_pipeline"
    node_id = "notify"


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
                "main": Artifact(name="main", data=object(), mime_type="image/raw"),
                "aux": Artifact(name="aux", data=object(), mime_type="image/raw"),
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
                    "output_artifact_name": "main",
                }
            },
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.payload.get("object_bbox01") == [
            0.35,
            0.30000000000000004,
            0.65,
            0.7000000000000001,
        ]
        detections = out.payload.get("vision", {}).get("detections")
        assert isinstance(detections, list)
        assert detections[0]["bbox01"] == [0.35, 0.30000000000000004, 0.65, 0.7000000000000001]

    asyncio.run(scenario())


def test_vision_detect_events_mode_emits_open_close_per_detection() -> None:
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
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 2
        opened, closed = out_packets
        assert opened.lifecycle == Lifecycle.OPEN
        assert closed.lifecycle == Lifecycle.CLOSE
        assert opened.stream_id == closed.stream_id
        assert opened.stream_id.startswith("det:camera:test:")
        assert closed.parent_packet_id == opened.packet_id
        assert opened.payload.get("event_id")
        assert opened.payload.get("event_id") == closed.payload.get("event_id")
        assert opened.payload.get("correlation_id")
        assert opened.payload.get("correlation_id") == closed.payload.get("correlation_id")
        assert opened.payload.get("tracking_id") is None
        assert opened.payload.get("object_category_label") == "person"
        assert opened.payload.get("object_confidence") == 0.88
        assert opened.payload.get("object_bbox01") == [0.1, 0.2, 0.3, 0.4]
        assert opened.payload.get("detected_objects") == [opened.payload.get("detected_object")]

    asyncio.run(scenario())


def test_vision_detect_events_mode_emits_independent_pairs_for_multiple_detections() -> None:
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
                    ),
                    DetectionObject(
                        label="car",
                        label_id=2,
                        score=0.77,
                        bbox01=(0.5, 0.2, 0.8, 0.7),
                        model_id="fake.detector",
                    ),
                ]

        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "events"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert [p.lifecycle for p in out_packets] == [
            Lifecycle.OPEN,
            Lifecycle.CLOSE,
            Lifecycle.OPEN,
            Lifecycle.CLOSE,
        ]
        first_open, first_close, second_open, second_close = out_packets
        assert first_open.stream_id == first_close.stream_id
        assert second_open.stream_id == second_close.stream_id
        assert first_open.stream_id != second_open.stream_id
        assert first_open.payload.get("event_id") != second_open.payload.get("event_id")
        assert first_open.payload.get("object_category_label") == "person"
        assert second_open.payload.get("object_category_label") == "car"

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
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "filter"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.stream_id == "camera:test"
        assert out.lifecycle == Lifecycle.UPDATE
        assert out.payload.get("event_id") is None
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
        runtime = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "filter"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert out_packets == []

    asyncio.run(scenario())


def test_vision_detect_events_mode_closes_core_notification(tmp_path: Path) -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def detect(self, frame, *, categories=None):  # noqa: ANN001
                _ = frame, categories
                return [
                    DetectionObject(
                        label="person",
                        label_id=0,
                        score=0.91,
                        bbox01=(0.1, 0.2, 0.3, 0.4),
                        model_id="fake.detector",
                    )
                ]

        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        deps = PipelineRuntimeDependencies(
            detector_backend_factory=lambda manifest: _Backend(),
            notifications_upsert=notifications.upsert,
            vision_model_registry=_build_registry(),
        )
        detect = VisionDetectRuntime({"model_id": "fake.detector", "emit_mode": "events"}, deps)
        notify = NotifyRuntime(
            {
                "notification_type": "pipelines.event",
                "title": "{{object_category_label}} detected",
                "update_interval_seconds": 0.0,
            },
            deps,
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100, "camera_id": "camera-main"},
            artifacts={"main": Artifact(name="main", data=object(), mime_type="image/raw")},
        )

        out_packets = await detect.process_packet(packet, _Context())
        for out in out_packets:
            await notify.process_packet(out, _NotifyContext())

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 1
        notif = items[0]
        assert notif["title"] == "person detected"
        payload = notif.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("status") == "closed"
        assert payload.get("lifecycle") == "close"
        assert payload.get("event_id") == out_packets[0].payload.get("event_id")

    asyncio.run(scenario())


def test_core_notify_creates_new_record_when_logical_event_reopens(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        deps = PipelineRuntimeDependencies(notifications_upsert=notifications.upsert)
        notify = NotifyRuntime(
            {
                "notification_type": "pipelines.tracking",
                "title": "{{object_category_label}}",
                "update_interval_seconds": 0.0,
            },
            deps,
        )

        for started_ts in (10.0, 30.0):
            common_payload = {
                "camera_id": "camera-main",
                "object_category_label": "person",
                "tracking_id": "track-reused",
                "event_id": "track-reused",
            }
            await notify.process_packet(
                Packet.create(
                    stream_id="obj:camera-main:track-reused",
                    lifecycle=Lifecycle.OPEN,
                    payload={**common_payload, "frame_ts": started_ts},
                ),
                _NotifyContext(),
            )
            await notify.process_packet(
                Packet.create(
                    stream_id="obj:camera-main:track-reused",
                    lifecycle=Lifecycle.CLOSE,
                    payload={**common_payload, "frame_ts": started_ts + 2.0},
                ),
                _NotifyContext(),
            )

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 2
        payloads = [item.get("payload") for item in items]
        assert all(isinstance(payload, dict) for payload in payloads)
        assert {payload.get("status") for payload in payloads if isinstance(payload, dict)} == {
            "closed"
        }
        starts = sorted(
            float(payload.get("event", {}).get("started_ts"))
            for payload in payloads
            if isinstance(payload, dict) and isinstance(payload.get("event"), dict)
        )
        assert starts == [10.0, 30.0]

    asyncio.run(scenario())


def test_core_notify_accumulates_stored_images_for_one_tracking_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        deps = PipelineRuntimeDependencies(notifications_upsert=notifications.upsert)
        notify = NotifyRuntime(
            {
                "notification_type": "pipelines.tracking",
                "title": "{{object_category_label}}",
                "update_interval_seconds": 60.0,
            },
            deps,
        )
        common_payload = {
            "camera_id": "camera-main",
            "object_category_label": "person",
            "tracking_id": "track-1",
            "event_id": "track-1",
        }

        await notify.process_packet(
            Packet.create(
                stream_id="obj:camera-main:track-1",
                lifecycle=Lifecycle.OPEN,
                payload={
                    **common_payload,
                    "frame_ts": 10.0,
                    "stored_images": {
                        "main": [
                            {"rel_path": "first.jpg", "stored_ts_ms": 10_000, "confidence": 0.7}
                        ]
                    },
                },
            ),
            _NotifyContext(),
        )
        await notify.process_packet(
            Packet.create(
                stream_id="obj:camera-main:track-1",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    **common_payload,
                    "frame_ts": 11.0,
                    "stored_images": {
                        "main": [
                            {"rel_path": "second.jpg", "stored_ts_ms": 11_000, "confidence": 0.9}
                        ]
                    },
                },
            ),
            _NotifyContext(),
        )
        await notify.process_packet(
            Packet.create(
                stream_id="obj:camera-main:track-1",
                lifecycle=Lifecycle.CLOSE,
                payload={**common_payload, "frame_ts": 12.0},
            ),
            _NotifyContext(),
        )

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 1
        payload = items[0].get("payload")
        assert isinstance(payload, dict)
        assert payload.get("status") == "closed"
        stored_images = payload.get("stored_images")
        assert isinstance(stored_images, dict)
        assert [item.get("rel_path") for item in stored_images.get("main", [])] == [
            "first.jpg",
            "second.jpg",
        ]

    asyncio.run(scenario())
