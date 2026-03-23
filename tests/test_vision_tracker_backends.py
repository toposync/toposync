from __future__ import annotations

from toposync_ext_vision.processing.trackers import (
    NorfairTrackerBackend,
    SimpleIouKalmanTrackerBackend,
)
from toposync_ext_vision.pipelines import DetectionObject


def _detections(bboxes: list[tuple[float, float, float, float]]) -> list[DetectionObject]:
    return [
        DetectionObject(
            label="person",
            label_id=0,
            score=0.9,
            bbox01=bbox01,
            model_id="fake.detector",
        )
        for bbox01 in bboxes
    ]


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
