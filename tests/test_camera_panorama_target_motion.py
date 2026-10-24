"""Prediction inputs are capture-bound directions, not object identity or physical qualification."""
import math

import pytest

from toposync_ext_cameras.panorama_target_motion import TargetMotion


def observation(sequence, timestamp, **changes):
    return {"scope": ["camera", "source", "job", 1, "artifact", "geometry"], "capture_instance": "capture",
        "generation": 1, "sequence": sequence, "observed_monotonic": timestamp, **changes}


def direction(angle):
    return [math.cos(angle), math.sin(angle), 0.]


def moving():
    motion = TargetMotion()
    for index in range(3):
        motion.observe(observation(index + 1, 100 + index * .1), direction(index * .02), 100 + index * .1)
    return motion


def test_prediction_uses_observation_intervals_and_never_counts_repeated_polls_as_samples():
    motion = moving()
    for _ in range(20):
        motion.observe(observation(3, 100.2), direction(.04), 100.2)
    assert len(motion.samples) == 3
    predicted = motion.predict(100.2, .1)
    assert math.atan2(predicted["ray"][1], predicted["ray"][0]) == pytest.approx(.06, abs=1e-4)
    assert predicted["sequence"] == 3 and predicted["horizon_seconds"] == pytest.approx(.1)
    assert predicted["timing_basis"] == "source_publication" and not predicted["physical_capture_verified"]
    assert motion.predict(100.2, .3) is None, "Do not extrapolate beyond the observed history"
    assert motion.predict(100.1, .1) is None
    assert motion.predict(102., .1) is None
    for invalid in [0, -1, float("nan"), float("inf")]:
        assert motion.predict(100.2, invalid) is None


@pytest.mark.parametrize("change", ["generation", "scope", "sequence", "timestamp", "future", "expired", "missing", "invalid_ray", "same_frame_changed"])
def test_discontinuity_or_invalid_evidence_cannot_keep_old_motion(change):
    motion = moving()
    sample = observation(4, 100.3)
    ray = direction(.06)
    if change == "generation":
        sample["generation"] = 2
    elif change == "scope":
        sample["scope"][4] = "another-artifact"
    elif change == "sequence":
        sample["sequence"] = 2
    elif change == "timestamp":
        sample["observed_monotonic"] = 100.1
    elif change == "future":
        sample["observed_monotonic"] = 101.
    elif change == "expired":
        sample["observed_monotonic"] = 98.
    elif change == "missing":
        sample = None
    elif change == "invalid_ray":
        ray = [2., 0, 0]
    elif change == "same_frame_changed":
        sample = observation(3, 100.2)
    motion.observe(sample, ray, 100.3)
    assert len(motion.samples) < 3
    assert motion.predict(100.3, .1) is None


@pytest.mark.parametrize("last_angle", [.02, -.01])
def test_stop_or_reversal_does_not_extend_previous_velocity(last_angle):
    motion = TargetMotion()
    for index, angle in enumerate([0, .02, last_angle]):
        motion.observe(observation(index + 1, 100 + index * .1), direction(angle), 100 + index * .1)
    assert motion.predict(100.2, .1) is None


@pytest.mark.parametrize("interval", [1 / 30, .1, .4])
def test_rate_projection_uses_target_cadence_without_extending_arrival_horizon(interval):
    motion = TargetMotion()
    for index in range(3):
        timestamp = 100 + index * interval
        motion.observe(observation(index + 1, timestamp), direction(index * interval * .2), timestamp)
    now = 100 + 2 * interval
    estimate = motion.predict_rate(now)
    assert estimate is not None
    assert 0 < estimate["horizon_seconds"] <= min(1., 2 * interval)
    angle = math.atan2(estimate["ray"][1], estimate["ray"][0])
    assert (angle - 2 * interval * .2) / estimate["horizon_seconds"] == pytest.approx(.2, abs=.001)
    assert estimate["observed_start_ray"] == direction(0)
    assert estimate["observed_end_ray"] == direction(2 * interval * .2)
    assert motion.predict(now, 2 * interval + .01) is None
    assert motion.predict_rate(now + 2 * interval + .01) is None
    assert motion.predict_rate(now - .01) is None


@pytest.mark.parametrize("interval", [1 / 30, .1, .3])
def test_temporal_history_survives_delayed_publications_without_extending_freshness(interval):
    motion = TargetMotion()
    for index in range(math.ceil(1 / interval) + 1):
        timestamp = 100 + index * interval
        sample = observation(index + 1, timestamp)
        ray = direction(index * interval * .065)
        motion.observe(sample, ray, timestamp)
    now = timestamp + .6
    estimate = motion.predict_rate(now)
    assert estimate is not None and .6 < estimate["horizon_seconds"] <= 1
    angle = math.atan2(estimate["ray"][1], estimate["ray"][0])
    assert angle == pytest.approx((now - 100 + estimate["cycle_seconds"]) * .065, abs=.001)
    retained = list(motion.samples)
    for _ in range(20):
        motion.observe(sample, ray, now)
    assert len(motion.samples) == len(retained)
    assert motion.predict(timestamp + 1.01, .01) is None
    assert motion.predict(now, .5) is None
    assert motion.predict_rate(timestamp - .01) is None


def test_history_storage_and_time_horizon_remain_bounded():
    motion = TargetMotion()
    for index in range(1000):
        timestamp = 100 + index * .001
        motion.observe(observation(index + 1, timestamp), direction(index * .00001), timestamp)
    assert len(motion.samples) == 64
    assert motion.predict(timestamp, .1) is None
    for index in range(1000, 1100):
        timestamp += .3
        motion.observe(observation(index + 1, timestamp), direction(index * .00001), timestamp)
    assert len(motion.samples) <= 5
    assert motion.samples[-1][1] - motion.samples[1][1] < 1
    motion.observe(observation(1101, timestamp + 1.1), direction(.2), timestamp + 1.1)
    assert len(motion.samples) == 1 and motion.predict_rate(timestamp + 1.1) is None


@pytest.mark.parametrize("ending", [[.06, .06], [.08, .04]])
def test_long_history_does_not_hide_a_recent_stop_or_reversal(ending):
    motion = TargetMotion()
    for index, angle in enumerate([0., .02, .04, .06, *ending]):
        timestamp = 100 + .2 * index
        motion.observe(observation(index + 1, timestamp), direction(angle), timestamp)
    assert motion.predict_rate(timestamp + .1) is None


@pytest.mark.parametrize("age,extended", [(.5, False), (.51, True)])
def test_recent_horizon_boundary_does_not_require_old_acceleration_due_to_roundoff(age, extended):
    motion = TargetMotion()
    for index, angle in enumerate([0., .008, .032, .056]):
        timestamp = 100.29999999999998 + index * .3
        motion.observe(observation(index + 1, timestamp), direction(angle), timestamp)
    prediction = motion.predict_rate(timestamp + age)
    assert prediction is not None
    disagreement = math.dist(prediction["history_ray"], prediction["comparison_ray"])
    assert (disagreement > .001) is extended
