"""Score-domain compatibility only; synthetic poses do not qualify gestures."""

from dataclasses import replace
import json
import math

import pytest

from test_vision_gestures import annotation, packet, runtime


SIMCC_SEMANTICS = "simcc_min_axis_maximum"


def scored_packet(score, timestamp=0, *, skeleton="halpe26", metadata=None):
    source = packet(timestamp, "raised")
    pose = source.payload["vision"]["poses"][0]
    pose["skeleton_id"] = skeleton
    pose["metadata"] = (
        {"landmark_score_semantics": SIMCC_SEMANTICS} if metadata is None else metadata
    )
    for landmark in pose["landmarks"]:
        landmark["model_score"] = score
    return source


@pytest.mark.parametrize("score", [0.6, 0.95, 1.0, 1.2, 12.0])
def test_declared_halpe_simcc_accepts_finite_scores_without_clamping(score):
    operator = runtime()
    source = scored_packet(score)
    points, reason = operator._points(source, operator._identity(source))
    assert reason == ""
    assert len(points) == 9
    assert all(
        landmark["model_score"] == score
        for landmark in source.payload["vision"]["poses"][0]["landmarks"]
    )


def test_serialized_score_metadata_reaches_temporal_gesture_without_reinterpretation():
    operator = runtime()
    results = []
    for timestamp in (0, 0.1, 0.2):
        source = scored_packet(1.2, timestamp)
        source = replace(source, payload=json.loads(json.dumps(source.payload, allow_nan=False)))
        results.append(annotation(operator, source))
        pose = source.payload["vision"]["poses"][0]
        assert pose["metadata"]["landmark_score_semantics"] == SIMCC_SEMANTICS
        assert all(landmark["model_score"] == 1.2 for landmark in pose["landmarks"])
    assert results[0]["status"] == "none", "score domain must not bypass dwell"
    assert results[0]["active"] == []
    assert results[-1]["status"] == "active"
    assert [(item["name"], item["side"]) for item in results[-1]["active"]] == [
        ("hand_raised", "left")
    ]
    assert results[-1]["active"][0]["evidence_current"] is True
    assert results[-1]["active"][0]["evidence"] == "heuristic_2d"


@pytest.mark.parametrize(
    "skeleton,metadata",
    [
        ("mediapipe_pose_33", {"landmark_score_semantics": SIMCC_SEMANTICS}),
        ("test", {"landmark_score_semantics": SIMCC_SEMANTICS}),
        ("halpe26", {}),
        ("halpe26", {"landmark_score_semantics": "simcc_mean_axis_maximum"}),
        ("halpe26", {"landmark_score_semantics": "probability"}),
        ("halpe26", {"landmark_score_semantics": True}),
        ("halpe26", "simcc_min_axis_maximum"),
        ("halpe26", []),
    ],
)
def test_above_one_requires_both_exact_skeleton_and_score_semantics(skeleton, metadata):
    operator = runtime()
    source = scored_packet(1.2, skeleton=skeleton, metadata=metadata)
    points, reason = operator._points(source, operator._identity(source))
    assert points is None
    assert reason == "landmarks_insufficient"


@pytest.mark.parametrize("metadata", [{}, {"landmark_score_semantics": SIMCC_SEMANTICS}])
def test_legacy_unit_interval_scores_remain_accepted(metadata):
    operator = runtime()
    source = scored_packet(0.95, skeleton="mediapipe_pose_33", metadata=metadata)
    points, reason = operator._points(source, operator._identity(source))
    assert reason == ""
    assert len(points) == 9


@pytest.mark.parametrize("score", [0.599, -1.0, math.nan, math.inf, -math.inf, True, None])
def test_declared_raw_score_still_requires_finite_number_and_minimum(score):
    operator = runtime()
    source = scored_packet(score)
    points, reason = operator._points(source, operator._identity(source))
    assert points is None
    assert reason == "landmarks_insufficient"


def test_configured_minimum_is_unchanged_for_raw_scores():
    operator = runtime(minimum_landmark_score=0.9)
    source = scored_packet(0.89)
    assert operator._points(source, operator._identity(source)) == (None, "landmarks_insufficient")


@pytest.mark.parametrize(
    "change",
    [
        {"invalid_reason": "missing"},
        {"provenance": "unknown"},
        {"visibility": "occluded"},
        {"visibility": "outside_image"},
        {"position": [1.1, 0.5]},
        {"position": [math.nan, 0.5]},
        {"position": None},
    ],
)
def test_raw_score_does_not_bypass_landmark_evidence_guards(change):
    operator = runtime()
    source = scored_packet(1.2)
    for landmark in source.payload["vision"]["poses"][0]["landmarks"]:
        landmark.update(change)
    assert operator._points(source, operator._identity(source)) == (None, "landmarks_insufficient")


def test_raw_score_does_not_bypass_stale_capture_or_out_of_order_guards():
    operator = runtime()
    source = scored_packet(1.2)
    source.payload["capture_evidence"]["published_at"] -= 10
    result = annotation(operator, source)
    assert (result["status"], result["reason"], result["active"]) == ("unknown", "stale_frame", [])
    annotation(operator, scored_packet(1.2, 1))
    result = annotation(operator, scored_packet(1.2, 0.5))
    assert (result["status"], result["reason"], result["active"]) == ("unknown", "out_of_order_frame", [])
