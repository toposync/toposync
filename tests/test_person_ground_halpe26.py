"""Halpe foot geometry is conditional support, never measured contact."""

import pytest

from test_metric_camera import known_camera
from test_person_ground import person_pose
from toposync_ext_cameras.pipelines.person_ground import PersonGroundConfig, estimate_body_and_feet


def _pose():
    pose = person_pose()
    pose["skeleton_id"] = "halpe26"
    pose["metadata"].pop("relative_landmarks_3d")
    pose["metadata"]["landmark_score_semantics"] = "simcc_min_axis_maximum"
    for point in pose["landmarks"]:
        point["name"] = point["name"].replace("foot_index", "big_toe")
    names = (
        "nose", "left_eye", "right_eye", "left_ear", "right_ear",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
        "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee",
        "right_knee", "left_ankle", "right_ankle", "head", "neck", "hip",
        "left_big_toe", "right_big_toe", "left_small_toe", "right_small_toe",
        "left_heel", "right_heel",
    )
    points = {point["name"]: point for point in pose["landmarks"]}
    pose["landmarks"] = [
        {**points.get(name, {"name": name, "position": None, "model_score": None,
                            "provenance": "image_estimate", "visibility": "unknown",
                            "invalid_reason": "missing"}), "index": index}
        for index, name in enumerate(names)
    ]
    return pose


def test_halpe_uses_explicit_heel_big_toe_anchor_without_claiming_verified_contact():
    pose = _pose()
    before = [point["name"] for point in pose["landmarks"]]
    result = estimate_body_and_feet(known_camera(), pose, PersonGroundConfig())
    for side in ("left", "right"):
        foot = result["feet"][side]
        assert foot["status"] == "estimated"
        assert foot["anchor_type"] == "heel_big_toe_support_midpoint"
        assert foot["support_landmark_names"] == [side + "_heel", side + "_big_toe"]
        assert foot["contact"] == "candidate"
        assert foot["support_consistency"] == "not_checked"
        assert foot["physical_contact_verified"] is False
        assert foot["position"] == pytest.approx([-0.13 if side == "left" else 0.13, 0, 0.015])
    assert [point["name"] for point in pose["landmarks"]] == before


def test_halpe_does_not_substitute_small_toe_or_legacy_name_when_big_toe_is_missing():
    for replacement in ("small_toe", "foot_index"):
        pose = _pose()
        for point in pose["landmarks"]:
            point["name"] = point["name"].replace("big_toe", replacement)
        result = estimate_body_and_feet(known_camera(), pose, PersonGroundConfig())
        assert all(foot["status"] == "unavailable" for foot in result["feet"].values())


def test_halpe_low_score_or_occluded_toe_cannot_form_support():
    for change in ({"model_score": 0.1}, {"visibility": "occluded"}, {"invalid_reason": "missing"}):
        pose = _pose()
        for point in pose["landmarks"]:
            if point["name"] == "left_big_toe":
                point.update(change)
        feet = estimate_body_and_feet(known_camera(), pose, PersonGroundConfig())["feet"]
        assert feet["left"]["status"] == "unavailable"
        assert feet["right"]["status"] == "estimated"


def test_mediapipe_preserves_original_heel_foot_index_anchor():
    feet = estimate_body_and_feet(known_camera(), person_pose(), PersonGroundConfig())["feet"]
    assert all(foot["anchor_type"] == "heel_toe_support_midpoint" for foot in feet.values())
