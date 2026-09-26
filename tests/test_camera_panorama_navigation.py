from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
from toposync_ext_cameras.panorama_navigation import (
    _continuous_pulse_plan,
    _fine_correction_has_measured_response,
    _qualified_navigation_pulse_limit,
    correction_step,
    localized_axis_measurement,
    MAXIMUM_LIVE_CENTER_ERROR_PIXELS,
    MAXIMUM_NAVIGATION_PULSE_SECONDS,
    reference_path,
    select_direct_native_reference,
    select_native_reference,
    VisualNavigator,
)
from toposync_ext_cameras.processing.panorama_localization import PanoramaLocalizer
from toposync_ext_cameras.processing.panorama_mapping import _rotation_basis


@pytest.mark.parametrize("case", ["useful", "nearby", "behind", "minor_gain", "nan", "wrong_rotation", "unsupported"])
def test_native_selection_shortens_geometry_without_motor_units(case):
    def rotation(degrees):
        angle = np.radians(degrees)
        return np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                         [-np.sin(angle), 0, np.cos(angle)]])

    target = _rotation_basis(rotation(20))[:, 2]
    anchor_angle = {"nearby": 0, "behind": -10, "minor_gain": 5}.get(case, 18)
    anchor_rotation = rotation(anchor_angle)
    record = {"ray": _rotation_basis(anchor_rotation)[:, 2].tolist(), "rotation_matrix": anchor_rotation.tolist(),
              "destination": {"pan": 23456, "tilt": -789}}
    if case == "nan":
        record["ray"][0] = float("nan")
    if case == "wrong_rotation":
        record["rotation_matrix"] = rotation(0).tolist()
    localizer = SimpleNamespace(
        lens={"width": 100, "height": 80, "fx": 100, "fy": 100, "cx": 49.5, "cy": 39.5, "distortion": []},
        target_reference=lambda ray: None if case == "unsupported" else {"id": "reference"},
    )
    selected = select_native_reference(localizer, {"rotation_matrix": rotation(0)}, target, [record])
    assert (selected is record) == (case == "useful")


@pytest.mark.parametrize("width", [640, 3840])
@pytest.mark.parametrize("case", ["exact", "within", "outside", "behind", "nan", "wrong_rotation", "unsupported", "zoom", "invalid_target"])
def test_direct_native_selection_needs_no_departure_but_requires_target_support(width, case):
    lens = {"width": width, "height": width, "fx": width, "fy": width,
            "cx": (width - 1) / 2, "cy": (width - 1) / 2, "distortion": []}
    basis = _rotation_basis(np.eye(3))
    record = {"ray": basis[:, 2].tolist(), "rotation_matrix": np.eye(3).tolist(),
              "destination": {"kind": "preset", "preserve_zoom": case != "zoom"}}
    offset = {"within": 11, "outside": 13}.get(case, 0) / min(width, 960)
    target = basis @ np.array([offset, 0, 1.0])
    target /= np.linalg.norm(target)
    if case == "behind":
        target *= -1
    if case == "invalid_target":
        target *= 2
    if case == "nan":
        record["ray"][0] = float("nan")
    if case == "wrong_rotation":
        record["ray"] = basis[:, 0].tolist()
    localizer = SimpleNamespace(lens=lens, target_reference=lambda ray: None if case == "unsupported" else {"id": "reference"})
    selected = select_direct_native_reference(localizer, target, [record])
    assert (selected is record) == (case in {"exact", "within"})


@pytest.mark.parametrize("offset", [0, 11, 13])
def test_direct_reference_defers_already_centred_hint_to_fresh_visual_measurement(offset):
    lens = {"width": 960, "height": 960, "fx": 960, "fy": 960,
            "cx": 479.5, "cy": 479.5, "distortion": []}
    rotation = np.eye(3)
    target = _rotation_basis(rotation)[:, 2]
    record = {"ray": target.tolist(), "rotation_matrix": rotation.tolist(),
              "destination": {"kind": "preset", "preserve_zoom": True}}
    angle = np.arctan(offset / 960)
    departure = {"rotation_matrix": [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                                     [-np.sin(angle), 0, np.cos(angle)]]}
    localizer = SimpleNamespace(lens=lens, target_reference=lambda ray: {"id": "reference"})
    assert select_direct_native_reference(localizer, target, [record]) is record
    selected = select_direct_native_reference(localizer, target, [record], departure)
    assert (selected is None) == (offset <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS)


@pytest.mark.parametrize("failure", [None, "budget", "old_frame", "moving", "optical_policy",
                                     "wrong_reference", "nan", "optics", "cancel", "unstable", "expired_once", "expired_always"])
@pytest.mark.parametrize("destination_kind", ["preset", "absolute"])
def test_native_approach_shares_budget_and_requires_observed_reference(failure, destination_kind):
    import time

    async def run():
        frame = {"received_monotonic": time.monotonic()}
        camera = SimpleNamespace(return_to=AsyncMock(return_value={"accepted": True}),
                                 verify_return_optical_state=AsyncMock())
        scanner = SimpleNamespace(camera=camera, physical_state="stopped", last_frame=frame,
                                  checkpoint={}, _check=lambda: None, _persist=AsyncMock(),
                                  refresh_stopped_frame=AsyncMock(return_value=frame))

        async def move(command, **kwargs):
            assert kwargs == {"allow_stationary": True,
                              "target": {"pan": .2, "tilt": .3} if destination_kind == "absolute" else None}
            await command()
            if failure == "cancel":
                raise asyncio.CancelledError()
            return {"frame": frame, "stable": failure != "unstable"}

        scanner._move = move
        navigator = VisualNavigator(scanner, None, maximum_commands=0 if failure == "budget" else 1)
        navigator.response = {"pan": np.array([1, 0])}
        navigator.response_rotations = {"pan": np.eye(3)}
        navigator.locate = AsyncMock(return_value={"status": "localized"})
        error = 13 if failure == "wrong_reference" else float("nan") if failure == "nan" else 1
        navigator._target_measurement = AsyncMock(return_value={"center_error_pixels": error})
        if failure in {"expired_once", "expired_always"}:
            from toposync_ext_cameras.panorama_navigation import _ExpiredQualifiedFrame
            navigator._target_measurement.side_effect = [
                _ExpiredQualifiedFrame(),
                {"center_error_pixels": 1} if failure == "expired_once" else _ExpiredQualifiedFrame(),
            ]
            scanner._reference_window = AsyncMock(return_value=frame)
        destination = {"kind": destination_kind, "preserve_zoom": failure != "optical_policy",
                       "pan": .2, "tilt": .3}
        if failure == "old_frame":
            frame["received_monotonic"] -= 2
        if failure == "moving":
            scanner.physical_state = "moving"
        if failure == "optics":
            camera.verify_return_optical_state.side_effect = PanoramaCaptureError("return_optical_state_mismatch")
        if failure and failure != "expired_once":
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else PanoramaCaptureError):
                await navigator.approach_reference(destination, [0, 0, 1])
        else:
            assert await navigator.approach_reference(destination, [0, 0, 1]) == {"status": "localized"}
            assert navigator.trace[-1]["state"] == "observed"
        rejected_before_dispatch = failure in {"budget", "old_frame", "moving", "optical_policy"}
        assert navigator.commands == (0 if rejected_before_dispatch else 1)
        assert camera.return_to.await_count == navigator.commands
        if not rejected_before_dispatch:
            assert not navigator.response and not navigator.response_rotations
        if failure and failure != "expired_once" and not rejected_before_dispatch:
            assert navigator.trace[-1]["state"] == "pending"
        if failure in {"expired_once", "expired_always"}:
            scanner._reference_window.assert_not_awaited()
            assert scanner.refresh_stopped_frame.await_count == 2
            assert navigator._target_measurement.await_count == 2
        if failure in {"optics", "cancel", "unstable"} or rejected_before_dispatch:
            navigator.locate.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "optics", "frame", "cancel"])
def test_native_arrival_overlaps_reads_and_drains_frame_before_geometry(failure):
    import time

    async def run():
        frame = {"received_monotonic": time.monotonic()}
        optics_started, frame_started, frame_finished = (asyncio.Event() for _ in range(3))
        blocked = asyncio.Event()

        async def verify(_destination):
            optics_started.set()
            await frame_started.wait()
            if failure == "optics":
                raise PanoramaCaptureError("return_optical_state_mismatch")
            if failure == "cancel":
                raise asyncio.CancelledError()

        async def refresh():
            frame_started.set()
            try:
                await optics_started.wait()
                if failure in {"optics", "cancel"}:
                    await blocked.wait()
                if failure == "frame":
                    raise PanoramaCaptureError("stop_unconfirmed")
                return frame
            finally:
                frame_finished.set()

        scanner = SimpleNamespace(
            camera=SimpleNamespace(verify_return_optical_state=verify),
            physical_state="stopped", last_frame=frame, checkpoint={},
            _check=lambda: None, _persist=AsyncMock(), refresh_stopped_frame=refresh,
            _move=AsyncMock(return_value={"frame": frame, "stable": True}),
        )
        navigator = VisualNavigator(scanner, None)
        navigator.locate = AsyncMock(return_value={"status": "localized"})
        navigator._target_measurement = AsyncMock(return_value={"center_error_pixels": 1})
        destination = {"kind": "preset", "preserve_zoom": True}
        operation = navigator.approach_reference(destination, [0, 0, 1])
        if failure:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else PanoramaCaptureError):
                await asyncio.wait_for(operation, timeout=1)
            navigator.locate.assert_not_awaited()
            assert navigator.trace[-1]["state"] == "pending"
        else:
            await asyncio.wait_for(operation, timeout=1)
            navigator.locate.assert_awaited_once()
            assert navigator.trace[-1]["state"] == "observed"
        assert frame_finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "position", "localization", "cancel"])
def test_stopped_preparation_overlaps_reads_and_drains_observation_on_failure(failure):
    async def run():
        position_started = asyncio.Event()
        observation_started = asyncio.Event()
        observation_finished = asyncio.Event()
        blocked = asyncio.Event()

        async def position():
            position_started.set()
            await observation_started.wait()
            if failure == "position":
                raise PanoramaCaptureError("position_unavailable")
            if failure == "cancel":
                await blocked.wait()
            return {"pan": .25}

        scanner = SimpleNamespace(camera=SimpleNamespace(position=position), physical_state="stopped",
                                  _check=lambda: None, last_pose=None)
        navigator = VisualNavigator(scanner, None)

        async def locate():
            observation_started.set()
            try:
                await position_started.wait()
                if failure == "localization":
                    raise PanoramaCaptureError("panorama_visual_support_insufficient")
                if failure in {"position", "cancel"}:
                    await blocked.wait()
                return {"status": "localized"}
            finally:
                observation_finished.set()

        navigator.locate = locate
        task = asyncio.create_task(navigator.prepare_stopped_view())
        await asyncio.wait_for(observation_started.wait(), timeout=1)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure:
            with pytest.raises(PanoramaCaptureError) as error:
                await asyncio.wait_for(task, timeout=1)
            assert error.value.code == ("position_unavailable" if failure == "position"
                                        else "panorama_visual_support_insufficient")
        else:
            await asyncio.wait_for(task, timeout=1)
            assert scanner.last_pose == {"pan": .25}
        assert observation_finished.is_set()
        assert navigator.commands == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_destination", "missing_ray", "invalid_target", "recall"])
def test_aim_never_continues_from_an_unconfirmed_native_reference(failure):
    navigator = VisualNavigator(SimpleNamespace(), SimpleNamespace(
        target_reference=lambda ray: None if failure == "invalid_target" else {"id": "target"}))
    navigator.locate = AsyncMock(return_value={"reference_id": "current"})
    navigator.approach_reference = AsyncMock(side_effect=PanoramaCaptureError("visual_native_reference_unconfirmed"))
    navigator._pulse = AsyncMock()
    with pytest.raises(PanoramaCaptureError):
        asyncio.run(navigator.aim([1, 0, 0],
            native_destination=None if failure == "missing_destination" else {"preserve_zoom": True},
            native_ray=None if failure == "missing_ray" else [1, 0, 0]))
    assert navigator.approach_reference.await_count == (1 if failure == "recall" else 0)
    navigator._pulse.assert_not_awaited()


def test_stopped_preparation_refuses_unconfirmed_motion():
    scanner = SimpleNamespace(physical_state="unknown", _check=lambda: None)
    navigator = VisualNavigator(scanner, None)
    with pytest.raises(PanoramaCaptureError, match="stop_unconfirmed"):
        asyncio.run(navigator.prepare_stopped_view())


@pytest.mark.parametrize("matrix", [np.array([[-2.0, 0.2], [0.1, 1.5]]), np.array([[2.0], [0.1]])])
def test_correction_measures_command_sign_and_reduces_controllable_error(matrix):
    error = matrix @ np.ones(matrix.shape[1]) * 0.1
    step = correction_step(matrix, error)
    assert np.linalg.norm(error + matrix @ step) < np.linalg.norm(error) * 0.25
    assert max(abs(step)) <= MAXIMUM_NAVIGATION_PULSE_SECONDS


@pytest.mark.parametrize(
    "matrix", [np.zeros((2, 2)), np.array([[1.0, 1.0], [1.0, 1.0]]), np.array([[np.nan], [1.0]])]
)
def test_unobservable_axis_response_never_returns_a_command(matrix):
    with pytest.raises(PanoramaCaptureError, match="visual_response_unavailable"):
        correction_step(matrix, np.array([1.0, 1.0]))


@pytest.mark.parametrize('changes', [
    {'verified': False}, {'support_scope': 'localized_command_transition'},
    {'inliers': 95}, {'overlap': .74}, {'target_features': 79},
    {'source_features': None}, {'analysis_size': []}, {'displacement': float('nan')},
])
def test_longer_navigation_pulse_requires_strong_distributed_overlap(changes):
    match = {'verified': True, 'support_scope': 'distributed_scene', 'inliers': 120,
             'overlap': .9, 'source_features': 100, 'target_features': 100,
             'analysis_size': [960, 540], 'displacement': 70.}
    assert _qualified_navigation_pulse_limit(.12, match) == .6
    assert _qualified_navigation_pulse_limit(.6, match) == 1.2
    assert _qualified_navigation_pulse_limit(.6, {**match, **changes}) == .6


def test_navigation_duration_adapts_to_diagonal_displacement_and_never_exceeds_scanner_limit():
    match = {'verified': True, 'support_scope': 'distributed_scene', 'inliers': 120,
             'overlap': .8, 'source_features': 100, 'target_features': 100,
             'analysis_size': [960, 540], 'displacement': 200.}
    assert _qualified_navigation_pulse_limit(.6, match) == pytest.approx(.648)
    assert _qualified_navigation_pulse_limit(1.2, {**match, 'displacement': 5.}) == 1.2


@pytest.mark.parametrize('duration', [.6, 1.2])
def test_extended_navigation_pulse_uses_existing_fresh_image_precondition(duration):
    import time
    frame = {'received_monotonic': time.monotonic()}
    calls = []

    async def persist():
        pass

    async def pulse(axis, sign, seconds, **options):
        calls.append(options)
        return {'frame': frame, 'stable': True, 'match': {}}

    scanner = SimpleNamespace(last_frame=frame, checkpoint={}, _check=lambda: None,
                              _persist=persist, _pulse=pulse, capabilities={'velocity_supported': True},
                              physical_state='stopped')
    navigator = VisualNavigator(scanner, None)
    asyncio.run(navigator._pulse('pan', duration))
    assert calls == ([{'expected_frame': frame}] if duration > .6 else [{}])


def test_navigation_consumes_confirmed_fresh_observation_without_renewing_ranked_frame():
    import time
    ranked = {'received_monotonic': time.monotonic(), 'sequence': 1}
    confirmed = {'received_monotonic': time.monotonic(), 'sequence': 4}

    async def persist():
        pass

    async def pulse(*_args, **_options):
        return {'frame': ranked, 'observation_frame': confirmed, 'stable': True, 'match': {}}

    scanner = SimpleNamespace(last_frame=ranked, checkpoint={}, _check=lambda: None,
                              _persist=persist, _pulse=pulse, capabilities={'velocity_supported': True},
                              physical_state='stopped')
    original_timestamp = ranked['received_monotonic']
    asyncio.run(VisualNavigator(scanner, None)._pulse('pan', .6))
    assert scanner.last_frame is confirmed
    assert ranked['received_monotonic'] == original_timestamp


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
    # target is still at the centre; the observed texture is over twenty pixels away.
    current = cv2.warpAffine(image, np.float32([[1, 0, 18], [0, 1, -12]]), (960, 540))
    measured = localizer.measure_target(current, {"rotation_matrix": np.eye(3).tolist()}, [1, 0, 0])
    assert measured is not None
    assert measured["error_pixels"] == pytest.approx([18, -12], abs=0.1)
    assert measured["center_error_pixels"] > MAXIMUM_LIVE_CENTER_ERROR_PIXELS
    assert (
        localizer.measure_target(
            np.zeros_like(image), {"rotation_matrix": np.eye(3).tolist()}, [1, 0, 0]
        )
        is None
    )


def test_localized_axis_measurement_keeps_pixels_angles_and_motor_units_separate():
    lens = {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5}
    located = {
        "rotation_matrix": np.eye(3).tolist(),
        "reference_id": "reference",
        "analysis_width": 960,
        "validation_matches": 24,
        "validation_p95_pixels": 1.5,
        "inlier_fraction": 0.8,
    }
    measured = localized_axis_measurement([1, 0, 0], lens, located)
    assert measured == {
        "method": "localized_optical_axis",
        "evidence": "held_out_feature_localization",
        "error_pixels": [0.0, 0.0],
        "center_error_pixels": 0.0,
        "analysis_width": 960.0,
        "validation_matches": 24,
        "validation_p95_pixels": 1.5,
        "inlier_fraction": 0.8,
        "reference_id": "reference",
    }
    assert localized_axis_measurement(
        [1, 0, 0], lens, {**located, "validation_p95_pixels": 8.1}
    ) is None


def test_navigation_accepts_qualified_localized_optical_axis_without_a_motor_command():
    import time

    lens = {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}

    class Scanner:
        capabilities = {"axes": {"pan": True, "tilt": True}}
        checkpoint = {}
        last_pose = {}
        last_frame = {
            "image": np.zeros((540, 960), np.uint8),
            "received_monotonic": time.monotonic(),
            "capture_evidence": {"capture_instance": "test", "generation": 1, "sequence": 1},
        }

        def _check(self):
            pass

    class Localizer:
        references = [reference]
        model = {"overlap_links": []}

        def __init__(self):
            self.lens = lens

        def target_reference(self, _ray):
            return reference

        def locate(self, *_args):
            return {
                "status": "localized",
                "rotation_matrix": np.eye(3).tolist(),
                "reference_id": "reference",
                "analysis_width": 960,
                "validation_matches": 24,
                "validation_p95_pixels": 1.5,
                "inlier_fraction": 0.8,
            }

        def measure_target(self, *_args):
            return None

    result = asyncio.run(VisualNavigator(Scanner(), Localizer()).aim([1, 0, 0]))
    assert result["verified"] is True
    assert result["commands"] == 0
    assert result["measurement"]["method"] == "localized_optical_axis"


def test_navigation_checks_fresh_image_arrival_before_rejecting_a_small_response():
    import time

    lens = {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}

    class Scanner:
        capabilities = {
            "relative_supported": True,
            "axes": {"pan": True, "tilt": True},
        }
        checkpoint = {}
        last_pose = {}
        pulses = 0
        last_frame = {
            "image": np.zeros((540, 960), np.uint8),
            "received_monotonic": time.monotonic(),
            "capture_evidence": {"capture_instance": "test", "generation": 1, "sequence": 1},
        }

        def _check(self):
            pass

        async def _persist(self):
            pass

        async def _pulse(self, *_args, **_kwargs):
            self.pulses += 1
            self.last_frame = {
                **self.last_frame,
                "received_monotonic": time.monotonic(),
                "capture_evidence": {
                    "capture_instance": "test",
                    "generation": 1,
                    "sequence": self.pulses + 1,
                },
            }
            return {"frame": self.last_frame, "pose": {}}

    scanner = Scanner()

    class Localizer:
        references = [reference]
        model = {"overlap_links": []}

        def __init__(self):
            self.lens = lens

        def target_reference(self, _ray):
            return reference

        def locate(self, *_args):
            return {
                "status": "localized",
                "rotation_matrix": np.eye(3).tolist(),
                "reference_id": "reference",
                "analysis_width": 960,
                "validation_matches": 24,
                "validation_p95_pixels": 1.5,
                "inlier_fraction": 0.8,
            }

        def measure_target(self, *_args):
            if scanner.pulses == 0:
                return None
            return {
                "method": "target_patch_correlation",
                "error_pixels": [0.0, 0.0],
                "center_error_pixels": 0.0,
                "analysis_width": 960,
            }

    result = asyncio.run(VisualNavigator(scanner, Localizer()).aim([1, 0.02, 0]))
    assert result["verified"] is True
    assert result["commands"] == 1
    assert result["measurement"]["method"] == "target_patch_correlation"


def test_navigation_without_frame_identity_does_not_probe_a_motor():
    class Scanner:
        last_frame = {"image": np.zeros((32, 32), np.uint8), "received_monotonic": 0}
        checkpoint = {}

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
@pytest.mark.parametrize("native_approach", [False, True])
def test_navigation_converges_or_refuses_unattainable_motor_precision(target_yaw, inverted, continuous, native_approach):
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
            self.angles += response[:, 0 if axis == "pan" else 1] * sign * amount * (_options.get("speed", .1) / .1)
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
        navigator = VisualNavigator(plant, localizer)
        if not native_approach:
            return await navigator.aim(target)
        anchor = basis(target_yaw * .8, 0)[:, 2]
        navigator.locate = AsyncMock(wraps=navigator.locate)

        async def approach(destination, ray):
            # A qualified native destination does not need a new departure
            # localization, but must still localize its observed arrival.
            assert navigator.locate.await_count == 0
            assert destination == {"prepared": True}
            assert np.allclose(ray, anchor)
            plant.angles = np.array([target_yaw * .8, 0])
            navigator.commands += 1
            await plant._reference_window()
            return await navigator.locate()

        navigator.approach_reference = AsyncMock(side_effect=approach)
        try:
            return await navigator.aim(target, native_destination={"prepared": True}, native_ray=anchor)
        finally:
            navigator.approach_reference.assert_awaited_once()
            assert navigator.locate.await_count > 0

    if continuous:
        # A high fixed speed may still exceed the accepted image-space arrival
        # tolerance. It must either arrive from fresh pixels or refuse the move.
        try:
            result = asyncio.run(run())
        except PanoramaCaptureError as error:
            assert error.code == "visual_control_resolution_unverified"
        else:
            assert result["verified"]
            assert result["measurement"]["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS
        assert all(abs(amount) >= .05 for _, amount in plant.commands)
        assert len(plant.commands) <= 64
        return
    result = asyncio.run(run())
    assert result["verified"]
    assert result["measurement"]["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS
    assert 0 < len(plant.commands) <= 16
    assert max(abs(command[1]) for command in plant.commands) <= MAXIMUM_NAVIGATION_PULSE_SECONDS
    assert plant.angles == pytest.approx(
        [target_yaw, 0.05], abs=MAXIMUM_LIVE_CENTER_ERROR_PIXELS / 650 * 1.1
    )


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
        checkpoint={},
        _check=lambda: None,
        last_frame={'image': np.zeros((20, 20), np.uint8), 'received_monotonic': time.monotonic()},
    )
    navigator = VisualNavigator(scanner, SimpleNamespace(locate=lambda *_: {'status': 'localized', 'rotation_matrix': current.tolist()}))
    navigator.response = {'pan': np.array([1., 0.]), 'tilt': np.array([0., 1.])}
    navigator.response_rotations = {'pan': np.eye(3), 'tilt': current.copy()}
    asyncio.run(navigator.locate())
    assert set(navigator.response) == {'tilt'}
    assert set(navigator.response_rotations) == {'tilt'}
    assert scanner.checkpoint['navigation_response_invalidations'] == [{
        'commands': 0, 'axis': 'pan', 'reason': 'view_rotation_exceeded',
        'angle_degrees': pytest.approx(np.degrees(.3)),
    }]


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
    scanner = SimpleNamespace(_check=lambda: None, last_frame=None, checkpoint={})

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


@pytest.mark.parametrize('verification', ['fresh', 'expired', 'ambiguous', 'unlocalized'])
@pytest.mark.parametrize('physical_state', ['stopped', 'unknown'])
def test_last_slow_recognition_gets_one_fresh_verification_without_using_old_pose(monkeypatch, verification, physical_state):
    from toposync_ext_cameras import panorama_navigation as navigation

    clock, calls = [10.0], []
    monkeypatch.setattr(navigation.time, 'monotonic', lambda: clock[0])
    scanner = SimpleNamespace(_check=lambda: None, last_frame=None, checkpoint={}, physical_state=physical_state)

    async def frame():
        return {'image': np.zeros((20, 20), np.uint8), 'received_monotonic': clock[0],
                'capture_evidence': {'sequence': len(calls) + 1}}

    def locate(image, evidence):
        calls.append(evidence['sequence'])
        if len(calls) <= 2 or (len(calls) == 4 and verification == 'unlocalized'):
            return {'status': 'unlocalized', 'reason': 'panorama_visual_localization_failed'}
        if len(calls) == 4 and verification == 'ambiguous':
            return {'status': 'ambiguous', 'reason': 'panorama_visual_localization_ambiguous'}
        if len(calls) == 3 or verification == 'expired':
            clock[0] += 2.0
        return {'status': 'localized', 'rotation_matrix': np.eye(3).tolist(),
                'verified_sequence': evidence['sequence']}

    scanner._reference_window = AsyncMock(side_effect=frame)
    scanner.refresh_stopped_frame = AsyncMock(side_effect=frame)
    navigator = VisualNavigator(scanner, SimpleNamespace(locate=locate))

    async def run():
        scanner.last_frame = await frame()
        if verification == 'fresh':
            result = await navigator.locate()
            assert result['verified_sequence'] == 4
            assert scanner.last_frame['received_monotonic'] == clock[0]
        else:
            reason = {'expired': 'panorama_frame_not_recent', 'ambiguous': 'panorama_visual_localization_ambiguous',
                      'unlocalized': 'panorama_visual_localization_failed'}[verification]
            with pytest.raises(PanoramaCaptureError, match=reason):
                await navigator.locate()

    asyncio.run(run())
    assert calls == [1, 2, 3, 4]
    assert navigator.commands == 0
    assert scanner.refresh_stopped_frame.await_count == (1 if physical_state == 'stopped' else 0)
    assert scanner._reference_window.await_count == (2 if physical_state == 'stopped' else 3)


def test_expired_localization_does_not_hide_endpoint_refresh_failure():
    from toposync_ext_cameras.panorama_navigation import _ExpiredQualifiedFrame

    scanner = SimpleNamespace(
        checkpoint={}, physical_state='stopped',
        refresh_stopped_frame=AsyncMock(side_effect=PanoramaCaptureError('stop_unconfirmed')),
        _reference_window=AsyncMock(),
    )
    navigator = VisualNavigator(scanner, None)
    navigator._locate_current = AsyncMock(side_effect=_ExpiredQualifiedFrame())
    with pytest.raises(PanoramaCaptureError, match='stop_unconfirmed'):
        asyncio.run(navigator.locate())
    scanner._reference_window.assert_not_awaited()
    navigator._locate_current.assert_awaited_once()
    assert navigator.commands == 0


@pytest.mark.parametrize("freshness", ["already_expired", "expires_during_lookup", "still_fresh"])
def test_cached_localization_cannot_refresh_an_expired_navigation_frame(tmp_path, monkeypatch, freshness):
    from toposync_ext_cameras import panorama_navigation as navigation

    image = np.zeros((540, 960), np.uint8)
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    localizer = PanoramaLocalizer(
        {"lens": {"width": 960, "height": 540, "fx": 650, "fy": 650, "cx": 479.5, "cy": 269.5},
         "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    evidence = {"capture_instance": "first", "generation": 1, "sequence": 1}
    monkeypatch.setattr(localizer, "_locate", lambda *_: {
        "status": "localized", "rotation_matrix": np.eye(3).tolist(),
    })
    localizer.locate(image, evidence)

    def unexpected_recognition(*args, **kwargs):
        pytest.fail("The repeated image should use its cached decision")

    monkeypatch.setattr(localizer, "_locate", unexpected_recognition)
    clock = [10.0]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    frame = {"image": image, "capture_evidence": evidence, "received_monotonic": clock[0]}
    references = []

    async def reference_window():
        references.append(True)
        return frame  # A stalled source must never make old pixels current.

    scanner = SimpleNamespace(_check=lambda: None, last_frame=frame, _reference_window=reference_window, checkpoint={})
    navigator = VisualNavigator(scanner, localizer)
    cached_lookup = localizer.locate_diagnostic

    def lookup(*args):
        if freshness == "expires_during_lookup":
            clock[0] += 2
        return cached_lookup(*args)

    monkeypatch.setattr(localizer, "locate_diagnostic", lookup)
    if freshness == "already_expired":
        clock[0] += 2
    elif freshness == "still_fresh":
        clock[0] += .7
        assert asyncio.run(navigator.locate())["status"] == "localized"
        assert not references, "A fresh cached decision must not trigger another stationary window"
        assert frame["received_monotonic"] == 10.0
        assert navigator.commands == 0
        return
    with pytest.raises(PanoramaCaptureError, match="panorama_frame_not_recent"):
        asyncio.run(navigator.locate())
    assert references
    assert frame["received_monotonic"] == 10.0
    assert navigator.commands == 0


@pytest.mark.parametrize("case", ["fresh", "old_position", "missing_position", "expired_image"])
def test_localization_keeps_raw_pose_lineage_without_authorizing_calibration(monkeypatch, case):
    from toposync_ext_cameras import panorama_navigation as navigation

    clock = [10.0]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    evidence = {"capture_instance": "current", "generation": 2, "sequence": 7}
    pose = {"native_pan": 905.0, "native_tilt": None,
            "observed_monotonic": 2.0 if case == "old_position" else 9.9,
            "position_provenance": {"native": {"source": "device", "units": "device_native",
                                               "started_monotonic": 9.8, "observed_monotonic": 9.9}}}
    scanner = SimpleNamespace(_check=lambda: None, checkpoint={}, physical_state="stopped",
                              last_pose=None if case == "missing_position" else pose,
                              last_frame={"image": np.zeros((32, 32), np.uint8),
                                          "received_monotonic": 9.8, "capture_evidence": evidence},
                              camera=SimpleNamespace(position=AsyncMock()), _reference_window=AsyncMock())
    rotation = np.eye(3).tolist()

    def locate(*_args):
        if case == "expired_image":
            clock[0] = 12.0
        return {"status": "localized", "rotation_matrix": rotation}

    navigator = VisualNavigator(scanner, SimpleNamespace(locate=locate))
    if case == "expired_image":
        with pytest.raises(PanoramaCaptureError, match="panorama_frame_not_recent"):
            asyncio.run(navigator._locate_current())
    else:
        assert asyncio.run(navigator._locate_current())["status"] == "localized"
    record = scanner.checkpoint["navigation_observation_timings"][-1]
    observed = record["observation"]
    assert observed["calibrated_pair"] is False
    assert observed["rotation_matrix"] == rotation
    assert observed["capture_evidence"] == evidence
    assert observed["frame_received_monotonic"] == 9.8
    assert record["final_frame_age_seconds"] == pytest.approx(clock[0] - 9.8)
    assert observed["position"] == ({} if case == "missing_position" else pose)
    # Subsequent mutation must not rewrite historical provenance.
    evidence["sequence"] = 8
    rotation[0][0] = 0
    pose["position_provenance"]["native"]["observed_monotonic"] = 20
    assert observed["capture_evidence"]["sequence"] == 7
    assert observed["rotation_matrix"][0][0] == 1
    if case != "missing_position":
        assert observed["position"]["position_provenance"]["native"]["observed_monotonic"] == 9.9
    scanner.camera.position.assert_not_awaited()
    scanner._reference_window.assert_not_awaited()
    assert navigator.commands == 0
    for _ in range(120):
        navigator._record_timing("sample", clock[0])
    assert len(scanner.checkpoint["navigation_observation_timings"]) == 96


@pytest.mark.parametrize("failure", [None, "refresh", "repeated_expiry"])
def test_expired_target_measurement_renews_stopped_observation_without_moving(failure):
    import time
    from toposync_ext_cameras.panorama_navigation import _ExpiredQualifiedFrame

    frame = {"received_monotonic": time.monotonic(), "capture_evidence": {"sequence": 2}}
    scanner = SimpleNamespace(_check=lambda: None, checkpoint={}, physical_state="stopped",
                              last_frame=frame, _persist=AsyncMock(),
                              refresh_stopped_frame=AsyncMock(return_value=frame),
                              _reference_window=AsyncMock(side_effect=AssertionError("Unnecessary full window")))
    localizer = SimpleNamespace(lens={"width": 960, "height": 540, "fx": 650, "fy": 650,
                                     "cx": 479.5, "cy": 269.5},
                                references=[{"id": "reference", "rotation_matrix": np.eye(3).tolist()}],
                                model={"overlap_links": []},
                                target_reference=lambda _: {"id": "reference"})
    navigator = VisualNavigator(scanner, localizer)
    navigator.locate = AsyncMock(return_value={"status": "localized", "reference_id": "reference",
                                              "rotation_matrix": np.eye(3).tolist()})
    navigator._pulse = AsyncMock(side_effect=AssertionError("Expiry cannot move the camera"))
    measurement = {"center_error_pixels": 1}
    navigator._target_measurement = AsyncMock(side_effect=[_ExpiredQualifiedFrame(), measurement])
    if failure == "refresh":
        scanner.refresh_stopped_frame.side_effect = PanoramaCaptureError("stop_observation_unconfirmed")
    elif failure == "repeated_expiry":
        navigator._target_measurement.side_effect = [_ExpiredQualifiedFrame() for _ in range(3)]
    if failure:
        with pytest.raises(PanoramaCaptureError, match="stop_observation_unconfirmed" if failure == "refresh" else "panorama_frame_not_recent"):
            asyncio.run(navigator.aim([1, 0, 0]))
    else:
        result = asyncio.run(navigator.aim([1, 0, 0]))
        assert result["verified"] and result["measurement"] == measurement
    assert scanner.refresh_stopped_frame.await_count == (2 if failure == "repeated_expiry" else 1)
    scanner._reference_window.assert_not_awaited()
    navigator._pulse.assert_not_awaited()
    assert navigator.commands == 0


def test_live_navigation_uses_existing_fine_pulses_before_acceleration_can_overshoot():
    """Replay the measured weak-to-strong gain and swallowed reversal near arrival."""
    from toposync_ext_cameras.panorama_scan import DEFAULT_CONTINUOUS_PULSE_SPEED
    import time
    from types import SimpleNamespace

    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    scanner = SimpleNamespace(
        capabilities={"velocity_supported": True, "axes": {"pan": True, "tilt": True}},
        checkpoint={}, last_frame={"capture_evidence": {}, "received_monotonic": time.monotonic()},
    )
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []},
                                target_reference=lambda _: reference)

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer, maximum_commands=16)
            self.error = np.array([.011, .067])
            self.response = {"tilt": np.array([0., .03944])}
            self.pulses = []
            self.direction = -1

        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": "reference"}

        def _error(self, *_):
            return self.error.copy()

        async def _target_measurement(self, *_):
            error = np.tan(self.error) * 560
            return {"error_pixels": error.tolist(), "center_error_pixels": float(np.linalg.norm(error)), "analysis_width": 960}

        async def _pulse(self, axis, amount, *, speed=DEFAULT_CONTINUOUS_PULSE_SPEED):
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})
            self.pulses.append((amount, speed))
            direction = np.sign(amount)
            if direction == self.direction:
                self.error[1] += .147 * amount * speed / DEFAULT_CONTINUOUS_PULSE_SPEED
            self.direction = direction

    navigator = Navigator()
    result = asyncio.run(navigator.aim([1, 0, 0]))
    assert result["verified"] is True
    assert result["measurement"]["center_error_pixels"] <= 12
    assert 1 <= navigator.commands <= 4
    assert all(.05 <= abs(amount) <= .15 for amount, _ in navigator.pulses)


@pytest.mark.parametrize("change", [None, "probe", "reversal", "target", "support", "gain", "axis", "nonfinite"])
def test_fine_correction_requires_two_consistent_qualified_coarse_movements(change):
    target = np.array([1., 0., 0.])
    trace = [{"state": "observed", "kind": "correction", "axis": "pan", "amount": amount,
              "speed": .1, "next_pulse_limit": 1.2, "target_ray": target.tolist(),
              "response": response}
             for amount, response in [(.6, [-.1776, -.0168]), (.9859, [-.1678, -.0088])]]
    if change == "probe":
        trace[0]["kind"] = "probe"
    elif change == "reversal":
        trace[0]["amount"] *= -1
    elif change == "target":
        trace[0]["target_ray"] = [0., 1., 0.]
    elif change == "support":
        trace[0]["next_pulse_limit"] = .6
    elif change == "gain":
        trace[0]["response"] = [-.04, 0.]
    elif change == "axis":
        trace[0]["axis"] = "tilt"
    elif change == "nonfinite":
        trace[0]["response"] = [float("nan"), 0.]
    assert _fine_correction_has_measured_response(
        "pan", .23, np.array([.0495, .0557]), target, trace,
    ) is (change is None)
    assert not _fine_correction_has_measured_response("pan", .7, np.array([.05, .06]), target, trace)
    assert not _fine_correction_has_measured_response("pan", .23, np.array([-.05, .06]), target, trace)
    assert not _fine_correction_has_measured_response("pan", .02, np.array([.05, .06]), target, trace)


def test_live_navigation_does_not_split_consistent_measured_correction():
    import time
    from toposync_ext_cameras.panorama_scan import DEFAULT_CONTINUOUS_PULSE_SPEED

    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    scanner = SimpleNamespace(
        capabilities={"velocity_supported": True, "axes": {"pan": True, "tilt": True}},
        checkpoint={}, last_frame={"capture_evidence": {}, "received_monotonic": time.monotonic()},
    )
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []},
                                target_reference=lambda _: reference)

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer, maximum_commands=16)
            self.error = np.array([.05, .01])
            self.response = {"pan": np.array([-.17, 0.])}
            self.trace = [{"state": "observed", "kind": "correction", "axis": "pan", "amount": amount,
                           "speed": .1, "next_pulse_limit": 1.2, "target_ray": [1, 0, 0],
                           "response": [-.17, 0.]}
                          for amount in [.6, 1.2]]
            self.pulses = []

        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": "reference"}

        def _error(self, *_):
            return self.error.copy()

        async def _target_measurement(self, *_):
            error = np.tan(self.error) * 560
            return {"error_pixels": error.tolist(), "center_error_pixels": float(np.linalg.norm(error)), "analysis_width": 960}

        async def _pulse(self, axis, amount, *, speed=DEFAULT_CONTINUOUS_PULSE_SPEED):
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})
            self.pulses.append((amount, speed))
            self.error[0] -= .17 * amount

    navigator = Navigator()
    result = asyncio.run(navigator.aim([1, 0, 0]))
    assert result["verified"] is True
    assert result["measurement"]["center_error_pixels"] <= 12
    assert navigator.commands == 1
    amount, speed = navigator.pulses[0]
    assert .15 < amount <= MAXIMUM_NAVIGATION_PULSE_SECONDS
    assert speed == DEFAULT_CONTINUOUS_PULSE_SPEED


def test_live_navigation_prioritizes_arrival_error_over_motor_duration():
    """Replay the final two-axis choice measured in the daylight pilot."""
    import time
    from toposync_ext_cameras.panorama_scan import DEFAULT_CONTINUOUS_PULSE_SPEED

    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    scanner = SimpleNamespace(
        capabilities={"velocity_supported": True, "axes": {"pan": True, "tilt": True}},
        checkpoint={}, last_frame={"capture_evidence": {}, "received_monotonic": time.monotonic()},
    )
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []},
                                target_reference=lambda _: reference)

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer, maximum_commands=16)
            self.error = np.array([.01751047, .02279382])
            self.response = {"pan": np.array([-.13907444, -.00215937]),
                             "tilt": np.array([-.00201988, .18715083])}
            self.pulses = []

        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": "reference"}

        def _error(self, *_):
            return self.error.copy()

        async def _target_measurement(self, *_):
            error = np.tan(self.error) * 560
            return {"error_pixels": error.tolist(), "center_error_pixels": float(np.linalg.norm(error)), "analysis_width": 960}

        async def _pulse(self, axis, amount, *, speed=DEFAULT_CONTINUOUS_PULSE_SPEED):
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})
            self.pulses.append(axis)
            self.error += self.response[axis] * amount

    navigator = Navigator()
    result = asyncio.run(navigator.aim([1, 0, 0]))
    assert result["verified"] is True
    assert result["measurement"]["center_error_pixels"] <= 12
    assert navigator.pulses == ["tilt"]


@pytest.mark.parametrize('stationary_verified,second_pulse_effective', [(True, True), (True, False), (False, False)])
def test_live_probe_escalates_once_only_after_qualified_no_effect(stationary_verified, second_pulse_effective):
    from types import SimpleNamespace
    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    scanner = SimpleNamespace(capabilities={"velocity_supported": True, "axes": {"pan": True, "tilt": True}},
                              checkpoint={}, last_frame={"capture_evidence": {}})
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []}, target_reference=lambda _: reference)

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer, maximum_commands=16)
            self.error = np.array([0., -.025])
            self.pulses = []

        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": "reference"}

        def _error(self, *_):
            return self.error.copy()

        async def _target_measurement(self, *_):
            error = np.tan(self.error) * 560
            return {"error_pixels": error.tolist(), "center_error_pixels": float(np.linalg.norm(error)), "analysis_width": 960}

        async def _pulse(self, axis, amount):
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})
            self.pulses.append(amount)
            self.last_pulse_stationary = stationary_verified
            if self.commands == 2 and second_pulse_effective:
                self.error[1] += .012
                self.last_pulse_stationary = False

    navigator = Navigator()
    if second_pulse_effective:
        assert asyncio.run(navigator.aim([1, 0, 0]))['measurement']['center_error_pixels'] <= 12
    else:
        with pytest.raises(PanoramaCaptureError, match='visual_response_unavailable'):
            asyncio.run(navigator.aim([1, 0, 0]))
    assert navigator.pulses == ([.12, .24] if stationary_verified else [.12])


def test_navigation_leaves_overlap_route_as_soon_as_requested_target_is_visible(monkeypatch):
    import toposync_ext_cameras.panorama_navigation as navigation
    from types import SimpleNamespace
    references = [{"id": name, "rotation_matrix": np.eye(3).tolist()} for name in ['start', 'middle', 'end']]
    scanner = SimpleNamespace(capabilities={"relative_supported": True, "axes": {"pan": True}},
                              checkpoint={}, last_frame={"capture_evidence": {}})
    localizer = SimpleNamespace(lens={"width": 960, "height": 540, "fx": 560, "fy": 560}, references=references,
                                model={"overlap_links": [['start', 'middle'], ['middle', 'end']]},
                                target_reference=lambda _: references[-1])
    target = np.array([0., 1., 0.])

    class Navigator(VisualNavigator):
        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": 'start' if not self.commands else 'middle'}

        def _error(self, *_):
            return np.array([.2 - .03 * self.commands, 0.])

        async def _target_measurement(self, measured_target, *_):
            assert np.array_equal(measured_target, target)
            return {"error_pixels": [0., 0.], "center_error_pixels": 0., "analysis_width": 960}

        async def _pulse(self, axis, amount):
            assert self.commands == 0, 'No further intermediate target may execute after acquiring the real target'
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})

    navigator = Navigator(scanner, localizer)
    monkeypatch.setattr(navigation, 'ray_to_image_pixel', lambda ray, *_args, **_kwargs:
                        None if np.array_equal(ray, target) and not navigator.commands else (480, 270))
    result = asyncio.run(navigator.aim(target))
    assert result['verified'] and result['commands'] == 1


@pytest.mark.parametrize('initial_error,first_axis', [([-.6, .12], 'pan'), ([.02, .6], 'tilt')])
def test_navigation_defers_unknown_axis_while_known_response_makes_progress(initial_error, first_axis):
    """A stale tilt gain must not force repeated tilt probes during a long pan."""
    lens = {'width': 960, 'height': 540, 'fx': 560, 'fy': 560, 'cx': 479.5, 'cy': 269.5}
    reference = {'id': 'reference', 'rotation_matrix': np.eye(3).tolist()}
    scanner = SimpleNamespace(capabilities={'relative_supported': True, 'axes': {'pan': True, 'tilt': True}},
                              checkpoint={}, last_frame={'capture_evidence': {}})
    localizer = SimpleNamespace(lens=lens, references=[reference], model={'overlap_links': []},
                                target_reference=lambda _: reference)

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer, maximum_commands=16)
            self.error = np.array(initial_error)
            self.response = {'pan': np.array([-.2, 0.])}

        async def locate(self):
            return {'rotation_matrix': np.eye(3).tolist(), 'reference_id': 'reference'}

        def _error(self, *_):
            return self.error.copy()

        async def _target_measurement(self, *_):
            pixels = np.tan(self.error) * 560
            return {'error_pixels': pixels.tolist(), 'center_error_pixels': float(np.linalg.norm(pixels)), 'analysis_width': 960}

        async def _pulse(self, axis, amount):
            assert self.commands < self.maximum_commands
            self.commands += 1
            self.trace.append({'axis': axis, 'amount': amount})
            self.error += np.array([-.2, 0.] if axis == 'pan' else [0., .24]) * amount

    navigator = Navigator()
    result = asyncio.run(navigator.aim([1., 0., 0.]))
    assert navigator.trace[0]['axis'] == first_axis
    assert any(entry['axis'] == 'tilt' for entry in navigator.trace)
    assert result['verified'] and result['measurement']['center_error_pixels'] <= 12


def _reference_probe_fixture():
    rotations = [cv2.Rodrigues(np.array([-angle, 0., 0.]))[0].tolist() for angle in (0., .04, .08)]
    photos = [{"id": str(i), "capture_instance": "original", "generation": 1, "sequence": 10+i*10,
               "region_command_id": f"region-{i}" if i else None,
               "control_command_id": f"control-{i}" if i else None} for i in range(3)]
    commands = [{"id": f"region-{i}", "state": "observed", "returning": False, "outcome": "capture_stable",
                 "baseline": {"capture_instance": "original", "generation": 1, "sequence": 5+i*10}}
                for i in (1, 2)]
    attempts = [{"outcome": "capture_stable", "requested_velocity": {"pan": 0., "tilt": -.1},
                 "command_receipt": {"command_id": f"control-{i}", "command_kind": "continuous_move", "accepted": True, "stale_after_execution": False},
                 "correction_precondition": {"verified": True, "overlap": .99, "displacement": .1}}
                for i in (1, 2)]
    model = {"captures": [{"id": str(i), "rotation_matrix": rotation} for i, rotation in enumerate(rotations)]}
    return model, photos, commands, attempts


@pytest.mark.parametrize("invalid", [None, "precondition", "receipt", "generation", "intervening", "two_axes", "duplicate", "disagreement", "nonfinite"])
def test_reference_probe_direction_requires_two_causal_nearby_commands(invalid):
    from toposync_ext_cameras.panorama_navigation import reference_probe_observations, reference_probe_direction
    model, photos, commands, attempts = _reference_probe_fixture()
    if invalid == "precondition":
        attempts[0]["correction_precondition"]["verified"] = False
    elif invalid == "receipt":
        attempts[0]["command_receipt"]["stale_after_execution"] = True
    elif invalid == "generation":
        photos[0]["generation"] = 2
    elif invalid == "intervening":
        commands.insert(0, {"id": "unobserved"})
    elif invalid == "two_axes":
        attempts[0]["requested_velocity"]["pan"] = .1
    elif invalid == "duplicate":
        photos[2]["control_command_id"] = "control-1"
        attempts[1]["command_receipt"]["command_id"] = "control-1"
    elif invalid == "disagreement":
        model["captures"][2]["rotation_matrix"] = cv2.Rodrigues(np.array([.08, 0., 0.]))[0].tolist()
    elif invalid == "nonfinite":
        attempts[0]["correction_precondition"]["overlap"] = float("nan")
    observations = reference_probe_observations(model, photos, commands, attempts)
    from toposync_ext_cameras.processing.panorama_mapping import _rotation_basis
    basis = _rotation_basis(np.eye(3))
    target = basis @ np.array([0., .05, 1.])
    assert reference_probe_direction("tilt", target, np.eye(3), observations) == (-1 if invalid is None else None)
    if invalid is None:
        assert reference_probe_direction("tilt", basis @ np.array([0., -.05, 1.]), np.eye(3), observations) == 1
        far = cv2.Rodrigues(np.array([0., .5, 0.]))[0]
        assert reference_probe_direction("tilt", _rotation_basis(far) @ np.array([0., .05, 1.]), far, observations) is None
        assert reference_probe_direction("pan", target, np.eye(3), observations) is None
        assert reference_probe_direction("tilt", basis @ np.array([.05, 0., 1.]), np.eye(3), observations) is None


def test_reference_direction_only_selects_short_probe_and_arrival_is_measured():
    from toposync_ext_cameras.panorama_navigation import reference_probe_observations
    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    scanner = SimpleNamespace(capabilities={"velocity_supported": True, "axes": {"tilt": True}},
                              checkpoint={}, last_frame={"capture_evidence": {}})
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []},
                                target_reference=lambda _: reference,
                                probe_observations=reference_probe_observations(*_reference_probe_fixture()))

    class Navigator(VisualNavigator):
        def __init__(self):
            super().__init__(scanner, localizer)
            self.rotation = np.eye(3)
            self.pulses = []

        async def locate(self):
            return {"rotation_matrix": self.rotation.tolist(), "reference_id": "reference"}

        async def _target_measurement(self, target, located):
            error = np.tan(self._error(target, located)) * 560
            return {"error_pixels": error.tolist(), "center_error_pixels": float(np.linalg.norm(error)), "analysis_width": 960}

        async def _pulse(self, axis, amount):
            assert not self.response, 'Historical direction must not install an old motor gain'
            self.commands += 1
            self.trace.append({"axis": axis, "amount": amount})
            self.pulses.append((axis, amount))
            self.rotation = self.rotation @ cv2.Rodrigues(np.array([amount*.2, 0., 0.]))[0]

    navigator = Navigator()
    from toposync_ext_cameras.processing.panorama_mapping import _rotation_basis
    result = asyncio.run(navigator.aim(_rotation_basis(np.eye(3)) @ np.array([0., .03, 1.])))
    assert navigator.pulses == [("tilt", -.12)]
    assert result["verified"] and result["measurement"]["center_error_pixels"] <= 12
    assert scanner.checkpoint["navigation_step"]["reference_probe_direction"] == -1


@pytest.mark.parametrize("recovers", [True, False])
def test_expiring_target_measurement_reobserves_before_arrival_without_motion(monkeypatch, recovers):
    from toposync_ext_cameras import panorama_navigation as navigation
    clock = [10.0]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    lens = {"width": 960, "height": 540, "fx": 560, "fy": 560, "cx": 479.5, "cy": 269.5}
    reference = {"id": "reference", "rotation_matrix": np.eye(3).tolist()}
    calls, windows = [], []

    def frame():
        return {"image": np.zeros((2, 2), np.uint8), "received_monotonic": clock[0],
                "capture_evidence": {"sequence": len(windows) + 1}}

    async def observe():
        windows.append(clock[0])
        return frame()

    def measure(*_):
        calls.append(clock[0])
        if not recovers or len(calls) == 1:
            clock[0] += 1.1
        return {"center_error_pixels": 0, "error_pixels": [0, 0], "analysis_width": 960}

    scanner = SimpleNamespace(checkpoint={}, last_frame=frame(), _reference_window=observe)
    localizer = SimpleNamespace(lens=lens, references=[reference], model={"overlap_links": []},
                                target_reference=lambda _: reference, measure_target=measure)

    class Navigator(VisualNavigator):
        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist(), "reference_id": "reference"}

        async def _pulse(self, *_args, **_kwargs):
            pytest.fail("An expired measurement must not dispatch a movement")

    navigator = Navigator(scanner, localizer)
    if recovers:
        result = asyncio.run(navigator.aim([1, 0, 0]))
        assert result["verified"] and result["capture_evidence"]["sequence"] == 2
        assert len(windows) == 1
    else:
        with pytest.raises(PanoramaCaptureError, match="panorama_frame_not_recent"):
            asyncio.run(navigator.aim([1, 0, 0]))
        assert len(windows) == 2
    assert navigator.commands == 0
