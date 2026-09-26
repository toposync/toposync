from __future__ import annotations

import json
from dataclasses import replace

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.processing.panorama_stability import (
    StabilitySettings,
    VisualStabilityDetector,
)


@pytest.fixture
def scene() -> np.ndarray:
    generator = np.random.default_rng(80512)
    canvas = generator.integers(35, 220, (240, 320), np.uint8)
    return cv2.GaussianBlur(canvas, (3, 3), 0.65)


def _detector(**changes) -> VisualStabilityDetector:
    return VisualStabilityDetector(replace(StabilitySettings(), analysis_width=320, **changes))


def _frame(scene: np.ndarray, x: float, index: int, *, wind: bool = False) -> np.ndarray:
    # Independent oracle: a translated textured plane and local moving foreground.
    image = cv2.warpAffine(
        scene,
        np.array([[1, 0, x], [0, 1, 0]], np.float32),
        (scene.shape[1], scene.shape[0]),
        borderMode=cv2.BORDER_REFLECT,
    )
    if wind:
        image[90:175, 80:160] = np.roll(scene[90:175, 80:160], index * 5, axis=1)
    noise = np.random.default_rng(index + 19).normal(0, 0.6, image.shape)
    return np.clip(image.astype(np.float64) + noise, 0, 255).astype(np.uint8)


def _observe(detector, image, index, *, media=None, received=None, generation=1, **options):
    return detector.observe(
        image,
        sequence=index,
        generation=generation,
        media_time=index * 0.1 if media is None else media,
        received_monotonic=100 + index * 0.1 if received is None else received,
        **options,
    )


def _moving_then_still(detector, scene, *, wind=False, pose=None):
    results = []
    for index in range(4):
        results.append(_observe(detector, _frame(scene, index * 3, index), index, pose=pose))
    detector.arm_stop(now=100.31)
    for index in range(4, 18):
        results.append(
            _observe(
                detector,
                _frame(scene, 9, index, wind=wind),
                index,
                pose=pose,
            )
        )
    return results


def test_global_motion_then_stationary_window_accepts_without_complete_pose(scene):
    detector = _detector()
    results = _moving_then_still(
        detector,
        scene,
        pose={"pan": None, "tilt": None, "zoom": None, "move_status": "UNKNOWN"},
    )
    assert all(not result["stable"] for result in results[:12])
    accepted = [result for result in results if result["stable"]]
    assert accepted
    assert accepted[-1]["evidence"] == "visual_transition_verified"
    assert accepted[-1]["has_motion_transition"]
    assert accepted[-1]["speed_px_s"] < 1
    assert accepted[-1]["window_media_seconds"] >= 0.8 - 1e-9
    assert accepted[-1]["window_observation_seconds"] >= 0.8 - 1e-9
    assert accepted[-1]["best_sequence"] >= 4
    assert all("qualified_frames" not in result for result in results if not result["stable"])
    qualified = accepted[-1]["qualified_frames"]
    assert len(qualified) == accepted[-1]["window_frames"]
    assert qualified[0]["sequence"] == accepted[-1]["best_sequence"]
    assert all(frame["sequence"] >= 4 and frame["generation"] == 1 for frame in qualified)
    json.dumps(results, allow_nan=False)


@pytest.mark.parametrize("continuity", ["same", "generation", "gap"])
def test_calibrated_recovery_only_establishes_transition_on_continuous_frames(scene, monkeypatch, continuity):
    calls = []

    def estimate(before, after):
        calls.append((before.copy(), after.copy()))
        return {"motion_pixels": 30, "inliers": 100, "validation_matches": 40,
                "validation_p95_pixels": 1, "method": "calibrated_frame_rotation"}

    detector = VisualStabilityDetector(replace(StabilitySettings(), analysis_width=320),
                                       transition_estimator=estimate)
    _observe(detector, scene, 0)
    monkeypatch.setattr(detector, "_motion", lambda *args: (None, {}))
    monkeypatch.setattr(detector, "_feature_motion", lambda *args: (None, {}))
    result = _observe(detector, _frame(scene, 30, 1), 1,
                      generation=2 if continuity == "generation" else 1,
                      received=102 if continuity == "gap" else 100.1,
                      media=2 if continuity == "gap" else .1)
    assert not result["stable"]
    assert bool(calls) == (continuity == "same")
    assert result["has_motion_transition"] == (continuity == "same")
    if continuity == "same":
        assert result["code"] == "motion_reacquired"
        detector.arm_stop(now=100.11)
        assert not detector._window


@pytest.mark.parametrize("media_timing", [False, True])
def test_slow_initial_motion_is_observed_before_a_new_stable_window(scene, media_timing):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )
    results = []
    for index in range(40):
        if index == 20:
            detector.arm_stop(now=102)
        results.append(detector.observe(
            _frame(scene, min(index, 20) * 0.1, index),
            sequence=index, generation=1,
            media_time=index * 0.1 if media_timing else None,
            received_monotonic=100 + index * 0.1,
        ))
    assert any(result["has_motion_transition"] for result in results[:20])
    assert not any(result["stable"] for result in results[20:27])
    assert results[-1]["stable"]
    assert results[-1]["evidence"] == "visual_transition_verified"


@pytest.mark.parametrize("kind", ["noise", "repeated", "foreground", "generation", "gap"])
def test_accumulated_transition_keeps_stationary_and_continuity_guards(scene, kind):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )
    results = []
    for index in range(30):
        offset = min(index, 3) * 0.08 if kind in {"generation", "gap"} else 0
        # A generation/gap splits the only displacement into subthreshold pieces.
        if index >= 4 and kind in {"generation", "gap"}:
            offset = 0.24 + min(index - 4, 2) * 0.08
        image = scene.copy() if kind == "repeated" else _frame(
            scene, offset, index, wind=kind == "foreground",
        )
        results.append(detector.observe(
            image, sequence=index, generation=2 if kind == "generation" and index >= 4 else 1,
            media_time=None,
            received_monotonic=100 + index * 0.1 + (1.1 if kind == "gap" and index >= 4 else 0),
        ))
    assert not any(result["has_motion_transition"] or result["stable"] for result in results)


@pytest.mark.parametrize("interval", [0.25, 0.4])
@pytest.mark.parametrize("media_timing", [True, False])
def test_low_frame_rate_retains_enough_stable_observations(scene, interval, media_timing):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )

    def observe(index, displacement):
        return detector.observe(
            _frame(scene, displacement, index), sequence=index, generation=1,
            media_time=index * interval if media_timing else None,
            received_monotonic=100 + index * interval,
        )

    for index in range(4):
        observe(index, index * 3)
    detector.arm_stop(now=100 + 3 * interval)
    results = [observe(index, 9) for index in range(4, 15)]
    assert not any(result["stable"] for result in results[:4])
    assert results[-1]["stable"]
    assert results[-1]["window_frames"] >= 5
    assert results[-1]["window_observation_seconds"] >= 4 * interval - 1e-9


def test_local_wind_is_not_mistaken_for_global_camera_motion(scene):
    results = _moving_then_still(_detector(), scene, wind=True)
    assert results[-1]["stable"]
    assert results[-1]["occupied_cells"] >= 6


def test_fast_transition_across_blurred_frame_still_requires_stable_window(scene):
    detector = _detector()
    _observe(detector, _frame(scene, 0, 0), 0)
    _observe(detector, np.full_like(scene, 127), 1)
    detector.arm_stop(now=100.15)
    # Recovery searches are throttled to twice per second; the blurred frame
    # used the first search. The next distinct frame stays within the gap gate.
    recovered = _observe(detector, _frame(scene, 70, 7), 7)
    assert recovered["code"] == "motion_reacquired"
    assert recovered["has_motion_transition"] and not recovered["stable"]
    assert recovered["speed_px_s"] is None
    assert recovered["motion_pixels"] == pytest.approx(70, abs=1)
    results = [_observe(detector, _frame(scene, 70, index), index) for index in range(8, 23)]
    assert not any(result["stable"] for result in results[:7])
    assert results[-1]["stable"]
    assert results[-1]["window_media_seconds"] >= 0.8 - 1e-9


def test_fast_transition_recovery_does_not_cross_decoder_restart(scene):
    detector = _detector()
    _observe(detector, _frame(scene, 0, 0), 0)
    _observe(detector, np.full_like(scene, 127), 1)
    results = [
        _observe(detector, _frame(scene, 70, index), index, generation=2)
        for index in range(2, 18)
    ]
    assert not any(result["has_motion_transition"] or result["stable"] for result in results)


def test_feature_recovery_does_not_promote_local_foreground_motion(scene, monkeypatch):
    detector = _detector()
    monkeypatch.setattr(detector, "_motion", lambda previous, current: (None, {}))
    _observe(detector, _frame(scene, 0, 0), 0)
    results = [_observe(detector, _frame(scene, 0, index, wind=True), index) for index in range(1, 12)]
    assert not any(result["has_motion_transition"] or result["stable"] for result in results)


def test_feature_recovery_handles_perspective_from_finite_tilt(scene):
    detector = _detector()
    intrinsic = np.array([[260, 0, 160], [0, 260, 120], [0, 0, 1]], np.float64)
    rotation, _ = cv2.Rodrigues(np.array([0.13, 0, 0], np.float64))
    expected = intrinsic @ rotation @ np.linalg.inv(intrinsic)
    current = cv2.warpPerspective(scene, expected, (320, 240))
    transform, metrics = detector._feature_motion(scene, current)
    assert transform is not None
    grid = detector._grid(scene.shape)[None]
    error = np.linalg.norm(cv2.perspectiveTransform(grid, transform) - cv2.perspectiveTransform(grid, expected), axis=2)
    assert np.max(error) < 1
    assert metrics["motion_pixels"] > 30


def test_feature_recovery_still_rejects_optical_zoom(scene):
    detector = _detector()
    current = cv2.warpAffine(scene, np.float32([[1.1, 0, -16], [0, 1.1, -12]]), (320, 240))
    transform, metrics = detector._feature_motion(scene, current)
    assert transform is None
    assert metrics.get("optical_change")


@pytest.mark.parametrize("background_columns, accepted", [(16, True), (8, False)])
def test_feature_consensus_recovery_preserves_original_inlier_fraction(
    monkeypatch, background_columns, accepted,
):
    detector = _detector()
    local = np.float32([(x, y) for y in np.linspace(10, 50, 8) for x in np.linspace(10, 60, 12)])
    background = np.float32([
        (x, y) for y in np.linspace(20, 215, 12)
        for x in np.linspace(20, 285, background_columns)
    ])
    source = np.concatenate((local, background))
    target = np.concatenate((local + [35, 0], background + [10, 0])).astype(np.float32)
    original_find = cv2.findHomography
    calls = []

    def find(first, second, *args, **kwargs):
        calls.append(len(first))
        if len(calls) == 1:
            # Force the first RANSAC consensus onto a dense local foreground.
            mask = np.zeros((len(first), 1), np.uint8)
            mask[:len(local)] = 1
            return np.float64([[1, 0, 35], [0, 1, 0], [0, 0, 1]]), mask
        assert detector._overlay_votes is None and detector._learned_overlay is None
        return original_find(first, second, *args, **kwargs)

    monkeypatch.setattr(cv2, "findHomography", find)
    transform, metrics = detector._fit_motion(source, target, (240, 320), projective=True)
    assert calls == [len(source), len(background)]
    assert metrics["tracks"] == len(source)
    assert metrics["inlier_fraction"] == pytest.approx(len(background) / len(source))
    if accepted:
        assert transform is not None
        assert transform[:2, 2] == pytest.approx([10, 0], abs=1e-5)
        assert metrics["occupied_cells"] == 12
        assert metrics["motion_pixels"] == pytest.approx(10, abs=1e-5)
    else:
        assert transform is None
        assert detector._overlay_votes is None and detector._learned_overlay is None


def test_feature_consensus_recovery_is_bounded_and_never_learns_rejected_overlays(monkeypatch):
    detector = _detector()
    local = np.float32([(x, y) for y in np.linspace(10, 50, 8) for x in np.linspace(10, 60, 12)])
    source = np.concatenate([local + [offset * 65, 0] for offset in range(4)]).astype(np.float32)
    target = np.concatenate([
        local + [offset * 65 + (offset + 1) * 5, 0] for offset in range(4)
    ]).astype(np.float32)
    calls = []

    def find(first, second, *_args, **_kwargs):
        calls.append(len(first))
        displacement = float((second - first)[0, 0])
        mask = (np.abs((second - first)[:, 0] - displacement) < .01).astype(np.uint8)[:, None]
        return np.float64([[1, 0, displacement], [0, 1, 0], [0, 0, 1]]), mask

    monkeypatch.setattr(cv2, "findHomography", find)
    transform, _ = detector._fit_motion(source, target, (240, 320), projective=True)
    assert transform is None and calls == [384, 288, 192]
    assert detector._overlay_votes is None and detector._learned_overlay is None


def test_smooth_camera_motion_does_not_pass_even_when_model_residual_is_small(scene):
    detector = _detector()
    results = [_observe(detector, _frame(scene, index * 0.3, index), index) for index in range(24)]
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "moving"
    assert results[-1]["inlier_fraction"] > 0.9
    assert results[-1]["speed_px_s"] > 2


def test_slow_subthreshold_motion_accumulates_and_fails_drift_limit(scene):
    # Isolate accumulated drift from the instantaneous speed veto. The synthetic
    # image resampler quantizes fractional positions, so its local LK estimates
    # vary while the independently prescribed trajectory advances continuously.
    detector = _detector(maximum_speed_pixels_per_second=2.0)
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    results = [
        _observe(detector, _frame(scene, 9 + (index - 3) * 0.08, index), index)
        for index in range(4, 24)
    ]
    assert not any(result["stable"] for result in results)
    assert any(result["code"] == "drifting" for result in results)
    assert max(result["speed_px_s"] for result in results) < 2


def test_reversal_or_instantaneous_pause_does_not_approve_oscillation(scene):
    detector = _detector()
    positions = [0, 3, 6, 9] + [9, 9.3, 9, 8.7, 9] * 5
    results = [
        _observe(detector, _frame(scene, position, index), index)
        for index, position in enumerate(positions)
    ]
    assert not any(result["stable"] for result in results)


def test_stationary_video_has_no_causal_transition_even_with_new_timestamps(scene):
    detector = _detector()
    detector.reset(require_motion_transition=False)
    results = [_observe(detector, _frame(scene, 0, index), index) for index in range(20)]
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "motion_transition_unobserved"


def test_independently_verified_physical_time_can_validate_initial_reference(scene):
    detector = _detector()
    detector.reset(require_motion_transition=False)
    results = [
        _observe(detector, _frame(scene, 0, index), index, physical_timestamp_verified=True)
        for index in range(16)
    ]
    assert results[-1]["stable"]
    assert results[-1]["evidence"] == "physical_timestamp_verified"
    assert not results[-1]["has_motion_transition"]


def test_frozen_decoder_with_advancing_sequence_and_pts_never_passes(scene):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    frozen = _frame(scene, 9, 4)
    results = [_observe(detector, frozen, index) for index in range(4, 20)]
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "media_time_discontinuity"
    assert not results[-1]["has_motion_transition"]


@pytest.mark.parametrize("media_timing", [True, False])
def test_interleaved_duplicates_preserve_but_never_extend_distinct_stability_window(scene, media_timing):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )
    results = []
    frame = scene
    for index in range(22):
        duplicate = index >= 4 and index % 2 == 0
        if not duplicate:
            frame = _frame(scene, min(index, 3) * 3, index)
        previous = detector._previous
        window_before = tuple(detector._window)
        result = detector.observe(
            frame, sequence=index, generation=1,
            media_time=index * .1 if media_timing else None,
            received_monotonic=100 + index * .1,
        )
        results.append(result)
        if duplicate:
            assert result["code"] == "repeated_image" and not result["stable"]
            assert detector._previous is previous
            assert tuple(detector._window) == window_before
    assert results[-1]["stable"] and results[-1]["has_motion_transition"]
    assert results[-1]["window_frames"] >= 5
    assert results[-1]["window_observation_seconds"] >= .8 - 1e-9
    assert results[-1]["best_sequence"] % 2 == 1
    assert (results[-1]["speed_px_s"] is None) is (not media_timing)


@pytest.mark.parametrize("media_timing", [True, False])
def test_prolonged_duplicates_forget_transition_even_when_receive_and_pts_advance(scene, media_timing):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )
    last_distinct = None
    for index in range(25):
        frame_index = min(index, 4) if index < 22 else index
        result = detector.observe(
            _frame(scene, min(index, 3) * 3, frame_index),
            sequence=index, generation=1,
            media_time=index * .05 if media_timing else None,
            received_monotonic=100 + index * .1,
        )
        assert not result["stable"]
        if index == 4:
            last_distinct = detector._previous
        if 5 <= index < 22:
            assert detector._previous is last_distinct
        if 15 <= index < 22:
            assert result["code"] == "observation_gap"
            assert not result["has_motion_transition"] and not detector._window
    assert not result["has_motion_transition"]


def test_verified_command_endpoint_can_use_ordered_identical_decoder_frames(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320), allow_observation_timing=True,
    )
    detector.reset(now=100.0)
    detector.arm_stop(now=100.0)
    armed = detector.arm_verified_endpoint_transition({
        "verified": True,
        "support_scope": "localized_command_transition",
        "overlap": 0.8,
        "displacement": 20.0,
        "shift_x": 20.0,
        "shift_y": 1.0,
        "inliers": 100,
        "model_candidates": [
            {"inliers": 100, "occupied_cells": 4, "hull_fraction": 0.08}
        ],
        "homography": [[1.0, 0.0, 20.0], [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]],
    })
    assert armed["has_motion_transition"]

    results = [
        detector.observe(
            scene,
            media_time=None,
            received_monotonic=100.0 + index * 0.1,
            sequence=index,
            generation=1,
        )
        for index in range(1, 13)
    ]

    assert results[-1]["stable"]
    assert results[-1]["endpoint_transport_frame"] is True
    assert results[-1]["window_observation_seconds"] >= 0.8 - 1e-9
    rejected = detector.observe(
        scene,
        media_time=None,
        received_monotonic=101.3,
        sequence=12,
        generation=1,
    )
    assert rejected["code"] == "sequence_not_ordered"
    assert not rejected["has_motion_transition"]


@pytest.mark.parametrize(
    ("inliers", "occupied_cells", "hull_fraction", "accepted"),
    [
        (80, 3, 0.10, True),
        (79, 3, 0.10, False),
        (160, 2, 0.20, False),
        (160, 3, 0.099, False),
    ],
)
def test_banded_command_transition_keeps_all_of_its_stronger_gates(
    inliers, occupied_cells, hull_fraction, accepted
):
    detector = _detector()
    result = detector.arm_verified_endpoint_transition({
        "verified": True,
        "support_scope": "banded_command_transition",
        "overlap": 0.8,
        "displacement": 20.0,
        "shift_x": 20.0,
        "shift_y": 1.0,
        "inliers": inliers,
        "model_candidates": [{
            "inliers": inliers,
            "occupied_cells": occupied_cells,
            "hull_fraction": hull_fraction,
        }],
        "homography": [[1.0, 0.0, 20.0], [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]],
    })

    assert result["has_motion_transition"] is accepted


@pytest.mark.parametrize(
    ("inliers", "occupied_cells", "hull_fraction", "accepted"),
    [
        (20, 4, 0.12, True),
        (19, 4, 0.12, False),
        (40, 3, 0.20, False),
        (40, 4, 0.119, False),
    ],
)
def test_sparse_distributed_command_transition_keeps_its_spatial_gates(
    inliers, occupied_cells, hull_fraction, accepted
):
    detector = _detector()
    result = detector.arm_verified_endpoint_transition({
        "verified": True,
        "support_scope": "sparse_distributed_command_transition",
        "overlap": 0.8,
        "displacement": 20.0,
        "shift_x": 16.0,
        "shift_y": 12.0,
        "inliers": inliers,
        "model_candidates": [{
            "inliers": inliers,
            "occupied_cells": occupied_cells,
            "hull_fraction": hull_fraction,
        }],
        "homography": [[1.0, 0.0, 16.0], [0.0, 1.0, 12.0], [0.0, 0.0, 1.0]],
    })

    assert result["has_motion_transition"] is accepted


@pytest.mark.parametrize(
    ("change", "accepted"),
    [
        (None, True),
        (("distinct_frames", 4), False),
        (("verified_models", 4), False),
        (("direction_consistent_models", 4), False),
        (("consistent_models", 3), False),
        (("required_magnitude_models", 3), False),
        (("direction_sign", 0), False),
        (("maximum_primary_deviation_pixels", 21.0), False),
    ],
)
def test_temporal_command_transition_requires_the_complete_consensus_contract(
    change, accepted
):
    consensus = {
        "axis": "pan",
        "distinct_frames": 7,
        "sampled_frames": 7,
        "verified_models": 7,
        "required_models": 5,
        "direction_sign": 1,
        "direction_consistent_models": 7,
        "consistent_models": 7,
        "required_magnitude_models": 4,
        "median_primary_shift_pixels": 100.0,
        "maximum_primary_deviation_pixels": 10.0,
        "allowed_primary_deviation_pixels": 20.0,
    }
    if change is not None:
        consensus[change[0]] = change[1]
    detector = _detector()
    result = detector.arm_verified_endpoint_transition({
        "verified": True,
        "support_scope": "temporal_command_transition",
        "overlap": 0.8,
        "displacement": 140.0,
        "shift_x": 100.0,
        "shift_y": 4.0,
        "inliers": 42,
        "model_candidates": [
            {"inliers": 42, "occupied_cells": 3, "hull_fraction": 0.07}
        ],
        "temporal_consensus": consensus,
        "homography": [[1.0, 0.0, 100.0], [0.0, 1.0, 4.0], [0.0, 0.0, 1.0]],
    })

    assert result["has_motion_transition"] is accepted


def test_duplicate_image_does_not_hide_regressing_media_timestamp(scene):
    detector = _detector()
    _observe(detector, scene, 0)
    _observe(detector, scene, 1, media=.2)
    result = _observe(detector, _frame(scene, 3, 2), 2, media=.1)
    assert result["code"] == "media_time_discontinuity"
    assert not result["has_motion_transition"]


@pytest.mark.parametrize("reason", ["optical_change", "spatial_support_rejected"])
def test_recovery_failure_reports_only_bounded_scalar_metrics(scene, monkeypatch, reason):
    detector = _detector()
    monkeypatch.setattr(detector, "_motion", lambda *_args: (None, {}))
    monkeypatch.setattr(detector, "_feature_motion", lambda *_args: (
        None, {reason: True, "tracks": 100, "inliers": 90, "occupied_cells": 4,
               "inlier_fraction": .9, "confidence": .4, "unbounded_points": np.ones((100, 2))},
    ))
    _observe(detector, scene, 0)
    result = _observe(detector, _frame(scene, 10, 1), 1)
    assert result["recovery_code"] == reason
    assert result["recovery_metrics"] == {
        "tracks": 100, "inliers": 90, "occupied_cells": 4, "inlier_fraction": .9, "confidence": .4,
        "method": "feature_recovery", "anchor_sequence": 0,
    }
    assert not result["has_motion_transition"] and not result["stable"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad_media", [0.2, 5.0])
def test_pts_discontinuity_discards_causal_evidence(scene, bad_media):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    result = _observe(detector, _frame(scene, 9, 4), 4, media=bad_media)
    assert result["code"] == "media_time_discontinuity"
    assert not result["has_motion_transition"]


def test_missing_pts_is_explicit_and_does_not_use_receive_time_as_media(scene):
    result = _detector().observe(
        scene,
        media_time=None,
        received_monotonic=100,
        sequence=1,
        generation=1,
        physical_timestamp_verified=True,
    )
    assert result["code"] == "media_time_unavailable"
    assert result["speed_px_s"] is None
    assert result["evidence"] == "insufficient"


def test_backlog_burst_cannot_supply_a_real_observation_window(scene):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    results = [
        _observe(detector, _frame(scene, 9, index), index, received=100.3 + index * 0.001)
        for index in range(4, 24)
    ]
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "settling"


def test_duplicate_sequence_and_decoder_restart_forget_motion_transition(scene):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    duplicate = _observe(detector, _frame(scene, 9, 4), 3, received=100.4)
    assert duplicate["code"] == "sequence_not_ordered"
    assert not duplicate["has_motion_transition"]
    restarted = _observe(detector, _frame(scene, 9, 5), 0, generation=2, received=100.5)
    assert restarted["code"] == "decoder_changed"
    assert not restarted["has_motion_transition"]


def test_pose_drift_vetoes_stationary_images_without_requiring_missing_axes(scene):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    results = [
        _observe(detector, _frame(scene, 9, index), index, pose={"pan": index * 0.005})
        for index in range(4, 18)
    ]
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "pose_changing"


def test_maximum_time_is_latched_failure_and_never_forces_capture(scene):
    detector = _detector()
    _observe(detector, scene, 0)
    result = _observe(detector, _frame(scene, 0, 1), 1, received=112.1)
    assert result["code"] == "timeout"
    assert not result["stable"]
    assert _observe(detector, scene, 2, received=112.2)["code"] == "timeout"
    detector.reset(now=112.3)
    assert _observe(detector, scene, 3, received=112.3)["code"] == "first_frame"


def test_textured_clock_alone_cannot_supply_distributed_support(scene):
    image = np.full_like(scene, 127)
    image[0:60, 0:160] = scene[0:60, 0:160]
    detector = _detector()
    results = [_observe(detector, _frame(image, index * 2, index), index) for index in range(6)]
    assert results[-1]["code"] == "insufficient_visual_support"
    assert not results[-1]["has_motion_transition"]


def test_known_overlay_is_masked_independent_of_its_screen_location(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        overlay_regions=[(0.25, 0.375, 0.5, 0.73)],
    )
    assert _moving_then_still(detector, scene, wind=True)[-1]["stable"]


def test_sharpness_is_compared_within_pose_and_waits_for_focus_to_settle(scene):
    detector = _detector()
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    results = []
    for index in range(4, 24):
        image = _frame(scene, 9, index)
        if index < 12:
            image = cv2.GaussianBlur(image, (5, 5), 1.2 if index % 2 else 0.5)
        results.append(_observe(detector, image, index))
    assert not any(result["stable"] for result in results[:8])
    assert results[-1]["stable"]
    assert results[-1]["best_sequence"] >= 12


def test_invalid_inputs_and_thresholds_fail_explicitly(scene):
    with pytest.raises(ValueError):
        StabilitySettings(timeout_seconds=0.1)
    with pytest.raises(ValueError):
        StabilitySettings(maximum_speed_pixels_per_second=float("nan"))
    with pytest.raises(ValueError):
        VisualStabilityDetector(overlay_regions=[(0.8, 0, 0.2, 1)])
    with pytest.raises(ValueError):
        _observe(_detector(), scene.astype(np.float32), 0)


def test_opted_in_observation_timing_has_no_media_speed_or_physical_claim(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    results = []
    for index in range(18):
        result = detector.observe(
            _frame(scene, min(index, 3) * 3, index),
            media_time=None,
            received_monotonic=100 + index * 0.1,
            sequence=index,
            generation=1,
            # This flag cannot turn local receipt time into physical/media time.
            physical_timestamp_verified=True,
        )
        results.append(result)
    assert results[-1]["stable"]
    assert results[-1]["evidence"] == "visual_transition_verified"
    assert results[-1]["timing_basis"] == "local_observation"
    assert all(result["speed_px_s"] is None for result in results)
    assert results[-1]["window_media_seconds"] is None
    assert results[-1]["window_observation_seconds"] >= 0.8 - 1e-9


def test_observation_fallback_requires_motion_and_rejects_frozen_frames(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    for index in range(16):
        result = detector.observe(
            _frame(scene, 0, index),
            media_time=None,
            received_monotonic=100 + index * 0.1,
            sequence=index,
            generation=1,
            physical_timestamp_verified=True,
        )
        assert not result["stable"]
    assert result["code"] == "motion_transition_unobserved"
    detector.reset()
    for index in range(18):
        result = detector.observe(
            _frame(scene, min(index, 3) * 3, min(index, 3)),
            media_time=None,
            received_monotonic=100 + index * 0.1,
            sequence=index,
            generation=1,
        )
        assert not result["stable"]
    assert result["code"] == "observation_gap"
    assert not result["has_motion_transition"]


def test_observation_clock_does_not_approve_a_backlog_burst(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    for index in range(18):
        result = detector.observe(
            _frame(scene, min(index, 3) * 3, index),
            media_time=None,
            received_monotonic=100 + index * 0.001,
            sequence=index,
            generation=1,
        )
        assert not result["stable"]
    assert result["code"] == "settling"


def test_loss_of_pts_requires_a_new_transition_even_when_fallback_is_allowed(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    for index in range(4):
        _observe(detector, _frame(scene, index * 3, index), index)
    result = detector.observe(
        _frame(scene, 9, 4),
        media_time=None,
        received_monotonic=100.4,
        sequence=4,
        generation=1,
    )
    assert result["code"] == "timing_basis_changed"
    assert not result["has_motion_transition"]


def test_observation_fallback_preserves_sequence_and_generation_guards(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    for index in range(4):
        detector.observe(
            _frame(scene, index * 3, index),
            media_time=None,
            received_monotonic=100 + index * 0.1,
            sequence=index,
            generation=1,
        )
    duplicate = detector.observe(
        _frame(scene, 9, 4),
        media_time=None,
        received_monotonic=100.4,
        sequence=3,
        generation=1,
    )
    assert duplicate["code"] == "sequence_not_ordered"
    assert not duplicate["has_motion_transition"]
    changed = detector.observe(
        _frame(scene, 9, 5),
        media_time=None,
        received_monotonic=100.5,
        sequence=0,
        generation=2,
    )
    assert changed["code"] == "decoder_changed"
    assert not changed["has_motion_transition"]


def test_screen_fixed_overlay_during_motion_does_not_disguise_camera_movement(scene):
    detector = _detector()
    results = []
    for index in range(18):
        image = _frame(scene, min(index, 4) * 3, index)
        # This sharp screen-fixed overlay is intentionally away from the top.
        image[150:230, 200:310] = scene[150:230, 200:310]
        results.append(_observe(detector, image, index))
    assert all(result["code"] == "moving" for result in results[1:5])
    assert results[-1]["stable"]
    assert results[-1]["has_motion_transition"]


def test_rotation_is_measured_at_distributed_points_not_only_image_center(scene):
    detector = _detector()
    results = []
    for index in range(14):
        transform = cv2.getRotationMatrix2D((160, 120), index * 0.3, 1)
        image = cv2.warpAffine(scene, transform, (320, 240), borderMode=cv2.BORDER_REFLECT)
        results.append(_observe(detector, image, index))
    assert not any(result["stable"] for result in results)
    assert results[-1]["code"] == "moving"
    assert results[-1]["speed_px_s"] > 4


def test_observation_gap_clears_transition_without_claiming_missing_media_timing(scene):
    detector = VisualStabilityDetector(
        replace(StabilitySettings(), analysis_width=320),
        allow_observation_timing=True,
    )
    for index in range(4):
        detector.observe(
            _frame(scene, index * 3, index),
            media_time=None,
            received_monotonic=100 + index * 0.1,
            sequence=index,
            generation=1,
        )
    result = detector.observe(
        _frame(scene, 9, 4),
        media_time=None,
        received_monotonic=102,
        sequence=4,
        generation=1,
    )
    assert result["code"] == "observation_gap"
    assert result["speed_px_s"] is None
    assert not result["has_motion_transition"]


def test_distributed_motion_through_distortion_does_not_supply_a_stable_transform(scene):
    detector = _detector()
    generator = np.random.default_rng(681)
    source = generator.uniform([8, 8], [311, 231], (600, 2)).astype(np.float32)
    # A smooth non-projective flow: local affine approximations agree, while
    # one similarity cannot explain the spatially varying displacement.
    target = source + np.column_stack((3 + 0.0002 * (source[:, 0] - 160) ** 2, np.zeros(600)))
    transform, _ = detector._fit_motion(source, target.astype(np.float32), scene.shape)
    assert transform is None
    evidence = detector._distributed_transition(source, target.astype(np.float32), scene.shape)
    assert evidence is not None and evidence["occupied_cells"] >= 6
    assert evidence["inlier_fraction"] >= 0.55
    assert not detector._has_motion_transition  # The helper cannot approve a frame.


@pytest.mark.parametrize("kind", ["foreground", "opposing", "zoom", "sparse", "collinear"])
def test_distributed_transition_rejects_inadequate_or_inconsistent_support(kind):
    detector = _detector()
    generator = np.random.default_rng(234)
    source = generator.uniform([8, 8], [311, 231], (600, 2)).astype(np.float32)
    target = source.copy()
    if kind == "foreground":
        selected = (source[:, 0] < 160) & (source[:, 1] < 120)
        target[selected, 0] += 4
    elif kind == "opposing":
        target[:, 0] += np.where(source[:, 1] < 120, 4, -4)
    elif kind == "zoom":
        target = (source - [160, 120]) * 1.1 + [160, 120]
    elif kind == "sparse":
        target[:70, 0] += 4
    else:
        source[:, 1] = 120
        target = source + [4, 0]
    assert detector._distributed_transition(source, target.astype(np.float32), (240, 320)) is None


def test_distributed_recovery_still_requires_new_post_stop_global_window(scene, monkeypatch):
    detector = _detector()
    _observe(detector, _frame(scene, 0, 0), 0)
    original = detector._motion
    monkeypatch.setattr(detector, "_motion", lambda previous, current: (
        None, {"distributed_transition": {"motion_pixels": 3, "occupied_cells": 9, "inliers": 200}}
    ))
    recovered = _observe(detector, _frame(scene, 3, 1), 1)
    assert recovered["has_motion_transition"] and not recovered["stable"]
    monkeypatch.setattr(detector, "_motion", original)
    detector.arm_stop(now=100.15)
    results = [_observe(detector, _frame(scene, 3, index), index) for index in range(2, 18)]
    assert not any(result["stable"] for result in results[:7])
    assert results[-1]["stable"]
    assert results[-1]["evidence"] == "visual_transition_verified"


@pytest.mark.parametrize("tracking_lost", [False, True])
def test_accumulated_distributed_transition_recovers_without_forging_stability(scene, monkeypatch, tracking_lost):
    detector = _detector()
    anchor = _frame(scene, 0, 0)
    _observe(detector, anchor, 0)
    original = detector._motion
    monkeypatch.setattr(detector, "_motion", lambda *_: (
        None if tracking_lost else np.eye(3), {"motion_pixels": 0.01},
    ))
    monkeypatch.setattr(detector, "_feature_motion", lambda *_: (None, {}))
    assert not _observe(detector, _frame(scene, 0.1, 1), 1)["has_motion_transition"]

    def motion(previous, current):
        if np.array_equal(previous, anchor):
            return None, {"distributed_transition": {
                "motion_pixels": 3, "occupied_cells": 9, "inliers": 200,
            }}
        return np.eye(3), {"motion_pixels": 0.01}

    monkeypatch.setattr(detector, "_motion", motion)
    results = [_observe(detector, _frame(scene, 3, index), index) for index in range(2, 8)]
    assert any(result["has_motion_transition"] for result in results)
    assert not any(result["stable"] for result in results)
    monkeypatch.setattr(detector, "_motion", original)
    detector.arm_stop(now=100.75)
    results = [_observe(detector, _frame(scene, 3, index), index) for index in range(8, 26)]
    assert not any(result["stable"] for result in results[:7])
    assert results[-1]["stable"]
