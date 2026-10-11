"""Synthetic command response, never physical qualification."""
import copy

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.panorama_approach import approach_pulse, build_approach, geometry_digest
from toposync_ext_cameras.processing.panorama_mapping import _rotation_basis


def fixture():
    model = {"captures": []}
    observations, attempts = [], []
    for index, duration in enumerate((.3, .4, .5)):
        first = cv2.Rodrigues(np.array([0., index * .01, 0.]))[0]
        last = first @ cv2.Rodrigues(np.array([0., duration * .3, 0.]))[0]
        observations.append({"axis": "pan", "direction": 1, "command_id": str(index),
                             "before_rotation": _rotation_basis(first).tolist(), "after_rotation": _rotation_basis(last).tolist()})
        attempts.append({"command_receipt": {"command_id": str(index)}, "pulse_seconds": duration,
                         "command_accepted_seconds": .1, "stop_requested_seconds": duration + .1,
                         "stop_accepted_seconds": duration + .2, "stop_command_accepted": True})
    return model, observations, attempts


@pytest.mark.parametrize("repetition", range(3))
@pytest.mark.parametrize("fault", [None, "stop", "timing", "duplicate", "conflict", "far", "axis"])
def test_local_prediction_requires_independent_consistent_support(repetition, fault):
    model, observations, attempts = fixture()
    if fault == "stop":
        attempts[0]["stop_command_accepted"] = False
    if fault == "timing":
        attempts[0]["stop_accepted_seconds"] = 5
    if fault == "duplicate":
        observations[1]["command_id"] = observations[0]["command_id"]
    if fault == "conflict":
        observations[1]["after_rotation"] = observations[1]["before_rotation"]
    approach = build_approach(model, observations, attempts, "optical-identity")
    rotation = cv2.Rodrigues(np.array([0., 1., 0.]))[0] if fault == "far" else np.eye(3)
    target = _rotation_basis(rotation) @ np.array([.25, 0., 1.])
    result = approach_pulse(approach, "tilt" if fault == "axis" else "pan", target, rotation)
    if fault is None:
        assert result is not None and .3 <= result <= .5
    else:
        assert result is None
    assert approach["status"] != "qualified"
    assert approach["geometry_digest"] == geometry_digest(model)
    assert geometry_digest({"captures": [1]}) != approach["geometry_digest"]


def test_unknown_version_and_opposite_direction_do_not_predict():
    model, observations, attempts = fixture()
    approach = build_approach(model, observations, attempts, "identity")
    target = _rotation_basis(np.eye(3)) @ np.array([-.25, 0., 1.])
    assert approach_pulse(approach, "pan", target, np.eye(3)) is None
    changed = copy.deepcopy(approach)
    changed["version"] = 2
    assert approach_pulse(changed, "pan", -target, np.eye(3)) is None


@pytest.mark.parametrize("fault", ["samples", "rotation", "response", "duration"])
def test_corrupt_saved_response_never_becomes_a_movement_prediction(fault):
    approach = build_approach(*fixture(), "identity")
    if fault == "samples":
        approach["samples"] = None
    elif fault == "rotation":
        approach["samples"][0]["before_rotation"] = [[0]]
    elif fault == "response":
        approach["samples"][0]["response"] = [float("nan"), 0, 0]
    else:
        approach["samples"][0]["duration_seconds"] = -1
    target = _rotation_basis(np.eye(3)) @ np.array([.25, 0, 1.])
    assert approach_pulse(approach, "pan", target, np.eye(3)) is None


def test_navigation_limit_never_extrapolates_below_observed_pulse_range():
    model, observations, attempts = fixture()
    for item in attempts:
        item["pulse_seconds"] = .8
        item["stop_requested_seconds"] = .9
        item["stop_accepted_seconds"] = 1.
    approach = build_approach(model, observations, attempts, "identity")
    target = _rotation_basis(np.eye(3)) @ np.array([.5, 0, 1.])
    assert approach_pulse(approach, "pan", target, np.eye(3)) is None
