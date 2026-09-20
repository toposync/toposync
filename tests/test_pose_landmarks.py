from __future__ import annotations

import json

import numpy as np
import pytest

from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.processing.pose_landmarks import (
    PoseLandmark,
    normalize_landmarks,
    project_pose_landmarks,
)


def _packet(artifact: Artifact | None = None, payload: dict | None = None) -> Packet:
    return Packet.create(
        stream_id="camera:pose-test",
        payload=payload or {},
        artifacts={artifact.name: artifact} if artifact is not None else {},
    )


def test_missing_nonfinite_and_external_landmarks_preserve_skeleton_slots() -> None:
    points = normalize_landmarks(
        [(1.2, -0.4, 3.5), None, (np.nan, 0.5, 0.9), (0.2, np.inf, 0.8), (0.4, 0.5, np.nan), [0.5]],
        ["shoulder", "elbow", "wrist", "hip", "knee", "ankle", "heel"],
    )
    assert [point.index for point in points] == list(range(7))
    assert [point.name for point in points] == [
        "shoulder",
        "elbow",
        "wrist",
        "hip",
        "knee",
        "ankle",
        "heel",
    ]
    assert points[0].position == (1.2, -0.4)
    assert points[0].model_score == 3.5
    assert points[1].position is None and points[1].invalid_reason == "missing"
    assert points[2].position is None and points[3].position is None
    assert points[4].position == (0.4, 0.5) and points[4].model_score is None
    assert points[5].invalid_reason == "invalid_keypoint"
    assert points[6].invalid_reason == "missing"
    assert all(point.visibility == "unknown" for point in points)
    assert all(point.provenance == "image_estimate" for point in points)
    assert json.loads(json.dumps([point.to_dict() for point in points], allow_nan=False))[0][
        "position"
    ] == [1.2, -0.4]


def test_visibility_is_explicit_and_model_score_never_implies_observation() -> None:
    points = normalize_landmarks([(0.1, 0.2, 1.0), (0.2, 0.3, 0.0)], visibility=["occluded"])
    assert points[0].visibility == "occluded"
    assert points[1].visibility == "unknown"
    assert [point.name for point in points] == ["landmark_0", "landmark_1"]
    with pytest.raises(ValueError, match="unique"):
        normalize_landmarks([], ["wrist", "wrist"])


@pytest.mark.parametrize(
    "position", [(float("inf"), 0.2), (0.2, float("nan")), ("0.2", 0.4), (True, 0.4), (0.1,)]
)
def test_direct_contract_construction_is_strict_json_safe(position: tuple) -> None:
    point = PoseLandmark(0, "wrist", position, model_score=float("inf"))
    assert point.position is None
    assert point.model_score is None
    json.dumps(point.to_dict(), allow_nan=False)


def test_nested_crop_resize_padding_rotation_reflection_round_trip() -> None:
    artifact = Artifact(
        name="main",
        data=np.zeros((81, 101, 3)),
        metadata={"image_geometry": image_geometry(101, 81, {"frame": "fixture"})},
    )
    for matrix, width, height in [
        ([[1, 0, -20], [0, 1, -10], [0, 0, 1]], 61, 51),
        ([[2, 0, 0], [0, 2, 0], [0, 0, 1]], 121, 101),
        ([[1, 0, 5], [0, 1, 7], [0, 0, 1]], 131, 115),
        ([[0, -1, 114], [1, 0, 0], [0, 0, 1]], 115, 131),
        ([[-1, 0, 114], [0, 1, 0], [0, 0, 1]], 115, 131),
    ]:
        artifact = Artifact(
            name="selected",
            data=np.zeros((height, width, 3)),
            metadata=transformed_geometry(artifact, matrix, width, height),
        )
    original_pixels = [(-10, 40), (100, 80), (30, 25)]
    # Independent analytic composition: rotation and reflection swap the axes.
    selected = [
        ((2 * (y - 10) + 7) / 114, (2 * (x - 20) + 5) / 130, 0.9) for x, y in original_pixels
    ]
    packet = _packet(
        artifact, {"frame_crop": {"output_artifact_name": "selected", "bbox01": [0, 0, 0.1, 0.1]}}
    )
    projected = project_pose_landmarks(
        normalize_landmarks(selected), packet, selected_artifact_name="selected"
    )
    for point, (x, y) in zip(projected, original_pixels):
        assert point.position == pytest.approx((x / 100, y / 80))
        assert point.visibility == "unknown"
    assert projected[0].position[0] < 0
    # Authoritative composed geometry prevented applying legacy crop twice.
    assert projected[1].position == pytest.approx((1, 1))


def test_projective_horizon_invalidates_only_affected_landmark() -> None:
    geometry = image_geometry(101, 101, {})
    geometry["to_source"] = [[1, 0, 0], [0, 1, 0], [1, 0, -50]]
    artifact = Artifact(
        name="main", data=np.zeros((101, 101, 3)), metadata={"image_geometry": geometry}
    )
    points = normalize_landmarks([(0.25, 0.25, 0.7), (0.5, 0.3, 0.8), None, (0.75, 0.75, 0.9)])
    projected = project_pose_landmarks(points, _packet(artifact))
    assert [point.index for point in projected] == [0, 1, 2, 3]
    assert projected[0].position == pytest.approx((-0.01, -0.01))
    assert projected[1].position is None
    assert projected[1].invalid_reason == "outside_projective_support"
    assert projected[2] is points[2]
    assert projected[3].position == pytest.approx((0.03, 0.03))


@pytest.mark.parametrize("damage", ["size", "singular", "nonfinite", "malformed"])
def test_invalid_authoritative_geometry_does_not_fall_back_to_identity(damage: str) -> None:
    geometry = image_geometry(101, 101, {})
    if damage == "size":
        geometry["image_size"] = [100, 101]
    elif damage == "singular":
        geometry["to_source"] = np.zeros((3, 3)).tolist()
    elif damage == "nonfinite":
        geometry["to_source"][0][0] = float("nan")
    else:
        geometry = {}
    artifact = Artifact(
        name="main", data=np.zeros((101, 101, 3)), metadata={"image_geometry": geometry}
    )
    points = normalize_landmarks([(0.25, 0.25, 0.7), None])
    projected = project_pose_landmarks(points, _packet(artifact))
    assert projected[0].position is None
    assert projected[0].invalid_reason == "invalid_image_geometry"
    assert projected[1] is points[1]


def test_legacy_warp_then_crop_is_unclipped_and_artifact_scoped() -> None:
    payload = {
        "frame_warp": {
            "output_artifact_name": "selected",
            "kind": "perspective",
            "homography_inv": [[2, 0, 0], [0, 2, 0], [0, 0, 1]],
            "source_frame_width": 101,
            "source_frame_height": 101,
            "dest_frame_width": 51,
            "dest_frame_height": 51,
        },
        "frame_crop": {"output_artifact_name": "selected", "bbox01": [0.2, 0.3, 0.7, 0.9]},
    }
    points = normalize_landmarks([(-0.25, 1.5, 1.2)])
    packet = _packet(payload=payload)
    projected = project_pose_landmarks(points, packet, selected_artifact_name="selected")
    assert projected[0].position == pytest.approx((0.075, 1.2))
    assert projected[0].model_score == 1.2
    assert project_pose_landmarks(points, packet, selected_artifact_name="other") == points


@pytest.mark.parametrize(
    "transform",
    [
        {"frame_crop": {"output_artifact_name": "main", "bbox01": [0, 0, np.nan, 1]}},
        {"frame_crop": {"output_artifact_name": "main", "bbox01": [0.5, 0, 0.2, 1]}},
        {"frame_warp": {"output_artifact_name": "main", "kind": "perspective"}},
    ],
)
def test_malformed_targeted_legacy_geometry_is_not_silently_ignored(transform: dict) -> None:
    projected = project_pose_landmarks(
        normalize_landmarks([(0.2, 0.3, 0.8)]), _packet(payload=transform)
    )
    assert projected[0].position is None
    assert projected[0].invalid_reason == "invalid_image_geometry"
