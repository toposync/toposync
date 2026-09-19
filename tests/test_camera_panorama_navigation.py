from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
from toposync_ext_cameras.panorama_navigation import (
    _continuous_pulse_plan,
    correction_step,
    reference_path,
    VisualNavigator,
)
from toposync_ext_cameras.processing.panorama_localization import PanoramaLocalizer


@pytest.mark.parametrize("matrix", [np.array([[-2.0, 0.2], [0.1, 1.5]]), np.array([[2.0], [0.1]])])
def test_correction_measures_command_sign_and_reduces_controllable_error(matrix):
    error = matrix @ np.ones(matrix.shape[1]) * 0.1
    step = correction_step(matrix, error)
    assert np.linalg.norm(error + matrix @ step) < np.linalg.norm(error) * 0.25
    assert max(abs(step)) <= 0.3


@pytest.mark.parametrize(
    "matrix", [np.zeros((2, 2)), np.array([[1.0, 1.0], [1.0, 1.0]]), np.array([[np.nan], [1.0]])]
)
def test_unobservable_axis_response_never_returns_a_command(matrix):
    with pytest.raises(PanoramaCaptureError, match="visual_response_unavailable"):
        correction_step(matrix, np.array([1.0, 1.0]))


def test_route_uses_only_existing_verified_overlaps_and_handles_cycles():
    localizer = SimpleNamespace(
        references=[{"id": name} for name in "abcd"],
        model={"overlap_links": [["a", "b"], ["b", "c"], ["c", "a"], ["unknown", "d"]]},
    )
    assert reference_path(localizer, "a", "c") == ["a", "c"]
    assert reference_path(localizer, "a", "d") is None
    assert reference_path(localizer, "a", "a") == ["a"]


def test_target_arrival_uses_image_measurement_instead_of_pose_prediction(tmp_path):
    random = np.random.default_rng(271)
    image = cv2.GaussianBlur(random.integers(0, 255, (540, 960), np.uint8), (5, 5), 1)
    path = tmp_path / "photograph.png"
    cv2.imwrite(str(path), image)
    lens = {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5}
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    # External image translation simulates pose-estimation error. The predicted
    # target is still at the centre; the observed texture is eleven pixels away.
    current = cv2.warpAffine(image, np.float32([[1, 0, 9], [0, 1, -6]]), (960, 540))
    measured = localizer.measure_target(current, {"rotation_matrix": np.eye(3).tolist()}, [1, 0, 0])
    assert measured is not None
    assert measured["error_pixels"] == pytest.approx([9, -6], abs=0.1)
    assert measured["center_error_pixels"] > 3
    assert (
        localizer.measure_target(
            np.zeros_like(image), {"rotation_matrix": np.eye(3).tolist()}, [1, 0, 0]
        )
        is None
    )


def test_navigation_without_frame_identity_does_not_probe_a_motor():
    class Scanner:
        last_frame = {"image": np.zeros((32, 32), np.uint8), "received_monotonic": 0}

        async def _reference_window(self):
            import time

            self.last_frame["received_monotonic"] = time.monotonic()
            return self.last_frame

        def _check(self):
            pass

    localizer = SimpleNamespace(
        locate=lambda *args: {"status": "unlocalized", "reason": "panorama_frame_identity_missing"}
    )
    navigator = VisualNavigator(Scanner(), localizer)
    with pytest.raises(PanoramaCaptureError, match="panorama_frame_identity_missing"):
        asyncio.run(navigator.aim([1, 0, 0]))
    assert navigator.commands == 0


@pytest.mark.parametrize("target_yaw,inverted", [(0.12, False), (0.95, True)])
@pytest.mark.parametrize("continuous", [False, True])
def test_navigation_converges_or_refuses_unattainable_motor_precision(target_yaw, inverted, continuous):
    import math
    import time

    # Physical plant: yaw/height respond with unknown signs and cross coupling.
    class Plant:
        angles = np.zeros(2)
        commands = []
        capabilities = {"continuous_supported": continuous, "velocity_supported": continuous,
                        "relative_supported": not continuous,
                        "axes": {"pan": True, "tilt": True}}
        checkpoint = {}
        last_frame = None
        last_pose = {}

        def _check(self):
            pass

        async def _persist(self):
            pass

        async def _reference_window(self):
            self.last_frame = {
                "image": np.zeros((540, 960), np.uint8),
                "received_monotonic": time.monotonic(),
                "capture_evidence": {
                    "capture_instance": "simulator",
                    "generation": 1,
                    "sequence": len(self.commands) + 1,
                },
            }
            return self.last_frame

        async def _pulse(self, axis, sign, amount, **_options):
            self.commands.append((axis, sign * amount))
            response = np.array([[-0.85 if inverted else 0.85, 0.06], [0.04, 0.75]])
            self.angles += response[:, 0 if axis == "pan" else 1] * sign * amount
            return {"frame": await self._reference_window(), "pose": {}}

    def basis(yaw, height):
        c, s, ch, sh = math.cos(yaw), math.sin(yaw), math.cos(height), math.sin(height)
        return np.array([[-s, sh * c, ch * c], [c, sh * s, ch * s], [0, -ch, sh]])

    origin = basis(0, 0)
    plant = Plant()
    lens = {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5}
    references = [
        {"id": str(i), "rotation_matrix": (origin.T @ basis(yaw, 0)).tolist()}
        for i, yaw in enumerate([0, 0.35, 0.7, 1.05])
    ]

    class Localizer:
        def locate(self, *_args):
            return {
                "status": "localized",
                "rotation_matrix": (origin.T @ basis(*plant.angles)).tolist(),
                "reference_id": str(
                    int(np.argmin(abs(np.array([0, 0.35, 0.7, 1.05]) - plant.angles[0])))
                ),
            }

        def target_reference(self, ray):
            return references[
                int(np.argmin(abs(np.array([0, 0.35, 0.7, 1.05]) - math.atan2(ray[1], ray[0]))))
            ]

        def measure_target(self, image, located, ray):
            # Independent pinhole measurement from the simulator's true pose.
            local = basis(*plant.angles).T @ ray
            error = 650 * local[:2] / local[2]
            return {
                "center_error_pixels": float(np.linalg.norm(error)),
                "error_pixels": error.tolist(),
                "analysis_width": 960,
            }

    localizer = Localizer()
    localizer.references, localizer.lens = references, lens
    localizer.model = {"overlap_links": [["0", "1"], ["1", "2"], ["2", "3"]]}
    target = basis(target_yaw, 0.05)[:, 2]

    async def run():
        await plant._reference_window()
        return await VisualNavigator(plant, localizer).aim(target)

    if continuous:
        # At this plant's high fixed speed, 50 ms exceeds the requested pixel
        # precision. The older ideal simulator silently accepted shorter pulses.
        with pytest.raises(PanoramaCaptureError, match="visual_control_resolution_unverified"):
            asyncio.run(run())
        assert all(abs(amount) >= .05 for _, amount in plant.commands)
        assert len(plant.commands) <= 64
        return
    result = asyncio.run(run())
    assert result["verified"]
    assert result["measurement"]["center_error_pixels"] <= 3
    assert 0 < len(plant.commands) <= 64
    assert max(abs(command[1]) for command in plant.commands) <= 0.3
    assert plant.angles == pytest.approx([target_yaw, 0.05], abs=0.006)


def test_minimum_pulse_is_used_only_when_observed_response_predicts_improvement():
    # Ideal correction is 22 ms, but a 50 ms pulse still improves this error.
    response = np.array([[.28], [0]])
    command = correction_step(response, np.array([-.008, .001]), minimum=.05)
    assert command == pytest.approx([.05])
    assert np.linalg.norm(np.array([-.008, .001]) + response @ command) < .0081
    # A smaller residual cannot justify the same minimum pulse. Stop instead.
    assert correction_step(response, np.array([-.002, .001]), minimum=.05) == pytest.approx([0])


def test_continuous_micro_pulse_uses_legal_duration_and_bounded_velocity():
    plan = _continuous_pulse_plan(
        np.array([-320.0, 0.0]), np.array([4.0, 0.0]), 0.01
    )
    assert plan == pytest.approx((0.05, 0.025, 0.0125))
    assert _continuous_pulse_plan(
        np.array([-20.0, 0.0]), np.array([4.0, 0.0]), 0.01
    ) is None
    assert _continuous_pulse_plan(
        np.array([-320.0, 0.0]), np.array([4.0, 0.0]), 0.08
    ) == pytest.approx((0.08, 0.1, 0.08))


def test_reference_return_qualifies_lower_velocity_from_the_resulting_image(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([30.0, 0.0])
    commands = []

    def match(reference, image):
        return {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        }

    async def pulse(axis, sign, amount, **options):
        speed = options.get("speed", 0.1)
        equivalent_amount = sign * amount * speed / 0.1
        commands.append((axis, sign, amount, speed, equivalent_amount))
        state[0] += -325 * equivalent_amount
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    monkeypatch.setattr(navigation, "_match", match)
    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        },
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )
    assert [command[:4] for command in commands] == [
        ("pan", 1, 0.08, 0.1),
        ("pan", 1, 0.05, 0.025),
    ]
    assert [command[4] for command in commands] == pytest.approx([0.08, 0.0125])
    assert np.linalg.norm(state) <= 3
    correction = scanner.checkpoint["return_corrections"][-1]
    assert correction["prediction_basis"] == "onvif_velocity_time_equivalence"
    assert correction["velocity_measured"] is False
    assert correction["response_ratio"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "lower_velocity_behavior, expected_reason",
    [
        ("ignored", "visual_response_unavailable"),
        ("quantized_to_default", "visual_error_increased"),
    ],
)
def test_reference_return_stops_after_unqualified_lower_velocity(
    monkeypatch, lower_velocity_behavior, expected_reason
):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([30.0, 0.0])
    commands = []

    def match(reference, image):
        return {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        }

    async def pulse(axis, sign, amount, **options):
        speed = options.get("speed", 0.1)
        commands.append((axis, sign, amount, speed))
        if len(commands) == 1:
            effective_amount = sign * amount
        elif lower_velocity_behavior == "ignored":
            effective_amount = 0.0
        else:
            effective_amount = sign * amount
        state[0] += -325 * effective_amount
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    monkeypatch.setattr(navigation, "_match", match)
    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        },
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )
    assert len(commands) == 2
    correction = scanner.checkpoint["return_corrections"][-1]
    assert correction["reason"] == expected_reason
    assert correction["response_qualified"] is False


def test_axis_response_is_aged_from_its_own_latest_measurement():
    import time
    current = cv2.Rodrigues(np.array([0., .3, 0.]))[0]
    scanner = SimpleNamespace(
        _check=lambda: None,
        last_frame={'image': np.zeros((20, 20), np.uint8), 'received_monotonic': time.monotonic()},
    )
    navigator = VisualNavigator(scanner, SimpleNamespace(locate=lambda *_: {'status': 'localized', 'rotation_matrix': current.tolist()}))
    navigator.response = {'pan': np.array([1., 0.]), 'tilt': np.array([0., 1.])}
    navigator.response_rotations = {'pan': np.eye(3), 'tilt': current.copy()}
    asyncio.run(navigator.locate())
    assert set(navigator.response) == {'tilt'}
    assert set(navigator.response_rotations) == {'tilt'}


@pytest.mark.parametrize('backlash', [False, True])
def test_reference_return_uses_observed_axis_and_refuses_noise_as_motor_gain(monkeypatch, backlash):
    from toposync_ext_cameras import panorama_navigation as navigation
    state = np.array([-10., 0.])
    commands = []

    def match(reference, image):
        return {'verified': True, 'overlap': 1., 'displacement': float(np.linalg.norm(state)),
                'shift_x': float(state[0]), 'shift_y': float(state[1])}

    async def pulse(axis, sign, amount, **options):
        commands.append((axis, sign * amount))
        # Fixed response on one axis; reversal can be swallowed by backlash.
        if not (backlash and sign < 0):
            speed = options.get("speed", 0.1)
            state[0 if axis == 'pan' else 1] += 200 * sign * amount * speed / 0.1
        return {'frame': {'image': np.zeros((1, 1), np.uint8)}}

    monkeypatch.setattr(navigation, '_match', match)
    scanner = SimpleNamespace(checkpoint={}, capabilities={'continuous_supported': True,
        'velocity_supported': True, 'axes': {'pan': True, 'tilt': True}}, _pulse=pulse)
    asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), {'frame': {'image': np.zeros((1, 1))}}))
    assert [axis for axis, _ in commands] == ['pan'] * 2
    assert commands[1][1] == -0.08, 'Reversal is measured before a larger correction'
    if backlash:
        assert scanner.checkpoint['return_corrections'][-1]['reason'] == 'visual_response_unavailable'
    else:
        assert scanner.checkpoint['return_corrections'][-1]['reason'] == 'visual_error_increased'
    assert np.linalg.norm(state) > 3


def test_reference_return_reverses_a_worsening_probe_before_trying_another_axis(
    monkeypatch,
):
    """Regression for Garagem capture 10: pan+ must not be followed by tilt+."""
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([-26.98, 2.95])
    initial_error = float(np.linalg.norm(state))
    gains = {
        "pan": np.array([-59.7, -18.3]),
        "tilt": np.array([14.5, 302.3]),
    }
    commands = []

    def match(reference, image):
        return {
            "verified": True,
            "overlap": 0.96,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        }

    async def pulse(axis, sign, amount, **options):
        speed = options.get("speed", 0.1)
        effective = sign * amount * speed / 0.1
        commands.append((axis, effective, options["expected_frame"]))
        state[:] += gains[axis] * effective
        return {
            "frame": {
                "image": np.zeros((1, 1), np.uint8),
                "capture_evidence": {"sequence": len(commands)},
            }
        }

    monkeypatch.setattr(navigation, "_match", match)
    initial_frame = {
        "image": np.zeros((1, 1), np.uint8),
        "capture_evidence": {"sequence": 0},
    }
    scanner = SimpleNamespace(
        checkpoint={"return_epoch": "final"},
        capabilities={
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        },
        _pulse=pulse,
    )

    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": initial_frame},
        )
    )

    assert [axis for axis, *_ in commands] == ["pan"] * 4
    assert commands[0][1] == pytest.approx(0.08)
    assert commands[1][1] == pytest.approx(-0.08)
    assert commands[0][2] is initial_frame
    assert np.linalg.norm(state) < initial_error
    assert [entry["kind"] for entry in scanner.checkpoint["return_corrections"]] == [
        "probe",
        "direction_probe",
        "correction",
        "correction",
    ]
    assert scanner.checkpoint["return_correction_state"] == "budget_exhausted"


def test_reference_return_uses_same_epoch_outbound_shift_to_choose_its_first_probe(monkeypatch):
    """A verified outward move can choose a safer first return direction.

    The seed remains a one-pulse visual probe: it has not earned a reusable
    mechanical response model until the post-pulse image qualifies it.
    """
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([-26.0, -1.7])
    commands = []

    def match(_reference, _image):
        return {
            "verified": True,
            "overlap": 0.97,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        }

    async def pulse(axis, sign, amount, **_options):
        commands.append((axis, sign, amount))
        assert axis == "pan"
        # The recorded outbound pan+ moved the image left.  The opposing
        # physical direction therefore reduces the current visual residual.
        state[:] += np.array([-300.0, -20.0]) * sign * amount
        return {
            "frame": {
                "image": np.zeros((1, 1), np.uint8),
                "capture_evidence": {"sequence": len(commands)},
            }
        }

    monkeypatch.setattr(navigation, "_match", match)
    scanner = SimpleNamespace(
        checkpoint={
            "return_epoch": "control:1:1:pan:1",
            "return_outbound_seed": {
                "return_epoch": "control:1:1:pan:1",
                "axis": "pan",
                "direction": 1,
                "duration_seconds": 0.35,
                "overlap": 0.97,
                "displacement": 26.7,
                "observed_shift": [-22.56, -1.70],
            },
        },
        capabilities={
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        },
        _pulse=pulse,
    )

    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )

    assert commands == [("pan", -1, 0.08)]
    correction = scanner.checkpoint["return_corrections"][-1]
    assert correction["kind"] == "outbound_inverse_probe"
    assert correction["outbound_seed"] == {
        "axis": "pan",
        "outbound_direction": 1,
        "duration_seconds": 0.35,
        "observed_shift": [-22.56, -1.7],
    }
    assert correction["response_qualified"] is True
    assert np.linalg.norm(state) <= 3


def test_reference_return_does_not_expand_pulse_after_one_direction_probe(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([-26.0, 0.0])
    commands = []

    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.97,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )

    async def pulse(axis, sign, amount, **_options):
        commands.append((axis, sign, amount))
        state[0] += -45.0 * sign * amount
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={
            "return_epoch": "control:1:1:pan:1",
            "return_outbound_seed": {
                "return_epoch": "control:1:1:pan:1",
                "axis": "pan",
                "direction": 1,
                "duration_seconds": 0.35,
                "overlap": 0.97,
                "displacement": 26.0,
                "observed_shift": [-22.9, 0.0],
            },
        },
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )

    assert commands[:2] == [("pan", -1, 0.08)] * 2
    assert commands[2:] == [("pan", -1, 0.15)] * 2
    corrections = scanner.checkpoint["return_corrections"]
    assert corrections[0]["kind"] == "outbound_inverse_probe"
    assert corrections[0]["qualified_response_samples"] == 0
    assert corrections[1]["kind"] == "correction"
    assert corrections[1]["qualified_response_samples"] == 1
    assert scanner.checkpoint["return_correction_state"] == "budget_exhausted"


def test_reference_return_ignores_stale_outbound_shift_seed(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([-10.0, 0.0])
    commands = []
    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )

    async def pulse(axis, sign, amount, **_options):
        commands.append((axis, sign, amount))
        state[0] = 2.0
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={
            "return_epoch": "current",
            "return_outbound_seed": {
                "return_epoch": "stale",
                "axis": "pan",
                "direction": 1,
                "duration_seconds": 0.35,
                "overlap": 0.97,
                "displacement": 10.0,
                "observed_shift": [-8.0, 0.0],
            },
        },
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )
    assert commands == [("pan", 1, 0.08)]
    assert scanner.checkpoint["return_corrections"][-1]["kind"] == "probe"


def test_reference_return_reuses_persisted_jacobian_only_from_the_same_view(
    monkeypatch,
):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([-31.0, 1.0])
    commands = []
    persisted_probe = {
        "kind": "probe",
        "axis": "pan",
        "amount": 0.08,
        "state": "observed",
        "response_qualified": True,
        "response_vector": [-60.0, -10.0],
        "response_direction": 1,
        "response_velocity": 0.1,
        "after_shift": state.tolist(),
        "after_pixels": float(np.linalg.norm(state)),
        "return_epoch": "final",
        "modality": "visual_continuous",
        "correction_command_index": 1,
    }
    checkpoint = {
        "return_epoch": "final",
        "return_correction_state": "active",
        "return_corrections": [copy.deepcopy(persisted_probe)],
        "return_correction_commands": [copy.deepcopy(persisted_probe)],
    }

    def match(reference, image):
        return {
            "verified": True,
            "overlap": 0.97,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        }

    async def pulse(axis, sign, amount, **options):
        commands.append((axis, sign * amount, options["expected_frame"]))
        state[:] += np.array([-60.0, -10.0]) * sign * amount
        return {
            "frame": {
                "image": np.zeros((1, 1), np.uint8),
                "capture_evidence": {"sequence": len(commands) + 1},
            }
        }

    monkeypatch.setattr(navigation, "_match", match)
    resumed_frame = {
        "image": np.zeros((1, 1), np.uint8),
        "capture_evidence": {"sequence": 2},
    }
    scanner = SimpleNamespace(
        checkpoint=checkpoint,
        capabilities={"velocity_supported": True, "axes": {"pan": True, "tilt": True}},
        _pulse=pulse,
    )

    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": resumed_frame},
        )
    )

    assert commands
    assert commands[0][0] == "pan"
    assert commands[0][1] == pytest.approx(-0.08)
    assert commands[0][2] is resumed_frame
    assert scanner.checkpoint["return_correction_resume"] == {
        "state": "verified",
        "axes": ["pan"],
        "return_epoch": "final",
    }
    assert len(scanner.checkpoint["return_correction_commands"]) <= 4


@pytest.mark.parametrize(
    "checkpoint_mutation, expected_reason",
    [
        ("planned", "return_correction_outcome_uncertain"),
        ("changed_view", "return_response_view_changed"),
        ("ledger_mismatch", "return_correction_ledger_mismatch"),
    ],
)
def test_reference_return_restart_fails_closed_without_replaying_or_resetting_budget(
    monkeypatch, checkpoint_mutation, expected_reason
):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([12.0, 0.0])
    entry = {
        "kind": "probe",
        "axis": "pan",
        "amount": 0.08,
        "state": "observed",
        "response_qualified": True,
        "response_vector": [-50.0, 0.0],
        "response_direction": 1,
        "response_velocity": 0.1,
        "after_shift": state.tolist(),
        "after_pixels": 12.0,
        "return_epoch": "final",
        "modality": "visual_continuous",
        "correction_command_index": 1,
    }
    trace_entry = copy.deepcopy(entry)
    command_entry = copy.deepcopy(entry)
    if checkpoint_mutation == "planned":
        trace_entry.update(state="planned", response_qualified=False)
        command_entry.update(state="planned", response_qualified=False)
    elif checkpoint_mutation == "changed_view":
        state[0] = 16.0
    else:
        command_entry["axis"] = "tilt"
    checkpoint = {
        "return_epoch": "final",
        "return_correction_state": "active",
        "return_corrections": [trace_entry],
        "return_correction_commands": [command_entry],
    }

    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        raise AssertionError("An uncertain return command must not be replayed")

    scanner = SimpleNamespace(
        checkpoint=checkpoint,
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1), np.uint8),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )

    assert commands == []
    assert len(scanner.checkpoint["return_correction_commands"]) == 1
    assert scanner.checkpoint["return_correction_state"] == "resume_unavailable"
    assert scanner.checkpoint["return_correction_resume"]["reason"] == expected_reason


def test_reference_return_rejects_low_overlap_before_learning_response(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    matches = iter(
        [
            {"verified": True, "overlap": 1.0, "displacement": 10.0,
             "shift_x": 10.0, "shift_y": 0.0},
            {"verified": True, "overlap": 0.84, "displacement": 5.0,
             "shift_x": 5.0, "shift_y": 0.0},
        ]
    )
    monkeypatch.setattr(navigation, "_match", lambda *_: next(matches))
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={"continuous_supported": True, "velocity_supported": True,
                      "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
        )
    )
    assert len(commands) == 1
    assert scanner.checkpoint["return_corrections"][-1]["reason"] == "insufficient_overlap"
    assert scanner.checkpoint["return_corrections"][-1]["response_qualified"] is False


def test_reference_return_stops_when_an_unmodeled_probe_increases_error(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    matches = iter(
        [
            {"verified": True, "overlap": 1.0, "displacement": 4.0,
             "shift_x": 4.0, "shift_y": 0.0},
            {"verified": True, "overlap": 1.0, "displacement": 6.0,
             "shift_x": 6.0, "shift_y": 0.0},
        ]
    )
    monkeypatch.setattr(navigation, "_match", lambda *_: next(matches))
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={"continuous_supported": True, "velocity_supported": True,
                      "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
        )
    )
    assert len(commands) == 1
    assert scanner.checkpoint["return_corrections"][-1]["reason"] == "visual_error_increased"
    assert scanner.checkpoint["return_corrections"][-1]["response_qualified"] is False


def test_reference_return_persists_intent_before_a_cancellable_microcommand(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 10.0,
                    "shift_x": 10.0, "shift_y": 0.0},
    )
    snapshots = []

    async def persist():
        snapshots.append(copy.deepcopy(scanner.checkpoint))

    async def pulse(*args, **kwargs):
        raise asyncio.CancelledError

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={"continuous_supported": True, "velocity_supported": True,
                      "axes": {"pan": True}},
        _pulse=pulse,
        _persist=persist,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            navigation.correct_reference(
                scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
            )
        )
    planned = snapshots[-1]["return_corrections"][-1]
    assert planned["state"] == "planned"
    assert planned["pulse_seconds"] == 0.08
    assert planned["velocity"] == 0.1
    assert planned["amount"] == 0.08
    assert snapshots[-1]["return_correction_state"] == "active"


def test_reference_return_resumes_only_the_remaining_interrupted_budget(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([10.0, 0.0])
    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0,
                    "displacement": float(np.linalg.norm(state)),
                    "shift_x": float(state[0]), "shift_y": float(state[1])},
    )
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        state[0] = 2.0
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={
            "return_correction_state": "active",
            "return_corrections": [{"state": "observed"} for _ in range(3)],
        },
        capabilities={"continuous_supported": True, "velocity_supported": True,
                      "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
        )
    )
    assert len(commands) == 1
    assert len(scanner.checkpoint["return_corrections"]) == 4


def test_reference_return_rejection_blocks_retries_without_resetting_job_budget(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 10.0,
            "shift_x": 10.0,
            "shift_y": 0.0,
        },
    )
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True},
        },
        _pulse=pulse,
    )
    result = {"frame": {"image": np.zeros((1, 1), np.uint8)}}
    for _ in range(6):
        result = asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), result))

    assert len(commands) == 1
    assert len(scanner.checkpoint["return_corrections"]) == 1
    assert len(scanner.checkpoint["return_correction_commands"]) == 1
    assert scanner.checkpoint["return_correction_state"] == "resume_unavailable"
    assert scanner.checkpoint["return_correction_resume"]["reason"] == (
        "return_correction_previously_rejected"
    )
    assert "return_correction_history" not in scanner.checkpoint


@pytest.mark.parametrize("field", ["overlap", "displacement", "shift_x", "shift_y"])
def test_reference_return_rejects_non_finite_matches_without_moving(monkeypatch, field):
    from toposync_ext_cameras import panorama_navigation as navigation

    measurement = {
        "verified": True,
        "overlap": 1.0,
        "displacement": 10.0,
        "shift_x": 10.0,
        "shift_y": 0.0,
    }
    measurement[field] = float("nan")
    monkeypatch.setattr(navigation, "_match", lambda *_: measurement)
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
        )
    )

    assert commands == []
    assert scanner.checkpoint["return_correction_state"] == "rejected"


def test_reference_return_with_a_spent_persisted_budget_never_moves(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 10.0,
            "shift_x": 10.0,
            "shift_y": 0.0,
        },
    )
    commands = []

    async def pulse(*args, **kwargs):
        commands.append((args, kwargs))
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={
            "return_correction_state": "active",
            "return_corrections": [
                {"state": "planned"} for _ in range(navigation.MAXIMUM_FINE_CORRECTIONS)
            ],
        },
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": {"image": np.zeros((1, 1))}}
        )
    )

    assert commands == []
    assert len(scanner.checkpoint["return_corrections"]) == navigation.MAXIMUM_FINE_CORRECTIONS


def test_reference_return_shares_budget_with_persisted_absolute_commands(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([20.0, 0.0])
    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )
    calls = []

    async def pulse(*args, **kwargs):
        calls.append((args, kwargs))
        state[0] -= 4.0
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    checkpoint = {
        "absolute_return_corrections": [
            {"kind": "probe", "state": "observed"},
            {"kind": "correction", "state": "planned"},
        ]
    }
    scanner = SimpleNamespace(
        checkpoint=checkpoint,
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    result = {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), result))
    asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), result))

    assert len(calls) == 2
    assert len(checkpoint["return_correction_commands"]) == 4
    assert [item["modality"] for item in checkpoint["return_correction_commands"]] == [
        "absolute",
        "absolute",
        "visual_continuous",
        "visual_continuous",
    ]
    assert checkpoint["return_correction_state"] == "budget_exhausted"


def test_relative_only_reference_return_records_relative_modality(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([10.0, 0.0])
    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )

    async def pulse(*_args, **_options):
        state[0] = 2.0
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={
            "relative_supported": True,
            "velocity_supported": False,
            "axes": {"pan": True},
        },
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner,
            np.zeros((1, 1)),
            {"frame": {"image": np.zeros((1, 1), np.uint8)}},
        )
    )

    assert scanner.checkpoint["return_correction_commands"][0]["modality"] == "visual_relative"


def test_reference_return_binds_each_pulse_to_the_measured_frame(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    state = np.array([10.0, 0.0])
    monkeypatch.setattr(
        navigation,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": float(np.linalg.norm(state)),
            "shift_x": float(state[0]),
            "shift_y": float(state[1]),
        },
    )
    initial_frame = {"image": np.zeros((1, 1), np.uint8)}
    expected_frames = []

    async def pulse(*args, **kwargs):
        expected_frames.append(kwargs["expected_frame"])
        state[0] = 2.0
        return {"frame": {"image": np.zeros((1, 1), np.uint8)}}

    scanner = SimpleNamespace(
        checkpoint={},
        capabilities={"velocity_supported": True, "axes": {"pan": True}},
        _pulse=pulse,
    )
    asyncio.run(
        navigation.correct_reference(
            scanner, np.zeros((1, 1)), {"frame": initial_frame}
        )
    )

    assert len(expected_frames) == 1
    assert expected_frames[0] is initial_frame


def test_reference_return_stops_after_gain_changes_with_an_unused_axis(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation
    state = np.array([15., 0.])
    commands = []

    def match(reference, image):
        return {'verified': True, 'overlap': 1., 'displacement': float(np.linalg.norm(state)),
                'shift_x': float(state[0]), 'shift_y': float(state[1])}

    async def pulse(axis, sign, amount, **options):
        commands.append((axis, sign * amount))
        speed = options.get("speed", 0.1)
        if len(commands) == 3:
            state[1] += 2.5
        else:
            gain = -100 if len(commands) == 1 else -50
            state[0] += gain * sign * amount * speed / 0.1
        return {'frame': {'image': np.zeros((1, 1))}}

    monkeypatch.setattr(navigation, '_match', match)
    scanner = SimpleNamespace(checkpoint={}, capabilities={'continuous_supported': True,
        'velocity_supported': True,
        'axes': {'pan': True, 'tilt': True}}, _pulse=pulse)
    asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), {'frame': {'image': np.zeros((1, 1))}}))
    assert len(commands) == 3, 'An unknown tilt must not permit another pulse after pan diverges'
    assert [axis for axis, _ in commands] == ['pan'] * 3
    assert scanner.checkpoint['return_corrections'][-1]['reason'] == 'visual_error_increased'
    assert scanner.checkpoint['return_corrections'][-1]['expected_shift_delta'] is not None


def test_reference_return_records_failed_pulse_without_retry(monkeypatch):
    from toposync_ext_cameras import panorama_navigation as navigation

    monkeypatch.setattr(navigation, '_match', lambda *_: {'verified': True, 'overlap': 1.,
        'displacement': 10., 'shift_x': 10., 'shift_y': 0.})
    calls = []

    async def pulse(*args, **kwargs):
        calls.append(args)
        raise navigation.PanoramaCaptureError('motion_not_observed')

    scanner = SimpleNamespace(checkpoint={}, capabilities={'continuous_supported': True,
        'velocity_supported': True, 'axes': {'pan': True}}, _pulse=pulse)
    with pytest.raises(navigation.PanoramaCaptureError, match='motion_not_observed'):
        asyncio.run(navigation.correct_reference(scanner, np.zeros((1, 1)), {'frame': {'image': np.zeros((1, 1))}}))
    assert len(calls) == 1
    assert scanner.checkpoint['return_corrections'][-1]['reason'] == 'motion_not_observed'


@pytest.mark.parametrize('failures,reason,expected_calls', [(1, 'panorama_visual_localization_failed', 2), (4, 'panorama_visual_localization_failed', 3), (4, 'panorama_visual_localization_ambiguous', 1)])
def test_localization_recovery_observes_new_frames_without_motor_or_threshold_changes(failures, reason, expected_calls):
    import time
    calls = []
    scanner = SimpleNamespace(_check=lambda: None, last_frame=None)

    async def frame():
        scanner.last_frame = {'image': np.zeros((20, 20), np.uint8), 'received_monotonic': time.monotonic(),
                              'capture_evidence': {'sequence': len(calls) + 1}}
        return scanner.last_frame

    def locate(image, evidence):
        calls.append(evidence['sequence'])
        return {'status': 'unlocalized', 'reason': reason} if len(calls) <= failures else {'status': 'localized', 'rotation_matrix': np.eye(3).tolist()}

    scanner._reference_window = frame
    navigator = VisualNavigator(scanner, SimpleNamespace(locate=locate))

    async def run():
        await frame()
        if failures == 1:
            assert (await navigator.locate())['status'] == 'localized'
        else:
            with pytest.raises(PanoramaCaptureError, match=reason):
                await navigator.locate()

    asyncio.run(run())
    assert calls == list(range(1, expected_calls + 1))
    assert navigator.commands == 0
