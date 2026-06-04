from __future__ import annotations

from toposync_ext_vision.processing.trackers import (
    ByteWorldTrackerBackend,
    NorfairTrackerBackend,
    SimpleIouKalmanTrackerBackend,
    available_tracker_backends,
    build_tracker_backend,
)
from toposync_ext_vision.pipelines import DetectionObject


def _detections(
    bboxes: list[tuple[float, float, float, float]],
    *,
    score: float = 0.9,
    label: str = "person",
    world_anchors: list[dict[str, float]] | None = None,
) -> list[DetectionObject]:
    return [
        DetectionObject(
            label=label,
            label_id=0,
            score=score,
            bbox01=bbox01,
            model_id="fake.detector",
            world_anchor=world_anchors[index] if world_anchors and index < len(world_anchors) else None,
        )
        for index, bbox01 in enumerate(bboxes)
    ]


def test_byte_world_backend_is_default_tracker_factory() -> None:
    backend = build_tracker_backend(" ", close_after_seconds=0.2)
    assert isinstance(backend, ByteWorldTrackerBackend)
    assert backend.tracker_id == "byte_world"
    assert any(item.get("id") == "byte_world" and item.get("available") for item in available_tracker_backends())


def test_byte_world_backend_keeps_stable_tracking_id_with_confidence_bands() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=0.5,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.78),
        frame_ts=1.00,
        metadata={"camera_id": "camera-main"},
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.13, 0.10, 0.23, 0.40)], score=0.34),
        frame_ts=1.05,
        metadata={"camera_id": "camera-main"},
    )

    assert len(first) == 1
    assert len(second) == 1
    assert second[0].tracking_id == first[0].tracking_id
    assert second[0].metadata["confidence_band"] == "continue"
    assert second[0].camera_id == "camera-main"


def test_byte_world_backend_low_confidence_detection_does_not_open_track() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=0.5,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
    )

    tracks = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.34),
        frame_ts=1.00,
    )

    assert tracks == []


def test_byte_world_backend_uses_world_anchor_as_auxiliary_match_signal() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=1.0,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
        use_world_anchor="auto",
        world_match_distance_meters=3.0,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.10, 0.10, 0.20, 0.40)],
            score=0.80,
            world_anchors=[{"x": 1.0, "z": 2.0, "confidence": 0.90}],
        ),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.72, 0.10, 0.82, 0.40)],
            score=0.55,
            world_anchors=[{"x": 1.6, "z": 2.2, "confidence": 0.90}],
        ),
        frame_ts=1.10,
    )

    assert len(second) == 1
    assert second[0].tracking_id == first[0].tracking_id
    assert second[0].world_anchor == {"x": 1.6, "z": 2.2, "confidence": 0.9}


def test_byte_world_backend_high_confidence_far_world_anchor_prevents_false_merge() -> None:
    backend = ByteWorldTrackerBackend(
        close_after_seconds=1.0,
        open_confidence_threshold=0.50,
        continue_confidence_threshold=0.25,
        use_world_anchor="auto",
        world_match_distance_meters=3.0,
    )

    first = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.10, 0.10, 0.25, 0.50)],
            score=0.80,
            world_anchors=[{"x": 1.0, "z": 1.0, "confidence": 0.95}],
        ),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections(
            [(0.11, 0.10, 0.26, 0.50)],
            score=0.80,
            world_anchors=[{"x": 20.0, "z": 1.0, "confidence": 0.95}],
        ),
        frame_ts=1.05,
    )

    assert len(second) == 1
    assert second[0].tracking_id != first[0].tracking_id


def test_byte_world_backend_does_not_merge_different_classes() -> None:
    backend = ByteWorldTrackerBackend(close_after_seconds=0.5)

    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)], score=0.8, label="person"),
        frame_ts=1.00,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.11, 0.10, 0.21, 0.40)], score=0.8, label="dog"),
        frame_ts=1.05,
    )

    assert second[0].tracking_id != first[0].tracking_id


def test_simple_iou_kalman_backend_keeps_stable_tracking_id() -> None:
    backend = SimpleIouKalmanTrackerBackend(close_after_seconds=0.2)

    meta = {
        "camera_id": "camera-main",
        "world_anchor": {"x": 1.5, "z": 2.5},
        "appearance_embedding_artifact_name": "appearance_embedding",
    }
    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)]),
        frame_ts=1.00,
        metadata=meta,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.14, 0.10, 0.24, 0.40)]),
        frame_ts=1.05,
        metadata=meta,
    )
    third = backend.update(
        "camera:test",
        None,
        _detections([(0.18, 0.10, 0.28, 0.40)]),
        frame_ts=1.10,
        metadata=meta,
    )

    tracking_ids = {
        first[0].tracking_id,
        second[0].tracking_id,
        third[0].tracking_id,
    }
    assert len(tracking_ids) == 1
    assert first[0].source_tracking_id is not None
    assert second[0].source_tracking_id == first[0].source_tracking_id
    assert first[0].camera_id == "camera-main"
    assert first[0].world_anchor == {"x": 1.5, "z": 2.5}
    assert first[0].appearance_embedding_artifact_name == "appearance_embedding"


def test_norfair_backend_keeps_stable_tracking_id() -> None:
    backend = NorfairTrackerBackend(close_after_seconds=0.2)

    meta = {
        "camera_id": "camera-main",
        "world_anchor": {"x": 1.5, "z": 2.5},
        "appearance_embedding_artifact_name": "appearance_embedding",
    }
    first = backend.update(
        "camera:test",
        None,
        _detections([(0.10, 0.10, 0.20, 0.40)]),
        frame_ts=1.00,
        metadata=meta,
    )
    second = backend.update(
        "camera:test",
        None,
        _detections([(0.12, 0.10, 0.22, 0.40)]),
        frame_ts=1.05,
        metadata=meta,
    )
    third = backend.update(
        "camera:test",
        None,
        _detections([(0.14, 0.10, 0.24, 0.40)]),
        frame_ts=1.10,
        metadata=meta,
    )

    tracking_ids = {
        first[0].tracking_id,
        second[0].tracking_id,
        third[0].tracking_id,
    }
    assert len(tracking_ids) == 1
    assert first[0].tracker_id == "norfair"
    assert second[0].source_tracking_id == first[0].source_tracking_id
    assert first[0].camera_id == "camera-main"
    assert first[0].world_anchor == {"x": 1.5, "z": 2.5}
    assert first[0].appearance_embedding_artifact_name == "appearance_embedding"


def test_tracker_backends_preserve_keypoints_hook() -> None:
    detection = DetectionObject(
        label="person",
        label_id=0,
        score=0.9,
        bbox01=(0.10, 0.10, 0.20, 0.40),
        keypoints=[(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)],
        model_id="fake.detector",
    )

    simple = SimpleIouKalmanTrackerBackend(close_after_seconds=0.2).update(
        "camera:test",
        None,
        [detection],
        frame_ts=1.00,
    )
    norfair = NorfairTrackerBackend(close_after_seconds=0.2).update(
        "camera:test",
        None,
        [detection],
        frame_ts=1.00,
    )

    assert simple[0].keypoints == [(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)]
    assert norfair[0].keypoints == [(0.12, 0.14, 0.95), (0.18, 0.36, 0.85)]
