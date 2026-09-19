"""Acquisition orchestration tests use local frames and never actuate hardware."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from toposync_ext_cameras import panorama_scan as scan
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError


class SimulatedCamera:
    """A textured view, observable movement, and independent command history."""

    camera_id = "camera"
    source_id = "wide"
    owner_id = "scan-job"
    optical_scale = 100

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.capture_instance = "simulated-camera"
        self.now = 100.0
        self.sequence = 0
        self.pan = 0.0
        self.tilt = 0.0
        self.target = (0.0, 0.0)
        self.remaining_motion_frames = 0
        self.lease: dict[str, Any] | None = None
        self.discover_error: str | None = None
        self.capabilities = {
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "source_identity": {
                "camera_id": self.camera_id,
                "source_id": self.source_id,
                "profile_token": "wide-profile",
                "width": 320,
                "height": 240,
                "device": {"manufacturer": "Synthetic", "model": "PTZ"},
            },
            "absolute_supported": True,
            "continuous": True,
            "limits": {
                "pan": {"min": -0.2, "max": 0.2},
                "tilt": {"min": -0.1, "max": 0.1},
            },
            "position": self._position(),
            "motion_automation": {"auto_tracking": False, "automatic_return": False},
        }
        generator = np.random.default_rng(50812)
        texture = generator.integers(25, 230, (240, 320), np.uint8)
        self.texture = cv2.GaussianBlur(texture, (3, 3), 0.6)
        self.blank = False
        self.return_offset = 0.0
        self.ignore_return_corrections = False

    def _position(self) -> dict[str, Any]:
        return {
            "pan": self.pan,
            "tilt": self.tilt,
            "zoom": None,
            "native_pan": None,
            "native_tilt": None,
            "move_status": "MOVING" if self.remaining_motion_frames else "IDLE",
            "error": "",
            "observed_monotonic": self.now,
        }

    async def discover(self) -> dict[str, Any]:
        self.events.append(("discover", None))
        if self.discover_error:
            raise PanoramaCaptureError(self.discover_error)
        return copy.deepcopy(self.capabilities)

    async def acquire(self) -> dict[str, Any]:
        self.events.append(("acquire", None))
        self.lease = {"lease_id": "lease", "fence": 1}
        return self.lease

    async def position(self) -> dict[str, Any]:
        return self._position()

    async def frame(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        # A frame acquisition yields to the concurrently submitted PTZ command.
        await asyncio.sleep(0)
        self.sequence += 1
        self.now += 0.1
        if self.remaining_motion_frames:
            self.pan += (self.target[0] - self.pan) / self.remaining_motion_frames
            self.tilt += (self.target[1] - self.tilt) / self.remaining_motion_frames
            self.remaining_motion_frames -= 1
        transform = np.array(
            [[1, 0, self.pan * self.optical_scale], [0, 1, self.tilt * self.optical_scale]],
            dtype=np.float32,
        )
        image = cv2.warpAffine(self.texture, transform, (320, 240), borderMode=cv2.BORDER_REFLECT)
        if self.blank:
            image[:] = 127
        else:
            # Real static video still has sensor noise; bit-identical frames
            # would exercise the detector's intentional frozen-feed rejection.
            noise = np.random.default_rng(self.sequence).normal(0, 0.5, image.shape)
            image = np.clip(image.astype(np.float64) + noise, 0, 255).astype(np.uint8)
        return {
            "image": cv2.cvtColor(image, cv2.COLOR_GRAY2BGR),
            "capture_instance": self.capture_instance,
            "sequence": self.sequence,
            "generation": 1,
            "media_time": self.sequence * 0.1,
            "received_monotonic": self.now,
            "published_at": self.now,
            "captured_monotonic": None,
            "physical_timestamp_verified": False,
            "width": 320,
            "height": 240,
            "transport": "configured",
        }

    async def move_absolute(self, *, pan: float, tilt: float) -> dict[str, Any]:
        self.events.append(("absolute", {"pan": pan, "tilt": tilt}))
        if self.ignore_return_corrections and "return" in _event_names(self):
            pan, tilt = self.return_offset, 0.0
        self.target = (pan, tilt)
        self.remaining_motion_frames = 4
        return {"accepted": True}

    async def move_velocity(
        self, *, pan: float = 0.0, tilt: float = 0.0, timeout_s: float = 0.5
    ) -> dict[str, Any]:
        self.events.append(("velocity", {"pan": pan, "tilt": tilt, "timeout_s": timeout_s}))
        self.target = (
            float(np.clip(self.pan + pan * timeout_s, -0.2, 0.2)),
            float(np.clip(self.tilt + tilt * timeout_s, -0.1, 0.1)),
        )
        self.remaining_motion_frames = 4
        return {"accepted": True}

    async def stop(self) -> dict[str, Any]:
        self.events.append(("stop", None))
        self.remaining_motion_frames = 0
        self.target = (self.pan, self.tilt)
        return {"accepted": True}

    async def save_return(self, role="original", *, before_create=None) -> dict[str, Any]:
        self.events.append(("save_return", role))
        return {"kind": "absolute", "role": role, "binding": {"profile_token": "simulated"}, "pan": self.pan, "tilt": self.tilt, "zoom": None}

    async def return_to(self, saved: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("return", copy.deepcopy(saved)))
        return await self.move_absolute(pan=saved["pan"] + self.return_offset, tilt=saved["tilt"])

    async def remove_return(self, saved: dict[str, Any]) -> None:
        self.events.append(("remove_return", copy.deepcopy(saved)))

    async def release(self) -> None:
        self.events.append(("release", None))
        self.lease = None

    async def close(self) -> None:
        await self.release()


def _event_names(camera: SimulatedCamera) -> list[str]:
    return [event[0] for event in camera.events]


async def _progress(_value: dict[str, Any]) -> None:
    pass


def _closed_pilot_cycle(**changes) -> dict[str, Any]:
    return {
        "visually_closed": True,
        "match_verified": True,
        "displacement": 3.0,
        "overlap": 0.85,
        **changes,
    }


def _translation_match(
    shift_x: float, shift_y: float, *, width: int = 960, height: int = 720
) -> dict[str, Any]:
    homography = np.array(
        [[1.0, 0.0, shift_x], [0.0, 1.0, shift_y], [0.0, 0.0, 1.0]]
    )
    overlap = scan._homography_overlap(homography, width=width, height=height)
    return {
        "verified": True,
        "shift_x": shift_x,
        "shift_y": shift_y,
        "displacement": math.hypot(shift_x, shift_y),
        "overlap": overlap,
        "homography": homography.tolist(),
        "analysis_size": [width, height],
    }


def _optical_sample(
    device_delta: float,
    shift_x: float,
    shift_y: float,
    *,
    width: int = 960,
    height: int = 540,
) -> dict[str, Any]:
    return {
        **_translation_match(shift_x, shift_y, width=width, height=height),
        "device_delta": device_delta,
        "cycle_closed": True,
    }


def _bidirectional_samples(
    device_delta: float,
    shift_x: float,
    shift_y: float,
    *,
    width: int = 960,
    height: int = 540,
) -> list[dict[str, Any]]:
    return [
        _optical_sample(device_delta, shift_x, shift_y, width=width, height=height),
        _optical_sample(-device_delta, -shift_x, -shift_y, width=width, height=height),
    ]


def _grid_contract_data(camera: SimulatedCamera) -> tuple[dict, dict, list, dict, dict]:
    limits = copy.deepcopy(camera.capabilities["limits"])
    raw_samples = {
        "pan": _bidirectional_samples(0.2, 50.0, 0.0),
        "tilt": _bidirectional_samples(0.1, 0.0, 40.0),
    }
    samples = {}
    for axis, values in raw_samples.items():
        samples[axis] = []
        for index, sample in enumerate(values):
            delta = sample["device_delta"]
            image_delta = [sample["shift_x"], sample["shift_y"]]
            gain_vector = [value / delta for value in image_delta]
            samples[axis].append(
                {
                    **sample,
                    "kind": "pilot_outward" if index == 0 else "pilot_return",
                    "pilot_cycle_id": f"{axis}:1",
                    "image_delta": image_delta,
                    "gain_vector": gain_vector,
                    "motion_gain": float(np.linalg.norm(gain_vector)),
                }
            )
    plan, geometry = scan._absolute_grid_plan(
        limits=limits,
        axis_samples=samples,
        captures_used=0,
        maximum_captures=scan.MAX_CAPTURES,
    )
    steps = {axis: geometry[axis]["step"] for axis in ("pan", "tilt")}
    return limits, samples, plan, geometry, steps


@pytest.mark.parametrize("return_confirmed", [True, False])
def test_control_verification_stops_at_first_unconfirmed_return(tmp_path, monkeypatch, return_confirmed):
    async def scenario():
        camera = SimulatedCamera()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.capabilities = {
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        }
        reference = cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR)
        original = tmp_path / "original.png"
        cv2.imwrite(str(original), reference)
        scanner.checkpoint["initial_path"] = str(original)
        pulses = []
        expected_frames = []

        async def window():
            return {"image": reference.copy()}

        async def pulse(axis, direction, duration, *, expected_frame=None):
            pulses.append((axis, direction, duration))
            expected_frames.append(expected_frame)
            return {"match": {"verified": True, "displacement": 10}, "stationary": False}

        async def restore():
            scanner.last_frame = {"image": reference.copy()}
            scanner.physical_state = "restored" if return_confirmed else "stopped"

        monkeypatch.setattr(scanner, "_reference_window", window)
        monkeypatch.setattr(scanner, "_pulse", pulse)
        monkeypatch.setattr(scanner, "_restore", restore)
        await scanner._verify_control()
        result = scanner.checkpoint["control_verification"]
        if return_confirmed:
            assert result["status"] == "verified" and len(pulses) == 12
            assert pulses == [(axis, direction, 0.12) for _ in range(3)
                              for axis, direction in (("pan", 1), ("pan", -1), ("tilt", 1), ("tilt", -1))]
        else:
            assert result["status"] == "return_unconfirmed"
            assert pulses == [("pan", 1, 0.12)]
        assert all(frame is not None for frame in expected_frames)
        assert not scanner.complete and not scanner.captures

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("comparison", "expected_status", "expected_reason", "expected_pulses"),
    [
        (
            {"verified": True, "overlap": 0.9, "displacement": 3.0},
            "verified",
            None,
            12,
        ),
        (
            {"verified": True, "overlap": 0.9, "displacement": 3.0001},
            "reference_unconfirmed",
            "reference_displacement_exceeded",
            0,
        ),
        (
            {"verified": False, "code": "insufficient_correspondences"},
            "reference_unconfirmed",
            "reference_match_unverified",
            0,
        ),
        (
            {"verified": True, "overlap": 0.8499, "displacement": 1.0},
            "reference_unconfirmed",
            "reference_overlap_insufficient",
            0,
        ),
        (
            {"verified": True, "overlap": float("nan"), "displacement": 1.0},
            "reference_unconfirmed",
            "reference_overlap_unavailable",
            0,
        ),
        (
            {"verified": True, "overlap": 0.9, "displacement": float("inf")},
            "reference_unconfirmed",
            "reference_displacement_unavailable",
            0,
        ),
    ],
)
def test_control_reference_gate_is_persisted_before_any_command(
    tmp_path,
    monkeypatch,
    comparison,
    expected_status,
    expected_reason,
    expected_pulses,
):
    async def scenario():
        camera = SimulatedCamera()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.capabilities = {
            "continuous_supported": True,
            "velocity_supported": True,
            "axes": {"pan": True, "tilt": True},
        }
        reference = cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR)
        original = tmp_path / "original.png"
        cv2.imwrite(str(original), reference)
        scanner.checkpoint["initial_path"] = str(original)
        pulses = []

        def frame():
            camera.sequence += 1
            return {
                "image": reference.copy(),
                "sequence": camera.sequence,
                "generation": 2,
                "received_monotonic": scan.time.monotonic(),
            }

        async def window():
            persisted_before_window = json.loads(
                (tmp_path / "scan-manifest.json").read_text()
            )
            assert persisted_before_window["control_verification"]["checks"][-1][
                "status"
            ] == "reference_pending"
            return frame()

        async def pulse(axis, direction, duration, *, expected_frame=None):
            pulses.append((axis, direction, duration, expected_frame))
            return {"match": {"verified": True, "displacement": 10}, "stationary": False}

        async def restore():
            scanner.last_frame = frame()
            scanner.physical_state = "restored"

        monkeypatch.setattr(scan, "_match", lambda *_: dict(comparison))
        monkeypatch.setattr(scanner, "_reference_window", window)
        monkeypatch.setattr(scanner, "_pulse", pulse)
        monkeypatch.setattr(scanner, "_restore", restore)
        await scanner._verify_control()
        return scanner, pulses

    scanner, pulses = asyncio.run(scenario())
    verification = scanner.checkpoint["control_verification"]
    assert verification["status"] == expected_status
    assert len(pulses) == expected_pulses
    first = verification["checks"][0]
    evidence = first["reference_comparison"]
    assert first["status"] == ("verified" if expected_reason is None else "reference_unconfirmed")
    assert first.get("error") == expected_reason
    assert evidence["reason"] == expected_reason
    assert evidence["frame_sequence"] == 1
    assert evidence["frame_generation"] == 2
    assert evidence["frame_age_seconds"] >= 0
    expected_overlap = comparison.get("overlap")
    expected_displacement = comparison.get("displacement")
    assert evidence["overlap"] == (
        expected_overlap
        if expected_overlap is not None and np.isfinite(expected_overlap)
        else None
    )
    assert evidence["displacement"] == (
        expected_displacement
        if expected_displacement is not None and np.isfinite(expected_displacement)
        else None
    )
    persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert persisted["control_verification"]["checks"][0]["reference_comparison"] == evidence
    if expected_reason is not None:
        assert scanner.physical_state == "stopped"
        assert persisted["physical_state"] == "stopped"
        assert not pulses


def test_control_verification_reconstruction_continues_after_verified_checks(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    reference = cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR)
    original = tmp_path / "original.png"
    cv2.imwrite(str(original), reference)
    checkpoint = json.loads(
        json.dumps(
            {
                "initial_path": str(original),
                "control_verification": {
                    "status": "running",
                    "checks": [
                        {
                            "repetition": 1,
                            "axis": "pan",
                            "direction": 1,
                            "status": "verified",
                        }
                    ],
                    "analysis_width": scan.ANALYSIS_WIDTH,
                    "commands_attempted": 2,
                    "command_budget": scan.MAX_CONTINUOUS_STEPS,
                },
            }
        )
    )
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = {
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": False},
    }
    sequence = 0
    pulses = []

    def frame():
        nonlocal sequence
        sequence += 1
        return {
            "image": reference.copy(),
            "sequence": sequence,
            "generation": 1,
            "received_monotonic": scan.time.monotonic(),
        }

    async def window():
        return frame()

    async def pulse(axis, direction, duration, *, expected_frame=None):
        pulses.append((axis, direction, duration, expected_frame))
        return {"match": {"verified": True, "displacement": 10}, "stationary": False}

    async def restore():
        scanner.last_frame = frame()
        scanner.physical_state = "restored"

    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 0.0,
            "shift_x": 0.0,
            "shift_y": 0.0,
        },
    )
    scanner._reference_window = window
    scanner._pulse = pulse
    scanner._restore = restore

    asyncio.run(scanner._verify_control())

    verification = scanner.checkpoint["control_verification"]
    assert pulses[0][:3] == ("pan", -1, 0.12)
    assert len(pulses) == 5
    assert len(verification["checks"]) == 6
    assert verification["checks"][0] == checkpoint["control_verification"]["checks"][0]
    assert verification["commands_attempted"] == 2
    assert verification["status"] == "verified"


def test_control_verification_reconstruction_never_replays_a_pending_check(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    reference = cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR)
    original = tmp_path / "original.png"
    cv2.imwrite(str(original), reference)
    pending = {
        "repetition": 1,
        "axis": "pan",
        "direction": 1,
        "status": "pending",
    }
    checkpoint = json.loads(
        json.dumps(
            {
                "initial_path": str(original),
                "control_verification": {
                    "status": "running",
                    "checks": [pending],
                    "commands_attempted": 1,
                    "command_budget": scan.MAX_CONTINUOUS_STEPS,
                },
            }
        )
    )
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = {
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": False},
    }
    pulses = []

    async def pulse(*args, **kwargs):
        pulses.append((args, kwargs))

    async def forbidden_window():
        raise AssertionError("A pending persisted check must not be replayed")

    scanner._pulse = pulse
    scanner._reference_window = forbidden_window
    asyncio.run(scanner._verify_control())

    verification = scanner.checkpoint["control_verification"]
    assert pulses == []
    assert verification["checks"] == [pending]
    assert verification["commands_attempted"] == 1
    assert verification["status"] == "resume_unconfirmed"
    assert verification["resume_error"] == "pending_check_not_replayed"


def test_control_verification_uses_one_coarse_recall_per_return_epoch(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras import panorama_navigation

    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    home = initial["image"].copy()
    away = np.roll(home, 6, axis=1)
    original = tmp_path / "original.png"
    cv2.imwrite(str(original), home)
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(original),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
        },
    )
    scanner.acquired = True
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": True,
        "relative_supported": False,
        "axes": {"pan": True, "tilt": False},
    }
    sequence = 0
    at_home = True
    pulses = []
    coarse_epochs = []

    def frame(image):
        nonlocal sequence
        sequence += 1
        return {
            "image": image.copy(),
            "sequence": sequence,
            "generation": 1,
            "received_monotonic": scan.time.monotonic(),
        }

    scanner.last_frame = frame(home)
    scanner.last_pose = camera._position()

    def matching(_reference, candidate):
        restored = np.array_equal(candidate, home)
        return {
            "verified": True,
            "overlap": 1.0,
            "displacement": 0.0 if restored else 6.0,
            "shift_x": 0.0 if restored else 6.0,
            "shift_y": 0.0,
        }

    async def reference_window():
        assert at_home, "A new diagnostic movement requires the previous return"
        scanner.last_frame = frame(home)
        return scanner.last_frame

    async def pulse(axis, direction, duration, *, expected_frame=None):
        nonlocal at_home
        assert at_home
        assert expected_frame is not None
        pulses.append((axis, direction, duration))
        at_home = False
        scanner.last_frame = frame(away)
        scanner.last_pose = camera._position()
        return {
            "frame": scanner.last_frame,
            "pose": scanner.last_pose,
            "match": {
                "verified": True,
                "overlap": 0.95,
                "displacement": 6.0,
                "shift_x": 6.0,
                "shift_y": 0.0,
            },
            "stationary": False,
        }

    async def coarse_move(command, **_options):
        nonlocal at_home
        epoch = scanner.checkpoint["return_epoch"]
        persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
        assert persisted["return_coarse_recalls"][epoch]["state"] == "planned"
        coarse_epochs.append(epoch)
        await command()
        at_home = len(coarse_epochs) == 1
        scanner.last_frame = frame(home if at_home else away)
        scanner.last_pose = camera._position()
        scanner.physical_state = "stopped"
        return {
            "frame": scanner.last_frame,
            "pose": scanner.last_pose,
            "stable": True,
        }

    async def confirm_stop():
        scanner.physical_state = "stopped"

    async def leave_unconfirmed(owner, _reference, result):
        owner.physical_state = "stopped"
        return result

    monkeypatch.setattr(scan, "_match", matching)
    monkeypatch.setattr(panorama_navigation, "correct_reference", leave_unconfirmed)
    scanner._reference_window = reference_window
    scanner._pulse = pulse
    scanner._move = coarse_move
    scanner._confirm_stop = confirm_stop

    asyncio.run(scanner._verify_control())

    verification = scanner.checkpoint["control_verification"]
    assert len(pulses) == 2
    assert verification["status"] == "return_unconfirmed"
    assert verification["checks"][0]["status"] == "verified"
    assert verification["checks"][1]["status"] == "movement_observed"
    expected_epochs = [check["return_epoch"] for check in verification["checks"]]
    assert coarse_epochs == expected_epochs
    assert set(scanner.checkpoint["return_coarse_recalls"]) == set(expected_epochs)
    assert scanner.checkpoint.get("return_correction_commands", []) == []
    assert verification["checks"][0]["return_outbound_seed"] == "stored"
    assert scanner.checkpoint["return_outbound_seed"] == {
        "return_epoch": expected_epochs[-1],
        "axis": "pan",
        "direction": verification["checks"][-1]["direction"],
        "duration_seconds": 0.12,
        "overlap": 0.95,
        "displacement": 6.0,
        "observed_shift": [6.0, 0.0],
        "source": "control_verification_outbound",
    }

    resumed_checkpoint = json.loads((tmp_path / "scan-manifest.json").read_text())
    resumed = scan._Scan(
        camera, tmp_path, _progress, lambda: False, resumed_checkpoint
    )
    resumed.acquired = True
    resumed.capabilities = scanner.capabilities
    resumed.last_frame = frame(away)
    resumed.last_pose = camera._position()

    async def forbidden_movement(*_args, **_options):
        raise AssertionError("The pending return epoch must never be replayed")

    async def resumed_stop():
        resumed.physical_state = "stopped"

    resumed._pulse = forbidden_movement
    resumed._reference_window = forbidden_movement
    resumed._move = forbidden_movement
    resumed._confirm_stop = resumed_stop
    asyncio.run(resumed._verify_control())
    assert resumed.checkpoint["control_verification"]["status"] == "resume_unconfirmed"
    asyncio.run(resumed._restore())

    assert len(pulses) == 2
    assert _event_names(camera).count("return") == 2
    assert resumed.checkpoint["return_epoch"] == expected_epochs[-1]


def _clock(monkeypatch: pytest.MonkeyPatch, camera: SimulatedCamera) -> None:
    # Replace only the scanner's module reference, never the process-wide clock.
    monkeypatch.setattr(
        scan, "time", SimpleNamespace(monotonic=lambda: camera.now, time=lambda: camera.now)
    )


def test_endpoint_commit_requires_a_new_unchanged_stream_window(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    candidate = asyncio.run(camera.frame())
    scanner.last_frame = candidate
    diagnostic = scan._AttemptDiagnostic("movement", total_seconds=3.0)

    assert asyncio.run(
        scanner._commit_endpoint_frame(
            candidate,
            diagnostic,
            deadline=camera.now + 1.0,
        )
    )
    assert diagnostic.value["endpoint_commit"]["verified"] is True
    assert diagnostic.value["endpoint_commit"]["frames"] >= 2


def test_endpoint_commit_rejects_motion_hidden_behind_a_static_backlog(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    candidate = asyncio.run(camera.frame())
    scanner.last_frame = candidate
    diagnostic = scan._AttemptDiagnostic("movement", total_seconds=3.0)
    original_frame = camera.frame
    calls = 0

    async def delayed_motion(*, timeout_s=3.0):
        nonlocal calls
        calls += 1
        if calls == 2:
            camera.pan = 0.2
            camera.target = (camera.pan, camera.tilt)
        return await original_frame(timeout_s=timeout_s)

    camera.frame = delayed_motion

    assert not asyncio.run(
        scanner._commit_endpoint_frame(
            candidate,
            diagnostic,
            deadline=camera.now + 1.0,
        )
    )
    commit = diagnostic.value["endpoint_commit"]
    assert commit["verified"] is False
    assert commit["reason"] == "endpoint_changed"
    assert commit["frames"] == 1
    assert commit["displacement"] > 0.5


def test_movement_observes_real_frames_until_visually_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def scenario():
        await camera.acquire()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_frame = await camera.frame()
        return await scanner._move(
            lambda: camera.move_absolute(pan=0.1, tilt=0.05),
            target={"pan": 0.1, "tilt": 0.05},
        )

    result = asyncio.run(scenario())

    assert result["stable"] is True
    assert result["evidence"]["has_motion_transition"] is True
    assert result["evidence"]["window_observation_seconds"] >= 0.8 - 1e-9
    assert result["pose"]["pan"] == pytest.approx(0.1)
    assert result["pose"]["tilt"] == pytest.approx(0.05)
    assert result["frame"]["sequence"] > 4
    assert "stop" in _event_names(camera)


def _late_endpoint_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    baseline = asyncio.run(camera.frame())
    path = tmp_path / "transition-late-endpoint.jpg"
    digest = scan._write_image(path, baseline["image"])
    transition = {
        "movement_id": "1234567890abcdef1234567890abcdef",
        "state": "pending",
        "stage": "pan",
        "row": 0,
        "direction": 1,
        "branch": -1,
        "intent": {
            "type": "seek_pulse",
            "axis": "pan",
            "direction": 1,
            "duration": 0.3,
        },
        "baseline": {
            "path": str(path),
            "sha256": digest,
            **{
                key: baseline.get(key)
                for key in (
                    "capture_instance",
                    "sequence",
                    "generation",
                    "media_time",
                    "received_monotonic",
                )
            },
        },
    }
    scanner.checkpoint["continuous_cursor"] = {"transition": transition}
    diagnostic = scan._AttemptDiagnostic("movement")
    diagnostic.value.update(
        movement_id=transition["movement_id"],
        command_outcome="accepted",
        stop_command_accepted=True,
    )
    camera.pan, camera.target, camera.remaining_motion_frames = 0.1, (0.1, 0.0), 0
    frames: deque[dict] = deque()
    terminal_frames: deque[dict] = deque()
    for _ in range(12):
        frame = asyncio.run(camera.frame())
        frames.append(frame)
        terminal = {
            key: frame.get(key)
            for key in (
                "capture_instance",
                "sequence",
                "generation",
                "media_time",
                "received_monotonic",
                "physical_timestamp_verified",
            )
        }
        terminal.update(
            image=scan._gray(frame["image"]),
            image_representation="analysis",
            source_width=frame["image"].shape[1],
            source_height=frame["image"].shape[0],
        )
        terminal_frames.append(terminal)
    return scanner, baseline, frames, terminal_frames, transition, diagnostic


@pytest.mark.parametrize("retain_only_latest", [False, True])
def test_late_endpoint_match_still_requires_a_full_stable_terminal_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retain_only_latest: bool
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    if retain_only_latest:
        frames = deque([frames[-1]])
    result = asyncio.run(
        scanner._late_endpoint_capture(
            baseline=baseline,
            frames=frames,
            terminal_frames=terminal,
            transition=transition,
            diagnostic=diagnostic,
            command_finished=True,
            stopped=True,
        )
    )
    assert result is not None and result["stable"] and not result["stationary"]
    assert result["frame"]["image"].shape == (240, 320, 3)
    assert result["match"]["verified"] and result["match"]["displacement"] > 2
    assert result["evidence"]["code"] == "stable"
    assert result["evidence"]["recovery_code"] == "verified_endpoint_transition"
    assert result["evidence"]["window_observation_seconds"] >= 0.8 - 1e-9
    assert result["evidence"]["best_sequence"] == result["frame"]["sequence"]
    assert {"generation": result["frame"]["generation"], "sequence": result["frame"]["sequence"]} in result["evidence"]["qualified_frames"]
    if retain_only_latest:
        assert result["frame"] is frames[-1]


def test_late_endpoint_reuses_the_verified_terminal_match_on_repetitive_scenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    original_match = scan._command_match
    calls = 0

    def match_once(*images):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("the already verified terminal frame must not be rematched")
        return original_match(*images)

    monkeypatch.setattr(scan, "_command_match", match_once)
    result = asyncio.run(scanner._late_endpoint_capture(
        baseline=baseline,
        frames=frames,
        terminal_frames=terminal,
        transition=transition,
        diagnostic=diagnostic,
        command_finished=True,
        stopped=True,
    ))

    assert result is not None and result["frame"] is frames[-1]
    assert result["evidence"]["best_sequence"] == frames[-1]["sequence"]
    assert diagnostic.value["late_endpoint_selection"] == "verified_terminal_frame"
    assert calls == 1


def test_first_reference_probe_recovers_a_missed_video_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    transition["intent"] = {
        "type": "reference_probe",
        "axis": "pan",
        "direction": 1,
        "row": 0,
        "duration": 0.3,
    }

    result = asyncio.run(scanner._late_endpoint_capture(
        baseline=baseline,
        frames=frames,
        terminal_frames=terminal,
        transition=transition,
        diagnostic=diagnostic,
        command_finished=True,
        stopped=True,
    ))

    assert result is not None and result["stable"]
    assert result["evidence"]["recovery_code"] == "verified_endpoint_transition"


def test_late_endpoint_accepts_ordered_identical_frames_after_command_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    endpoint = frames[-1]["image"].copy()
    endpoint_analysis = scan._gray(endpoint)
    for frame, observation in zip(frames, terminal, strict=True):
        frame["image"] = endpoint.copy()
        observation["image"] = endpoint_analysis.copy()

    result = asyncio.run(scanner._late_endpoint_capture(
        baseline=baseline,
        frames=frames,
        terminal_frames=terminal,
        transition=transition,
        diagnostic=diagnostic,
        command_finished=True,
        stopped=True,
    ))

    assert result is not None and result["stable"]
    assert result["evidence"]["endpoint_transport_frame"] is True
    assert result["evidence"]["window_observation_seconds"] >= 0.8 - 1e-9


@pytest.mark.parametrize("invalidity", ["movement", "stop", "decoder", "unstable"])
def test_late_endpoint_rejects_unbound_or_unstable_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidity: str
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    if invalidity == "movement":
        diagnostic.value["movement_id"] = "abcdef1234567890abcdef1234567890"
    elif invalidity == "stop":
        diagnostic.value["stop_command_accepted"] = False
    elif invalidity == "decoder":
        terminal[-1]["capture_instance"] = "replacement-decoder"
    else:
        terminal[-1]["image"] = cv2.warpAffine(
            terminal[-1]["image"],
            np.float32([[1, 0, 3], [0, 1, 0]]),
            terminal[-1]["image"].shape[::-1],
            borderMode=cv2.BORDER_REFLECT,
        )
    result = asyncio.run(
        scanner._late_endpoint_capture(
            baseline=baseline,
            frames=frames,
            terminal_frames=terminal,
            transition=transition,
            diagnostic=diagnostic,
            command_finished=True,
            stopped=True,
        )
    )
    assert result is None


def test_late_endpoint_rejects_motion_on_the_wrong_command_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        scan,
        "_command_match",
        lambda *_images: {
            "verified": True,
            "support_scope": "localized_command_transition",
            "overlap": 0.8,
            "displacement": 20.0,
            "shift_x": 1.0,
            "shift_y": 20.0,
            "inliers": 100,
            "model_candidates": [
                {"inliers": 100, "occupied_cells": 4, "hull_fraction": 0.08}
            ],
            "homography": [[1.0, 0.0, 1.0], [0.0, 1.0, 20.0], [0.0, 0.0, 1.0]],
        },
    )

    result = asyncio.run(
        scanner._late_endpoint_capture(
            baseline=baseline,
            frames=frames,
            terminal_frames=terminal,
            transition=transition,
            diagnostic=diagnostic,
            command_finished=True,
            stopped=True,
        )
    )

    assert result is None
    assert diagnostic.value["late_endpoint_rejection"] == "endpoint_axis_unverified"


def test_late_endpoint_reports_the_transition_gate_that_rejected_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        scan,
        "_axis_command_match",
        lambda *_images, **_options: {
            "verified": False,
            "code": "correspondences_not_distributed",
            "model_candidates": [
                {"inliers": 79, "occupied_cells": 3, "hull_fraction": 0.05}
            ],
        },
    )

    async def temporal_rejection(*_arguments, **_options):
        return {
            "verified": False,
            "code": "temporal_endpoint_models_insufficient",
            "temporal_consensus": {
                "distinct_frames": 7,
                "sampled_frames": 7,
                "verified_models": 4,
                "required_models": 5,
            },
        }

    monkeypatch.setattr(scan, "_temporal_command_match", temporal_rejection)

    result = asyncio.run(scanner._late_endpoint_capture(
        baseline=baseline,
        frames=frames,
        terminal_frames=terminal,
        transition=transition,
        diagnostic=diagnostic,
        command_finished=True,
        stopped=True,
    ))

    assert result is None
    assert diagnostic.value["late_endpoint_rejection"] == (
        "endpoint_transition_match_unverified"
    )
    assert diagnostic.value["late_endpoint_transition_match"] == {
        "verified": False,
        "code": "temporal_endpoint_models_insufficient",
        "temporal_consensus": {
            "distinct_frames": 7,
            "sampled_frames": 7,
            "verified_models": 4,
            "required_models": 5,
        },
    }
    assert diagnostic.value["late_endpoint_single_match"]["code"] == (
        "correspondences_not_distributed"
    )


def test_late_endpoint_uses_temporal_consensus_for_a_sparse_stopped_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scanner, baseline, frames, terminal, transition, diagnostic = _late_endpoint_case(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        scan,
        "_axis_command_match",
        lambda *_images, **_options: {
            "verified": False,
            "code": "correspondences_not_distributed",
        },
    )

    async def temporal(_first, _frames, *, axis):
        assert axis == "pan"
        endpoint = terminal[-2]
        return {
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
            "temporal_consensus": {
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
            },
            "endpoint_frame_identity": {
                key: endpoint[key]
                for key in (
                    "capture_instance",
                    "sequence",
                    "generation",
                    "received_monotonic",
                )
            },
            "homography": [
                [1.0, 0.0, 100.0],
                [0.0, 1.0, 4.0],
                [0.0, 0.0, 1.0],
            ],
        }

    monkeypatch.setattr(scan, "_temporal_command_match", temporal)
    result = asyncio.run(scanner._late_endpoint_capture(
        baseline=baseline,
        frames=frames,
        terminal_frames=terminal,
        transition=transition,
        diagnostic=diagnostic,
        command_finished=True,
        stopped=True,
    ))

    assert result is not None and result["stable"]
    assert result["match"]["support_scope"] == "temporal_command_transition"
    assert diagnostic.value["late_endpoint_transition_match"][
        "temporal_consensus"
    ]["consistent_models"] == 7


def test_textureless_stationary_frames_cannot_prove_mechanical_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    camera.blank = True
    camera.pan = 0.2
    camera.target = (0.2, 0.0)
    _clock(monkeypatch, camera)

    async def scenario():
        await camera.acquire()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_frame = await camera.frame()
        return await scanner._move(
            lambda: camera.move_velocity(pan=0.2, timeout_s=0.5),
            duration=0.5,
            allow_stationary=True,
        )

    with pytest.raises(PanoramaCaptureError, match="motion_not_observed"):
        asyncio.run(scenario())

    assert not (tmp_path / "capture-0000.jpg").exists()
    assert "stop" in _event_names(camera)
    assert camera.now < 120, "A missing visual signal must reach the bounded timeout"


@pytest.mark.parametrize("endpoint_analysis_seconds", [0.0, 1.2])
def test_no_effect_window_is_qualified_before_unneeded_endpoint_analysis(
    tmp_path, monkeypatch, endpoint_analysis_seconds
):
    """Simulated analysis cost must not expire a real policy observation gate."""
    camera = SimulatedCamera()
    camera.tilt = -0.1
    camera.target = (0.0, -0.1)
    _clock(monkeypatch, camera)
    endpoint_calls = []

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

        async def expensive_endpoint(**arguments):
            at_deadline = arguments["diagnostic"].value.get("budget_hit") == "total"
            endpoint_calls.append(at_deadline)
            if at_deadline:
                camera.now += endpoint_analysis_seconds
            return None

        monkeypatch.setattr(scanner, "_late_endpoint_capture", expensive_endpoint)
        return await scanner._move(
            lambda: camera.move_velocity(tilt=-0.1, timeout_s=0.3),
            duration=0.3, allow_stationary=True,
            requested_velocity={"pan": 0.0, "tilt": -0.1},
        )

    result = asyncio.run(scenario())
    assert result["stationary"] is True
    assert result["stable"] is False
    assert not any(endpoint_calls)
    attempt = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
    assert attempt["outcome"] == "stationary_boundary_observed"
    assert attempt["stop_command_accepted"] is True
    assert attempt["terminal_scene"]["transport_window_verified"] is True
    assert attempt["no_motion_observation_policy"] == "full_attempt_budget"
    assert not list(tmp_path.glob("capture-*.jpg"))


def test_unconfirmed_command_records_the_cleanup_stop_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def rejected_command():
        raise PanoramaCaptureError(
            "movement_unconfirmed",
            ptz_failure_code="transport_timeout",
            ptz_failure_stage="command_dispatch",
        )

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        with pytest.raises(PanoramaCaptureError, match="movement_unconfirmed"):
            await scanner._move(
                rejected_command,
                duration=0.5,
                allow_stationary=True,
                requested_velocity={"pan": 0.1, "tilt": 0.0},
            )

    asyncio.run(scenario())
    attempt = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
    assert attempt["command_outcome"] == "unconfirmed"
    assert attempt["command_failure_code"] == "movement_unconfirmed"
    assert attempt["ptz_failure_code"] == "transport_timeout"
    assert attempt["ptz_failure_stage"] == "command_dispatch"
    assert "command_accepted_seconds" not in attempt
    assert attempt["stop_reason"] == "attempt_cleanup"
    assert attempt["stop_command_accepted"] is True
    assert attempt["stop_accepted_seconds"] >= 0


def test_slow_position_queries_do_not_interrupt_continuous_video_observation(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_position, original_frame = camera.position, camera.frame

    async def slow_position():
        camera.now += 1.2
        return await original_position()

    async def timed_frame(**options):
        frame = await original_frame(**options)
        frame["media_time"] = camera.now - 100
        return frame

    camera.position, camera.frame = slow_position, timed_frame

    async def scenario():
        await camera.acquire()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        return await scanner._move(
            lambda: camera.move_velocity(pan=0.2, timeout_s=0.5),
            duration=0.5,
        )

    result = asyncio.run(scenario())
    assert result["stable"]
    assert result["pose"]["pan"] == pytest.approx(camera.pan)
    assert result["evidence"]["has_motion_transition"]
    assert result["evidence"]["window_observation_seconds"] >= 0.8 - 1e-9


def test_continuous_visual_capture_survives_unavailable_position_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def unavailable_position():
        raise PanoramaCaptureError("position_unavailable")

    camera.position = unavailable_position

    async def scenario():
        await camera.acquire()
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        return await scanner._move(
            lambda: camera.move_velocity(pan=0.2, timeout_s=0.5),
            duration=0.5,
        )

    result = asyncio.run(scenario())

    assert result["stable"]
    assert result["evidence"]["has_motion_transition"]
    assert result["pose"]["position_evidence"] == "unavailable"
    attempt = json.loads((tmp_path / "scan-diagnostics.json").read_text())[
        "attempts"
    ][-1]
    assert attempt["position_readback"] == "unavailable_after_visual_stop"
    assert attempt["outcome"] == "capture_stable"


def test_observational_readback_never_hides_control_loss(tmp_path: Path):
    camera = SimulatedCamera()

    async def lost_position():
        raise PanoramaCaptureError("control_lost")

    camera.position = lost_position
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

    with pytest.raises(PanoramaCaptureError, match="control_lost"):
        asyncio.run(scanner._observational_readback())


def test_discovery_failure_never_acquires_control_or_moves(tmp_path: Path):
    camera = SimulatedCamera()
    camera.discover_error = "source_binding_unverified"

    with pytest.raises(PanoramaCaptureError, match="source_binding_unverified"):
        asyncio.run(
            scan.run_panorama_scan(camera, tmp_path, progress=_progress, cancelled=lambda: False)
        )

    assert "discover" in _event_names(camera)
    assert not set(_event_names(camera)) & {"acquire", "absolute", "velocity", "return", "stop"}


def test_cancellation_stops_without_automatically_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    def cancelled() -> bool:
        return bool(set(_event_names(camera)) & {"absolute", "velocity"})

    result = asyncio.run(
        scan.run_panorama_scan(camera, tmp_path, progress=_progress, cancelled=cancelled)
    )

    events = _event_names(camera)
    assert set(events) & {"absolute", "velocity"}, "Cancellation is injected after a real command"
    assert "stop" in events
    assert "return" not in events
    assert events[-1] in {"release", "close"}
    assert result["complete"] is False
    assert result["checkpoint"]["source_identity"] == camera.capabilities["source_identity"]


def test_unexpected_failure_writes_only_bounded_local_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def failing_progress(_event):
        raise RuntimeError("rtsp://private:secret@example.test/live")

    result = asyncio.run(
        scan.run_panorama_scan(
            camera,
            tmp_path,
            progress=failing_progress,
            cancelled=lambda: False,
        )
    )

    diagnostic = json.loads((tmp_path / "internal-failure.json").read_text())
    assert [item["stage"] for item in diagnostic["failures"]] == [
        "acquisition",
        "final_checkpoint",
    ]
    assert all(item["type"] == "RuntimeError" for item in diagnostic["failures"])
    assert all(len(item["frames"]) <= 12 for item in diagnostic["failures"])
    assert "rtsp" not in json.dumps(diagnostic)
    assert "secret" not in json.dumps(diagnostic)
    assert result["physical_state"] in {"stopped", "restored"}
    assert {item["code"] for item in result["issues"]} >= {
        "acquisition_failed",
        "checkpoint_write_failed",
    }


def test_bounded_capture_restores_and_verifies_original_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    camera.capabilities["limits"]["tilt"] = {"min": -0.2, "max": 0.2}
    _clock(monkeypatch, camera)
    monkeypatch.setattr(scan, "MAX_CAPTURES", 3)

    result = asyncio.run(
        scan.run_panorama_scan(camera, tmp_path, progress=_progress, cancelled=lambda: False)
    )

    assert result["complete"] is False
    assert result["physical_state"] == "restored"
    assert _event_names(camera)[-1] == "release"
    assert len(result["captures"]) == 2
    for capture in result["captures"]:
        assert Path(capture["path"]).is_absolute()
        assert Path(capture["path"]).is_file()
        assert capture["quality"]["has_motion_transition"] is True
    assert (tmp_path / "return-validation.json").is_file()
    assert (tmp_path / "return-final.jpg").is_file()
    assert camera.pan == pytest.approx(0)
    assert camera.tilt == pytest.approx(0)


def test_mismatched_reported_pose_space_falls_back_to_continuous_without_absolute_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    camera.capabilities["defaults"] = {"absolute": "normalized-position"}
    for bounds in camera.capabilities["limits"].values():
        bounds["space"] = "normalized-position"
    original_position = camera._position

    def custom_position():
        return {**original_position(), "pan_tilt_space": "vendor-degrees"}

    camera._position = custom_position
    used_continuous = []

    async def reference_window(self, **_options):
        return await camera.frame()

    async def save_destination(self):
        self.saved_return = {"kind": "preset", "preset_token": "safe-return"}
        return True

    async def continuous(self):
        used_continuous.append(True)

    async def restore(self):
        self.physical_state = "stopped"

    monkeypatch.setattr(scan._Scan, "_reference_window", reference_window)
    monkeypatch.setattr(scan._Scan, "_save_original_destination", save_destination)
    monkeypatch.setattr(scan._Scan, "_continuous_scan", continuous)
    monkeypatch.setattr(scan._Scan, "_restore", restore)

    asyncio.run(
        scan.run_panorama_scan(
            camera, tmp_path, progress=_progress, cancelled=lambda: False
        )
    )

    assert used_continuous == [True]
    assert "absolute" not in _event_names(camera)


def test_absolute_readback_rejects_a_different_pan_tilt_space(tmp_path: Path):
    camera = SimulatedCamera()
    camera.capabilities["defaults"] = {"absolute": "normalized-position"}
    for bounds in camera.capabilities["limits"].values():
        bounds["space"] = "normalized-position"
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = camera.capabilities

    async def incompatible_position():
        return {**camera._position(), "pan_tilt_space": "vendor-degrees"}

    camera.position = incompatible_position
    with pytest.raises(PanoramaCaptureError, match="normalized_motion_unavailable"):
        asyncio.run(scanner._absolute_readback())


def test_common_pilot_rejects_other_axis_drift_relative_to_primary_delta(tmp_path: Path):
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, None)
    matching = _translation_match(3.0, 2.0)

    rejected = scanner._axis_response_observation(
        "pan",
        matching,
        {"pan": 0.0, "tilt": 0.0},
        {"pan": 0.005, "tilt": 0.018},
        kind="pilot_outward",
    )
    accepted = scanner._axis_response_observation(
        "pan",
        matching,
        {"pan": 0.0, "tilt": 0.0},
        {"pan": 0.005, "tilt": 0.0009},
        kind="pilot_outward",
    )

    assert rejected is None
    assert accepted is not None


@pytest.mark.parametrize(
    "closure,expected",
    [
        (_closed_pilot_cycle(), {"pan", "tilt"}),
        ({"visually_closed": True}, set()),
        (_closed_pilot_cycle(match_verified=False), set()),
        (_closed_pilot_cycle(displacement=float("nan")), set()),
        (_closed_pilot_cycle(displacement=3.0001), set()),
        (_closed_pilot_cycle(overlap=float("nan")), set()),
        (_closed_pilot_cycle(overlap=0.8499), set()),
    ],
)
def test_absolute_finish_requires_every_contributing_pilot_cycle_to_close(
    closure, expected
):
    checkpoint = {
        "pilot_response": {"pan": 300.0, "tilt": 280.0},
        "pilot_attempts": [
            {
                "axis": axis,
                "outward_verified": True,
                "cycle_closure": dict(closure),
            }
            for axis in ("pan", "tilt")
        ],
    }

    assert scan._pilot_axes_authorized_for_absolute_finish(checkpoint) == expected


def test_unverified_pilot_attempt_with_unclosed_cycle_blocks_absolute_finish():
    checkpoint = {
        "pilot_response": {"pan": 300.0},
        "pilot_attempts": [
            {
                "axis": "pan",
                "outward_verified": True,
                "cycle_closure": _closed_pilot_cycle(),
            },
            {
                "axis": "pan",
                "outward_verified": False,
                "return_verified": False,
                "cycle_closure": _closed_pilot_cycle(
                    visually_closed=False,
                    match_verified=False,
                    displacement=None,
                    overlap=None,
                ),
            },
        ],
    }

    assert "pan" not in scan._pilot_axes_authorized_for_absolute_finish(checkpoint)


@pytest.mark.parametrize(
    ("visit_all_edges", "connected", "expected_complete"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_absolute_completion_requires_confirmed_edges_and_connected_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    visit_all_edges: bool,
    connected: bool,
    expected_complete: bool,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    top = 0.1 if visit_all_edges else 0.0
    plan = [
        {"pan": -0.2, "tilt": -0.1, "row": 0},
        {"pan": 0.2, "tilt": -0.1, "row": 0},
        {"pan": 0.2, "tilt": top, "row": 1},
        {"pan": -0.2, "tilt": top, "row": 1},
    ]

    async def settled_move(self, command, *, target=None, duration=0, allow_stationary=False):
        # The real detector is covered above. Here this seam injects prescribed
        # acquisition evidence to isolate the coverage acceptance rules.
        await command()
        for _ in range(4):
            self.last_frame = await camera.frame()
        self.last_pose = await camera.position()
        return {
            "frame": self.last_frame,
            "evidence": {"stable": True, "test_evidence": "prescribed"},
            "pose": self.last_pose,
            "match": {"verified": connected, "displacement": 30.0, "overlap": 0.8},
            "stable": True,
            "stationary": False,
        }

    monkeypatch.setattr(scan._Scan, "_move", settled_move)
    if not connected:
        monkeypatch.setattr(
            scan, "_match", lambda first, second: {"verified": False, "code": "test_missing_link"}
        )

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"plan": plan})
        scanner.capabilities = await camera.discover()
        scanner.last_pose = await camera.position()
        scanner.last_frame = await camera.frame()
        await scanner._absolute_scan(camera.capabilities["limits"])
        return scanner.result()

    result = asyncio.run(scenario())

    assert result["complete"] is expected_complete
    assert len(result["captures"]) == 4
    boundaries = result["coverage"]["boundaries"]
    if visit_all_edges:
        assert all(
            boundaries[edge]["confirmed"] for edge in ("pan_min", "pan_max", "tilt_min", "tilt_max")
        )
    else:
        assert not boundaries.get("tilt_max", {}).get("confirmed", False)
    if not connected:
        assert result["coverage"]["failed_positions"] > 0
        assert any(issue["code"] == "coverage_connection_unverified" for issue in result["issues"])

        async def resume_finished_plan():
            scanner = scan._Scan(
                camera, tmp_path, _progress, lambda: False, copy.deepcopy(result["checkpoint"])
            )
            scanner.capabilities = await camera.discover()
            scanner.last_pose = await camera.position()
            scanner.last_frame = await camera.frame()
            await scanner._absolute_scan(camera.capabilities["limits"])
            return scanner.result()

        resumed = asyncio.run(resume_finished_plan())
        assert resumed["complete"] is False, "Resuming must not erase an unresolved coverage gap"


@pytest.mark.parametrize("observed_progress", [False, True])
def test_continuous_limit_requires_progress_before_repeated_stationarity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_progress: bool
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    count = 0

    async def pulse(self, axis, direction, duration, **_options):
        nonlocal count
        count += 1
        moving = observed_progress and count == 1
        return {
            "frame": await camera.frame(),
            "evidence": {"stable": moving, "test_evidence": "prescribed"},
            "pose": await camera.position(),
            "match": {
                "verified": True,
                "displacement": 8.0 if moving else 0.0,
                "shift_x": 8.0 if moving else 0.0,
                "overlap": 0.98,
            },
            "stable": moving,
            "stationary": not moving,
        }

    monkeypatch.setattr(scan._Scan, "_pulse", pulse)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.capabilities = await camera.discover()
        scanner.last_frame = await camera.frame()
        outcome, _ = await scanner._seek("pan", 1, row=0, duration=0.3)
        return outcome, scanner.result()

    outcome, result = asyncio.run(scenario())

    assert outcome == ("limit" if observed_progress else "unknown")
    assert (
        bool(result["coverage"]["boundaries"].get("pan_max", {}).get("confirmed"))
        is observed_progress
    )
    # When the initial point might already be an endpoint, the scanner tries one
    # short opposite excursion. No visual progress still leaves the limit unknown.
    assert count == 3


@pytest.mark.parametrize("retry_moves", [False, True])
def test_continuous_rows_keep_a_successful_vertical_retry(tmp_path, monkeypatch, retry_moves):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_frame = asyncio.run(camera.frame())
    path = tmp_path / "band.jpg"
    cv2.imwrite(str(path), scanner.last_frame["image"])
    capture = {"id": "anchor", "path": str(path), "row_index": 0, "quality": {"stable": True}}
    scanner.captures.append(capture)
    scanner.physical_state = "stopped"
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "branch": 1,
        "row": 0,
        "bands": {"0": {}},
        "anchor": {"capture_id": "anchor", "path": str(path), "row": 0},
    }
    scanner.checkpoint["continuous_cursor"] = cursor
    pulses = iter([False, False, retry_moves, retry_moves, False])

    async def pulse(*args, **_options):
        moving = next(pulses)
        scanner.last_frame = await camera.frame()
        return {
            "stationary": not moving,
            "stable": moving,
            "match": {"verified": True, "shift_y": 12, "displacement": 12 if moving else 0},
            "frame": scanner.last_frame,
            "pose": camera._position(),
            "evidence": {},
        }

    scanner._pulse = pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *images: {
            "verified": True,
            "overlap": 0.6 if camera.sequence > 4 else 1.0,
            "displacement": 12 if camera.sequence > 4 else 0.0,
            "shift_y": 12,
        },
    )
    if retry_moves:
        assert asyncio.run(scanner._next_band(cursor))
        assert cursor["row"] == 1 and cursor["stage"] == "pan"
        assert scanner.captures[-1]["role"] == "row_connection"
    else:
        with pytest.raises(PanoramaCaptureError, match="tilt_terminal_limit_unconfirmed"):
            asyncio.run(scanner._next_band(cursor))
        assert len(scanner.captures) == 1 and "tilt_max" not in scanner.boundaries


def test_reference_pan_band_completes_without_tilt_on_a_pan_only_camera(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {"continuous_supported": True, "axes": {"pan": True, "tilt": False}}
    scanner.last_frame = asyncio.run(camera.frame())
    events = []

    async def pulse(axis, direction, duration, **_options):
        events.append((axis, direction))
        scanner.last_frame = await camera.frame()
        return {
            "stable": True,
            "stationary": False,
            "frame": scanner.last_frame,
            "pose": camera._position(),
            "evidence": {},
            "match": {"verified": True, "shift_x": 12, "shift_y": 0, "displacement": 13},
        }

    async def seek(axis, direction, *, row, duration, store=True):
        events.append((axis, direction))
        return "limit", duration

    scanner._pulse, scanner._seek = pulse, seek
    asyncio.run(scanner._continuous_scan())
    assert events == [("pan", 1), ("pan", -1), ("pan", 1)]
    assert scanner.complete
    assert scanner._coverage_progress() == {
        "primary_complete": True,
        "bands_completed": 1,
        "current_band": 0,
        "stage": "done",
        "regions_pending": 0,
        "continued_after_recovery": False,
    }


def test_reference_search_leaves_a_view_where_pan_only_rotates_the_image(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_frame = asyncio.run(camera.frame())
    events = []

    async def pulse(axis, direction, duration, **_options):
        events.append(axis)
        scanner.last_frame = await camera.frame()
        return {
            "stable": True,
            "stationary": False,
            "frame": scanner.last_frame,
            "pose": camera._position(),
            "evidence": {},
            "match": {
                "verified": True,
                "shift_x": 0 if len(events) == 1 else 20,
                "shift_y": 0,
                "displacement": 30,
            },
        }

    scanner._pulse = pulse
    cursor = {}
    asyncio.run(scanner._find_reference(cursor))
    assert events == ["pan", "tilt", "pan"]
    assert len(scanner.captures) == 2
    assert scanner.captures[-1]["role"] == "reference_connection"
    assert Path(cursor["reference_path"]).is_file()


def test_continuous_resume_visits_pending_band_in_saved_direction(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    path = tmp_path / "reference.jpg"
    cv2.imwrite(str(path), initial["image"])
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": -1,
        "direction": -1,
        "branch": -1,
        "horizontal_duration": 0.3,
        "finished_branches": [],
        "recovery_attempts": {},
        "bands": {"0": {"complete": True, "edges": {}}, "-1": {"complete": False, "edges": {}}},
    }
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {"mode": "continuous", "continuous_cursor": cursor},
    )
    scanner.capabilities = {"continuous_supported": True}
    scanner.captures = [{"id": "reference", "path": str(path), "row_index": 0, "quality": {"stable": True}}]
    cursor["reference_path"] = str(path)
    cursor["reference_destination"] = {"kind": "absolute", "role": "work", "binding": {}, "pan": 0.0, "tilt": 0.0, "capture_id": "reference", "path": str(path)}
    scanner.last_frame = initial
    scanner.boundaries["tilt_min"] = {"confirmed": True}
    rows = []

    async def seek(axis, direction, *, row, duration, store=True):
        rows.append((row, direction))
        return "limit", duration

    async def stop_before_other_branch(cursor):
        raise PanoramaCaptureError("test_other_branch")

    scanner._seek, scanner._return_to_reference_band = seek, stop_before_other_branch
    with pytest.raises(PanoramaCaptureError, match="test_other_branch"):
        asyncio.run(scanner._continuous_scan())
    assert rows == [(-1, -1), (-1, 1)]
    assert scanner._coverage_progress()["bands_completed"] == 2


def test_relative_pulse_uses_displacement_and_never_calls_velocity(tmp_path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {"relative_supported": True, "axes": {"pan": True}}
    moves = []

    async def relative(**axes):
        moves.append(axes)

    async def move(command, **options):
        await command()
        return options

    scanner._move, camera.move_relative = move, relative
    result = asyncio.run(scanner._pulse("pan", -1, 0.3))
    assert moves == [{"pan": -0.03, "tilt": 0.0}]
    assert "duration" not in result
    assert "velocity" not in _event_names(camera)


def test_legacy_continuous_job_requires_a_new_capture_for_precise_resume():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = {"mode": "continuous", "captures": [{"id": "a"}, {"id": "b"}]}
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    checkpoint["continuous_cursor"] = {"version": 2}
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    checkpoint["active_seconds"] = 20.0
    checkpoint["captures"][0]["row_index"] = 0
    checkpoint["continuous_cursor"] = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "recovery_attempts": {},
        "anchor": {"capture_id": "a", "row": 0},
    }
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


@pytest.mark.parametrize(
    ("failure_code", "at_reference", "expected_state"),
    [
        ("motion_not_observed", True, "restored"),
        ("stability_timeout", True, "restored"),
        ("motion_not_observed", False, "stopped"),
        ("control_lost", True, "ownership_lost"),
    ],
)
def test_preset_return_checks_fresh_stopped_view_after_tracking_failure(
    tmp_path, monkeypatch, failure_code, at_reference, expected_state
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.jpg"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {"initial_path": str(path), "return": {"kind": "preset", "preset_token": "owned"}},
    )
    scanner.acquired = True
    camera.pan = 0.3
    scanner.last_frame = asyncio.run(camera.frame())

    async def return_to(saved):
        camera.events.append(("return", saved))

    async def failed_move(command, **options):
        await command()
        scanner.physical_state = "unknown"
        camera.pan = 0.0 if at_reference else 0.3
        raise PanoramaCaptureError(failure_code)

    camera.return_to, scanner._move = return_to, failed_move
    asyncio.run(scanner._restore())
    assert scanner.physical_state == expected_state
    assert _event_names(camera).count("return") == 1
    assert _event_names(camera).count("stop") == (0 if failure_code == "control_lost" else 1)
    if expected_state == "restored":
        validation = json.loads((tmp_path / "return-validation.json").read_text())
        assert validation["verified"] and validation["displacement"] <= 3
        assert (tmp_path / "return-final.jpg").is_file()
        assert not scanner.issues
    else:
        assert any(issue["code"] == "return_framing_unconfirmed" for issue in scanner.issues)
        assert not (tmp_path / "return-final.jpg").exists()


def test_large_overlapping_absolute_return_uses_coarse_recall_before_visual_finish(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras import panorama_navigation

    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False,
                        {"initial_path": str(path), "return": {"kind": "absolute", "pan": 0., "tilt": 0.}})
    scanner.capabilities = {
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    camera.pan = 0.15
    camera.return_offset = 0.06
    scanner.last_frame = asyncio.run(camera.frame())
    corrected = []
    async def exhausted_return(owner, reference, result):
        corrected.append(owner)
        return result

    monkeypatch.setattr(panorama_navigation, "correct_reference", exhausted_return)
    asyncio.run(scanner._restore())
    assert corrected == [scanner]
    assert _event_names(camera).count("return") == 1
    assert _event_names(camera).count("absolute") == 1
    assert scanner.physical_state != "restored"


def test_unclosed_pilot_uses_one_coarse_recall_then_safe_visual_finish(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras import panorama_navigation

    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "pilot_response": {"pan": 500.0},
            "pilot_attempts": [
                {
                    "axis": "pan",
                    "outward_verified": True,
                    "cycle_closure": {"visually_closed": False, "displacement": 7.0},
                }
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = {
        **camera.capabilities,
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    comparisons = deque(
        [
            {"verified": True, "overlap": 0.9, "displacement": 46.0},
            {
                "verified": True,
                "overlap": 1.0,
                "displacement": 6.0,
                "shift_x": 6.0,
                "shift_y": 0.0,
            },
        ]
    )
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: comparisons.popleft() if len(comparisons) > 1 else comparisons[0],
    )

    async def coarse_move(command, **_options):
        await command()
        scanner.physical_state = "stopped"
        scanner.last_frame = await camera.frame()
        scanner.last_pose = await camera.position()
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    visual_calls = []

    async def visual_finish(owner, reference, result):
        visual_calls.append((owner, result))
        return result

    async def forbidden_absolute(*_args, **_options):
        raise AssertionError("An unclosed pilot cycle cannot authorize absolute finishing")

    scanner._move = coarse_move
    scanner._absolute = forbidden_absolute
    monkeypatch.setattr(panorama_navigation, "correct_reference", visual_finish)

    asyncio.run(scanner._restore())

    assert _event_names(camera).count("return") == 1
    assert len(visual_calls) == 1
    assert scanner.checkpoint["return_correction_strategy"] == {
        "coarse": "saved_destination",
        "finish": "visual_continuous",
        "pilot_cycles_closed": False,
        "absolute_axes": [],
        "visual_axes": ["pan"],
        "return_epoch": "final",
        "shared_command_budget": 4,
        "commands_already_planned": 0,
    }


def test_unclosed_pilot_without_safe_visual_control_refuses_absolute_finish(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "pilot_response": {"pan": 500.0},
            "pilot_attempts": [
                {
                    "axis": "pan",
                    "outward_verified": True,
                    "cycle_closure": {"visually_closed": False},
                }
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )

    async def coarse_move(command, **_options):
        await command()
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    async def forbidden_absolute(*_args, **_options):
        raise AssertionError("An unclosed pilot cycle cannot authorize absolute finishing")

    scanner._move = coarse_move
    scanner._absolute = forbidden_absolute
    asyncio.run(scanner._restore())

    assert scanner.checkpoint["return_correction_strategy"]["finish"] == "unavailable"
    assert scanner.checkpoint["absolute_return_corrections"] == []
    assert scanner.physical_state == "stopped"


@pytest.mark.parametrize("tilt_history", ["absent", "failed"])
def test_absolute_finish_never_uses_an_axis_without_a_closed_pilot_cycle(
    tmp_path, monkeypatch, tilt_history
):
    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    pilot_attempts = [
        {
            "axis": "pan",
            "outward_verified": True,
            "cycle_closure": _closed_pilot_cycle(),
        }
    ]
    if tilt_history == "failed":
        pilot_attempts.append(
            {
                "axis": "tilt",
                "outward_verified": True,
                "cycle_closure": _closed_pilot_cycle(
                    visually_closed=False,
                    match_verified=False,
                    displacement=8.0,
                ),
            }
        )
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            # A finite legacy response alone cannot authorize an axis.
            "pilot_response": {"pan": 500.0, "tilt": 500.0},
            "pilot_attempts": pilot_attempts,
        },
    )
    scanner.acquired = True
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": False,
        "relative_supported": False,
        "axes": {},
    }
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 0.0,
            "shift_y": 6.0,
        },
    )

    async def coarse_move(command, **_options):
        await command()
        scanner.physical_state = "stopped"
        return {
            "frame": scanner.last_frame,
            "pose": scanner.last_pose,
            "stable": True,
        }

    async def forbidden_absolute(*_args, **_options):
        raise AssertionError("Tilt lacks a qualified closed pilot cycle")

    scanner._move = coarse_move
    scanner._absolute = forbidden_absolute

    assert scan._pilot_axes_authorized_for_absolute_finish(scanner.checkpoint) == {
        "pan"
    }
    asyncio.run(scanner._restore())

    strategy = scanner.checkpoint["return_correction_strategy"]
    assert strategy["absolute_axes"] == ["pan"]
    assert scanner.checkpoint["absolute_return_corrections"] == []
    assert _event_names(camera).count("return") == 1
    assert scanner.physical_state == "stopped"


def test_absolute_finish_changes_only_the_axis_with_a_closed_pilot_cycle(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    camera.tilt = 0.037
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.037},
            "pilot_response": {"pan": 500.0, "tilt": 500.0},
            "pilot_attempts": [
                {
                    "axis": "pan",
                    "outward_verified": True,
                    "cycle_closure": _closed_pilot_cycle(),
                },
                {
                    "axis": "tilt",
                    "outward_verified": True,
                    "cycle_closure": _closed_pilot_cycle(
                        visually_closed=False,
                        match_verified=False,
                        displacement=8.0,
                    ),
                },
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": False,
        "relative_supported": False,
        "axes": {},
    }
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )

    async def coarse_move(command, **_options):
        await command()
        scanner.physical_state = "stopped"
        return {
            "frame": scanner.last_frame,
            "pose": scanner.last_pose,
            "stable": True,
        }

    correction_calls = []

    async def capture_correction(target, **options):
        correction_calls.append((dict(target), options))
        raise asyncio.CancelledError

    scanner._move = coarse_move
    scanner._absolute = capture_correction

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scanner._restore())

    assert scanner.checkpoint["return_correction_strategy"]["absolute_axes"] == [
        "pan"
    ]
    assert len(correction_calls) == 1
    target, options = correction_calls[0]
    assert target["pan"] != scanner.last_pose["pan"]
    assert target["tilt"] == scanner.last_pose["tilt"]
    assert options["expected_frame"] is scanner.last_frame
    commands = scanner.checkpoint["return_correction_commands"]
    assert [command["modality"] for command in commands] == ["absolute_correction"]
    assert not any(command.get("axis") == "tilt" for command in commands)


@pytest.mark.parametrize("cancel_after_dispatch", [False, True])
def test_coarse_recall_intent_is_never_resent_by_a_second_restore(
    tmp_path, monkeypatch, cancel_after_dispatch
):
    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
        },
    )
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )
    persisted_at_dispatch = []

    async def coarse_move(command, **_options):
        await command()
        persisted_at_dispatch.append(
            json.loads((tmp_path / "scan-manifest.json").read_text())["return_coarse_recall"]
        )
        scanner.physical_state = "stopped"
        if cancel_after_dispatch:
            raise asyncio.CancelledError
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    async def confirm_stop():
        scanner.physical_state = "stopped"

    scanner._move = coarse_move
    scanner._confirm_stop = confirm_stop
    if cancel_after_dispatch:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scanner._restore())
    else:
        asyncio.run(scanner._restore())
    resumed_checkpoint = json.loads((tmp_path / "scan-manifest.json").read_text())
    resumed = scan._Scan(
        camera, tmp_path, _progress, lambda: False, resumed_checkpoint
    )
    resumed.acquired = True
    resumed.capabilities = camera.capabilities
    resumed.last_frame, resumed.last_pose = initial, camera._position()

    async def forbidden_move(*_args, **_options):
        raise AssertionError("A persisted coarse intent must never be resent")

    async def resumed_stop():
        resumed.physical_state = "stopped"

    resumed._move = forbidden_move
    resumed._confirm_stop = resumed_stop
    asyncio.run(resumed._restore())

    assert _event_names(camera).count("return") == 1
    assert persisted_at_dispatch[0]["state"] == "planned"
    assert resumed.checkpoint["return_coarse_recall"]["state"] == (
        "planned" if cancel_after_dispatch else "observed"
    )


def test_resumed_normal_capture_materializes_a_new_epoch_before_outbound_motion(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    legacy_recall = {"epoch": "final", "state": "observed"}
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "return_epoch": "final",
            "return_coarse_recall": legacy_recall,
            "return_coarse_recalls": {"final": legacy_recall},
            "physical_state": "restored",
            "captures": [{"id": "existing-grid-frame", "role": "grid"}],
        },
    )
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    scanner.physical_state = "stopped"
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    dispatched = []
    original_absolute = camera.move_absolute

    async def audited_absolute(*, pan, tilt):
        manifest = json.loads((tmp_path / "scan-manifest.json").read_text())
        dispatched.append(
            {
                "epoch": manifest["return_epoch"],
                "recalls": copy.deepcopy(manifest["return_coarse_recalls"]),
            }
        )
        return await original_absolute(pan=pan, tilt=tilt)

    camera.move_absolute = audited_absolute

    # Persisting an opened/observed resume does not rotate the epoch.
    asyncio.run(scanner._persist())
    assert scanner.checkpoint["return_epoch"] == "final"
    opened = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert opened["restored_return_epoch"] == "final"
    assert not dispatched

    asyncio.run(scanner._absolute({"pan": 0.1, "tilt": 0.0}))
    scan_epoch = scanner.checkpoint["return_epoch"]
    assert scan_epoch.startswith("normal:") and scan_epoch != "final"
    assert dispatched[0] == {
        "epoch": scan_epoch,
        "recalls": {"final": legacy_recall},
    }

    asyncio.run(scanner._restore())
    assert _event_names(camera).count("return") == 1
    assert scanner.checkpoint["return_coarse_recalls"][scan_epoch]["state"] == "observed"
    assert dispatched[1]["epoch"] == scan_epoch
    assert dispatched[1]["recalls"][scan_epoch]["state"] == "planned"

    # Merely retrying the completed return keeps the same epoch and command count.
    asyncio.run(scanner._restore())
    assert scanner.checkpoint["return_epoch"] == scan_epoch
    assert _event_names(camera).count("return") == 1


def test_safe_continuous_without_a_controllable_axis_uses_calibrated_absolute_finish(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras import panorama_navigation

    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.png"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "pilot_response": {"pan": 500.0, "tilt": 500.0},
            "pilot_attempts": [
                {
                    "axis": axis,
                    "outward_verified": True,
                    "cycle_closure": _closed_pilot_cycle(),
                }
                for axis in ("pan", "tilt")
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = {
        **camera.capabilities,
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {},
    }
    scanner.last_frame, scanner.last_pose = initial, camera._position()
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )

    async def coarse_move(command, **_options):
        await command()
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    async def forbidden_visual(*_args):
        raise AssertionError("Global ContinuousMove support is insufficient without an axis")

    absolute_calls = []

    async def interrupted_absolute(target, **options):
        absolute_calls.append((target, options))
        raise asyncio.CancelledError

    scanner._move = coarse_move
    scanner._absolute = interrupted_absolute
    monkeypatch.setattr(panorama_navigation, "correct_reference", forbidden_visual)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scanner._restore())

    assert len(absolute_calls) == 1
    assert scanner.checkpoint["return_correction_strategy"]["finish"] == "absolute_calibrated"
    assert scanner.checkpoint["return_correction_strategy"]["visual_axes"] == []


def test_absolute_return_retries_share_one_persisted_correction_budget(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    initial_path = tmp_path / "initial.jpg"
    cv2.imwrite(str(initial_path), initial["image"])
    checkpoint = {
        "initial_path": str(initial_path),
        "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
        "pilot_response": {"pan": 500.0, "tilt": 500.0},
        "absolute_return_corrections": [
            {"kind": "correction", "state": "observed", "before_pixels": 8.0}
            for _ in range(scan.MAXIMUM_RETURN_CORRECTIONS)
        ],
        "absolute_return_correction_state": "rejected",
    }
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    camera.pan = 0.1
    scanner.last_frame = asyncio.run(camera.frame())
    scanner.last_pose = camera._position()

    async def coarse_move(command, **_options):
        await command()
        camera.remaining_motion_frames = 0
        scanner.last_frame = await camera.frame()
        scanner.last_pose = await camera.position()
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    scanner._move = coarse_move
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )

    asyncio.run(scanner._restore())

    assert _event_names(camera).count("absolute") == 1, "Only the coarse return may run"
    assert len(scanner.checkpoint["absolute_return_corrections"]) == 4
    assert scanner.checkpoint["absolute_return_correction_state"] == "budget_exhausted"


def test_absolute_return_persists_intent_before_a_cancellable_correction(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())
    initial_path = tmp_path / "initial.jpg"
    cv2.imwrite(str(initial_path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(initial_path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "pilot_response": {"pan": 500.0, "tilt": 500.0},
            "pilot_attempts": [
                {
                    "axis": axis,
                    "outward_verified": True,
                    "cycle_closure": _closed_pilot_cycle(),
                }
                for axis in ("pan", "tilt")
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    camera.pan = 0.1
    scanner.last_frame = asyncio.run(camera.frame())
    scanner.last_pose = camera._position()

    async def coarse_move(command, **_options):
        await command()
        camera.remaining_motion_frames = 0
        scanner.last_frame = await camera.frame()
        scanner.last_pose = await camera.position()
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "pose": scanner.last_pose, "stable": True}

    scanner._move = coarse_move
    observed = []

    async def interrupted_correction(target, **options):
        manifest = json.loads((tmp_path / "scan-manifest.json").read_text())
        observed.append((target, options, manifest))
        raise asyncio.CancelledError

    scanner._absolute = interrupted_correction
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 6.0,
            "shift_x": 6.0,
            "shift_y": 0.0,
        },
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scanner._restore())

    assert len(observed) == 1
    assert observed[0][1]["expected_frame"] is scanner.last_frame
    planned = observed[0][2]["absolute_return_corrections"][-1]
    assert planned["state"] == "planned"
    assert observed[0][2]["absolute_return_correction_state"] == "active"
    assert len(scanner.checkpoint["absolute_return_corrections"]) == 1


def test_checkpoint_write_failure_still_stops_and_releases_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def fail_persistence(self):
        raise OSError("simulated local disk failure")

    monkeypatch.setattr(scan._Scan, "_persist", fail_persistence)
    try:
        asyncio.run(
            scan.run_panorama_scan(camera, tmp_path, progress=_progress, cancelled=lambda: False)
        )
    except OSError:
        pass

    events = _event_names(camera)
    assert "acquire" in events
    assert "stop" in events
    assert events[-1] == "release", "Manifest persistence must not bypass resource cleanup"
    assert camera.lease is None


def test_failed_resume_relocalization_releases_video_without_slewing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint = {
        "version": 1,
        "source_identity": copy.deepcopy(camera.capabilities["source_identity"]),
        "captures": [],
    }

    result = asyncio.run(
        scan.run_panorama_scan(
            camera,
            tmp_path,
            progress=_progress,
            cancelled=lambda: False,
            checkpoint=checkpoint,
        )
    )

    events = _event_names(camera)
    assert camera.sequence > 0, "The resume path opened the video source"
    assert events[-1] == "release"
    assert not set(events) & {"absolute", "velocity", "return"}
    assert any(issue["code"] == "relocalization_required" for issue in result["issues"])


def test_unexpected_exception_after_command_still_stops_and_releases_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def fail_after_movement(self, limits):
        await self.camera.move_absolute(pan=0.1, tilt=0.05)
        raise RuntimeError("simulated unexpected processing failure")

    monkeypatch.setattr(scan._Scan, "_absolute_scan", fail_after_movement)
    try:
        asyncio.run(
            scan.run_panorama_scan(camera, tmp_path, progress=_progress, cancelled=lambda: False)
        )
    except RuntimeError:
        pass

    events = _event_names(camera)
    movement_index = events.index("absolute")
    assert "stop" in events[movement_index + 1 :]
    assert events[-1] == "release"
    assert camera.lease is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("profile_token", "telephoto-profile"), ("width", 640), ("source_id", "telephoto")],
)
def test_resume_rejects_changed_optical_identity_before_motion(
    tmp_path: Path, field: str, replacement: Any
):
    camera = SimulatedCamera()
    identity = copy.deepcopy(camera.capabilities["source_identity"])
    identity[field] = replacement
    checkpoint = {
        "version": 1,
        "source_identity": identity,
        "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0, "zoom": None},
        "initial_path": str(tmp_path / "initial.jpg"),
        "captures": [],
        "mode": "absolute",
        "plan": [],
        "next_index": 0,
        "boundaries": {},
    }

    with pytest.raises(PanoramaCaptureError):
        asyncio.run(
            scan.run_panorama_scan(
                camera,
                tmp_path,
                progress=_progress,
                cancelled=lambda: False,
                checkpoint=checkpoint,
            )
        )

    assert "discover" in _event_names(camera)
    assert not set(_event_names(camera)) & {"acquire", "absolute", "velocity", "return"}


@pytest.mark.parametrize("legacy", [True, False])
def test_absolute_grid_checkpoint_contract_is_rejected_before_control_or_motion(
    tmp_path: Path, legacy: bool
):
    camera = SimulatedCamera()
    camera.capabilities["defaults"] = {"absolute": "normalized-position"}
    for bounds in camera.capabilities["limits"].values():
        bounds["space"] = "normalized-position"
    limits, samples, plan, geometry, steps = _grid_contract_data(camera)
    checkpoint = {
        "version": 1,
        "source_identity": copy.deepcopy(camera.capabilities["source_identity"]),
        "mode": "absolute",
        "plan": plan,
        "grid_geometry": geometry,
        "pilot_steps": steps,
        "pilot_observations": samples,
        "captures": [],
    }
    if not legacy:
        checkpoint["absolute_grid"] = scan._absolute_grid_contract(
            capabilities=camera.capabilities,
            limits=limits,
            plan=plan,
            grid_geometry=geometry,
            pilot_steps=steps,
            optical_samples=samples,
        )
        checkpoint["absolute_grid"]["target_overlap"] = 0.5

    with pytest.raises(
        PanoramaCaptureError, match="absolute_grid_checkpoint_incompatible"
    ):
        asyncio.run(
            scan.run_panorama_scan(
                camera,
                tmp_path,
                progress=_progress,
                cancelled=lambda: False,
                checkpoint=checkpoint,
            )
        )

    assert _event_names(camera) == ["discover"]


@pytest.mark.parametrize("mutation", ["point", "step", "geometry", "sample"])
def test_absolute_grid_contract_rejects_mutated_planning_evidence_before_control(
    tmp_path: Path, mutation: str
):
    camera = SimulatedCamera()
    camera.capabilities["defaults"] = {"absolute": "normalized-position"}
    for bounds in camera.capabilities["limits"].values():
        bounds["space"] = "normalized-position"
    limits, samples, plan, geometry, steps = _grid_contract_data(camera)
    checkpoint = {
        "version": 1,
        "source_identity": copy.deepcopy(camera.capabilities["source_identity"]),
        "mode": "absolute",
        "plan": plan,
        "grid_geometry": geometry,
        "pilot_steps": steps,
        "pilot_observations": samples,
        "captures": [],
    }
    checkpoint["absolute_grid"] = scan._absolute_grid_contract(
        capabilities=camera.capabilities,
        limits=limits,
        plan=plan,
        grid_geometry=geometry,
        pilot_steps=steps,
        optical_samples=samples,
    )
    if mutation == "point":
        checkpoint["plan"][0]["pan"] += 0.001
    elif mutation == "step":
        checkpoint["pilot_steps"]["pan"] *= 0.9
    elif mutation == "geometry":
        checkpoint["grid_geometry"]["tilt"]["step"] *= 0.9
        checkpoint["pilot_steps"]["tilt"] = checkpoint["grid_geometry"]["tilt"]["step"]
    else:
        checkpoint["pilot_observations"]["pan"][0]["homography"][0][2] += 1.0

    with pytest.raises(
        PanoramaCaptureError, match="absolute_grid_checkpoint_incompatible"
    ):
        asyncio.run(
            scan.run_panorama_scan(
                camera,
                tmp_path,
                progress=_progress,
                cancelled=lambda: False,
                checkpoint=checkpoint,
            )
        )
    assert _event_names(camera) == ["discover"]


def test_reference_window_starts_after_decoder_startup_within_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame

    async def cold_frame(**options):
        if camera.sequence == 0:
            camera.now += 5.0
        return await original_frame(**options)

    camera.frame = cold_frame
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    result = asyncio.run(scanner._reference_window(timeout=4.0))
    assert result["return_reference_evidence"]["window_observation_seconds"] >= 0.8
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["outcome"] == "reference_observed"
    assert 5 <= attempt["first_frame_wait_seconds"] < 5.2
    assert 0.8 <= attempt["observed_elapsed_seconds"] < 4.0
    assert attempt["total_elapsed_seconds"] < 12.0
    assert attempt["last_result"]["timing_basis"] == "media"
    assert attempt["code_counts"]["motion_transition_unobserved"] >= 5
    assert attempt["receive_intervals"]["mean"] == pytest.approx(0.1)
    assert list(tmp_path.glob("*.jpg")) == [], "Diagnostics never add photographs"
    assert json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1] == attempt


def test_reference_window_ignores_interleaved_duplicates_between_distinct_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame
    last_distinct: np.ndarray | None = None

    async def frame_with_interleaved_duplicates(**options):
        nonlocal last_distinct
        frame = await original_frame(**options)
        if camera.sequence > 2 and camera.sequence % 2:
            assert last_distinct is not None
            frame["image"] = last_distinct.copy()
        else:
            last_distinct = frame["image"].copy()
        return frame

    camera.frame = frame_with_interleaved_duplicates
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    reference = asyncio.run(scanner._reference_window(timeout=2.0))
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]

    assert reference["return_reference_evidence"]["window_observation_seconds"] >= 0.8
    assert attempt["outcome"] == "reference_observed"
    assert attempt["code_counts"]["repeated_image"] >= 4
    assert attempt["maximum_candidate_frames"] >= 5


@pytest.mark.parametrize(
    ("frames_before_timeout", "expected_code", "expected_budget_hit"),
    [
        (0, "frame_acquisition_timeout", None),
        (3, "stop_observation_unconfirmed", "observation"),
    ],
)
def test_reference_window_distinguishes_initial_timeout_from_timeout_after_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames_before_timeout: int,
    expected_code: str,
    expected_budget_hit: str | None,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame

    async def eventually_times_out(**options):
        if camera.sequence >= frames_before_timeout:
            raise asyncio.TimeoutError
        return await original_frame(**options)

    camera.frame = eventually_times_out
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    with pytest.raises(PanoramaCaptureError, match=expected_code):
        asyncio.run(scanner._reference_window(timeout=2.0))
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]

    assert attempt["frame_count"] == frames_before_timeout
    assert attempt["outcome"] == expected_code
    assert attempt.get("budget_hit") == expected_budget_hit


def test_reference_startup_cannot_exceed_total_acquisition_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame

    async def cold_frame(**options):
        camera.now += 12.0
        return await original_frame(**options)

    camera.frame = cold_frame
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    with pytest.raises(PanoramaCaptureError, match="stop_observation_unconfirmed"):
        asyncio.run(scanner._reference_window(timeout=4.0))
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["budget_hit"] == "total"
    assert attempt["frame_count"] == 1
    assert attempt["outcome"] == "stop_observation_unconfirmed"


@pytest.mark.parametrize("frozen_forever", [False, True])
def test_reference_uses_remaining_budget_after_temporary_frozen_video(tmp_path, monkeypatch, frozen_forever):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame

    async def initially_frozen(**options):
        frame = await original_frame(**options)
        if frozen_forever or camera.sequence < 50:
            frame["image"] = cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR)
        return frame

    camera.frame = initially_frozen
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    if frozen_forever:
        with pytest.raises(PanoramaCaptureError, match="stop_observation_unconfirmed"):
            asyncio.run(scanner._reference_window())
        assert scanner.checkpoint["diagnostics"]["attempts"][-1]["budget_hit"] == "total"
    else:
        result = asyncio.run(scanner._reference_window())
        assert result["return_reference_evidence"]["window_observation_seconds"] >= 0.8
        elapsed = scanner.checkpoint["diagnostics"]["attempts"][-1]["total_elapsed_seconds"]
        assert 5 < elapsed < 12
    assert not set(_event_names(camera)) & {"velocity", "absolute", "return"}


def test_failed_reference_records_actual_rejection_codes_without_frame_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    camera.blank = True
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    with pytest.raises(PanoramaCaptureError, match="stop_observation_unconfirmed"):
        asyncio.run(scanner._reference_window(timeout=1.1))
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["code_counts"]["repeated_image"] > 5
    assert attempt["last_result"]["code"] == "media_time_discontinuity"
    assert attempt["last_result"]["has_motion_transition"] is False
    assert attempt["comparison_count"] == 0
    assert attempt["budget_hit"] == "observation"
    assert attempt["last_result"]["evidence"] == "insufficient"


def test_attempt_diagnostics_are_bounded_and_exclude_unlisted_frame_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    frame = asyncio.run(camera.frame())
    frame.update(password="private-password", rtsp_url="rtsp://private", image_path="private-path")
    for index in range(50):
        attempt = scan._AttemptDiagnostic("movement")
        attempt.frame(
            frame,
            {
                "code": "moving",
                "speed_px_s": 10.0,
                "confidence": 0.7,
                "image": frame["image"],
                "password": "private-password",
            },
        )
        attempt.value["outcome"] = "interrupted"
        scanner._diagnostic(attempt)
    persisted = (tmp_path / "scan-diagnostics.json").read_text()
    assert len(json.loads(persisted)["attempts"]) == scan.MAX_DIAGNOSTIC_ATTEMPTS
    assert len(persisted) < 100_000
    assert "private" not in persisted
    assert "password" not in persisted
    assert '"image"' not in persisted


def test_movement_replay_is_lossless_bounded_and_keeps_timing_without_secrets(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    frame = asyncio.run(camera.frame())
    gray = scan._gray(frame["image"])
    _, encoded = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    monkeypatch.setattr(scan, "MAX_REPLAY_BYTES", encoded.nbytes)
    for _ in range(3):
        attempt = scan._AttemptDiagnostic("movement")
        attempt.frame(frame, {"code": "first_frame"}, pose={"pan": .1, "password": "private-secret"})
        attempt.frame(frame, {"code": "moving"})
        attempt.value["outcome"] = "motion_not_observed"
        asyncio.run(scanner._preserve_replay(attempt))
    records = list(tmp_path.glob("replay-rejected-*.json"))
    assert len(records) == 2
    record = json.loads(records[0].read_text())
    assert not record["complete"]
    assert record["frames"][0]["sequence"] == frame["sequence"]
    assert record["frames"][0]["received_monotonic"] == frame["received_monotonic"]
    assert "private-secret" not in records[0].read_text()
    with np.load(records[0].with_suffix(".npz"), allow_pickle=False) as images:
        assert list(images) == ["frame_0"]
        np.testing.assert_array_equal(images["frame_0"], gray)


@pytest.mark.parametrize("distributed_background", [True, False])
def test_matching_tries_another_consensus_without_relaxing_spatial_gates(
    monkeypatch: pytest.MonkeyPatch, distributed_background: bool
):
    generator = np.random.default_rng(8512)
    foreground = generator.uniform([80, 330], [220, 410], (120, 2)).astype(np.float32)
    background = generator.uniform([30, 20], [930, 520], (80, 2)).astype(np.float32)
    source = np.concatenate([foreground, background]) if distributed_background else foreground
    target = (
        np.concatenate([foreground + [50, 2], background + [-30, -4]])
        if distributed_background
        else foreground + [50, 2]
    )
    descriptors = generator.normal(size=(len(source), 128)).astype(np.float32)

    class Features:
        def detectAndCompute(self, image, _mask):
            points = source if image[0, 0] == 0 else target
            return [cv2.KeyPoint(float(x), float(y), 3) for x, y in points], descriptors

    monkeypatch.setattr(scan.cv2, "SIFT_create", lambda **options: Features())
    result = scan._match(np.zeros((540, 960), np.uint8), np.ones((540, 960), np.uint8))
    assert result["verified"] is distributed_background
    candidates = result["model_candidates"]
    assert candidates[0]["hull_fraction"] < 0.12
    assert len(candidates) <= 3
    if distributed_background:
        assert candidates[1]["occupied_cells"] >= 4
        assert candidates[1]["hull_fraction"] >= 0.12
        assert result["shift_x"] == pytest.approx(-30, abs=0.01)
        assert result["shift_y"] == pytest.approx(-4, abs=0.01)


def test_robust_homography_is_reproducible_across_concurrent_calls():
    generator = np.random.default_rng(13092)
    source = generator.uniform([20, 20], [940, 700], (160, 2)).astype(np.float32)
    target = source + np.float32([87, -6])
    target[-35:] = generator.uniform([20, 20], [940, 700], (35, 2))

    def estimate(_attempt: int):
        homography, mask = scan._deterministic_homography(source, target)
        assert homography is not None and mask is not None
        return homography, mask

    with ThreadPoolExecutor(max_workers=4) as executor:
        estimates = list(executor.map(estimate, range(12)))

    expected_homography, expected_mask = estimates[0]
    for homography, mask in estimates[1:]:
        np.testing.assert_array_equal(mask, expected_mask)
        np.testing.assert_allclose(homography, expected_homography, atol=0, rtol=0)


def test_command_match_accepts_localized_support_without_weakening_global_match(
    monkeypatch: pytest.MonkeyPatch,
):
    generator = np.random.default_rng(9173)
    source = generator.uniform([80, 260], [400, 440], (120, 2)).astype(np.float32)
    target = source + [45, 3]
    descriptors = generator.normal(size=(len(source), 128)).astype(np.float32)

    class Features:
        def detectAndCompute(self, image, _mask):
            points = source if image[0, 0] == 0 else target
            return [cv2.KeyPoint(float(x), float(y), 3) for x, y in points], descriptors

    monkeypatch.setattr(scan.cv2, "SIFT_create", lambda **options: Features())
    first = np.zeros((540, 960), np.uint8)
    second = np.ones((540, 960), np.uint8)

    assert scan._match(first, second)["verified"] is False
    command = scan._command_match(first, second)
    assert command["verified"] is True
    assert command["support_scope"] == "localized_command_transition"
    assert command["inliers"] >= 80
    assert command["model_candidates"][0]["hull_fraction"] >= 0.06
    anchor = scan._anchor_match(first, second)
    assert anchor["verified"] is True
    assert anchor["support_scope"] == "localized_anchor"
    detector = scan.VisualStabilityDetector(allow_observation_timing=True)
    assert not detector.arm_verified_endpoint_transition(anchor)["has_motion_transition"]


def test_command_match_accepts_a_high_support_horizontal_texture_band(
    monkeypatch: pytest.MonkeyPatch,
):
    generator = np.random.default_rng(55291)
    source = generator.uniform([20, 10], [710, 100], (140, 2)).astype(np.float32)
    target = source + [45, 3]
    descriptors = generator.normal(size=(len(source), 128)).astype(np.float32)

    class Features:
        def detectAndCompute(self, image, _mask):
            points = source if image[0, 0] == 0 else target
            return [cv2.KeyPoint(float(x), float(y), 3) for x, y in points], descriptors

    monkeypatch.setattr(scan.cv2, "SIFT_create", lambda **options: Features())
    first = np.zeros((540, 960), np.uint8)
    second = np.ones((540, 960), np.uint8)

    assert scan._match(first, second)["verified"] is False
    command = scan._command_match(first, second)
    assert command["verified"] is True
    assert command["support_scope"] == "banded_command_transition"
    assert command["inliers"] >= 80
    support = command["model_candidates"][0]
    assert support["occupied_cells"] == 3
    assert 0.10 <= support["hull_fraction"] < 0.12


def test_axis_command_match_accepts_strong_vertical_banded_support_for_tilt(
    monkeypatch: pytest.MonkeyPatch,
):
    ordinary = {"verified": False, "code": "correspondences_not_distributed"}
    vertical = {
        "verified": True,
        "support_scope": "localized_command_transition",
        "inliers": 175,
        "model_candidates": [
            {"inliers": 175, "occupied_cells": 3, "hull_fraction": 0.087}
        ],
        "overlap": 0.91,
        "displacement": 31.0,
        "shift_x": 1.5,
        "shift_y": 29.0,
    }
    monkeypatch.setattr(scan, "_command_match", lambda *_: ordinary)
    calls = []

    def correspondence(*_images, **options):
        calls.append(options)
        return vertical

    monkeypatch.setattr(scan, "_correspondence_match", correspondence)
    image = np.zeros((540, 960), dtype=np.uint8)

    result = scan._axis_command_match(image, image, axis="tilt")
    assert result["verified"] is True
    assert result["support_scope"] == "tilt_banded_command_transition"
    assert calls == [
        {
            "localized_command_support": True,
            "minimum_inliers": 120,
            "minimum_occupied_cells": 3,
            "minimum_hull_fraction": 0.075,
        }
    ]


def test_axis_command_match_accepts_strong_horizontal_banded_support_for_pan(
    monkeypatch: pytest.MonkeyPatch,
):
    ordinary = {"verified": False, "code": "correspondences_not_distributed"}
    monkeypatch.setattr(scan, "_command_match", lambda *_: ordinary)
    monkeypatch.setattr(
        scan,
        "_correspondence_match",
        lambda *_images, **_options: {
            "verified": True,
            "overlap": 0.92,
            "displacement": 41.0,
            "shift_x": 39.0,
            "shift_y": 4.0,
        },
    )
    image = np.zeros((540, 960), dtype=np.uint8)

    result = scan._axis_command_match(image, image, axis="pan")
    assert result["verified"] is True
    assert result["support_scope"] == "pan_banded_command_transition"


def test_axis_command_match_rejects_banded_support_on_the_wrong_axis(
    monkeypatch: pytest.MonkeyPatch,
):
    ordinary = {"verified": False, "code": "correspondences_not_distributed"}
    monkeypatch.setattr(scan, "_command_match", lambda *_: ordinary)
    monkeypatch.setattr(
        scan,
        "_correspondence_match",
        lambda *_images, **_options: {
            "verified": True,
            "overlap": 0.9,
            "displacement": 42.0,
            "shift_x": 40.0,
            "shift_y": 7.0,
        },
    )
    image = np.zeros((540, 960), dtype=np.uint8)

    assert scan._axis_command_match(image, image, axis="tilt") is ordinary


def test_command_match_accepts_sparse_features_only_when_broadly_distributed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "insufficient_correspondences"},
    )
    calls = []

    def correspondence(*_images, **options):
        calls.append(options)
        minimum = options.get("minimum_inliers")
        if minimum != 20:
            return {"verified": False, "code": "insufficient_correspondences"}
        return {
            "verified": True,
            "support_scope": "localized_command_transition",
            "inliers": 23,
            "model_candidates": [
                {"inliers": 23, "occupied_cells": 4, "hull_fraction": 0.23}
            ],
            "overlap": 0.86,
            "displacement": 147.0,
            "shift_x": 76.0,
            "shift_y": 46.0,
            "homography": [[1.0, 0.0, 76.0], [0.0, 1.0, 46.0], [0.0, 0.0, 1.0]],
        }

    monkeypatch.setattr(scan, "_correspondence_match", correspondence)
    image = np.zeros((540, 960), dtype=np.uint8)
    result = scan._command_match(image, image.copy())

    assert result["verified"] is True
    assert result["support_scope"] == "sparse_distributed_command_transition"
    assert calls[-1] == {
        "localized_command_support": True,
        "minimum_inliers": 20,
        "minimum_occupied_cells": 4,
        "minimum_hull_fraction": 0.12,
    }


def test_temporal_command_match_requires_repeatable_sparse_endpoint_geometry(
    monkeypatch: pytest.MonkeyPatch,
):
    frames = [
        {
            "image": np.full((20, 30), index, dtype=np.uint8),
            "capture_instance": "test-decoder",
            "sequence": index,
            "generation": 1,
            "received_monotonic": 100.0 + index * 0.1,
        }
        for index in range(1, 8)
    ]

    def sparse_match(_first, second, **_options):
        shift = 100.0 + float(second[0, 0])
        return {
            "verified": True,
            "support_scope": "localized_command_transition",
            "inliers": 42,
            "model_candidates": [
                {"inliers": 42, "occupied_cells": 3, "hull_fraction": 0.07}
            ],
            "overlap": 0.78,
            "displacement": shift * 1.4,
            "shift_x": shift,
            "shift_y": 4.0,
            "homography": [
                [1.0, 0.0, shift],
                [0.0, 1.0, 4.0],
                [0.0, 0.0, 1.0],
            ],
        }

    monkeypatch.setattr(scan, "_correspondence_match", sparse_match)
    result = asyncio.run(
        scan._temporal_command_match(
            np.zeros((20, 30), dtype=np.uint8), frames, axis="pan"
        )
    )

    assert result["verified"] is True
    assert result["support_scope"] == "temporal_command_transition"
    assert result["shift_x"] == pytest.approx(107.0)
    assert result["temporal_consensus"] == {
        "axis": "pan",
        "distinct_frames": 7,
        "sampled_frames": 7,
        "verified_models": 7,
        "required_models": 5,
        "direction_sign": 1,
        "direction_consistent_models": 7,
        "consistent_models": 7,
        "required_magnitude_models": 4,
        "median_primary_shift_pixels": 104.0,
        "maximum_primary_deviation_pixels": 3.0,
        "allowed_primary_deviation_pixels": 20.8,
    }


def test_temporal_command_match_rejects_an_inconsistent_final_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    frames = [
        {
            "image": np.full((20, 30), index, dtype=np.uint8),
            "capture_instance": "test-decoder",
            "sequence": index,
            "generation": 1,
            "received_monotonic": 100.0 + index * 0.1,
        }
        for index in range(1, 8)
    ]

    def sparse_match(_first, second, **_options):
        shift = -100.0 if second[0, 0] >= 5 else 100.0
        return {
            "verified": True,
            "inliers": 42,
            "model_candidates": [
                {"inliers": 42, "occupied_cells": 3, "hull_fraction": 0.07}
            ],
            "overlap": 0.78,
            "displacement": 140.0,
            "shift_x": shift,
            "shift_y": 4.0,
            "homography": [
                [1.0, 0.0, shift],
                [0.0, 1.0, 4.0],
                [0.0, 0.0, 1.0],
            ],
        }

    monkeypatch.setattr(scan, "_correspondence_match", sparse_match)
    result = asyncio.run(
        scan._temporal_command_match(
            np.zeros((20, 30), dtype=np.uint8), frames, axis="pan"
        )
    )

    assert result["verified"] is False
    assert result["code"] == "temporal_endpoint_consensus_unverified"


def test_temporal_command_match_accepts_a_directional_majority_with_model_jitter(
    monkeypatch: pytest.MonkeyPatch,
):
    frames = [
        {
            "image": np.full((20, 30), index, dtype=np.uint8),
            "capture_instance": "test-decoder",
            "sequence": index,
            "generation": 1,
            "received_monotonic": 100.0 + index * 0.1,
        }
        for index in range(1, 8)
    ]
    shifts = [100.0, 102.0, 98.0, 101.0, 145.0, 150.0, 155.0]

    def sparse_match(_first, second, **_options):
        shift = shifts[int(second[0, 0]) - 1]
        return {
            "verified": True,
            "inliers": 42,
            "model_candidates": [
                {"inliers": 42, "occupied_cells": 3, "hull_fraction": 0.07}
            ],
            "overlap": 0.78,
            "displacement": shift * 1.2,
            "shift_x": shift,
            "shift_y": 4.0,
            "homography": [
                [1.0, 0.0, shift],
                [0.0, 1.0, 4.0],
                [0.0, 0.0, 1.0],
            ],
        }

    monkeypatch.setattr(scan, "_correspondence_match", sparse_match)
    result = asyncio.run(
        scan._temporal_command_match(
            np.zeros((20, 30), dtype=np.uint8), frames, axis="pan"
        )
    )

    assert result["verified"] is True
    assert result["temporal_consensus"]["direction_consistent_models"] == 7
    assert result["temporal_consensus"]["consistent_models"] == 4
    assert result["temporal_consensus"]["required_magnitude_models"] == 4


@pytest.mark.parametrize(("translation", "stationary"), [(0.1, True), (30.0, False)])
def test_no_effect_match_measures_narrow_support_without_becoming_motion_evidence(
    monkeypatch: pytest.MonkeyPatch, translation: float, stationary: bool
):
    generator = np.random.default_rng(3319)
    # Two occupied cells model a textured strip on an otherwise plain wall.
    # That is deliberately insufficient for navigation or panorama geometry.
    source = generator.uniform([80, 240], [430, 330], (64, 2)).astype(np.float32)
    target = source + [translation, 0.05]
    descriptors = generator.normal(size=(len(source), 128)).astype(np.float32)

    class Features:
        def detectAndCompute(self, image, _mask):
            points = source if image[0, 0] == 0 else target
            return [cv2.KeyPoint(float(x), float(y), 3) for x, y in points], descriptors

    monkeypatch.setattr(scan.cv2, "SIFT_create", lambda **options: Features())
    first = np.zeros((540, 960), np.uint8)
    second = np.ones((540, 960), np.uint8)

    assert scan._match(first, second)["verified"] is False
    assert scan._command_match(first, second)["verified"] is False
    result = scan._no_effect_match(first, second)
    assert result["verified"] is True
    assert result["support_scope"] == "localized_no_effect"
    assert (result["displacement"] <= scan.UNCONFIRMED_NO_EFFECT_PIXELS) is stationary
    detector = scan.VisualStabilityDetector(allow_observation_timing=True)
    assert not detector.arm_verified_endpoint_transition(result)["has_motion_transition"]


def test_no_effect_match_accepts_fewer_points_only_with_broad_spatial_support(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )
    calls = []

    def correspondence(*_images, **options):
        calls.append(options)
        if options["minimum_inliers"] == 48:
            return {"verified": False, "code": "insufficient_correspondences"}
        return {
            "verified": True,
            "support_scope": "localized_command_transition",
            "inliers": 37,
            "model_candidates": [
                {"inliers": 37, "occupied_cells": 3, "hull_fraction": 0.13}
            ],
            "overlap": 0.997,
            "displacement": 0.04,
            "shift_x": 0.03,
            "shift_y": 0.01,
            "homography": [[1.0, 0.0, 0.03], [0.0, 1.0, 0.01], [0.0, 0.0, 1.0]],
        }

    monkeypatch.setattr(scan, "_correspondence_match", correspondence)
    image = np.zeros((540, 960), dtype=np.uint8)
    result = scan._no_effect_match(image, image.copy())

    assert result["verified"] is True
    assert result["support_scope"] == "sparse_distributed_no_effect"
    assert calls[-1] == {
        "localized_command_support": True,
        "minimum_inliers": 24,
        "minimum_occupied_cells": 3,
        "minimum_hull_fraction": 0.10,
    }


def test_native_stationary_evidence_requires_unchanged_commanded_axes():
    before = {"native_pan": 18.0, "native_tilt": 200.0}
    stopped = {
        "native_pan": 18.0,
        "native_tilt": 200.0,
        "move_status": "IDLE",
        "error": "",
    }

    evidence = scan._native_axes_unchanged(
        before, stopped, {"pan": -0.1, "tilt": 0.0}
    )

    assert evidence == {
        "verified": True,
        "axes": {"pan": {"before": 18.0, "after": 18.0}},
        "move_status": "IDLE",
    }
    assert scan._native_axes_unchanged(
        before,
        {**stopped, "native_pan": 17.0},
        {"pan": -0.1, "tilt": 0.0},
    ) is None
    assert scan._native_axes_unchanged(
        before, stopped, {"pan": 0.0, "tilt": 0.0}
    ) is None


def test_axis_direction_evidence_uses_prior_camera_response():
    captures = [
        {
            "movement": {"axis": "pan", "direction": -1},
            "previous_overlap": {
                "verified": True,
                "shift_x": shift,
                "shift_y": 3.0,
            },
        }
        for shift in (92.0, 101.0, 96.0)
    ]
    matching = {
        "verified": True,
        "overlap": 0.86,
        "displacement": 83.0,
        "shift_x": -82.0,
        "shift_y": 4.0,
    }

    evidence = scan._axis_direction_evidence(captures, "pan", 1, matching)

    assert evidence is not None
    assert evidence["verified"] is True
    assert evidence["expected_image_shift_sign"] == -1
    assert scan._axis_direction_evidence(
        captures, "pan", -1, matching
    ) is None


def test_pilot_reduces_excursion_and_returns_between_rejected_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    commands = []

    async def absolute(self, target, *, allow_stationary=False):
        before = self.last_pose["pan"]
        commands.append(target["pan"])
        await camera.move_absolute(**target)
        for _ in range(4):
            self.last_frame = await camera.frame()
        self.last_pose = await camera.position()
        delta = self.last_pose["pan"] - before
        matching = _translation_match(delta * 300, 0.0)
        matching["verified"] = abs(delta) <= 0.015
        return {
            "frame": self.last_frame,
            "pose": self.last_pose,
            "match": matching,
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
        }

    monkeypatch.setattr(scan._Scan, "_absolute", absolute)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        scanner.last_frame = await camera.frame()
        step = await scanner._pilot_axis(
            "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
        )
        return step, scanner.checkpoint

    step, checkpoint = asyncio.run(scenario())
    assert commands == pytest.approx([0.02, 0.0, 0.01, 0.0, 0.02, 0.0])
    assert step == pytest.approx(0.01)
    assert len(checkpoint["pilot_attempts"]) == 3
    assert checkpoint["grid_pilot_budget"]["cycles_planned"] == 3
    assert checkpoint["pilot_response"]["pan"] == pytest.approx(300)
    assert all(
        record["cycle_closure"]["geometry_status"] == "not_validated"
        for record in checkpoint["pilot_attempts"]
    )
    assert all(
        np.isfinite(record["cycle_closure"]["overlap"])
        for record in checkpoint["pilot_attempts"]
    )


def test_pilot_never_commits_geometry_when_the_visual_cycle_does_not_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())
    moves = []

    async def absolute(target, **_options):
        before = dict(scanner.last_pose)
        scanner.last_pose = {**before, **target}
        delta = scanner.last_pose["pan"] - before["pan"]
        moves.append(target["pan"])
        return {
            "frame": await camera.frame(),
            "pose": dict(scanner.last_pose),
            "match": _translation_match(delta * 500, delta * 120),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
        }

    async def accept(result, **_options):
        return result

    async def persist():
        pass

    scanner._absolute = absolute
    scanner._accept = accept
    scanner._persist = persist
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {
            "verified": True,
            "overlap": 0.9,
            "displacement": 3.01,
        },
    )

    with pytest.raises(PanoramaCaptureError, match="pilot_cycle_unconfirmed"):
        asyncio.run(
            scanner._pilot_axis(
                "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
            )
        )

    assert moves == pytest.approx([0.02, 0.0])
    assert not scanner.checkpoint.get("pilot_observations")
    assert "plan" not in scanner.checkpoint


@pytest.mark.parametrize("state", ["planned", "outward_observed"])
def test_persisted_incomplete_pilot_never_replays_a_command(tmp_path: Path, state: str):
    camera = SimulatedCamera()
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "pilot_attempts": [
                {
                    "axis": "pan",
                    "attempt": 1,
                    "cycle_id": "pan:1",
                    "requested_delta": 0.02,
                    "state": state,
                }
            ],
            "grid_pilot_budget": {
                "maximum_cycles_per_axis": scan.MAX_GRID_PILOT_CYCLES_PER_AXIS,
                "captures_per_cycle": 2,
                "cycles_planned": 1,
            },
        },
    )
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())
    commands = []

    async def absolute(*args, **kwargs):
        commands.append((args, kwargs))
        raise AssertionError("persisted pilot command was replayed")

    scanner._absolute = absolute
    with pytest.raises(PanoramaCaptureError, match="pilot_resume_unavailable"):
        asyncio.run(
            scanner._pilot_axis(
                "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
            )
        )
    assert commands == []


def test_persisted_closed_pilot_reuses_optical_evidence_without_movement(tmp_path: Path):
    camera = SimulatedCamera()
    samples = _bidirectional_samples(0.04, 300.0, 0.0)
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "pilot_attempts": [
                {
                    "axis": "pan",
                    "attempt": 1,
                    "cycle_id": "pan:1",
                    "requested_delta": 0.04,
                    "state": "cycle_closed",
                    "cycle_closure": _closed_pilot_cycle(),
                }
            ],
            "pilot_observations": {"pan": samples},
            "grid_pilot_budget": {
                "maximum_cycles_per_axis": scan.MAX_GRID_PILOT_CYCLES_PER_AXIS,
                "captures_per_cycle": 2,
                "cycles_planned": 1,
            },
        },
    )
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())

    async def forbidden_absolute(*_args, **_kwargs):
        raise AssertionError("closed pilot must be reused")

    scanner._absolute = forbidden_absolute
    step = asyncio.run(
        scanner._pilot_axis(
            "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
        )
    )
    assert step == pytest.approx(0.04)


def test_closed_pilot_crash_boundary_rehydrates_nested_evidence_without_movement(
    tmp_path: Path,
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    camera = SimulatedCamera()
    _, samples_by_axis, _, _, _ = _grid_contract_data(camera)
    samples = samples_by_axis["pan"]
    nested = [
        {
            key: copy.deepcopy(value)
            for key, value in sample.items()
            if key not in {"cycle_closed", "pilot_cycle_id"}
        }
        for sample in samples
    ]
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": "one"}, {"id": "two"}],
        "pilot_attempts": [
            {
                "axis": "pan",
                "attempt": 1,
                "cycle_id": "pan:1",
                "requested_delta": 0.2,
                "state": "cycle_closed",
                "cycle_closure": _closed_pilot_cycle(),
                "outward_observation": nested[0],
                "return_observation": nested[1],
            }
        ],
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": scan.MAX_GRID_PILOT_CYCLES_PER_AXIS,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    unchanged = copy.deepcopy(checkpoint)
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None
    assert checkpoint == unchanged

    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())

    async def forbidden_absolute(*_args, **_kwargs):
        raise AssertionError("closed nested pilot was replayed")

    scanner._absolute = forbidden_absolute
    step = asyncio.run(
        scanner._pilot_axis(
            "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
        )
    )
    assert step == pytest.approx(0.2)
    assert len(scanner.checkpoint["pilot_observations"]["pan"]) == 2


def test_stationary_no_op_then_closed_pilot_resumes_without_replaying_movement(
    tmp_path: Path,
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    camera = SimulatedCamera()
    _, samples_by_axis, _, _, _ = _grid_contract_data(camera)
    samples = copy.deepcopy(samples_by_axis["pan"])
    for sample in samples:
        sample["pilot_cycle_id"] = "pan:2"
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": "one"}, {"id": "two"}],
        "pilot_attempts": [
            {
                "axis": "pan",
                "attempt": 1,
                "cycle_id": "pan:1",
                "requested_delta": 0.01,
                "state": "stationary_no_op",
            },
            {
                "axis": "pan",
                "attempt": 2,
                "cycle_id": "pan:2",
                "requested_delta": 0.2,
                "state": "cycle_closed",
                "cycle_closure": _closed_pilot_cycle(),
            },
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": scan.MAX_GRID_PILOT_CYCLES_PER_AXIS,
            "captures_per_cycle": 2,
            "cycles_planned": 2,
        },
    }
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None

    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())

    async def forbidden_absolute(*_args, **_kwargs):
        raise AssertionError("closed pilot after no-op was replayed")

    scanner._absolute = forbidden_absolute
    step = asyncio.run(
        scanner._pilot_axis(
            "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
        )
    )
    assert step == pytest.approx(0.2)

    no_op_only = copy.deepcopy(checkpoint)
    no_op_only["pilot_attempts"] = no_op_only["pilot_attempts"][:1]
    no_op_only["pilot_observations"] = {}
    no_op_only["grid_pilot_budget"]["cycles_planned"] = 1
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": no_op_only})
        == "pilot_resume_unavailable"
    )


def test_persisted_pilot_budget_mismatch_fails_before_movement(tmp_path: Path):
    camera = SimulatedCamera()
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "pilot_attempts": [],
            "grid_pilot_budget": {
                "maximum_cycles_per_axis": scan.MAX_GRID_PILOT_CYCLES_PER_AXIS,
                "captures_per_cycle": 2,
                "cycles_planned": 1,
            },
        },
    )
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())
    scanner._absolute = lambda *_args, **_kwargs: pytest.fail("movement attempted")
    with pytest.raises(PanoramaCaptureError, match="pilot_resume_unavailable"):
        asyncio.run(
            scanner._pilot_axis(
                "pan", {"pan": 0.0, "tilt": 0.0}, camera.capabilities["limits"]
            )
        )


def test_absolute_scan_persists_canonical_limits_before_first_pilot(tmp_path: Path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = camera.capabilities
    scanner.last_pose = camera._position()
    persisted = []

    async def persist():
        persisted.append(copy.deepcopy(scanner.checkpoint))

    async def pilot(*_args, **_kwargs):
        assert persisted
        assert persisted[-1]["absolute_pilot_limits"] == {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        }
        raise scan._Stopped()

    scanner._persist = persist
    scanner._pilot_axis = pilot
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._absolute_scan(camera.capabilities["limits"]))


@pytest.mark.parametrize("stored_limits", [None, "malformed", "tampered"])
def test_preplan_resume_rejects_missing_or_changed_pilot_limits_before_control(
    tmp_path: Path, stored_limits: str | None
):
    camera = SimulatedCamera()
    limits: Any
    if stored_limits is None:
        limits = None
    elif stored_limits == "malformed":
        limits = {"pan": {"min": float("nan"), "max": 0.2, "space": None}}
    else:
        limits = {
            "pan": {"min": -0.2, "max": 0.19, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        }
    checkpoint = {
        "version": 1,
        "source_identity": copy.deepcopy(camera.capabilities["source_identity"]),
        "mode": "absolute",
        "captures": [],
        "pilot_attempts": [{"axis": "pan", "state": "cycle_closed"}],
        "absolute_pilot_limits": limits,
    }

    with pytest.raises(PanoramaCaptureError, match="pilot_resume_unavailable"):
        asyncio.run(
            scan.run_panorama_scan(
                camera,
                tmp_path,
                progress=_progress,
                cancelled=lambda: False,
                checkpoint=checkpoint,
            )
        )
    assert _event_names(camera) == ["discover"]


@pytest.mark.parametrize("axis", ["pan", "tilt"])
def test_quantized_pilot_expands_only_after_confirmed_stationary_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, axis: str
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = camera.capabilities
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())
    origin = {"pan": 0.0, "tilt": 0.0}
    commands = []
    stop_confirmations = []

    async def absolute(target, **_options):
        commands.append(target[axis])
        if len(commands) == 1:
            raise PanoramaCaptureError("motion_not_observed")
        before = dict(scanner.last_pose)
        scanner.last_pose = {**before, **target}
        delta = scanner.last_pose[axis] - before[axis]
        shift = delta * 300
        return {
            "frame": scanner.last_frame,
            "pose": dict(scanner.last_pose),
            "match": _translation_match(shift if axis == "pan" else 0, shift if axis == "tilt" else 0),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
        }

    async def confirm_stop():
        stop_confirmations.append(True)
        scanner.physical_state = "stopped"

    async def readback():
        return dict(scanner.last_pose)

    async def accept(result, **_options):
        return result

    scanner._absolute = absolute
    scanner._confirm_stop = confirm_stop
    scanner._absolute_readback = readback
    scanner._accept = accept
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )

    step = asyncio.run(scanner._pilot_axis(axis, origin, camera.capabilities["limits"]))
    first_amplitude = 0.02 if axis == "pan" else 0.01
    assert commands[0] == pytest.approx(first_amplitude)
    assert commands[1] == pytest.approx(first_amplitude * 2)
    assert stop_confirmations == [True]
    assert scanner.checkpoint["pilot_attempts"][0]["state"] == "stationary_no_op"
    assert len(scanner.checkpoint["pilot_attempts"]) <= scan.MAX_GRID_PILOT_CYCLES_PER_AXIS
    assert step > 0


def test_absolute_grid_uses_per_axis_optical_overlap_without_symmetric_span_cap():
    plan, geometry = scan._absolute_grid_plan(
        limits={
            "pan": {"min": -1.0, "max": 1.0},
            "tilt": {"min": -1.0, "max": 1.0},
        },
        axis_samples={
            "pan": _bidirectional_samples(0.17, 293.42, 0.0),
            "tilt": _bidirectional_samples(0.67, 0.0, 160.8),
        },
        captures_used=4,
        maximum_captures=256,
    )

    assert geometry["pan"]["count"] == 13
    assert geometry["tilt"]["count"] == 4
    assert len(plan) == 52
    assert geometry["pan"]["step"] == pytest.approx(1 / 6)
    assert geometry["tilt"]["step"] == pytest.approx(2 / 3)
    assert geometry["pan"]["step"] < geometry["tilt"]["step"]
    assert all(
        axis_geometry["predicted_overlap"] >= scan.GRID_TARGET_OVERLAP
        for axis_geometry in geometry.values()
    )
    assert {item["pan"] for item in plan} >= {-1.0, 1.0}
    assert sorted({item["tilt"] for item in plan}) == pytest.approx(
        [-1.0, -1 / 3, 1 / 3, 1.0]
    )
    assert [item["pan"] for item in plan[:13]] == sorted(
        item["pan"] for item in plan[:13]
    )
    assert [item["pan"] for item in plan[13:26]] == sorted(
        (item["pan"] for item in plan[13:26]), reverse=True
    )


@pytest.mark.parametrize(
    ("span", "sample", "maximum_count"),
    [
        (2.0, _optical_sample(0.1, 1e-13, 0.0), 256),
        (2.0, _optical_sample(-0.1, -172.6, 0.0), 256),
        (2.0, _optical_sample(0.1, 0.0, 24.0), 256),
        (1e-7, _optical_sample(1e-7, 1.0, 0.0), 256),
        (2.0, _optical_sample(0.1, 300.0, 0.0), 256),
    ],
)
def test_axis_overlap_geometry_is_finite_bounded_and_preserves_overlap(
    span: float, sample: dict[str, Any], maximum_count: int
):
    geometry = scan._axis_overlap_geometry(
        span=span,
        optical_samples=[
                sample,
                _optical_sample(
                    -sample["device_delta"],
                    -sample["shift_x"],
                    -sample["shift_y"],
                ),
        ],
        maximum_count=maximum_count,
    )

    assert 2 <= geometry["count"] <= maximum_count
    assert math.isfinite(geometry["step"])
    assert 0 < geometry["step"] <= span
    assert (geometry["count"] - 1) * geometry["step"] == pytest.approx(span)
    assert geometry["predicted_overlap"] >= scan.GRID_TARGET_OVERLAP
    assert geometry["optical_sample_count"] == 2


@pytest.mark.parametrize("field,value", [("device_delta", 0.0), ("overlap", float("nan")), ("verified", False)])
def test_axis_overlap_geometry_rejects_invalid_optical_sample(field: str, value: Any):
    samples = _bidirectional_samples(0.1, 20.0, 5.0)
    samples[0][field] = value
    with pytest.raises(PanoramaCaptureError) as captured:
        scan._axis_overlap_geometry(
            span=2.0,
            optical_samples=samples,
            maximum_count=256,
        )
    assert captured.value.code == "pilot_geometry_unconfirmed"


def test_axis_overlap_geometry_uses_cross_axis_homography_without_axis_mapping():
    geometry = scan._axis_overlap_geometry(
        span=2.0,
        optical_samples=_bidirectional_samples(0.15, 0.0, 170.0),
        maximum_count=256,
    )
    slower_only = scan._axis_overlap_geometry(
        span=2.0,
        optical_samples=_bidirectional_samples(0.1, 0.0, 120.0),
        maximum_count=256,
    )

    assert geometry["optical_sample_count"] == 2
    assert geometry["step"] > slower_only["step"]
    assert geometry["predicted_overlap"] >= scan.GRID_TARGET_OVERLAP


def test_grid_never_extrapolates_pilot_homography_past_observed_amplitude():
    width, height, focal = 960, 540, 650.0
    intrinsic = np.array(
        [[focal, 0.0, (width - 1) / 2], [0.0, focal, (height - 1) / 2], [0.0, 0.0, 1.0]]
    )
    rotation_vector = np.array([-0.0937431, -0.16781892, 0.2770923])
    scale = 1.3410093652721584

    def pinhole_homography(vector):
        rotation = cv2.Rodrigues(np.asarray(vector, dtype=np.float64))[0]
        homography = intrinsic @ rotation @ np.linalg.inv(intrinsic)
        return homography / homography[2, 2]

    pilot = pinhole_homography(rotation_vector)
    old_linear = np.eye(3) + scale * (pilot - np.eye(3))
    old_linear /= old_linear[2, 2]
    actual = pinhole_homography(rotation_vector * scale)
    linear_overlap = scan._homography_overlap(old_linear, width=width, height=height)
    actual_overlap = scan._homography_overlap(actual, width=width, height=height)
    pilot_overlap = scan._homography_overlap(pilot, width=width, height=height)

    assert linear_overlap == pytest.approx(0.6804, abs=0.0006)
    assert actual_overlap == pytest.approx(0.6698, abs=0.0006)
    assert pilot_overlap is not None and pilot_overlap > scan.GRID_TARGET_OVERLAP
    sample = {
        "verified": True,
        "device_delta": 0.05,
        "overlap": pilot_overlap,
        "homography": pilot.tolist(),
        "analysis_size": [width, height],
        "cycle_closed": True,
    }
    reverse_pilot = np.linalg.inv(pilot)
    reverse_pilot /= reverse_pilot[2, 2]
    reverse_sample = {
        **sample,
        "device_delta": -0.05,
        "overlap": scan._homography_overlap(reverse_pilot, width=width, height=height),
        "homography": reverse_pilot.tolist(),
    }
    with pytest.raises(PanoramaCaptureError, match="coverage_exceeds_capture_budget"):
        scan._axis_overlap_geometry(
            span=0.05 * scale,
            optical_samples=[sample, reverse_sample],
            maximum_count=2,
        )


@pytest.mark.parametrize("safe_continuous", [True, False])
def test_unclosed_real_scale_pilot_falls_back_only_to_safe_continuous(
    tmp_path: Path, safe_continuous: bool
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": safe_continuous,
        "axes": {"pan": True, "tilt": True},
    }
    scanner.checkpoint["pilot_attempts"] = [
        {
            "axis": axis,
            "cycle_closure": {
                "visually_closed": False,
                "match_verified": True,
                "overlap": 0.9,
                "displacement": displacement,
            },
        }
        for axis, displacement in (("pan", 31.0), ("tilt", 17.0))
    ]
    continuous_calls = []

    async def failed_absolute(_limits):
        raise PanoramaCaptureError("pilot_cycle_unconfirmed")

    async def continuous():
        continuous_calls.append(True)

    scanner._absolute_scan = failed_absolute
    scanner._continuous_scan = continuous

    if safe_continuous:
        asyncio.run(scanner._acquire_panorama(camera.capabilities["limits"]))
        assert continuous_calls == [True]
        assert scanner.checkpoint["absolute_grid_fallback"]["state"] == "complete"
    else:
        with pytest.raises(PanoramaCaptureError, match="pilot_cycle_unconfirmed"):
            asyncio.run(scanner._acquire_panorama(camera.capabilities["limits"]))
        assert continuous_calls == []
        assert scanner.checkpoint["absolute_grid_fallback"]["state"] == "unavailable"
    assert scanner.checkpoint["absolute_grid_fallback"]["absolute_plan_published"] is False
    assert "plan" not in scanner.checkpoint


def test_unclosed_absolute_pilot_falls_back_to_relative_only_control(tmp_path: Path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": False,
        "continuous_supported": False,
        "relative_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    continuous_calls = []

    async def failed_absolute(_limits):
        raise PanoramaCaptureError("pilot_cycle_unconfirmed")

    async def continuous():
        continuous_calls.append(True)

    scanner._absolute_scan = failed_absolute
    scanner._continuous_scan = continuous
    asyncio.run(scanner._acquire_panorama(camera.capabilities["limits"]))

    assert continuous_calls == [True]
    assert scanner.checkpoint["absolute_grid_fallback"] == {
        "state": "complete",
        "reason": "pilot_cycle_unconfirmed",
        "from": "absolute",
        "to": "continuous",
        "continuous_mode": "relative",
        "absolute_plan_published": False,
    }


def test_persisted_relative_fallback_overrides_newly_available_velocity(tmp_path: Path):
    camera = SimulatedCamera()
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "mode": "continuous",
            "absolute_grid_fallback": {
                "state": "active",
                "reason": "pilot_cycle_unconfirmed",
                "from": "absolute",
                "to": "continuous",
                "continuous_mode": "relative",
                "absolute_plan_published": False,
            },
        },
    )
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": True,
        "relative_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    relative_calls = []

    async def relative(**delta):
        relative_calls.append(delta)
        return {"accepted": True}

    async def move(command, **_options):
        await command()
        return {"stable": True}

    camera.move_relative = relative
    scanner._move = move
    asyncio.run(scanner._pulse("pan", 1, 0.3))

    assert relative_calls == [{"pan": 0.03, "tilt": 0.0}]
    assert "velocity" not in _event_names(camera)


@pytest.mark.parametrize("resume_marker", ["pending", "cursor"])
@pytest.mark.parametrize("continuous_mode", ["velocity", "relative"])
def test_continuous_fallback_resume_never_reenters_absolute_pilots(
    tmp_path: Path, resume_marker: str, continuous_mode: str
):
    camera = SimulatedCamera()
    checkpoint = {
        "mode": "continuous_fallback_pending",
        "absolute_grid_fallback": {
            "state": "active",
            "reason": "pilot_cycle_unconfirmed",
            "from": "absolute",
            "to": "continuous",
            "continuous_mode": continuous_mode,
            "absolute_plan_published": False,
        },
    }
    if resume_marker == "cursor":
        checkpoint.update(
            mode="continuous",
            continuous_cursor={"version": scan.CONTINUOUS_CURSOR_VERSION, "stage": "reference"},
        )
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": continuous_mode == "velocity",
        "relative_supported": continuous_mode == "relative",
        "axes": {"pan": True, "tilt": True},
    }
    continuous_calls = []

    async def forbidden_absolute(_limits):
        raise AssertionError("fallback resume reentered absolute planning")

    async def continuous():
        continuous_calls.append(True)

    scanner._absolute_scan = forbidden_absolute
    scanner._continuous_scan = continuous
    asyncio.run(scanner._acquire_panorama(camera.capabilities["limits"]))
    assert continuous_calls == [True]
    assert scanner.checkpoint["absolute_grid_fallback"]["state"] == "complete"


def test_continuous_cursor_is_persisted_before_reference_discovery_can_move(tmp_path: Path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {
        **camera.capabilities,
        "continuous_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    persisted = []

    async def persist():
        persisted.append(copy.deepcopy(scanner.checkpoint))

    async def find_reference(_cursor):
        assert persisted[-1]["mode"] == "continuous"
        assert persisted[-1]["continuous_cursor"]["stage"] == "reference"
        raise scan._Stopped()

    scanner._persist = persist
    scanner._find_reference = find_reference
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._continuous_scan())
    assert len(persisted) == 1


def test_quantized_pilot_exhaustion_routes_to_safe_continuous_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {
        **camera.capabilities,
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    scanner.last_pose = camera._position()
    scanner.last_frame = asyncio.run(camera.frame())
    commands = []
    continuous_calls = []

    async def quantized_no_op(target, **_options):
        commands.append(target["pan"])
        raise PanoramaCaptureError("motion_not_observed")

    async def confirm_stop():
        scanner.physical_state = "stopped"

    async def readback():
        return dict(scanner.last_pose)

    async def continuous():
        continuous_calls.append(True)

    scanner._absolute = quantized_no_op
    scanner._confirm_stop = confirm_stop
    scanner._absolute_readback = readback
    scanner._continuous_scan = continuous
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )

    asyncio.run(scanner._acquire_panorama(camera.capabilities["limits"]))
    assert commands == pytest.approx([0.02, 0.04, 0.08])
    assert continuous_calls == [True]
    assert scanner.checkpoint["grid_pilot_budget"]["cycles_planned"] == 3
    assert scanner.checkpoint["absolute_grid_fallback"]["state"] == "complete"


def test_absolute_grid_refuses_to_weaken_overlap_to_fit_capture_budget():
    options = {
        "limits": {
            "pan": {"min": -1.0, "max": 1.0},
            "tilt": {"min": -1.0, "max": 1.0},
        },
        "axis_samples": {
            "pan": _bidirectional_samples(0.17, 293.42, 0.0),
            "tilt": _bidirectional_samples(0.67, 0.0, 160.8),
        },
        "captures_used": 205,
        "maximum_captures": 256,
    }
    with pytest.raises(PanoramaCaptureError) as captured:
        scan._absolute_grid_plan(**options)
    assert captured.value.code == "coverage_exceeds_capture_budget"

    with pytest.raises(PanoramaCaptureError) as extreme:
        scan._axis_overlap_geometry(
            span=2.0,
            optical_samples=_bidirectional_samples(0.01, 20.0, 0.0),
            maximum_count=10,
        )
    assert extreme.value.code == "coverage_exceeds_capture_budget"


def test_absolute_grid_accepts_a_fixed_axis_and_keeps_both_moving_axis_limits():
    plan, geometry = scan._absolute_grid_plan(
        limits={
            "pan": {"min": -1.0, "max": 1.0},
            "tilt": {"min": 0.25, "max": 0.25},
        },
        axis_samples={"pan": _bidirectional_samples(0.17, 293.42, 0.0), "tilt": []},
        captures_used=2,
        maximum_captures=256,
    )

    assert geometry["tilt"]["count"] == 1
    assert geometry["tilt"]["step"] == 0
    assert plan[0] == {"pan": -1.0, "tilt": 0.25, "row": 0}
    assert plan[-1] == {"pan": 1.0, "tilt": 0.25, "row": 0}
    assert all(math.isfinite(item[axis]) for item in plan for axis in ("pan", "tilt"))


@pytest.mark.parametrize("other_axis", [None, 0.015])
def test_local_return_probe_rejects_unknown_or_confounded_other_axis(
    tmp_path: Path, other_axis: float | None
):
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, None)
    accepted = scanner._remember_axis_response(
        "pan",
        {"verified": True, "shift_x": 6.0, "shift_y": 15.0},
        {"pan": 0, "tilt": 0},
        {"pan": 0.006, "tilt": other_axis},
        kind="return_probe",
    )
    assert not accepted
    assert "return_jacobian" not in scanner.checkpoint


def test_return_recovers_partial_gain_when_reported_pose_already_matches_saved_pose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame
    visual_offset = 0.0

    async def optical_frame(**options):
        frame = await original_frame(**options)
        image = cv2.warpAffine(
            camera.texture,
            np.array(
                [[1, 0, camera.pan * 600 + visual_offset], [0, 1, camera.tilt * 300]], np.float32
            ),
            (320, 240),
            borderMode=cv2.BORDER_REFLECT,
        )
        noise = np.random.default_rng(camera.sequence).normal(0, 0.5, image.shape)
        frame["image"] = cv2.cvtColor(
            np.clip(image + noise, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR
        )
        return frame

    camera.frame = optical_frame
    initial = asyncio.run(camera.frame())
    camera.pan = 0.1
    pilot = asyncio.run(camera.frame())
    initial_path, pilot_path = tmp_path / "initial.jpg", tmp_path / "pilot.jpg"
    cv2.imwrite(str(initial_path), initial["image"])
    cv2.imwrite(str(pilot_path), pilot["image"])
    checkpoint = {
        "source_identity": camera.capabilities["source_identity"],
        "initial_path": str(initial_path),
        "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0, "zoom": None},
        "captures": [
            {
                "id": "pilot",
                "path": str(pilot_path),
                "role": "pilot_pan",
                "pose": {"pan": 0.1, "tilt": 0.0},
            }
        ],
        "pilot_attempts": [
            {
                "axis": "pan",
                "outward_verified": True,
                "cycle_closure": _closed_pilot_cycle(),
            }
        ],
    }
    camera.pan = 0.0
    camera.target = (0.0, 0.0)
    visual_offset = 10.7  # 32.1 pixels at the independent 960-pixel analysis scale.
    camera.events.clear()
    result = asyncio.run(
        scan.run_panorama_scan(
            camera,
            tmp_path,
            progress=_progress,
            cancelled=lambda: False,
            checkpoint=checkpoint,
            return_only=True,
        )
    )
    assert result["physical_state"] == "restored"
    assert result["checkpoint"]["pilot_response"]["pan"] == pytest.approx(1800, rel=0.03)
    comparison = json.loads((tmp_path / "return-validation.json").read_text())
    assert comparison["displacement"] <= 3
    positions = [event[1]["pan"] for event in camera.events if event[0] == "absolute"]
    assert positions[0] == 0.0, "Initial recall readback already equals the saved position"
    assert len(positions) <= 5
    assert max(abs(second - first) for first, second in zip(positions, positions[1:])) <= 0.01001


@pytest.mark.parametrize("physical_state", ["stopped", "stop_unconfirmed"])
def test_uncertain_return_preserves_temporary_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, physical_state: str
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    saved = {"kind": "preset", "preset_token": "return-token", "owner_id": camera.owner_id}
    checkpoint = {"source_identity": camera.capabilities["source_identity"], "return": saved}

    async def unconfirmed(self):
        self.physical_state = physical_state
        self.issues.append({"code": "return_framing_unconfirmed"})

    monkeypatch.setattr(scan._Scan, "_restore", unconfirmed)
    result = asyncio.run(
        scan.run_panorama_scan(
            camera,
            tmp_path,
            progress=_progress,
            cancelled=lambda: False,
            checkpoint=checkpoint,
            return_only=True,
        )
    )
    assert "remove_return" not in _event_names(camera)
    assert result["checkpoint"]["return"] == saved


@pytest.mark.parametrize("target_tilt", [0.05, 0.003])
def test_absolute_stop_waits_for_optical_motion_when_status_reports_target_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_tilt: float
):
    class AheadOfMotor(SimulatedCamera):
        delay = 0
        reported_early = False
        optical_scale = 600  # 1800 pixels/native unit at the detector's 960-pixel width.

        def _position(self):
            pose = super()._position()
            if self.reported_early:
                pose.update(pan=self.target[0], tilt=self.target[1], move_status="IDLE")
            return pose

        async def move_absolute(self, **target):
            receipt = await super().move_absolute(**target)
            self.delay = 12
            self.reported_early = True
            return receipt

        async def frame(self, **options):
            # Let the command schedule its mechanical delay before deciding
            # whether this frame can advance the independently rendered motor.
            await asyncio.sleep(0)
            if self.delay:
                pending = self.remaining_motion_frames
                self.remaining_motion_frames = 0
                frame = await super().frame(**options)
                self.remaining_motion_frames = pending
                self.delay -= 1
                return frame
            return await super().frame(**options)

    camera = AheadOfMotor()
    _clock(monkeypatch, camera)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        return await scanner._move(
            lambda: camera.move_absolute(pan=0.0, tilt=target_tilt),
            target={"pan": 0.0, "tilt": target_tilt},
            allow_stationary=True,
        )

    result = asyncio.run(scenario())
    assert result["stable"]
    assert camera.tilt == pytest.approx(target_tilt)
    attempt = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
    assert attempt["first_target_readback_seconds"] < attempt["first_motion_transition_seconds"]
    assert attempt["stop_requested_seconds"] > attempt["first_motion_transition_seconds"]
    assert attempt["motion_transition_before_stop"] is True
    assert attempt["stop_reason"] == "visual_settled"


def test_continuous_stop_uses_remaining_transport_budget_after_preflight(tmp_path, monkeypatch):
    class SlowPreflight(SimulatedCamera):
        acknowledged_at = None
        first_stop_at = None

        async def move_velocity(self, **options):
            self.now += 2.0
            receipt = await super().move_velocity(**options)
            self.acknowledged_at = self.now
            return {**receipt, "pulse_remaining_seconds": 0.3}

        async def stop(self):
            if self.first_stop_at is None:
                self.first_stop_at = self.now
            return await super().stop()

    camera = SlowPreflight()
    _clock(monkeypatch, camera)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        return await scanner._move(lambda: camera.move_velocity(pan=0.1, timeout_s=0.4),
                                   duration=0.4, allow_stationary=True)

    asyncio.run(scenario())
    assert camera.first_stop_at - camera.acknowledged_at >= 0.3
    assert camera.first_stop_at - camera.acknowledged_at < 0.6


def test_slow_progress_callback_finishes_before_a_movement_is_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def progress(event):
        if event.get("phase") == "waiting_for_stability":
            assert event["physical_state"] == "unknown"
            await asyncio.sleep(0)
            assert "absolute" not in _event_names(camera)
            camera.now += 2.0

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        scanner.physical_state = "stopped"
        return await scanner._move(
            lambda: camera.move_absolute(pan=0.1, tilt=0.05),
            target={"pan": 0.1, "tilt": 0.05},
        )

    assert asyncio.run(scenario())["stable"]


def test_telemetry_thins_long_observations_and_preserves_first_and_last_samples(
    monkeypatch: pytest.MonkeyPatch,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    diagnostic = scan._AttemptDiagnostic("movement")
    for index in range(5000):
        camera.now = 100.0 + index * 0.01
        diagnostic.frame(
            {"received_monotonic": camera.now, "media_time": None, "generation": 1},
            {
                "timing_basis": "local_observation",
                "motion_pixels": 0.2,
                "speed_px_s": 20.0,  # Cannot survive without media timestamps.
                "drift_pixels": 0.4,
                "confidence": 0.9,
                "state": "unexpected-internal-code",
                "transport": "rtsp://private-secret@example.test/path",
            },
            pose={"pan": 0.2, "tilt": None, "zoom": 0.5, "password": "private-secret"},
        )
    diagnostic.value["outcome"] = "stability_timeout"
    telemetry = diagnostic.telemetry()
    samples = telemetry["samples"]
    assert len(samples) <= 128
    assert samples[0]["elapsed_seconds"] == 0
    assert samples[-1]["elapsed_seconds"] == pytest.approx(49.99)
    assert [sample["elapsed_seconds"] for sample in samples] == sorted(
        sample["elapsed_seconds"] for sample in samples
    )
    assert all(sample["speed_px_s"] is None and sample["media_time"] is None for sample in samples)
    assert all(sample["state"] == "unknown" for sample in samples)
    assert telemetry["outcome"] == "timeout"
    assert "private-secret" not in json.dumps(telemetry, allow_nan=False)
    assert set(samples[-1]["pose"]) == {"pan", "tilt", "native_pan", "native_tilt"}


def test_completed_movement_telemetry_is_sent_once_after_stop_with_local_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame
    events = []

    async def frame_without_media_time(**options):
        return {**await original_frame(**options), "media_time": None}

    async def progress(event):
        if event.get("telemetry"):
            assert camera.remaining_motion_frames == 0
            assert "stop" in _event_names(camera)
        events.append(copy.deepcopy(event))

    camera.frame = frame_without_media_time

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        result = await scanner._move(
            lambda: camera.move_absolute(pan=0.1, tilt=0.05),
            target={"pan": 0.1, "tilt": 0.05},
        )
        await scanner._emit("capturing")
        await scanner._emit("capturing")
        return result

    assert asyncio.run(scenario())["stable"]
    delivered = [event["telemetry"] for event in events if "telemetry" in event]
    assert len(delivered) == 1
    telemetry = delivered[0]
    assert telemetry["timing_basis"] == "local_observation"
    assert telemetry["outcome"] == "accepted"
    assert telemetry["first_motion_transition_seconds"] < telemetry["stop_requested_seconds"]
    assert all(sample["speed_px_s"] is None for sample in telemetry["samples"])
    persisted = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
    assert persisted["samples"] == telemetry["samples"]


@pytest.mark.parametrize("previous_failed_attempt", [False, True])
def test_coarse_device_arrival_handles_quantization_without_successful_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_failed_attempt: bool
):
    class QuantizedTilt(SimulatedCamera):
        async def move_absolute(self, *, pan, tilt):
            return await super().move_absolute(pan=pan, tilt=round(tilt * 64) / 64)

    camera = QuantizedTilt()
    camera.tilt = 21 / 64
    camera.target = (0.0, camera.tilt)
    _clock(monkeypatch, camera)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        if previous_failed_attempt:
            with pytest.raises(PanoramaCaptureError, match="motion_not_observed"):
                await scanner._absolute({"pan": 0.0, "tilt": 1 / 3})
        assert "last_settled_absolute" not in scanner.checkpoint
        result = await scanner._absolute({"pan": 0.1, "tilt": 1 / 3})
        return result, scanner.checkpoint

    result, checkpoint = asyncio.run(scenario())
    assert result["stable"]
    assert camera.tilt == 21 / 64
    attempt = checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["arrival_policy"] == "coarse_device_arrival"
    assert attempt["coarse_device_arrival"] is True
    assert attempt["arrival_delta_device_units"]["tilt"] == pytest.approx(21 / 64 - 1 / 3)
    assert attempt["requested_target"] == {"pan": 0.1, "tilt": 1 / 3}
    assert attempt["total_elapsed_seconds"] < 5
    assert "last_settled_absolute" not in checkpoint


def test_coarse_arrival_does_not_accept_a_large_command_mismatch_even_when_image_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class MissesTarget(SimulatedCamera):
        async def move_absolute(self, *, pan, tilt):
            return await super().move_absolute(pan=pan - 0.05, tilt=tilt)

    camera = MissesTarget()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_pose = camera._position()
    with pytest.raises(PanoramaCaptureError, match="stability_timeout"):
        asyncio.run(scanner._absolute({"pan": 0.1, "tilt": 0.05}))
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["code_counts"]["stable"] > 0
    assert attempt["coarse_device_arrival"] is False
    assert attempt["arrival_delta_device_units"]["pan"] == pytest.approx(-0.05)
    assert not scanner.captures and "stop" in _event_names(camera)


def _graph_node(scanner, plan, index, marker):
    image = np.full((240, 320, 3), marker * 20, np.uint8)
    path = scanner.directory / f"node-{index}.jpg"
    cv2.imwrite(str(path), image)
    capture = {
        "id": f"node-{index}",
        "path": str(path),
        "role": "grid",
        "plan_index": index,
        "row_index": plan[index]["row"],
        "pose": {axis: plan[index][axis] for axis in ("pan", "tilt")},
    }
    scanner.captures.append(capture)
    return capture


def _marker_match(allowed):
    def matching(first, second):
        pair = tuple(
            sorted((round(float(first[0, 0, 0]) / 20), round(float(second[0, 0, 0]) / 20)))
        )
        return (
            {"verified": True, "overlap": 0.8, "displacement": 50.0}
            if pair in allowed
            else {"verified": False, "code": "correspondences_not_distributed"}
        )

    return matching


def test_vertical_grid_edge_connects_when_the_last_horizontal_view_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    plan = [
        {"pan": -0.2, "tilt": -0.1, "row": 0},
        {"pan": 0.2, "tilt": -0.1, "row": 0},
        {"pan": -0.2, "tilt": 0.1, "row": 1},
        {"pan": 0.2, "tilt": 0.1, "row": 1},
    ]
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, {"plan": plan})
    nodes = [_graph_node(scanner, plan, index, index + 1) for index in range(4)]
    scanner._prepare_graph(plan)
    scanner._record_edge(
        nodes[0], nodes[1], {"verified": True, "overlap": 0.8}, method="grid_neighbour"
    )
    scanner._record_edge(
        nodes[0], nodes[2], {"verified": True, "overlap": 0.8}, method="grid_neighbour"
    )
    monkeypatch.setattr(scan, "_match", _marker_match({(2, 4)}))
    monkeypatch.setattr(scan, "_texture_support", lambda image: {"distributed": False})
    asyncio.run(scanner._connect_grid(nodes[3], plan, base_remaining=0))
    confirmed, unresolved = scanner._graph_confirmation(plan, set(range(4)))
    assert confirmed == {0, 1, 2, 3} and not unresolved
    assert any(
        {edge["first"], edge["second"]} == {"node-1", "node-3"}
        for edge in scanner.checkpoint["graph_edges"]
    )
    assert len(scanner.captures) == 4


@pytest.mark.parametrize(
    ("capture_budget", "textured", "expected_bridges"), [(4, True, 1), (3, True, 0), (4, False, 0)]
)
def test_midpoint_bridge_requires_texture_and_preserves_every_remaining_base_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_budget: int,
    textured: bool,
    expected_bridges: int,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    plan = [
        {"pan": -0.2, "tilt": 0, "row": 0},
        {"pan": 0.2, "tilt": 0, "row": 0},
        {"pan": 0.2, "tilt": 0.1, "row": 1},
    ]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"plan": plan})
    nodes = [_graph_node(scanner, plan, index, index + 1) for index in range(2)]
    scanner._prepare_graph(plan)
    commands = []

    async def absolute(target):
        commands.append(target)
        frame = {**await camera.frame(), "image": np.full((240, 320, 3), 60, np.uint8)}
        return {"stable": True, "frame": frame, "pose": target, "evidence": {}, "match": {}}

    scanner._absolute = absolute
    monkeypatch.setattr(scan, "MAX_CAPTURES", capture_budget)
    monkeypatch.setattr(scan, "_texture_support", lambda image: {"distributed": textured})
    monkeypatch.setattr(scan, "_match", _marker_match({(1, 3), (2, 3)}))
    asyncio.run(scanner._connect_grid(nodes[1], plan, base_remaining=1))
    assert len(commands) == expected_bridges
    assert len(scanner.captures) + 1 <= capture_budget
    if expected_bridges:
        assert commands == [{"pan": 0.0, "tilt": 0.0}]
        assert scanner._graph_confirmation(plan, {0, 1}) == ({0, 1}, set())
        # Resume keeps the exact verified edges and consumed subdivision budget.
        scanner.checkpoint["captures"] = scanner.captures
        checkpoint = copy.deepcopy(scanner.checkpoint)
        resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
        resumed._prepare_graph(plan)
        assert resumed.checkpoint["graph_edges"] == checkpoint["graph_edges"]
        assert resumed.checkpoint["bridge_attempts"] == checkpoint["bridge_attempts"]
        assert resumed._graph_confirmation(plan, {0, 1}) == ({0, 1}, set())


def test_bridge_joins_rows_that_already_have_internal_horizontal_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    plan = [
        {"pan": pan, "tilt": tilt, "row": row}
        for row, tilt in enumerate((-0.1, 0.1))
        for pan in (-0.2, 0.2)
    ]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"plan": plan})
    nodes = [_graph_node(scanner, plan, index, index + 1) for index in range(4)]
    scanner._prepare_graph(plan)
    scanner._record_edge(
        nodes[0], nodes[1], {"verified": True, "overlap": 0.8}, method="grid_neighbour"
    )

    async def absolute(target):
        return {
            "stable": True,
            "frame": {**await camera.frame(), "image": np.full((240, 320, 3), 100, np.uint8)},
            "pose": target,
            "evidence": {},
            "match": {},
        }

    scanner._absolute = absolute
    monkeypatch.setattr(scan, "_texture_support", lambda image: {"distributed": True})
    monkeypatch.setattr(scan, "_match", _marker_match({(3, 4), (2, 5), (4, 5)}))
    asyncio.run(scanner._connect_grid(nodes[3], plan, base_remaining=0))
    assert len(scanner.captures) == 5
    assert scanner._graph_confirmation(plan, {0, 1, 2, 3}) == ({0, 1, 2, 3}, set())


def test_bridge_subdivision_budget_survives_resume_and_cannot_repeat_indefinitely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    plan = [{"pan": -0.2, "tilt": 0, "row": 0}, {"pan": 0.2, "tilt": 0, "row": 0}]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"plan": plan})
    nodes = [_graph_node(scanner, plan, index, index + 1) for index in range(2)]
    scanner._prepare_graph(plan)
    commands = []

    async def absolute(target):
        commands.append(target)
        return {
            "stable": True,
            "frame": await camera.frame(),
            "pose": target,
            "evidence": {},
            "match": {},
        }

    scanner._absolute = absolute
    monkeypatch.setattr(scan, "_texture_support", lambda image: {"distributed": True})
    monkeypatch.setattr(scan, "_match", _marker_match(set()))
    asyncio.run(scanner._connect_grid(nodes[1], plan, base_remaining=0))
    assert len(commands) == 2
    scanner.checkpoint["captures"] = scanner.captures
    resumed = scan._Scan(
        camera, tmp_path, _progress, lambda: False, copy.deepcopy(scanner.checkpoint)
    )
    resumed._absolute = absolute
    asyncio.run(resumed._connect_grid(resumed.captures[1], plan, base_remaining=0))
    assert len(commands) == 2
    assert len(resumed.checkpoint["bridge_attempts"]["0:1"]["subdivisions"]) == 2


def test_texture_support_rejects_a_wall_and_keeps_distributed_detail():
    camera = SimulatedCamera()
    assert scan._texture_support(cv2.cvtColor(camera.texture, cv2.COLOR_GRAY2BGR))["distributed"]
    wall = np.full((240, 320, 3), 128, np.uint8)
    cv2.putText(wall, "14:43:00", (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    assert not scan._texture_support(wall)["distributed"]


def test_origin_first_row_order_preserves_unvisited_prefix_and_resumes_every_base_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    plan = [
        {"pan": pan, "tilt": tilt, "row": row}
        for row, tilt in enumerate((-0.1, 0.0, 0.1))
        for pan in (-0.2, 0.2)
    ]
    checkpoint = {"plan": plan, "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0}}
    scanner = scan._Scan(
        camera, tmp_path, _progress, lambda: len(scanner.captures) >= 1, checkpoint
    )
    scanner.capabilities = camera.capabilities
    scanner.last_pose = initial_pose = camera._position()

    async def absolute(target):
        return {
            "stable": True,
            "frame": await camera.frame(),
            "pose": target,
            "evidence": {},
            "match": {},
        }

    scanner._absolute = absolute
    monkeypatch.setattr(
        scan, "_match", lambda *images: {"verified": True, "overlap": 0.8, "displacement": 10}
    )
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._absolute_scan(camera.capabilities["limits"]))
    assert scanner.captures[0]["plan_index"] == 2
    assert scanner.checkpoint["visited_indices"] == [2]
    assert scanner.checkpoint["next_index"] == 0
    resumed = scan._Scan(
        camera, tmp_path, _progress, lambda: False, copy.deepcopy(scanner.checkpoint)
    )
    resumed.capabilities, resumed.last_pose, resumed._absolute = (
        camera.capabilities,
        initial_pose,
        absolute,
    )
    asyncio.run(resumed._absolute_scan(camera.capabilities["limits"]))
    assert {capture["plan_index"] for capture in resumed.captures} == set(range(6))
    assert resumed.checkpoint["visited_indices"] == list(range(6))
    assert resumed.checkpoint["next_index"] == 6
    assert resumed.complete


def test_legacy_graph_association_requires_a_unique_target_in_the_saved_row(tmp_path: Path):
    plan = [
        {"pan": -0.01, "tilt": 0.0, "row": 0},
        {"pan": 0.01, "tilt": 0.0, "row": 0},
        {"pan": 0.0, "tilt": 0.01, "row": 1},
    ]
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, {"plan": plan})
    scanner.captures = [
        {"id": "ambiguous", "role": "grid", "row_index": 0, "pose": {"pan": 0.0, "tilt": 0.0}}
    ]
    scanner._prepare_graph(plan)
    assert "plan_index" not in scanner.captures[0]
    confirmed, unresolved = scanner._graph_confirmation(plan, {0, 1, 2})
    assert not confirmed and unresolved == {0, 1, 2}


def test_legacy_confirmation_flags_cannot_complete_without_persisted_visual_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    plan = [{"pan": -0.2, "tilt": -0.1, "row": 0}, {"pan": 0.2, "tilt": 0.1, "row": 1}]
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "plan": plan,
            "next_index": 2,
            "confirmed_indices": [0, 1],
            "boundaries": {
                edge: {"confirmed": True} for edge in ("pan_min", "pan_max", "tilt_min", "tilt_max")
            },
        },
    )
    scanner.last_pose = camera._position()
    for index in range(2):
        _graph_node(scanner, plan, index, index + 1)
    commands = []

    async def absolute(target):
        commands.append(target)
        return {
            "stable": True,
            "frame": await camera.frame(),
            "pose": target,
            "evidence": {},
            "match": {},
        }

    scanner._absolute = absolute
    monkeypatch.setattr(
        scan, "_match", lambda *images: {"verified": False, "code": "test_missing_link"}
    )
    asyncio.run(scanner._absolute_scan(camera.capabilities["limits"]))
    assert commands, "Old confirmation flags must be rechecked against real graph evidence"
    assert not scanner.complete
    assert scanner.checkpoint["unresolved_indices"]


@pytest.mark.parametrize("failure_code", ["motion_not_observed", "control_lost"])
def test_failed_return_correction_observes_stop_without_another_return_or_false_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_code: str
):
    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())
    path = tmp_path / "initial.jpg"
    cv2.imwrite(str(path), initial["image"])
    scanner = scan._Scan(
        camera,
        tmp_path,
        _progress,
        lambda: False,
        {
            "initial_path": str(path),
            "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0},
            "pilot_response": {"pan": 500.0, "tilt": 500.0},
            "pilot_attempts": [
                {
                    "axis": axis,
                    "outward_verified": True,
                    "cycle_closure": _closed_pilot_cycle(),
                }
                for axis in ("pan", "tilt")
            ],
        },
    )
    scanner.acquired = True
    scanner.capabilities = camera.capabilities
    scanner.last_frame, scanner.last_pose = initial, camera._position()

    async def move(command, **options):
        await command()
        for _ in range(4):
            scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"
        return {"stable": True, "frame": scanner.last_frame, "pose": camera._position()}

    corrections = []

    async def failed_correction(target, **options):
        corrections.append(target)
        scanner.physical_state = "unknown"
        raise PanoramaCaptureError(failure_code)

    observations = []

    async def reference_window(**options):
        observations.append(options)
        return initial

    scanner._move, scanner._absolute, scanner._reference_window = (
        move,
        failed_correction,
        reference_window,
    )
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *images: {
            "verified": True,
            "overlap": 1.0,
            "displacement": 5.1,
            "shift_x": 0.2,
            "shift_y": 4.7,
        },
    )
    asyncio.run(scanner._restore())
    assert len(corrections) == 1
    assert _event_names(camera).count("return") == 1
    assert any(issue["code"] == "return_framing_unconfirmed" for issue in scanner.issues)
    if failure_code == "control_lost":
        assert scanner.physical_state == "ownership_lost"
        assert not observations and "stop" not in _event_names(camera)
    else:
        assert scanner.physical_state == "stopped"
        assert len(observations) == 1 and _event_names(camera).count("stop") == 1
    assert scanner.physical_state != "restored"


@pytest.mark.parametrize(("texture", "expected_attempts"), [(False, 1), (True, 3)])
def test_timeout_retries_only_skip_a_fresh_untextured_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, texture: bool, expected_attempts: int
):
    class UnmovingVideo(SimulatedCamera):
        async def move_absolute(self, *, pan, tilt):
            self.events.append(("absolute", {"pan": pan, "tilt": tilt}))
            return {"accepted": True}

        async def frame(self, **options):
            frame = await super().frame(**options)
            if not texture:
                noise = np.random.default_rng(frame["sequence"]).normal(0, 0.5, (240, 320, 3))
                frame["image"] = np.clip(127 + noise, 0, 255).astype(np.uint8)
            return frame

    camera = UnmovingVideo()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(
        camera, tmp_path, _progress, lambda: False, {"plan": [{"pan": 0.1, "tilt": 0.05, "row": 0}]}
    )
    scanner.last_pose = camera._position()
    asyncio.run(scanner._absolute_scan(camera.capabilities["limits"]))
    assert _event_names(camera).count("absolute") == expected_attempts
    assert not scanner.complete and not scanner.captures and not scanner.boundaries
    assert scanner.checkpoint["unresolved_indices"] == [0]
    assert (
        any(issue["code"] == "scene_texture_insufficient" for issue in scanner.issues)
        is not texture
    )
    observations = scanner.checkpoint["rejected_observations"]
    assert len(observations) == expected_attempts
    assert all(
        record["evidence"] == "observation_only" and record["qualified_capture"] is False
        for record in observations
    )
    assert all(
        Path(record["path"]).is_file() and len(record["sha256"]) == 64 for record in observations
    )
    if not texture:
        assert 12 <= camera.now - 100 < 14


@pytest.mark.parametrize(
    "invalidity",
    ["stale", "sequence", "generation", "media_time", "dimensions", "source_dimensions"],
)
def test_unreliable_video_cannot_be_classified_as_an_empty_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidity: str
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    frames = deque(asyncio.run(camera.frame()) for _ in range(11))
    if invalidity == "stale":
        for frame in frames:
            frame["received_monotonic"] -= 5
    elif invalidity == "sequence":
        frames[-1]["sequence"] = frames[-2]["sequence"]
    elif invalidity == "generation":
        frames[-1]["generation"] += 1
    elif invalidity == "dimensions":
        frames[-1]["image"] = frames[-1]["image"][:120]
    elif invalidity == "source_dimensions":
        frames[-1]["source_width"] = 160
        frames[-1]["source_height"] = 120
    else:
        frames[-1]["media_time"] = frames[-2]["media_time"] - 0.1
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    assert asyncio.run(scanner._terminal_scene(frames)) is None


def test_terminal_scene_uses_distinct_frames_around_interleaved_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    frames = deque()
    last_distinct = None
    for index in range(11):
        frame = asyncio.run(camera.frame())
        if index % 2 and last_distinct is not None:
            frame["image"] = last_distinct.copy()
        else:
            last_distinct = frame["image"].copy()
        frames.append(frame)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

    scene = asyncio.run(scanner._terminal_scene(frames))

    assert scene is not None
    assert scene["classification"] == "scene_texture_available"
    assert scene["received_frames"] == 11
    assert scene["distinct_frames"] == 6
    assert scene["observed_window_seconds"] >= 0.8


def test_terminal_scene_distinguishes_static_live_transport_from_decoder_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    frames = deque(asyncio.run(camera.frame()) for _ in range(11))
    static_image = frames[-1]["image"].copy()
    for frame in frames:
        frame["image"] = static_image.copy()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

    scene = asyncio.run(scanner._terminal_scene(frames))

    assert scene is not None
    assert scene["transport_window_verified"] is True
    assert scene["visual_change_observed"] is False
    assert scene["received_frames"] == 11
    assert scene["distinct_frames"] == 1
    frames[-1]["sequence"] = frames[-2]["sequence"]
    assert asyncio.run(scanner._terminal_scene(frames)) is None


def test_rejected_observation_write_failure_does_not_mask_timeout_or_skip_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)

    async def ignore_motion(**target):
        camera.events.append(("absolute", target))
        return {"accepted": True}

    camera.move_absolute = ignore_motion

    def fail_write(*args):
        raise OSError("simulated unavailable optional observation storage")

    monkeypatch.setattr(scan, "_write_image", fail_write)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_pose = camera._position()
    with pytest.raises(PanoramaCaptureError, match="motion_not_observed"):
        asyncio.run(scanner._absolute({"pan": 0.1, "tilt": 0.05}))
    assert "stop" in _event_names(camera)
    assert scanner.checkpoint["diagnostics"]["attempts"][-1]["outcome"] == "motion_not_observed"
    assert any(issue["code"] == "rejected_observation_write_failed" for issue in scanner.issues)


def test_rejected_observation_retention_counts_orphans_and_preserves_existing_files(tmp_path: Path):
    originals = {}
    for index in range(16):
        path = tmp_path / f"rejected-observation-original-{index}.jpg"
        path.write_bytes(f"existing-reference-{index}".encode())
        originals[path] = path.read_bytes()
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, None)
    diagnostic = scan._AttemptDiagnostic("movement")
    diagnostic.value["outcome"] = "motion_not_observed"
    asyncio.run(
        scanner._preserve_rejected(
            {"image": np.zeros((100, 100, 3), np.uint8)},
            {"evidence": "observation_only"},
            diagnostic,
        )
    )
    assert len(list(tmp_path.glob("rejected-observation-*.jpg"))) == 16
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert scanner.checkpoint["rejected_observations"] == []


def test_transient_initial_frame_failure_uses_remaining_original_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame
    attempts = 0

    async def cold_frame(**options):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            camera.now += 6.2
            raise PanoramaCaptureError("fresh_frame_unavailable")
        return await original_frame(**options)

    camera.frame = cold_frame
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    reference = asyncio.run(scanner._reference_window())
    assert reference["return_reference_evidence"]["provisional"]
    diagnostic = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert diagnostic["transient_frame_failures"] == 1
    assert 6.2 <= diagnostic["first_frame_wait_seconds"] < 7
    assert 0.8 <= diagnostic["observed_elapsed_seconds"] < 4
    assert diagnostic["total_elapsed_seconds"] < 12
    assert not set(_event_names(camera)) & {"absolute", "velocity", "return"}


def test_continued_initial_frame_failure_does_not_restart_the_twelve_second_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    attempts = 0

    async def unavailable_frame(**options):
        nonlocal attempts
        attempts += 1
        camera.now += options["timeout_s"] * 2
        raise PanoramaCaptureError("fresh_frame_unavailable")

    camera.frame = unavailable_frame
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    with pytest.raises(PanoramaCaptureError, match="fresh_frame_unavailable"):
        asyncio.run(scanner._reference_window())
    diagnostic = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempts == 2 and diagnostic["frame_count"] == 0
    assert diagnostic["total_elapsed_seconds"] == pytest.approx(12)
    assert diagnostic["budget_hit"] == "total"


def test_initial_source_binding_failure_is_never_retried_as_decoder_startup(tmp_path: Path):
    camera = SimulatedCamera()
    attempts = 0

    async def invalid_binding(**options):
        nonlocal attempts
        attempts += 1
        raise PanoramaCaptureError("source_binding_unverified")

    camera.frame = invalid_binding
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    with pytest.raises(PanoramaCaptureError, match="source_binding_unverified"):
        asyncio.run(scanner._reference_window())
    assert attempts == 1
    assert "transient_frame_failures" not in scanner.checkpoint["diagnostics"]["attempts"][-1]


@pytest.mark.parametrize(
    ("current_scale", "duplicate_views", "accepted"),
    [(1.0, False, True), (1.03, False, False), (1.0, True, False)],
)
def test_relocalization_keeps_early_pilot_evidence_as_the_scan_grows_without_relaxing_gates(
    tmp_path: Path, current_scale: float, duplicate_views: bool, accepted: bool
):
    camera = SimulatedCamera()
    initial = asyncio.run(camera.frame())["image"]
    shifted = (
        initial
        if duplicate_views
        else cv2.warpAffine(
            initial, np.float32([[1, 0, 6], [0, 1, 0]]), (320, 240), borderMode=cv2.BORDER_REFLECT
        )
    )
    paths = [
        tmp_path / name
        for name in ("unobservable.jpg", "initial-pilot-return.jpg", "second-pilot.jpg")
    ]
    for path, image in zip(paths, (np.zeros_like(initial), initial, shifted), strict=True):
        cv2.imwrite(str(path), image)
    captures = [
        {
            "id": f"capture-{index:04d}",
            "path": str(paths[index] if index in {1, 2} else paths[0]),
            "role": "pilot_return" if index == 1 else ("pilot_pan" if index == 2 else "grid"),
        }
        for index in range(200)
    ]
    previous_pool = set(range(194, 200)) | set(np.linspace(0, 199, 12, dtype=int))
    assert not previous_pool & {1, 2}, "The old sampler omitted both required views"
    current = cv2.warpAffine(
        initial,
        np.float32(
            [
                [current_scale, 0, 160 * (1 - current_scale)],
                [0, current_scale, 120 * (1 - current_scale)],
            ]
        ),
        (320, 240),
        borderMode=cv2.BORDER_REFLECT,
    )
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"captures": captures})
    if accepted:
        asyncio.run(scanner._relocalize({"image": current}))
    else:
        with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
            asyncio.run(scanner._relocalize({"image": current}))
    assert not set(_event_names(camera)) & {"absolute", "velocity", "return"}


def test_high_resolution_timeout_keeps_a_full_terminal_window_inside_shared_memory_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class HighResolutionWall(SimulatedCamera):
        async def move_absolute(self, **target):
            self.events.append(("absolute", target))
            return {"accepted": True}

        async def frame(self, **options):
            frame = await super().frame(**options)
            # 12 fps and 2880x1620 reproduce the real buffer pressure. Seven
            # integral RGB frames fit in96MiB, but span only half a second.
            self.now -= 0.1 - 1 / 12
            frame["received_monotonic"] = self.now
            frame["media_time"] = frame["sequence"] / 12
            noise = np.random.default_rng(frame["sequence"]).normal(0, 0.5, (540, 960))
            gray = np.clip(127 + noise, 0, 255).astype(np.uint8)
            frame["image"] = cv2.cvtColor(cv2.resize(gray, (2880, 1620)), cv2.COLOR_GRAY2BGR)
            return frame

    camera = HighResolutionWall()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(
        camera, tmp_path, _progress, lambda: False, {"plan": [{"pan": 0.1, "tilt": 0.05, "row": 0}]}
    )
    scanner.last_pose = camera._position()
    asyncio.run(scanner._absolute_scan(camera.capabilities["limits"]))
    assert _event_names(camera).count("absolute") == 1
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["terminal_frame_count"] >= 10
    assert attempt["terminal_window_seconds"] >= 0.8
    assert attempt["frame_buffer_peak_bytes"] <= 96 * 1024**2
    assert attempt["terminal_scene"]["classification"] == "scene_texture_insufficient"
    rejected = scanner.checkpoint["rejected_observations"][-1]
    assert (rejected["image_width"], rejected["image_height"]) == (960, 540)
    assert (rejected["source_width"], rejected["source_height"]) == (2880, 1620)
    assert rejected["image_representation"] == "analysis" and rejected["qualified_capture"] is False
    assert cv2.imread(rejected["path"]).shape[:2] == (540, 960)
    assert not scanner.captures and not scanner.boundaries


@pytest.mark.parametrize("size", [(2880, 1620), (3840, 2160)])
def test_terminal_analysis_buffer_never_replaces_the_integral_qualified_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    original_frame = camera.frame

    async def full_resolution_frame(**options):
        frame = await original_frame(**options)
        return {**frame, "image": cv2.resize(frame["image"], size)}

    camera.frame = full_resolution_frame
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_pose = camera._position()
    result = asyncio.run(scanner._absolute({"pan": 0.1, "tilt": 0.05}))
    assert result["stable"] and result["frame"]["image"].shape == (size[1], size[0], 3)
    assert result["evidence"]["best_sequence"] == result["frame"]["sequence"]
    assert (
        scanner.checkpoint["diagnostics"]["attempts"][-1]["frame_buffer_peak_bytes"] <= 96 * 1024**2
    )
    capture = asyncio.run(scanner._accept(result, row=0))
    assert capture["quality"]["best_sequence"] == capture["sequence"] == result["frame"]["sequence"]
    assert capture["generation"] == result["frame"]["generation"]
    assert capture["capture_instance"] == result["frame"]["capture_instance"]
    assert cv2.imread(capture["path"]).shape == (size[1], size[0], 3)


def test_inconclusive_relocalization_does_not_retain_full_resolution_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    camera = SimulatedCamera()
    captures = [
        {"id": str(index), "path": str(index), "role": "pilot_pan" if index < 12 else "grid"}
        for index in range(200)
    ]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {"captures": captures})
    decoded_frames = []
    between_calls = 0

    def private_image(path):
        # The previous loop variable may remain alive; older 4K frames must not.
        assert sum(reference() is not None for reference in decoded_frames) <= 1
        image = np.zeros((2160, 3840, 3), dtype=np.uint8)
        decoded_frames.append(weakref.ref(image))
        return image

    def matching(first, second):
        nonlocal between_calls
        if first.ndim == 2:
            between_calls += 1
            assert first.shape == second.shape == (540, 960)
            matrix = np.eye(3)
        else:
            # Every view links, but a different field of view prevents acceptance.
            assert first.shape == second.shape == (2160, 3840, 3)
            matrix = np.diag([1.03, 1.03, 1.0])
        return {"verified": True, "inliers": 80, "displacement": 20, "homography": matrix}

    monkeypatch.setattr(scanner, "_private_image", private_image)
    monkeypatch.setattr(scan, "_match", matching)
    with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
        asyncio.run(scanner._relocalize({"image": np.zeros((2160, 3840, 3), dtype=np.uint8)}))
    assert len(decoded_frames) > 20
    assert between_calls == len(decoded_frames) - 1
    assert not set(_event_names(camera)) & {"absolute", "velocity", "return"}


def test_continuous_relocalization_rejects_an_old_band_before_accepting_graph_links(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    frame = asyncio.run(camera.frame())
    path = tmp_path / "pending.jpg"
    cv2.imwrite(str(path), frame["image"])
    scanner.captures = [{"path": str(path)}, {"path": str(path)}]
    scanner.checkpoint["continuous_cursor"] = {"version": scan.CONTINUOUS_CURSOR_VERSION, "stage": "pan"}
    scanner.checkpoint["active_seconds"] = 0.0
    monkeypatch.setattr(scan, "_match", lambda *_: {"verified": True, "displacement": 100})
    with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
        asyncio.run(scanner._relocalize(frame))


def test_fixed_tilt_remains_eligible_for_an_absolute_horizontal_scan(tmp_path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = camera.capabilities
    scanner.last_pose = camera._position()
    scanner.capabilities["limits"]["tilt"] = {"min": 0.0, "max": 0.0}
    assert scanner._limits() is not None


def test_band_coverage_does_not_publish_private_checkpoint_images(tmp_path):
    scanner = scan._Scan(SimulatedCamera(), tmp_path, _progress, lambda: False, None)
    scanner.coverage["bands"] = {
        "0": {
            "complete": True,
            "edges": {"-1": "limit", "1": "limit"},
            "end_path": "/private/reference.jpg",
        }
    }
    assert scanner.result()["coverage"]["bands"] == {
        "0": {"complete": True, "edges": {"-1": "limit", "1": "limit"}}
    }
    assert scanner.coverage["bands"]["0"]["end_path"] == "/private/reference.jpg"


@pytest.mark.parametrize("connected", [True, False])
def test_horizontal_observation_retry_grows_duration_and_requires_a_full_visual_link(
    tmp_path, monkeypatch, connected
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_frame = asyncio.run(camera.frame())
    calls = []

    async def pulse(axis, direction, duration, **options):
        calls.append(duration)
        if len(calls) == 1:
            raise PanoramaCaptureError("motion_not_observed")
        scanner.last_frame = await camera.frame()
        stationary = len(calls) > 2
        return {
            "frame": scanner.last_frame,
            "pose": camera._position(),
            "stable": not stationary,
            "stationary": stationary,
            "evidence": {},
            "match": {
                "verified": True,
                "displacement": 0 if stationary else 30,
                "shift_x": 0 if stationary else 30,
                "shift_y": 0,
                "overlap": 0.8,
            },
        }

    scanner._pulse = pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": connected,
            "displacement": 30,
            "shift_x": 30,
            "shift_y": 0,
            "overlap": 0.8,
        },
    )
    if connected:
        outcome, _ = asyncio.run(scanner._seek("pan", 1, row=0, duration=0.8))
        assert outcome == "limit"
        assert calls[:2] == [0.8, 1.2]
        assert len(scanner.captures) == 1
    else:
        with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
            asyncio.run(scanner._seek("pan", 1, row=0, duration=0.8))
        assert not scanner.captures


def test_seek_accepts_a_command_endpoint_through_its_verified_anchor_precondition(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        axes={"pan": True, "tilt": True},
    )
    scanner.last_frame = anchor_frame
    scanner.last_pose = camera._position()
    expected_frames = []

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        expected_frames.append(expected_frame)
        frame = await camera.frame()
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "precondition_match": {
                "verified": True,
                "support_scope": "distributed_scene",
                "inliers": 120,
                "overlap": 0.99,
                "displacement": 0.5,
                "shift_x": 0.5,
                "shift_y": 0.0,
            },
            "match": {
                "verified": True,
                "support_scope": "localized_command_transition",
                "inliers": 90,
                "overlap": 0.76,
                "displacement": 85.0,
                "shift_x": 85.0,
                "shift_y": 1.0,
            },
        }

    async def stop_after_capture(_event):
        if len(scanner.captures) == 2:
            raise scan._Stopped

    scanner._pulse = pulse
    scanner.progress = stop_after_capture
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {
            "verified": False,
            "code": "correspondences_not_distributed",
        },
    )

    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.6))

    assert len(expected_frames) == 1
    np.testing.assert_array_equal(
        expected_frames[0]["image"],
        cv2.imread(checkpoint["continuous_cursor"]["seek"]["origin_path"]),
    )
    accepted = scanner.captures[-1]["previous_overlap"]
    assert accepted["verified"] is True
    assert accepted["connection_method"] == "verified_precondition_chain"
    assert accepted["anchor_precondition"]["displacement"] == pytest.approx(0.5)


def test_sparse_command_only_connection_returns_before_becoming_an_anchor(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        axes={"pan": True, "tilt": True},
    )
    scanner.last_frame = anchor_frame
    scanner.last_pose = camera._position()

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        frame = await camera.frame()
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "precondition_match": {
                "verified": True,
                "support_scope": "distributed_scene",
                "overlap": 0.99,
                "displacement": 0.2,
            },
            "match": {
                "verified": True,
                "support_scope": "sparse_distributed_command_transition",
                "inliers": 22,
                "overlap": 0.96,
                "displacement": 8.0,
                "shift_x": 8.0,
                "shift_y": 0.0,
            },
        }

    return_only = []

    async def return_to_anchor(*_args, **options):
        return_only.append(options["return_only"])
        raise PanoramaCaptureError("coverage_connection_unverified")

    scanner._pulse = pulse
    scanner._subdivide_failed_connection = return_to_anchor
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "insufficient_correspondences"},
    )

    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.12))

    assert return_only == [True]
    assert len(scanner.captures) == 1
    assert scanner.checkpoint["continuous_cursor"]["anchor"]["capture_id"] == (
        "independent-anchor"
    )


def _connection_subdivision_fixture(
    tmp_path,
    monkeypatch,
    *,
    anchor_verified: bool = True,
    retry_connected: bool = True,
    retry_stationary: bool = False,
    retry_error: str | None = None,
    retry_command_accepted: bool = True,
    retry_stop_accepted: bool = True,
    retry_movement_id: str = "0123456789abcdef0123456789abcdef",
    retry_diagnostic_movement_id: str | None = None,
    interrupt_at: str | None = None,
    relative_return: bool = False,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        relative_supported=False,
        absolute_supported=not relative_return,
        axes={"pan": True, "tilt": True},
    )
    scanner.last_frame = anchor_frame
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    cursor = scanner.checkpoint["continuous_cursor"]
    cursor["seek"]["duration"] = 1.2

    camera.pan = 0.18
    failed_frame = asyncio.run(camera.frame())
    camera.pan = 0.08
    retry_frame = asyncio.run(camera.frame())
    camera.pan = 0.0
    durations = []
    absolute_targets = []
    match_calls = []

    async def confirm_stop():
        scanner.physical_state = "stopped"

    async def absolute(target, **options):
        absolute_targets.append(copy.deepcopy(target))
        recovery = cursor["connection_recovery"]
        assert options["connection_recovery_phase"] == "return"
        assert recovery["state"] == "planned"
        recovery["state"] = "return_pending"
        cursor["transition"] = {
            "state": "pending",
            "stage": cursor["stage"],
            "row": cursor["row"],
            "direction": cursor["direction"],
            "branch": cursor["branch"],
            "intent": {
                "type": "connection_anchor_return",
                "target": copy.deepcopy(target),
            },
        }
        await scanner._persist()
        if interrupt_at == "return":
            raise scan._Stopped
        scanner.last_frame = {**anchor_frame, "received_monotonic": camera.now}
        scanner.last_pose = {**camera._position(), **target}
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "stable": True, "stationary": False}

    async def pulse(axis, direction, duration, **options):
        durations.append(duration)
        if relative_return and len(durations) == 2:
            recovery = cursor["connection_recovery"]
            assert (axis, direction, duration) == ("pan", -1, pytest.approx(1.2))
            assert options["intent_type"] == "connection_anchor_return"
            assert options["connection_recovery_phase"] == "return"
            assert recovery["state"] == "planned"
            recovery["state"] = "return_pending"
            cursor["transition"] = {
                "state": "pending",
                "stage": cursor["stage"],
                "row": cursor["row"],
                "direction": cursor["direction"],
                "branch": cursor["branch"],
                "intent": {
                    "type": "connection_anchor_return",
                    "axis": "pan",
                    "direction": -1,
                    "row": 0,
                    "step": recovery["identity"]["step"],
                    "duration": duration,
                    "anchor": dict(cursor["anchor"]),
                },
            }
            scanner.last_frame = {**anchor_frame, "received_monotonic": camera.now}
            scanner.last_pose = camera._position()
            scanner.physical_state = "stopped"
            return {"frame": scanner.last_frame, "stable": True, "stationary": False}
        assert (axis, direction) == ("pan", 1)
        if len(durations) == 1:
            scanner.last_frame = failed_frame
            scanner.last_pose = {**camera._position(), "pan": 0.18}
            scanner.physical_state = "stopped"
            return {
                "frame": failed_frame,
                "pose": scanner.last_pose,
                "stable": True,
                "stationary": False,
                "evidence": {"stable": True},
                "match": {"verified": True, "overlap": 0.8, "displacement": 80},
            }
        recovery = cursor["connection_recovery"]
        assert duration == pytest.approx(0.6)
        assert options["intent_type"] == "connection_halfstep"
        assert options["connection_recovery_phase"] == "retry"
        assert recovery["state"] == "anchor_confirmed"
        cursor["seek"]["steps"] = recovery["retry_step"]
        recovery["state"] = "retry_pending"
        cursor["transition"] = {
            "movement_id": retry_movement_id,
            "state": "pending",
            "stage": cursor["stage"],
            "row": cursor["row"],
            "direction": cursor["direction"],
            "branch": cursor["branch"],
            "intent": {
                "type": "connection_halfstep",
                "axis": "pan",
                "direction": 1,
                "row": 0,
                "step": recovery["retry_step"],
                "duration": duration,
                "anchor": dict(cursor["anchor"]),
            },
        }
        await scanner._persist()
        if interrupt_at == "retry":
            raise scan._Stopped
        if retry_error is not None:
            scanner.checkpoint.setdefault("diagnostics", {"version": 1, "attempts": []})[
                "attempts"
            ].append(
                {
                    "kind": "movement",
                    "movement_id": (
                        retry_movement_id
                        if retry_diagnostic_movement_id is None
                        else retry_diagnostic_movement_id
                    ),
                    "outcome": retry_error,
                    "command_outcome": (
                        "accepted" if retry_command_accepted else "unconfirmed"
                    ),
                    "stop_command_accepted": retry_stop_accepted,
                }
            )
            scanner.physical_state = (
                "unknown" if retry_stop_accepted else "stop_unconfirmed"
            )
            scanner._stop_failed = not retry_stop_accepted
            raise PanoramaCaptureError(retry_error)
        scanner.last_frame = retry_frame
        scanner.last_pose = {**camera._position(), "pan": 0.08}
        scanner.physical_state = "stopped"
        return {
            "frame": retry_frame,
            "pose": scanner.last_pose,
            "stable": not retry_stationary,
            "stationary": retry_stationary,
            "evidence": {"stable": True},
            "match": {
                "verified": True,
                "overlap": 1.0 if retry_stationary else 0.8,
                "displacement": 0 if retry_stationary else 40,
            },
        }

    async def current_anchor(_cursor, **_options):
        return anchor_verified

    def matching(*_images):
        match_calls.append(True)
        if len(match_calls) == 1:
            return {
                "verified": False,
                "code": "correspondences_not_distributed",
            }
        return {
            "verified": retry_connected,
            "code": None if retry_connected else "correspondences_not_distributed",
            "overlap": 0.8 if retry_connected else 0.0,
            "displacement": 40 if retry_connected else None,
            "shift_x": 40 if retry_connected else None,
            "shift_y": 0 if retry_connected else None,
        }

    scanner._confirm_stop = confirm_stop
    scanner._absolute = absolute
    scanner._pulse = pulse
    scanner._current_anchor = current_anchor
    monkeypatch.setattr(scan, "_match", matching)
    return scanner, cursor, durations, absolute_targets


def test_command_only_connection_returns_to_anchor_without_retrying(tmp_path, monkeypatch):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch
    )
    cursor["seek"].update(steps=1, duration=scan.MINIMUM_SEEK_PULSE_SECONDS)
    failed_result = {
        "frame": scanner.last_frame,
        "stable": True,
        "stationary": False,
        "match": {
            "verified": True,
            "support_scope": "sparse_distributed_command_transition",
            "overlap": 0.96,
            "displacement": 8.0,
        },
    }

    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(
            scanner._subdivide_failed_connection(
                cursor,
                cursor["seek"],
                axis="pan",
                direction=1,
                row=0,
                failed_duration=scan.MINIMUM_SEEK_PULSE_SECONDS,
                failed_result=failed_result,
                failed_connection={
                    "verified": False,
                    "code": "insufficient_correspondences",
                },
                step_limit=scan.MAX_CONTINUOUS_STEPS,
                error_code="coverage_connection_unverified",
                return_only=True,
            )
        )

    assert durations == []
    assert targets == [{"pan": 0.0, "tilt": 0.0}]
    recovery = cursor["connection_recovery"]
    assert recovery["mode"] == "return_only"
    assert recovery["state"] == "failed"
    assert recovery["failure_reason"] == "command_only_connection_returned"
    assert "retry_duration" not in recovery
    assert cursor["anchor"]["capture_id"] == "independent-anchor"
    assert cursor["relocalization_failed"] is False

    recovered = asyncio.run(
        scanner._recover(
            cursor,
            PanoramaCaptureError("coverage_connection_unverified"),
            axis="pan",
        )
    )
    assert recovered is True
    assert "connection_recovery" not in cursor
    assert cursor["connection_recovery_history"][-1]["mode"] == "return_only"
    assert cursor["bands"]["0"]["edges"]["1"] == "unconfirmed"
    assert cursor["stage"] == "pan" and cursor["direction"] == -1
    assert cursor["recovery"]["state"] == "confirmed"


def test_observational_connection_failure_returns_to_anchor_and_accepts_one_halfstep(
    tmp_path, monkeypatch
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch
    )

    async def stop_after_capture(event):
        if event["phase"] == "capturing" and event["captures_accepted"] == 2:
            raise scan._Stopped

    scanner.progress = stop_after_capture
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2, 0.6]
    assert targets == [{"pan": 0.0, "tilt": 0.0}]
    assert cursor["connection_recovery"]["state"] == "complete"
    assert cursor["connection_recovery"]["attempt"] == 1
    assert cursor["seek"]["subdivided_steps"] == [1]
    assert cursor["seek"]["steps"] == 2
    assert len(scanner.captures) == 2
    assert scanner.captures[-1]["previous_overlap"]["verified"] is True


def test_observational_connection_failure_uses_verified_relative_return_without_absolute_pose(
    tmp_path, monkeypatch
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch, relative_return=True
    )

    async def stop_after_capture(event):
        if event["phase"] == "capturing" and event["captures_accepted"] == 2:
            raise scan._Stopped

    scanner.progress = stop_after_capture

    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert durations == [1.2, 1.2, 0.6]
    assert targets == []
    assert cursor["connection_recovery"]["state"] == "complete"
    assert cursor["connection_recovery"]["return_method"] == "relative_pulse"
    assert cursor["connection_recovery"]["return_duration"] == pytest.approx(1.2)


def test_connection_halfstep_is_not_issued_when_returned_anchor_does_not_validate(
    tmp_path, monkeypatch
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch, anchor_verified=False
    )
    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2]
    assert targets == [{"pan": 0.0, "tilt": 0.0}]
    assert cursor["connection_recovery"]["state"] == "failed"
    assert cursor["connection_recovery"]["failure_reason"] == "anchor_unconfirmed"
    assert cursor["relocalization_failed"] is True
    assert len(scanner.captures) == 1


def test_connection_halfstep_that_still_does_not_link_finishes_partial(
    tmp_path, monkeypatch
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch, retry_connected=False
    )
    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2, 0.6]
    assert len(targets) == 1
    assert cursor["connection_recovery"]["state"] == "failed"
    assert cursor["connection_recovery"]["failure_reason"] == (
        "correspondences_not_distributed"
    )
    assert cursor["seek"]["subdivided_steps"] == [1]
    assert len(scanner.captures) == 1


def test_stationary_connection_halfstep_is_deadband_not_a_physical_boundary(
    tmp_path, monkeypatch
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path,
        monkeypatch,
        retry_stationary=True,
    )
    cursor["seek"]["stationary_count"] = 0

    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert durations == [1.2, 0.6]
    assert targets == [{"pan": 0.0, "tilt": 0.0}]
    assert cursor["connection_recovery"]["state"] == "failed"
    assert cursor["connection_recovery"]["failure_reason"] == (
        "halfstep_motion_not_observed"
    )
    assert cursor["seek"]["stationary_count"] == 0
    assert cursor["seek"].get("boundary") is None
    assert len(scanner.captures) == 1


def test_accepted_halfstep_observation_failure_is_archived_as_partial_without_poisoning(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path,
        monkeypatch,
        retry_error="motion_not_observed",
    )

    with pytest.raises(
        PanoramaCaptureError, match="coverage_connection_unverified"
    ) as caught:
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert durations == [1.2, 0.6]
    assert targets == [{"pan": 0.0, "tilt": 0.0}]
    assert cursor["connection_recovery"]["state"] == "failed"
    assert cursor["connection_recovery"]["failure_reason"] == (
        "halfstep_motion_not_observed"
    )
    assert cursor["transition"]["state"] == "accepted"
    assert cursor["transition"]["outcome"] == "halfstep_observation_failed"
    assert len(scanner.captures) == 1
    assert not SourcePanoramaService._can_resume({"_checkpoint": scanner.checkpoint})
    assert (
        SourcePanoramaService._resume_unavailable_code(
            {"_checkpoint": scanner.checkpoint}
        )
        == "relocalization_required"
    )
    cursor["resume_anchor_verified"] = True
    assert (
        SourcePanoramaService._resume_unavailable_code(
            {"_checkpoint": scanner.checkpoint, "physical_state": "stopped"}
        )
        == "relocalization_required"
    )
    assert scan._pending_seek_intent(cursor) is None

    async def forbidden_movement(*_args, **_options):
        raise AssertionError("a terminal half-step must never be replayed")

    scanner._pulse = forbidden_movement
    scanner._absolute = forbidden_movement
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2, 0.6]

    cursor["reference_path"] = cursor["anchor"]["path"]
    recovered = asyncio.run(scanner._recover(cursor, caught.value, axis="pan"))

    assert recovered is True
    assert "connection_recovery" not in cursor
    assert cursor["connection_recovery_history"][-1]["failure_reason"] == (
        "halfstep_motion_not_observed"
    )
    assert cursor["bands"]["0"]["edges"]["1"] == "unconfirmed"
    assert {issue["code"] for issue in scanner.issues} >= {"horizontal_coverage_partial"}
    assert scan._pending_seek_intent(cursor) is None


def test_stale_accepted_diagnostic_cannot_terminalize_current_halfstep(
    tmp_path, monkeypatch
):
    scanner, cursor, _, _ = _connection_subdivision_fixture(
        tmp_path,
        monkeypatch,
        retry_error="motion_not_observed",
        retry_diagnostic_movement_id="fedcba9876543210fedcba9876543210",
    )

    with pytest.raises(PanoramaCaptureError, match="motion_not_observed") as caught:
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert cursor["connection_recovery"]["state"] == "retry_pending"
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(scanner._recover(cursor, caught.value, axis="pan"))


def test_archived_halfstep_failure_continues_only_from_verified_local_anchor(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    scanner, cursor, _, _ = _connection_subdivision_fixture(
        tmp_path,
        monkeypatch,
        retry_error="motion_not_observed",
    )
    with pytest.raises(
        PanoramaCaptureError, match="coverage_connection_unverified"
    ) as caught:
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    # The failed half-step has already returned to and visually confirmed the
    # current anchor. The live runner may therefore cover the untouched side,
    # but the missing working destination still prevents a later restart from
    # treating that local observation as a resumable route.
    assert asyncio.run(scanner._recover(cursor, caught.value, axis="pan")) is True
    assert "connection_recovery" not in cursor
    assert cursor["connection_recovery_history"][-1]["state"] == "failed"
    assert cursor["transition"]["state"] == "accepted"
    assert cursor["stage"] == "pan"
    assert cursor["direction"] == -1
    assert cursor["recovery"]["state"] == "confirmed"
    assert {issue["code"] for issue in scanner.issues} >= {"horizontal_coverage_partial"}
    assert (
        SourcePanoramaService._resume_unavailable_code(
            {"_checkpoint": scanner.checkpoint}
        )
        == "relocalization_required"
    )


@pytest.mark.parametrize("interrupt_at", ["return", "retry"])
def test_interrupted_connection_recovery_never_replays_an_ambiguous_movement(
    tmp_path, monkeypatch, interrupt_at
):
    scanner, cursor, durations, targets = _connection_subdivision_fixture(
        tmp_path, monkeypatch, interrupt_at=interrupt_at
    )
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    saved_recovery = saved["continuous_cursor"]["connection_recovery"]
    assert saved_recovery["state"] == (
        "return_pending" if interrupt_at == "return" else "retry_pending"
    )
    with pytest.raises(ValueError, match="connection recovery"):
        scan._pending_seek_intent(saved["continuous_cursor"])

    resumed = scan._Scan(
        scanner.camera,
        tmp_path,
        _progress,
        lambda: False,
        saved,
    )
    resumed.capabilities = scanner.capabilities
    resumed.last_frame = scanner.last_frame

    async def forbidden(*_args, **_options):
        raise AssertionError("an ambiguous recovery movement must not be replayed")

    resumed._pulse = forbidden
    resumed._absolute = forbidden
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(resumed._seek("pan", 1, row=0, duration=1.2))
    assert durations == ([1.2] if interrupt_at == "return" else [1.2, 0.6])
    assert len(targets) == 1


@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_unobservable_first_side_gets_one_retry_after_capturing_opposite_side(
    tmp_path,
    monkeypatch,
    retry_succeeds,
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_frame = asyncio.run(camera.frame())
    scanner.capabilities = {"continuous_supported": True, "axes": {"tilt": False}}
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": 0,
        "direction": -1,
        "branch": -1,
        "bands": {"0": {"edges": {}, "complete": False}},
        "horizontal_duration": 0.3,
        "finished_branches": [],
        "recovery_attempts": {},
    }
    path = tmp_path / "working-reference.jpg"
    cv2.imwrite(str(path), scanner.last_frame["image"])
    scanner.captures = [{"id": "reference", "path": str(path), "row_index": 0, "quality": {"stable": True}}]
    cursor["reference_path"] = str(path)
    cursor["reference_destination"] = {"kind": "absolute", "role": "work", "binding": {}, "pan": 0.0, "tilt": 0.0, "capture_id": "reference", "path": str(path)}
    scanner.checkpoint["continuous_cursor"] = cursor
    directions = []

    async def seek(axis, direction, **options):
        directions.append(direction)
        if direction == -1 and (not retry_succeeds or directions.count(-1) == 1):
            raise PanoramaCaptureError("motion_not_observed")
        return "limit", 0.3

    async def reference(cursor):
        assert cursor.pop("after_reference_stage") == "pan"
        cursor.update(direction=cursor.pop("after_reference_direction"), stage="pan")

    scanner._seek, scanner._return_to_reference_band = seek, reference
    asyncio.run(scanner._continuous_scan())
    assert directions == [-1, 1, -1]
    assert cursor["recovery_attempts"] == {"pan:0:1": 1, "pan:0:-1": 1}
    if retry_succeeds:
        assert cursor["bands"]["0"]["edges"] == {"-1": "limit", "1": "limit"}
        assert "horizontal_coverage_blocked" not in cursor
        assert scanner.complete and scanner._coverage_progress()["primary_complete"]
        assert not any(
            issue["code"] == "horizontal_coverage_partial" for issue in scanner.issues
        )
    else:
        assert cursor["bands"]["0"]["edges"] == {
            "-1": "unconfirmed",
            "1": "limit",
        }
        assert cursor["horizontal_coverage_blocked"] is True
        assert not scanner.complete and not scanner._coverage_progress()["primary_complete"]


def test_transient_failure_on_second_primary_side_gets_one_local_retry(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    frame = asyncio.run(camera.frame())
    path = tmp_path / "primary-anchor.jpg"
    cv2.imwrite(str(path), frame["image"])
    scanner.captures = [
        {
            "id": "primary-anchor",
            "path": str(path),
            "row_index": 0,
            "quality": {"stable": True},
        }
    ]
    scanner.last_frame = frame
    scanner.physical_state = "stopped"
    scanner.capabilities = {
        "continuous_supported": True,
        "axes": {"pan": True, "tilt": False},
    }
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": 0,
        "direction": 1,
        "branch": -1,
        "bands": {
            "0": {
                "complete": False,
                "edges": {"-1": "limit"},
                "origin": "center",
            }
        },
        "reference_path": str(path),
        "horizontal_duration": 0.3,
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {
            "capture_id": "primary-anchor",
            "path": str(path),
            "row": 0,
        },
    }
    scanner.checkpoint.update(
        mode="continuous",
        active_seconds=0.0,
        captures=scanner.captures,
        continuous_cursor=cursor,
    )
    attempts = 0

    async def seek(*_args, **_options):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PanoramaCaptureError("coverage_connection_unverified")
        return "limit", 0.3

    async def confirm_stop():
        scanner.physical_state = "stopped"

    async def current_anchor(*_args, **_options):
        return True

    scanner._seek = seek
    scanner._confirm_stop = confirm_stop
    scanner._current_anchor = current_anchor

    asyncio.run(scanner._continuous_scan())

    assert attempts == 2
    assert cursor["recovery_attempts"] == {"pan:0:1": 1}
    assert cursor["bands"]["0"]["edges"] == {"-1": "limit", "1": "limit"}
    assert cursor["bands"]["0"]["complete"] is True
    assert "horizontal_coverage_blocked" not in cursor
    assert not any(
        issue["code"] == "horizontal_coverage_partial" for issue in scanner.issues
    )


def test_chained_connection_reduces_next_pulse_before_a_visual_gap():
    duration, reason = scan._next_continuous_seek_duration(
        1.2,
        image_extent=960,
        image_shift=147,
        connection={"connection_method": "verified_precondition_chain"},
    )
    assert duration == pytest.approx(0.6)
    assert reason == "chained_connection"


def test_distributed_connection_keeps_overlap_target_adaptation():
    duration, reason = scan._next_continuous_seek_duration(
        0.3,
        image_extent=960,
        image_shift=120,
        connection={"support_scope": "distributed_scene"},
    )
    assert duration == pytest.approx(0.6)
    assert reason is None


def test_low_visual_support_reduces_next_pulse_before_the_match_is_lost():
    duration, reason = scan._next_continuous_seek_duration(
        1.2,
        image_extent=960,
        image_shift=148,
        connection={
            "support_scope": "distributed_scene",
            "inliers": 62,
            "verified": True,
        },
    )
    assert duration == pytest.approx(0.6)
    assert reason == "low_visual_support"


def test_declining_scene_features_reduce_next_pulse_before_a_blank_region():
    duration, reason = scan._next_continuous_seek_duration(
        1.2,
        image_extent=960,
        image_shift=144,
        connection={
            "support_scope": "distributed_scene",
            "source_features": 1413,
            "target_features": 821,
            "inliers": 183,
            "verified": True,
        },
    )
    assert duration == pytest.approx(0.6)
    assert reason == "declining_visual_support"


def test_resume_at_verified_reference_can_explore_opposite_side_of_partial_primary_band(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    initial = asyncio.run(camera.frame())["image"]
    shifted = cv2.warpAffine(
        initial, np.float32([[1, 0, 6], [0, 1, 0]]), (320, 240), borderMode=cv2.BORDER_REFLECT
    )
    paths = [tmp_path / f"view-{i}.jpg" for i in range(3)]
    for path, picture in zip(paths, [initial, shifted, np.zeros_like(initial)], strict=True):
        cv2.imwrite(str(path), picture)
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": 0,
        "direction": -1,
        "branch": -1,
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {"capture_id": "2", "path": str(paths[2]), "row": 0},
        "reference_path": str(paths[0]),
        "bands": {"0": {"complete": False, "edges": {}}},
    }
    checkpoint = {
        "continuous_cursor": cursor,
        "active_seconds": 20.0,
        "captures": [
            {"id": str(i), "path": str(p), "row_index": 0, "quality": {"stable": True}}
            for i, p in enumerate(paths)
        ],
    }
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    asyncio.run(scanner._relocalize({"image": initial, "received_monotonic": camera.now}))
    assert cursor["after_reference_direction"] == 1
    assert cursor["stage"] == "return_reference"
    assert cursor["recovery"]["state"] == "confirmed"
    assert cursor["bands"]["0"]["edges"]["-1"] == "unconfirmed"
    assert any(issue["code"] == "horizontal_coverage_partial" for issue in scanner.issues)
    assert not set(_event_names(camera)) & {"absolute", "velocity", "return"}


def _recovery_scan(tmp_path, monkeypatch, *, row=0, direction=1):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {"continuous_supported": True, "axes": {"pan": True, "tilt": True}}
    reference = asyncio.run(camera.frame())
    camera.pan = 0.08
    local = asyncio.run(camera.frame())
    for index, (frame, band) in enumerate(((reference, 0), (local, row))):
        path = tmp_path / f"fixture-anchor-{index}.jpg"
        cv2.imwrite(str(path), frame["image"])
        scanner.captures.append(
            {
                "id": f"anchor-{index}",
                "path": str(path),
                "row_index": band,
                "quality": {"stable": True},
            }
        )
    anchor = scanner.captures[-1]
    bands = {"0": {"complete": row != 0, "edges": {"-1": "limit", "1": "limit"}}}
    bands[str(row)] = {
        "complete": False,
        "edges": {str(-direction): "limit"} if row == 0 else {},
        "origin": "center",
    }
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": row,
        "direction": direction,
        "branch": -1,
        "bands": bands,
        "reference_path": scanner.captures[0]["path"],
        "horizontal_duration": 0.3,
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {"capture_id": anchor["id"], "path": anchor["path"], "row": row},
    }
    scanner.last_frame, scanner.last_pose = local, camera._position()
    scanner.physical_state = "stopped"
    scanner.saved_return = {"kind": "absolute", "pan": 0.0, "tilt": 0.0}
    cursor["reference_destination"] = {
        **scanner.saved_return, "role": "work", "binding": {"profile_token": "simulated"},
        "capture_id": scanner.captures[0]["id"], "path": scanner.captures[0]["path"],
    }
    scanner.checkpoint.update(continuous_cursor=cursor, active_seconds=0.0, mode="continuous")
    return scanner, camera, cursor, reference, local


def _visual_band_return_scan(tmp_path, monkeypatch, *, with_destination=False, optical_scale=100):
    camera = SimulatedCamera()
    camera.optical_scale = optical_scale
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = {
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True, "tilt": True},
    }
    origin = asyncio.run(camera.frame())
    origin_path = tmp_path / "visual-return-origin.jpg"
    cv2.imwrite(str(origin_path), origin["image"])
    camera.pan = 0.2
    camera.target = (camera.pan, camera.tilt)
    endpoint = asyncio.run(camera.frame())
    endpoint_path = tmp_path / "visual-return-endpoint.jpg"
    cv2.imwrite(str(endpoint_path), endpoint["image"])
    scanner.captures = [
        {
            "id": "origin",
            "path": str(origin_path),
            "row_index": 0,
            "quality": {"stable": True},
        },
        {
            "id": "endpoint",
            "path": str(endpoint_path),
            "row_index": 0,
            "quality": {"stable": True},
            "movement": {
                "type": "seek_pulse",
                "axis": "pan",
                "direction": 1,
                "duration": 2.0,
                "anchor_capture_id": "origin",
            },
        },
    ]
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": 0,
        "direction": 1,
        "branch": -1,
        "bands": {
            "0": {
                "complete": True,
                "edges": {"-1": "limit", "1": "limit"},
                "origin": "center",
            }
        },
        "reference_path": str(origin_path),
        "horizontal_duration": 0.3,
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {
            "capture_id": "endpoint",
            "path": str(endpoint_path),
            "row": 0,
        },
    }
    if with_destination:
        cursor["reference_destination"] = {
            "kind": "absolute",
            "role": "work",
            "binding": {"profile_token": "simulated"},
            "pan": 0.0,
            "tilt": 0.0,
            "capture_id": "origin",
            "path": str(origin_path),
        }
    scanner.checkpoint.update(
        mode="continuous",
        active_seconds=0.0,
        captures=scanner.captures,
        continuous_cursor=cursor,
    )
    scanner.last_frame = endpoint
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    return scanner, camera, cursor


def _return_target_match(x, y=0.0):
    return {
        "verified": True, "support_scope": "distributed_scene", "inliers": 100,
        "overlap": 0.9, "analysis_size": [640, 480],
        "homography": [[1, 0, x], [0, 1, y], [0, 0, 1]],
    }


@pytest.mark.parametrize(
    ("remaining", "expected_direction"), [(40, -1), (-40, 1), (120, None), (160, None)]
)
def test_visual_band_return_correction_uses_observed_undertravel_and_overtravel(
    remaining, expected_direction
):
    plan = scan._visual_band_return_correction_plan(
        _return_target_match(120), _return_target_match(remaining), direction=-1, duration=1.0
    )
    if expected_direction is None:
        assert plan is None
    else:
        assert plan["direction"] == expected_direction
        assert scan.MINIMUM_SEEK_PULSE_SECONDS <= plan["duration"] <= 1.0
        assert plan["predicted_pixels"] < abs(remaining)


@pytest.mark.parametrize("failure", ["tilt", "localized", "invalid_model", "low_overlap"])
def test_visual_band_return_correction_rejects_unexplained_or_weak_geometry(failure):
    before, after = _return_target_match(120), _return_target_match(40)
    if failure == "tilt":
        before, after = _return_target_match(120, 40), _return_target_match(40, 40)
    elif failure == "localized":
        after["support_scope"] = "localized_anchor"
    elif failure == "invalid_model":
        after["homography"][0][0] = float("nan")
    else:
        after["overlap"] = 0.2
    assert scan._visual_band_return_correction_plan(
        before, after, direction=-1, duration=1.0
    ) is None


def _install_visual_return_response(scanner, camera, *, gain=1.0, failure=None):
    commands = []

    async def reference_window(**_options):
        return await camera.frame()

    async def pulse(axis, direction, duration, **options):
        commands.append((axis, direction, duration))
        assert axis == "pan"
        if failure == "cancel" and len(commands) == 2:
            raise PanoramaCaptureError("cancelled")
        # The head's response deliberately differs from the recorded outbound
        # duration. Only the resulting images reveal the actual stopped pose.
        if failure != "no_correction_effect" or len(commands) == 1:
            camera.pan += direction * duration * 0.1 * gain
        camera.target = (camera.pan, camera.tilt)
        camera.blank = failure == "lost_texture"
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.last_pose = camera._position()
        scanner.physical_state = "stopped"
        return {"frame": frame, "pose": scanner.last_pose, "stable": True,
                "stationary": False}

    scanner._reference_window = reference_window
    scanner._pulse = pulse
    return commands


@pytest.mark.parametrize("gain", [0.5, 1.5])
def test_visual_band_return_reaches_node_after_stopping_between_photographs(
    tmp_path, monkeypatch, gain
):
    scanner, camera, cursor = _visual_band_return_scan(tmp_path, monkeypatch, optical_scale=400)
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))
    original_captures = copy.deepcopy(scanner.captures)
    original_images = [Path(capture["path"]).read_bytes() for capture in scanner.captures]
    commands = _install_visual_return_response(scanner, camera, gain=gain)

    asyncio.run(scanner._return_to_band_origin(cursor))

    assert 2 <= len(commands) <= 4
    assert commands[1][1] == (-1 if gain < 1 else 1)
    assert cursor["stage"] == "step" and cursor["anchor"]["capture_id"] == "origin"
    assert cursor["anchor_observation"]["displacement"] <= 15
    assert cursor["anchor_observation"]["overlap"] >= 0.85
    assert scanner.captures == original_captures
    assert [Path(capture["path"]).read_bytes() for capture in scanner.captures] == original_images
    assert scanner.checkpoint["visual_band_returns"][0]["commands"] == len(commands)


@pytest.mark.parametrize("failure", ["lost_texture", "cancel", "no_correction_effect"])
def test_visual_band_return_stops_without_replaying_failed_correction(
    tmp_path, monkeypatch, failure
):
    scanner, camera, cursor = _visual_band_return_scan(tmp_path, monkeypatch, optical_scale=400)
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))
    commands = _install_visual_return_response(scanner, camera, gain=0.5, failure=failure)
    expected_error = "cancelled" if failure == "cancel" else "band_origin_return_unverified"
    with pytest.raises(PanoramaCaptureError, match=expected_error):
        asyncio.run(scanner._return_to_band_origin(cursor))
    assert len(commands) == (1 if failure == "lost_texture" else 2)
    assert cursor["anchor"]["capture_id"] == "endpoint" and cursor["stage"] == "pan"
    assert cursor["relocalization_failed"] is True
    if failure != "lost_texture":
        # The durable cursor validates, but its interrupted correction cannot
        # authorize another pulse or reset the reserved command count.
        persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
        restored_cursor = persisted["continuous_cursor"]
        assert scan._visual_band_return(restored_cursor, captures=scanner.captures)
        with pytest.raises(PanoramaCaptureError, match="band_origin_return_unverified"):
            asyncio.run(scanner._return_to_band_origin(restored_cursor))
        assert len(commands) == 2
        assert restored_cursor["visual_band_return"]["commands"] == 2
    else:
        assert scanner.checkpoint["rejected_motion_pairs"][-1]["code"] == expected_error


def test_visual_band_return_correction_respects_reserved_route_budget(tmp_path, monkeypatch):
    scanner, camera, cursor = _visual_band_return_scan(tmp_path, monkeypatch, optical_scale=400)
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))
    cursor["visual_band_return"]["commands"] = scan.MAX_VISUAL_BAND_RETURN_COMMANDS - 1
    commands = _install_visual_return_response(scanner, camera, gain=0.5)
    with pytest.raises(PanoramaCaptureError, match="band_origin_return_unverified"):
        asyncio.run(scanner._return_to_band_origin(cursor))
    assert len(commands) == 1
    assert cursor["visual_band_return"]["commands"] == scan.MAX_VISUAL_BAND_RETURN_COMMANDS
    assert cursor["stage"] == "pan"


def test_completed_band_returns_through_verified_capture_graph_without_new_photos(
    tmp_path, monkeypatch
):
    scanner, camera, cursor = _visual_band_return_scan(tmp_path, monkeypatch)
    original_hashes = [capture["id"] for capture in scanner.captures]
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))

    assert cursor["visual_band_return"]["capture_ids"] == ["endpoint", "origin"]
    assert cursor["stage"] == "pan"

    async def reference_window(**_options):
        return await camera.frame()

    async def pulse(axis, direction, duration, **options):
        assert (axis, direction, duration) == ("pan", -1, 2.0)
        assert options["expected_frame"]["sequence"] < camera.sequence + 2
        camera.pan = 0.0
        camera.target = (camera.pan, camera.tilt)
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.last_pose = camera._position()
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": scanner.last_pose,
            "stable": True,
            "stationary": False,
            "match": scan._match(options["expected_frame"]["image"], frame["image"]),
        }

    scanner._reference_window = reference_window
    scanner._pulse = pulse
    asyncio.run(scanner._return_to_band_origin(cursor))

    assert cursor["stage"] == "step"
    assert cursor["anchor"]["capture_id"] == "origin"
    assert "visual_band_return" not in cursor and "seek" not in cursor
    assert [capture["id"] for capture in scanner.captures] == original_hashes
    assert scanner.checkpoint["visual_band_returns"] == [
        {
            "row": 0,
            "origin_capture_id": "origin",
            "commands": 1,
            "method": "verified_visual_capture_graph",
        }
    ]


def test_visual_band_return_combines_adjacent_short_reverse_steps(tmp_path, monkeypatch):
    scanner, camera, cursor = _visual_band_return_scan(tmp_path, monkeypatch)
    origin, endpoint = scanner.captures
    middle_frame = asyncio.run(camera.frame())
    middle_path = tmp_path / "visual-return-middle.jpg"
    cv2.imwrite(str(middle_path), middle_frame["image"])
    middle = {
        "id": "middle",
        "path": str(middle_path),
        "row_index": 0,
        "quality": {"stable": True},
        "movement": {
            "type": "seek_pulse",
            "axis": "pan",
            "direction": 1,
            "duration": 0.3,
            "anchor_capture_id": "origin",
        },
    }
    endpoint["movement"] = {
        "type": "seek_pulse",
        "axis": "pan",
        "direction": 1,
        "duration": 0.3,
        "anchor_capture_id": "middle",
    }
    scanner.captures = [origin, middle, endpoint]
    scanner.checkpoint["captures"] = scanner.captures
    cursor["anchor"] = {"capture_id": "endpoint", "path": endpoint["path"], "row": 0}

    async def choose_origin(_cursor, **_options):
        return origin

    scanner._band_transition_capture = choose_origin
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))

    async def reference_window(**_options):
        return await camera.frame()

    async def pulse(axis, direction, duration, **options):
        assert (axis, direction, duration) == ("pan", -1, 0.6)
        camera.pan = 0.0
        camera.target = (camera.pan, camera.tilt)
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.last_pose = camera._position()
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": scanner.last_pose,
            "stable": True,
            "stationary": False,
            "match": scan._match(options["expected_frame"]["image"], frame["image"]),
        }

    scanner._reference_window = reference_window
    scanner._pulse = pulse
    asyncio.run(scanner._return_to_band_origin(cursor))

    assert cursor["stage"] == "step"
    assert cursor["anchor"]["capture_id"] == "origin"


def test_saved_work_destination_is_preferred_for_primary_band_return(tmp_path, monkeypatch):
    scanner, _, cursor = _visual_band_return_scan(
        tmp_path, monkeypatch, with_destination=True
    )

    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))

    assert cursor["stage"] == "return_reference"
    assert cursor["recovery"]["state"] == "planned"
    assert cursor["after_reference_stage"] == "step"
    assert "visual_band_return" not in cursor


def test_completed_band_uses_textured_interior_node_before_vertical_transition(
    tmp_path, monkeypatch
):
    scanner, camera, cursor = _visual_band_return_scan(
        tmp_path, monkeypatch, with_destination=True
    )
    origin, endpoint = scanner.captures
    camera.pan = 0.1
    camera.target = (camera.pan, camera.tilt)
    middle_frame = asyncio.run(camera.frame())
    middle_path = tmp_path / "visual-return-middle.jpg"
    cv2.imwrite(str(middle_path), middle_frame["image"])
    middle = {
        "id": "middle",
        "path": str(middle_path),
        "row_index": 0,
        "quality": {"stable": True},
        "texture_support": {
            "distributed": True,
            "features": 2000,
            "occupied_cells": 12,
            "hull_fraction": 0.9,
        },
        "movement": {
            "type": "seek_pulse",
            "axis": "pan",
            "direction": 1,
            "duration": 1.0,
            "anchor_capture_id": "origin",
        },
    }
    origin["texture_support"] = {
        "distributed": True,
        "features": 48,
        "occupied_cells": 4,
        "hull_fraction": 0.12,
    }
    endpoint["movement"] = {
        "type": "seek_pulse",
        "axis": "pan",
        "direction": 1,
        "duration": 1.0,
        "anchor_capture_id": "middle",
    }
    scanner.captures = [origin, middle, endpoint]
    scanner.checkpoint["captures"] = scanner.captures

    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))

    route = cursor["visual_band_return"]
    assert route["capture_ids"] == ["endpoint", "middle"]
    assert route["origin_capture_id"] == "middle"
    assert cursor["bands"]["0"]["transition_capture_id"] == "middle"
    assert cursor["stage"] == "pan"


def test_visual_band_return_rejects_a_route_not_bound_to_capture_connections(
    tmp_path, monkeypatch
):
    scanner, _, cursor = _visual_band_return_scan(tmp_path, monkeypatch)
    asyncio.run(scanner._prepare_band_origin_return(cursor, row=0, after_direction=-1))
    scanner.captures[-1]["movement"]["anchor_capture_id"] = "missing"

    with pytest.raises(ValueError, match="connection"):
        scan._visual_band_return(cursor, captures=scanner.captures)


def test_working_reference_return_reaches_post_preflight_view_and_restores_original(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, reference, local = _recovery_scan(tmp_path, monkeypatch)
    camera.pan = -0.15
    original = asyncio.run(camera.frame())
    original_path = tmp_path / "original-before-preflight.jpg"
    cv2.imwrite(str(original_path), original["image"])
    scanner.checkpoint["initial_path"] = str(original_path)
    scanner.saved_return = {"kind": "absolute", "pan": -0.15, "tilt": 0.0}
    cursor["reference_destination"] = {
        "kind": "absolute", "role": "work", "binding": {"profile_token": "simulated"},
        "pan": 0.0, "tilt": 0.0,
        "capture_id": scanner.captures[0]["id"], "path": scanner.captures[0]["path"],
    }
    cursor.update(stage="return_reference", after_reference_stage="step", after_reference_direction=1)
    cursor["recovery"] = {"destination": "step:0:1", "state": "planned"}
    cursor["recovery_attempts"] = {"step:0:1": 1}
    camera.pan, camera.target = 0.08, (0.08, 0.0)
    scanner.last_frame = local
    scanner.captures[-1]["movement"] = {"duration": 8.0}

    async def observe_destination(command, **options):
        assert options["attempt_seconds"] == scan.ATTEMPT_SECONDS + 2
        await command()
        for _ in range(4):
            frame = await camera.frame()
        scanner.last_frame, scanner.last_pose = frame, camera._position()
        scanner.physical_state = "stopped"
        return {"frame": frame, "pose": scanner.last_pose, "stable": True}

    scanner._move = observe_destination
    asyncio.run(scanner._return_to_reference_band(cursor))
    assert camera.pan == pytest.approx(0.0)
    assert cursor["stage"] == "step" and cursor["anchor"]["capture_id"] == "anchor-0"
    assert scan._match(reference["image"], scanner.last_frame["image"])["displacement"] <= 15
    asyncio.run(scanner._restore())
    assert camera.pan == pytest.approx(-0.15)
    assert scanner.physical_state == "restored"
    assert [saved["pan"] for name, saved in camera.events if name == "return"] == [0.0, -0.15]


@pytest.mark.parametrize("row", [0, -1])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("local_reference", [False, True])
def test_pan_failure_recovers_any_side_or_band_before_vertical_exploration(
    tmp_path, monkeypatch, row, direction, local_reference
):
    scanner, camera, cursor, reference, local = _recovery_scan(
        tmp_path, monkeypatch, row=row, direction=direction
    )
    events = []
    preserved = copy.deepcopy(scanner.captures)

    async def seek(*args, **options):
        events.append("pan_failure")
        if row != 0 and local_reference and events.count("pan_failure") == 2:
            assert args[1] == -direction
            return "limit", cursor["horizontal_duration"]
        scanner.last_frame = {**local, "image": np.zeros_like(local["image"])}
        scanner.physical_state = "unknown"
        raise PanoramaCaptureError("coverage_connection_unverified")

    async def confirm_stop():
        events.append("observed_stop")
        scanner.physical_state = "stopped"
        scanner.last_frame = (
            local if local_reference else {**local, "image": np.zeros_like(local["image"])}
        )

    async def returned(command, **options):
        # This focused recovery test replaces ``_move``. Mirror the production
        # method's atomic pre-dispatch write before asserting the durable state.
        if options.get("persist_recovery_return_intent"):
            cursor["recovery"]["state"] = "pending"
            cursor["transition"] = {
                "state": "pending",
                "stage": "return_reference",
                "row": cursor["row"],
                "direction": cursor["direction"],
                "branch": cursor["branch"],
            }
        assert cursor["recovery"]["state"] == "pending"
        assert cursor["recovery_attempts"][cursor["recovery"]["destination"]] == 1
        events.append("return")
        await command()
        scanner.last_frame = reference
        scanner.physical_state = "stopped"
        scanner.last_pose = {"pan": 0.0, "tilt": 0.0}
        return {"frame": reference, "stable": True}

    async def next_band(active):
        assert active["recovery"]["state"] == "confirmed"
        assert await scanner._current_anchor(active, row=active["row"])
        events.append("tilt")
        raise scan._Stopped

    scanner._seek, scanner._confirm_stop = seek, confirm_stop
    scanner._move, scanner._next_band = returned, next_band
    if row == 0:
        asyncio.run(scanner._continuous_scan())
    else:
        with pytest.raises(scan._Stopped):
            asyncio.run(scanner._continuous_scan())
    assert events[0:2] == ["pan_failure", "observed_stop"]
    assert (events[-1] == "tilt") is (row != 0)
    assert ("return" in events) is (row != 0 and not local_reference)
    assert scanner.captures == preserved
    assert cursor["bands"][str(row)]["edges"][str(direction)] == "unconfirmed"
    assert not cursor["bands"][str(row)]["complete"]
    assert cursor["row"] == (0 if not local_reference else row)
    if row == 0:
        assert cursor["horizontal_coverage_blocked"] is True
        assert cursor["bands"]["0"]["edges"] == {
            str(direction): "unconfirmed",
            str(-direction): "limit",
        }
    if row != 0 and not local_reference:
        assert cursor["branch"] == 1 and cursor["bands"]["0"]["complete"]
    assert not scanner._coverage_progress()["continued_after_recovery"]


@pytest.mark.parametrize("direction", [-1, 1])
def test_center_origin_later_band_requires_both_of_its_own_pan_limits(
    tmp_path, monkeypatch, direction
):
    scanner, _, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch, row=-1, direction=direction)
    visited = []

    async def seek(axis, side, **options):
        visited.append(side)
        assert not cursor["bands"]["-1"]["complete"]
        return "limit", 0.3

    async def next_band(active):
        assert active["bands"]["-1"]["complete"]
        raise scan._Stopped

    scanner._seek, scanner._next_band = seek, next_band
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._continuous_scan())
    assert visited == [direction, -direction]
    assert cursor["bands"]["-1"]["edges"] == {"-1": "limit", "1": "limit"}


@pytest.mark.parametrize(
    "failure",
    [
        "control_lost",
        "stop_unconfirmed",
        "optical_state_changed",
        "camera_configuration_changed",
        "source_geometry_changed",
        "capture_write_failed",
    ],
)
def test_recovery_never_turns_safety_failures_into_exploratory_movement(
    tmp_path, monkeypatch, failure
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    with pytest.raises(PanoramaCaptureError, match=failure):
        asyncio.run(scanner._recover(cursor, PanoramaCaptureError(failure), axis="pan"))
    assert camera.events == []
    assert cursor["recovery_attempts"] == {}


@pytest.mark.parametrize("state", ["planned", "pending"])
def test_interrupted_recovery_intention_is_not_reissued_without_observing_destination(
    tmp_path, monkeypatch, state
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor.update(
        stage="return_reference", after_reference_stage="step", after_reference_direction=-1
    )
    cursor["recovery"] = {"destination": "step:0:-1", "state": state}
    cursor["recovery_attempts"] = {"step:0:-1": 1}

    async def interrupted(event):
        if cursor["recovery"]["state"] == "pending":
            raise scan._Stopped

    scanner.progress = interrupted if state == "planned" else _progress
    scanner.checkpoint["return"] = scanner.saved_return
    expected = scan._Stopped if state == "planned" else PanoramaCaptureError
    with pytest.raises(expected):
        asyncio.run(scanner._return_to_reference_band(cursor))
    persisted = copy.deepcopy(scanner.checkpoint)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, persisted)
    scanner.saved_return = persisted["return"]
    with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
        asyncio.run(scanner._return_to_reference_band(persisted["continuous_cursor"]))
    assert camera.events == []
    assert persisted["continuous_cursor"]["recovery_attempts"] == {"step:0:-1": 1}


def test_recovery_interrupted_before_durable_return_intent_remains_safely_planned(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch)
    cursor.update(
        stage="return_reference", after_reference_stage="step", after_reference_direction=-1
    )
    cursor["recovery"] = {"destination": "step:0:-1", "state": "planned"}
    cursor["recovery_attempts"] = {"step:0:-1": 1}
    awaitable = scanner._persist()
    asyncio.run(awaitable)

    async def interrupt_before_intent(event):
        if event["phase"] == "waiting_for_stability":
            raise scan._Stopped

    scanner.progress = interrupt_before_intent
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._return_to_reference_band(cursor))
    assert not set(_event_names(camera)) & {"return", "absolute", "velocity"}

    persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
    persisted_cursor = persisted["continuous_cursor"]
    assert persisted_cursor["recovery"]["state"] == "planned"
    assert "transition" not in persisted_cursor

    resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, persisted)
    resumed.capabilities = scanner.capabilities
    resumed.saved_return = persisted["return"]
    resumed.last_frame = local
    resumed.last_pose = camera._position()
    resumed.physical_state = "stopped"
    asyncio.run(resumed._return_to_reference_band(persisted_cursor))
    assert _event_names(camera).count("return") == 1
    assert persisted_cursor["recovery"]["state"] == "confirmed"
    assert persisted_cursor["stage"] == "step"


def test_recovery_return_persists_pending_state_with_transition_before_dispatch(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor.update(
        stage="return_reference", after_reference_stage="step", after_reference_direction=-1
    )
    cursor["recovery"] = {"destination": "step:0:-1", "state": "planned"}
    cursor["recovery_attempts"] = {"step:0:-1": 1}
    asyncio.run(scanner._persist())
    persisted_states = []

    async def interrupt_after_intent(_event):
        durable = json.loads((tmp_path / "scan-manifest.json").read_text())[
            "continuous_cursor"
        ]
        transition = durable.get("transition")
        persisted_states.append(
            (durable["recovery"]["state"], transition.get("state") if transition else None)
        )
        if durable["recovery"]["state"] == "pending":
            assert {
                key: transition.get(key)
                for key in ("state", "stage", "row", "direction", "branch")
            } == {
                "state": "pending",
                "stage": "return_reference",
                "row": cursor["row"],
                "direction": cursor["direction"],
                "branch": cursor["branch"],
            }
            assert scan._movement_attempt_id(transition.get("movement_id")) == transition.get(
                "movement_id"
            )
            raise scan._Stopped

    scanner.progress = interrupt_after_intent
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._return_to_reference_band(cursor))
    assert ("pending", "pending") in persisted_states
    assert ("pending", None) not in persisted_states
    assert not set(_event_names(camera)) & {"return", "absolute", "velocity"}

    durable_cursor = json.loads((tmp_path / "scan-manifest.json").read_text())[
        "continuous_cursor"
    ]
    assert scan._pending_seek_intent(durable_cursor) is None
    malformed = copy.deepcopy(durable_cursor)
    malformed.pop("transition")
    with pytest.raises(ValueError, match="atomic transition"):
        scan._pending_seek_intent(malformed)


@pytest.mark.parametrize("invalid", ["missing", "rejected", "wrong_row", "wrong_path"])
def test_invalid_navigation_anchor_never_starts_vertical_motion(tmp_path, monkeypatch, invalid):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"
    if invalid == "missing":
        cursor.pop("anchor")
    elif invalid == "rejected":
        scanner.captures[-1]["quality"]["stable"] = False
    elif invalid == "wrong_row":
        cursor["anchor"]["row"] = 2
    else:
        cursor["anchor"]["path"] = scanner.captures[0]["path"]
    with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
        asyncio.run(scanner._next_band(cursor))
    assert camera.events == []


def test_failed_vertical_connection_keeps_intermediate_and_explores_opposite_height(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"
    attempts = {-1: 0, 1: 0}

    async def pulse(axis, side, duration, **_options):
        assert axis == "tilt"
        attempts[side] += 1
        if side == -1 and attempts[side] == 2:
            scanner.physical_state = "unknown"
            raise PanoramaCaptureError("vertical_connection_unverified")
        before = scanner.last_frame
        camera.tilt = side * (0.3 if attempts[side] == 1 else 0.95)
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": scan._match(before["image"], frame["image"]),
        }

    scanner._pulse = pulse

    async def scenario():
        with pytest.raises(PanoramaCaptureError, match="vertical_connection_unverified"):
            await scanner._next_band(cursor)
        intermediate = copy.deepcopy(scanner.captures[-1])
        assert intermediate["row_index"] == -1 and intermediate["role"] == "row_connection"
        assert "-1" not in cursor["bands"]
        assert await scanner._recover(
            cursor, PanoramaCaptureError("vertical_connection_unverified"), axis="tilt"
        )
        assert cursor["stage"] == "return_reference" and cursor["branch"] == 1
        await scanner._return_to_reference_band(cursor)
        assert cursor["recovery"]["state"] == "confirmed" and cursor["row"] == 0
        assert await scanner._next_band(cursor)
        assert cursor["row"] == 1 and cursor["stage"] == "pan"
        assert cursor["bands"]["1"]["origin"] == "center"
        assert cursor["bands"]["1"]["edges"] == {}
        assert not cursor["bands"]["1"]["complete"]
        assert intermediate in scanner.captures
        assert len(scanner.captures) >= 5

    asyncio.run(scenario())
    assert _event_names(camera).count("return") == 1
    assert cursor["pending_branches"] == [-1]
    assert cursor["recovery_attempts"] == {"step:0:1": 1}


def test_connected_vertical_walk_requires_measured_spacing_when_origin_leaves_view(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"

    async def current_anchor(*_args, **_options):
        return True

    pulse_count = 0

    async def pulse(axis, direction, duration, **_options):
        nonlocal pulse_count
        pulse_count += 1
        assert (axis, direction) == ("tilt", -1)
        assert duration == pytest.approx((0.3, 0.6, 1.2)[pulse_count - 1])
        camera.tilt = -0.3 * pulse_count
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": {"verified": True},
        }

    scanner._current_anchor = current_anchor
    scanner._pulse = pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {
            "verified": True,
            "overlap": 0.8,
            "displacement": 100,
            "shift_x": 0,
            "shift_y": 90,
        },
    )

    assert asyncio.run(scanner._next_band(cursor))
    assert cursor["row"] == -1 and cursor["stage"] == "pan"
    assert pulse_count == 3
    assert cursor["bands"]["-1"]["start_path"] == scanner.captures[-1]["path"]
    assert cursor["bands"]["-1"]["spacing"]["method"] == "accumulated_vertical_shift"
    assert cursor["bands"]["-1"]["spacing"]["vertical_displacement_pixels"] == 270
    assert scanner.captures[-1]["role"] == "row_connection"


def test_connected_vertical_walk_uses_its_causal_command_transition(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"
    scanner.captures[-1].update(
        capture_instance=local["capture_instance"],
        generation=local["generation"],
        sequence=local["sequence"],
    )

    async def current_anchor(*_args, **_options):
        return True

    async def pulse(axis, direction, duration, *, expected_frame=None):
        assert (axis, direction, duration) == ("tilt", -1, 0.3)
        assert expected_frame is local
        camera.tilt = -0.3
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": {
                "verified": True,
                "support_scope": "localized_command_transition",
                "overlap": 0.91,
                    "displacement": 240.0,
                "shift_x": 0.5,
                    "shift_y": 240.0,
            },
            "precondition_match": {
                "verified": True,
                "overlap": 0.99,
                "displacement": 0.2,
                "shift_x": 0.1,
                "shift_y": 0.2,
            },
        }

    scanner._current_anchor = current_anchor
    scanner._pulse = pulse
    monkeypatch.setattr(
        scan,
        "_no_effect_match",
        lambda *_: {
            "verified": True,
            "support_scope": "localized_no_effect",
            "overlap": 0.99,
            "displacement": 0.3,
            "shift_x": 0.1,
            "shift_y": 0.2,
        },
    )
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )

    assert asyncio.run(scanner._next_band(cursor))
    assert cursor["row"] == -1 and cursor["stage"] == "pan"
    assert scanner.captures[-1]["previous_overlap"]["connection_method"] == (
        "vertical_precondition_chain"
    )
    assert cursor["horizontal_duration"] == pytest.approx(0.15)


def test_vertical_band_spacing_uses_only_verified_adjacent_motion_or_origin_overlap():
    state = {}
    unresolved_origin = {"verified": False, "code": "correspondences_not_distributed"}

    reached, first = scan._vertical_band_spacing(
        state,
        connection={"verified": True, "shift_y": 25.0},
        origin_connection=unresolved_origin,
        image_height=540,
    )
    assert not reached
    assert first["vertical_displacement_pixels"] == 25
    state["vertical_displacement_pixels"] = first["vertical_displacement_pixels"]

    reached, second = scan._vertical_band_spacing(
        state,
        connection={"verified": True, "shift_y": 150.0},
        origin_connection=unresolved_origin,
        image_height=540,
    )
    assert reached
    assert second["method"] == "accumulated_vertical_shift"
    assert second["target_displacement_pixels"] == pytest.approx(172.8)

    reached, direct = scan._vertical_band_spacing(
        {},
        connection={"verified": True, "shift_y": 5.0},
        origin_connection={"verified": True, "overlap": 0.67},
        image_height=540,
    )
    assert reached
    assert direct["method"] == "verified_origin_overlap"


# Append to tests/test_camera_panorama_scan.py to reuse its synthetic camera and clock.
# This file intentionally contains no live transport, application or camera access.


def _independent_budget_checkpoint(tmp_path, camera):
    frame = asyncio.run(camera.frame())
    path = tmp_path / "independent-anchor.jpg"
    cv2.imwrite(str(path), frame["image"])
    capture = {
        "id": "independent-anchor",
        "path": str(path),
        "row_index": 0,
        "quality": {"stable": True},
        "pose": camera._position(),
        "capture_instance": frame["capture_instance"],
        "generation": frame["generation"],
        "sequence": frame["sequence"],
    }
    checkpoint = {
        "mode": "continuous",
        "active_seconds": 0.0,
        "source_identity": copy.deepcopy(camera.capabilities["source_identity"]),
        "captures": [capture],
        "return": {"kind": "absolute", "pan": 0.0, "tilt": 0.0, "zoom": None},
        "initial_path": str(path),
        "continuous_cursor": {
            "version": scan.CONTINUOUS_CURSOR_VERSION,
            "stage": "pan",
            "row": 0,
            "direction": 1,
            "branch": -1,
            "bands": {"0": {"complete": False, "edges": {}}},
            "finished_branches": [],
            "recovery_attempts": {},
            "anchor": {"capture_id": capture["id"], "path": str(path), "row": 0},
            "seek": {
                "axis": "pan",
                "direction": 1,
                "row": 0,
                "origin_path": str(path),
                "progress": True,
                "steps": 0,
                "stationary_count": 1,
                "origin_excursion": False,
            },
        },
    }
    return checkpoint, frame


def test_new_seek_fences_first_command_with_latest_stopped_frame(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["continuous_cursor"].pop("seek")
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    current_frame = asyncio.run(camera.frame())
    scanner.capabilities, scanner.last_frame = camera.capabilities, current_frame
    expected_frames = []

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        expected_frames.append(expected_frame)
        raise PanoramaCaptureError("lease_lost")

    async def stopped_window(*, timeout=3.0):
        assert timeout == 3.0
        return current_frame

    async def current_anchor(_cursor, *, row):
        assert row == 0
        return True

    scanner._pulse = pulse
    scanner._reference_window = stopped_window
    scanner._current_anchor = current_anchor

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", -1, row=0, duration=0.6))

    assert expected_frames == [current_frame]


def test_new_seek_connects_first_capture_through_fresh_native_anchor(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor.pop("seek")
    checkpoint["captures"][0]["pose"].update(
        native_pan=0.0,
        move_status="IDLE",
    )
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    current_frame = anchor_frame
    moved_frame = asyncio.run(camera.frame())
    scanner.capabilities, scanner.last_frame = camera.capabilities, current_frame
    pulse_calls = 0

    async def position():
        return {
            **camera._position(),
            "native_pan": 0.0,
            "move_status": "IDLE",
        }

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        nonlocal pulse_calls
        pulse_calls += 1
        if pulse_calls > 1:
            raise PanoramaCaptureError("lease_lost")
        assert expected_frame is current_frame
        return {
            "frame": moved_frame,
            "pose": {
                **camera._position(),
                "native_pan": 107.0,
                "move_status": "IDLE",
            },
            "stable": True,
            "stationary": False,
            "match": {
                "verified": True,
                "overlap": 0.72,
                "displacement": 84.0,
                "shift_x": 84.0,
                "shift_y": 1.0,
            },
            "precondition_match": {
                "verified": True,
                "overlap": 0.99,
                "displacement": 0.2,
                "shift_x": 0.2,
                "shift_y": 0.1,
            },
            "evidence": {"stable": True, "state": "stable", "code": "stable"},
        }

    async def stopped_window(*, timeout=3.0):
        assert timeout == 3.0
        return current_frame

    async def current_anchor(_cursor, *, row):
        assert row == 0
        return True

    scanner._pulse = pulse
    scanner._observational_readback = position
    scanner._reference_window = stopped_window
    scanner._current_anchor = current_anchor
    monkeypatch.setattr(
        scan,
        "_no_effect_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.6))

    assert len(scanner.captures) == 2
    assert scanner.captures[-1]["previous_overlap"]["connection_method"] == (
        "native_pose_recovery_chain"
    )
    assert scanner.captures[-1]["previous_overlap"]["anchor_precondition"][
        "evidence"
    ] == "fresh_seek_native_pose"


def test_new_seek_connects_through_the_exact_frame_that_validated_its_anchor(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor.pop("seek")
    cursor["anchor_observation"] = {
        **cursor["anchor"],
        "capture_instance": anchor_frame["capture_instance"],
        "generation": anchor_frame["generation"],
        "sequence": anchor_frame["sequence"],
        "overlap": 0.99,
        "displacement": 3.5,
        "evidence": "accepted_capture",
    }
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    moved_frame = asyncio.run(camera.frame())
    scanner.capabilities, scanner.last_frame = camera.capabilities, anchor_frame
    pulse_calls = 0

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        nonlocal pulse_calls
        pulse_calls += 1
        if pulse_calls > 1:
            raise PanoramaCaptureError("lease_lost")
        assert expected_frame is anchor_frame
        return {
            "frame": moved_frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "match": {
                "verified": True,
                "overlap": 0.72,
                "displacement": 84.0,
                "shift_x": 84.0,
                "shift_y": 1.0,
            },
            "precondition_match": {
                "verified": True,
                "overlap": 0.99,
                "displacement": 0.2,
                "shift_x": 0.2,
                "shift_y": 0.1,
            },
            "evidence": {"stable": True, "state": "stable", "code": "stable"},
        }

    async def stopped_window(*, timeout=3.0):
        raise AssertionError("A fresh accepted anchor must not require another window")

    async def current_anchor(_cursor, *, row):
        assert row == 0
        return True

    scanner._pulse = pulse
    scanner._reference_window = stopped_window
    scanner._current_anchor = current_anchor
    monkeypatch.setattr(
        scan,
        "_no_effect_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.99,
            "displacement": 3.5,
            "shift_x": 3.5,
            "shift_y": 0.1,
        },
    )
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.6))

    assert len(scanner.captures) == 2
    overlap = scanner.captures[-1]["previous_overlap"]
    assert overlap["connection_method"] == "fresh_seek_precondition_chain"
    assert overlap["anchor_precondition"]["evidence"] == (
        "fresh_seek_verified_anchor"
    )


def test_native_axis_direction_uses_the_camera_observed_response():
    captures = []
    for index, native_pan in enumerate((400.0, 300.0, 200.0, 100.0)):
        captures.append(
            {
                "native_pan_position": native_pan,
                "movement": (
                    {"type": "seek_pulse", "axis": "pan", "direction": -1}
                    if index
                    else None
                ),
            }
        )

    evidence = scan._native_axis_direction_evidence(
        captures,
        "pan",
        -1,
        {"native_pan": 100.0},
        {"native_pan": 0.0},
    )

    assert evidence is not None
    assert evidence["verified"] is True
    assert evidence["samples"] == 3
    assert evidence["native_delta"] == -100.0
    assert (
        scan._native_axis_direction_evidence(
            captures,
            "pan",
            -1,
            {"native_pan": 100.0},
            {"native_pan": 200.0},
        )
        is None
    )


def test_seek_accepts_a_causal_late_endpoint_with_learned_axis_direction(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    history = []
    for index, shift in enumerate((92.0, 101.0, 96.0)):
        history.append({
            **copy.deepcopy(checkpoint["captures"][0]),
            "id": f"history-{index}",
            "movement": {"type": "seek_pulse", "axis": "pan", "direction": -1},
            "previous_overlap": {
                "verified": True,
                "overlap": 0.8,
                "displacement": shift,
                "shift_x": shift,
                "shift_y": 3.0,
            },
        })
    checkpoint["captures"] = history + checkpoint["captures"]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, anchor_frame
    scanner.last_pose = camera._position()
    calls = 0

    async def pulse(axis, direction, duration, **_options):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PanoramaCaptureError("lease_lost")
        _record_pending_seek_transition(
            scanner,
            cursor,
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        movement_id = "1" * 32
        cursor["transition"]["movement_id"] = movement_id
        scanner.checkpoint.setdefault("diagnostics", {"version": 1, "attempts": []})[
            "attempts"
        ].append({
            "kind": "movement",
            "outcome": "motion_not_observed",
            "movement_id": movement_id,
            "command_outcome": "accepted",
            "stop_command_accepted": True,
            "correction_precondition": {
                "verified": True,
                "overlap": 0.99,
                "displacement": 0.1,
            },
        })
        raise PanoramaCaptureError("motion_not_observed")

    async def confirm_stop():
        scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"

    recovered_match = {
        "verified": True,
        "overlap": 0.86,
        "displacement": 83.0,
        "shift_x": -82.0,
        "shift_y": 4.0,
        "support_scope": "distributed_scene",
    }
    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(scan, "_match", lambda *_: copy.deepcopy(recovered_match))

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert scanner.captures[-1]["movement"]["type"] == "seek_pulse"
    assert scanner.captures[-1]["previous_overlap"]["shift_x"] == -82.0
    assert cursor["transition"]["outcome"] == "late_endpoint_recovered"
    assert scanner.checkpoint["late_endpoint_recoveries"][-1]["direction"][
        "verified"
    ] is True


def test_seek_accepts_a_causal_late_endpoint_with_learned_native_direction(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    base_capture = checkpoint["captures"][0]
    history = []
    for index, native_pan in enumerate((400.0, 300.0, 200.0, 100.0)):
        history.append(
            {
                **copy.deepcopy(base_capture),
                "id": f"native-history-{index}",
                "native_pan_position": native_pan,
                "pose": {**base_capture["pose"], "native_pan": native_pan},
                "movement": (
                    {"type": "seek_pulse", "axis": "pan", "direction": -1}
                    if index
                    else None
                ),
            }
        )
    checkpoint["captures"] = history
    cursor["anchor"] = {
        "capture_id": history[-1]["id"],
        "path": history[-1]["path"],
        "row": 0,
    }
    cursor["direction"] = -1
    cursor["seek"]["direction"] = -1
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, anchor_frame
    calls = 0

    async def pulse(axis, direction, duration, **_options):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PanoramaCaptureError("lease_lost")
        _record_pending_seek_transition(
            scanner,
            cursor,
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        movement_id = "2" * 32
        cursor["transition"]["movement_id"] = movement_id
        scanner.checkpoint.setdefault("diagnostics", {"version": 1, "attempts": []})[
            "attempts"
        ].append(
            {
                "kind": "movement",
                "outcome": "motion_not_observed",
                "movement_id": movement_id,
                "command_outcome": "accepted",
                "stop_command_accepted": True,
                "correction_precondition": {
                    "verified": True,
                    "overlap": 0.99,
                    "displacement": 0.1,
                },
            }
        )
        raise PanoramaCaptureError("motion_not_observed")

    async def confirm_stop():
        scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"
        scanner.checkpoint["diagnostics"]["attempts"].append(
            {"kind": "return_reference", "outcome": "reference_observed"}
        )

    readbacks = 0

    async def native_readback():
        nonlocal readbacks
        readbacks += 1
        return {
            "native_pan": 100.0 if readbacks == 1 else 0.0,
            "native_tilt": None,
            "move_status": "IDLE",
            "error": "",
        }

    recovered_match = {
        "verified": True,
        "overlap": 0.9,
        "displacement": 90.0,
        "shift_x": 20.0,
        "shift_y": 40.0,
        "support_scope": "distributed_scene",
    }
    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    scanner._observational_readback = native_readback
    monkeypatch.setattr(scan, "_no_effect_match", lambda *_: copy.deepcopy(recovered_match))

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", -1, row=0, duration=1.2))

    recovery = scanner.checkpoint["late_endpoint_recoveries"][-1]
    assert recovery["direction_basis"] == "native_position_response"
    assert recovery["direction"]["native_delta"] == -100.0
    assert cursor["transition"]["outcome"] == "late_endpoint_recovered"


def _record_pending_seek_transition(scanner, cursor, frame, *, axis, direction, duration):
    path = scanner.directory / f"test-pending-{axis}-{cursor[('seek' if axis == 'pan' else 'vertical_seek')]['steps']}.jpg"
    digest = scan._write_image(path, frame["image"])
    state = cursor["seek" if axis == "pan" else "vertical_seek"]
    cursor["transition"] = {
        "state": "pending",
        "stage": cursor["stage"],
        "row": cursor["row"],
        "direction": cursor["direction"],
        "branch": cursor["branch"],
        "intent": {
            "type": "seek_pulse",
            "axis": axis,
            "direction": direction,
            "row": state["row"],
            "step": state["steps"],
            "duration": duration,
            "anchor": dict(cursor["anchor"]),
        },
        "baseline": {
            "path": str(path),
            "sha256": digest,
            "capture_instance": frame["capture_instance"],
            "sequence": frame["sequence"],
            "generation": frame["generation"],
            "media_time": frame.get("media_time"),
            "received_monotonic": frame["received_monotonic"],
        },
    }


def test_move_persists_exact_causal_seek_intent_before_uncertain_dispatch(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor["seek"].update(steps=1, duration=0.6)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    scanner.last_pose = camera._position()

    async def rejected_command():
        raise PanoramaCaptureError("movement_unconfirmed")

    with pytest.raises(PanoramaCaptureError, match="movement_unconfirmed"):
        asyncio.run(
            scanner._move(
                rejected_command,
                duration=0.6,
                allow_stationary=True,
                requested_velocity={"pan": 0.1, "tilt": 0.0},
                movement_intent={"axis": "pan", "direction": 1, "duration": 0.6},
            )
        )
    intent = scan._pending_seek_intent(scanner.checkpoint["continuous_cursor"])
    assert intent is not None
    assert intent["axis"] == "pan" and intent["direction"] == 1
    assert intent["step"] == 1 and intent["duration"] == pytest.approx(0.6)
    assert Path(intent["baseline"]["path"]).is_file()
    assert len(intent["baseline"]["sha256"]) == 64
    assert intent["baseline"]["capture_instance"] == camera.capture_instance


def test_new_durable_transition_prunes_unreferenced_causal_baselines(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["continuous_cursor"]["seek"].update(steps=1, duration=0.6)
    orphan = tmp_path / "transition-orphan.jpg"
    scan._write_image(orphan, frame["image"])
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    scanner.last_pose = camera._position()

    async def rejected_command():
        raise PanoramaCaptureError("movement_unconfirmed")

    with pytest.raises(PanoramaCaptureError, match="movement_unconfirmed"):
        asyncio.run(
            scanner._move(
                rejected_command,
                duration=0.6,
                allow_stationary=True,
                movement_intent={"axis": "pan", "direction": 1, "duration": 0.6},
            )
        )
    kept = Path(scanner.checkpoint["continuous_cursor"]["transition"]["baseline"]["path"])
    assert kept.is_file()
    assert not orphan.exists()
    assert list(tmp_path.glob("transition-*.jpg")) == [kept]


@pytest.mark.parametrize("anchor_displacement", [0.49, 4.09, 25.0])
def test_independent_stationary_seek_requires_the_persisted_anchor(
    tmp_path, monkeypatch, anchor_displacement
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.last_frame, scanner.physical_state = frame, "stopped"
    scanner.capabilities = camera.capabilities
    anchor = copy.deepcopy(checkpoint["continuous_cursor"]["anchor"])
    pulses = []

    async def stationary_pulse(axis, direction, duration, **_options):
        pulses.append((axis, direction))
        return {
            "frame": await camera.frame(),
            "pose": camera._position(),
            "stable": False,
            "stationary": True,
            "match": {"verified": True, "displacement": 0.0, "overlap": 1.0},
        }

    scanner._pulse = stationary_pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.9,
            "displacement": anchor_displacement,
        },
    )
    if anchor_displacement <= 15:
        outcome, _ = asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))
        assert outcome == "limit" and scanner.boundaries["pan_max"]["confirmed"]
    else:
        with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
            asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))
        assert "pan_max" not in scanner.boundaries
        assert "outcome" not in scanner.checkpoint["continuous_cursor"]["seek"]
    assert pulses == [("pan", 1)]
    assert len(scanner.captures) == 1
    assert scanner.checkpoint["continuous_cursor"]["anchor"] == anchor


def test_independent_all_64_seek_intents_remain_consumed_across_restart(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    pulses = []

    async def interrupted_pulse(axis, direction, duration, **_options):
        persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
        pulses.append(persisted["continuous_cursor"]["seek"]["steps"])
        raise scan._Stopped

    for expected_count in range(1, 65):
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
        scanner.capabilities = camera.capabilities
        scanner.last_frame = frame
        scanner._pulse = interrupted_pulse
        with pytest.raises(scan._Stopped):
            asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))
        checkpoint = json.loads((tmp_path / "scan-manifest.json").read_text())
        assert checkpoint["continuous_cursor"]["seek"]["steps"] == expected_count
        camera.now += 3600.0
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.last_frame, scanner._pulse = frame, interrupted_pulse
    assert asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))[0] == "budget"
    assert pulses == list(range(1, 65))
    assert len(scanner.captures) == 1 and not scanner.boundaries


def test_interrupted_horizontal_limit_probe_never_resumes_as_main_seek(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor["seek"].update(progress=False, stationary_count=1)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    pulses = []

    async def interrupt_probe(axis, direction, duration, **_options):
        pulses.append((axis, direction, duration))
        if len(pulses) == 2:
            persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
            intent = persisted["continuous_cursor"]["limit_probe_intent"]
            assert intent["state"] == "pending" and intent["direction"] == -1
            assert intent["step"] == 2 and intent["axis"] == "pan"
            raise scan._Stopped
        return {
            "frame": await camera.frame(),
            "pose": camera._position(),
            "stable": False,
            "stationary": True,
            "match": {"verified": True, "displacement": 0.0, "overlap": 1.0},
        }

    scanner._pulse = interrupt_probe
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))

    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    intent = scan._pending_limit_probe_intent(saved["continuous_cursor"])
    assert intent is not None and intent["direction"] == -1
    resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, saved)
    resumed.capabilities, resumed.last_frame = camera.capabilities, frame

    async def forbidden_pulse(*_args, **_kwargs):
        raise AssertionError("A pending limit probe must not become a main seek pulse")

    resumed._pulse = forbidden_pulse
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(resumed._seek("pan", 1, row=0, duration=0.3))
    assert pulses == [("pan", 1, 0.3), ("pan", -1, 0.3)]


def test_interrupted_vertical_limit_probe_never_resumes_as_main_step(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"
    cursor["vertical_seek"] = {
        "row": 0,
        "branch": -1,
        "anchor": dict(cursor["anchor"]),
        "steps": 0,
        "progress": False,
        "stationary": 1,
        "duration": 0.3,
        "excursion": False,
    }
    pulses = []

    async def interrupt_probe(axis, direction, duration, **_options):
        pulses.append((axis, direction, duration))
        if len(pulses) == 2:
            persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
            intent = persisted["continuous_cursor"]["limit_probe_intent"]
            assert intent["state"] == "pending" and intent["direction"] == 1
            assert intent["step"] == 2 and intent["axis"] == "tilt"
            raise scan._Stopped
        return {
            "frame": await camera.frame(),
            "pose": camera._position(),
            "stable": False,
            "stationary": True,
            "match": {"verified": True, "displacement": 0.0, "overlap": 1.0},
        }

    scanner._pulse = interrupt_probe
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._next_band(cursor))

    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    intent = scan._pending_limit_probe_intent(saved["continuous_cursor"])
    assert intent is not None and intent["direction"] == 1
    resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, saved)
    resumed.capabilities, resumed.last_frame = camera.capabilities, local

    async def forbidden_pulse(*_args, **_kwargs):
        raise AssertionError("A pending limit probe must not become a main step pulse")

    resumed._pulse = forbidden_pulse
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(resumed._next_band(saved["continuous_cursor"]))
    assert pulses == [("tilt", -1, 0.3), ("tilt", 1, 0.3)]


@pytest.mark.parametrize("no_effect_displacement", [0.0, 0.49])
def test_unconfirmed_seek_retries_only_after_fresh_causal_baseline(
    tmp_path, monkeypatch, no_effect_displacement
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor["direction"] = -1
    cursor["seek"].update(direction=-1, duration=1.2)
    cursor["transition"] = {
        "state": "pending",
        "stage": "pan",
        "row": 0,
        "direction": -1,
        "branch": -1,
    }
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    scanner.physical_state = "unknown"
    durations = []
    stop_observed = []

    async def pulse(axis, direction, duration, **_options):
        durations.append(duration)
        _record_pending_seek_transition(
            scanner,
            cursor,
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        if len(durations) == 1:
            raise PanoramaCaptureError("movement_unconfirmed")
        return {
            "frame": await camera.frame(),
            "pose": camera._position(),
            "stable": False,
            "stationary": True,
            "match": {"verified": True, "displacement": 0.0, "overlap": 1.0},
        }

    async def confirm_stop():
        stop_observed.append(True)
        scanner.physical_state = "stopped"
        scanner.last_frame = await camera.frame()

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.99,
            "displacement": no_effect_displacement,
        },
    )
    outcome, duration = asyncio.run(scanner._seek("pan", -1, row=0, duration=1.2))
    assert outcome == "limit" and duration == pytest.approx(0.6)
    assert stop_observed == [True]
    assert durations == [1.2, 0.6]
    assert cursor["seek"]["steps"] == 2
    assert cursor["seek"]["command_failures"] == 1
    assert cursor["transition"]["state"] == "accepted"
    assert scanner.checkpoint["seek_recovery_level"]["pan"] == 1


@pytest.mark.parametrize("observed_displacement", [0.51, 1.9, 4.0, 25.0])
def test_unconfirmed_seek_never_retries_when_effect_is_not_excluded(
    tmp_path, monkeypatch, observed_displacement
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    durations = []

    async def pulse(axis, direction, duration, **_options):
        durations.append(duration)
        _record_pending_seek_transition(
            scanner,
            checkpoint["continuous_cursor"],
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        raise PanoramaCaptureError("movement_unconfirmed")

    async def confirm_stop():
        scanner.physical_state = "stopped"
        scanner.last_frame = await camera.frame()

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.99,
            "displacement": observed_displacement,
        },
    )
    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2]
    assert checkpoint["continuous_cursor"]["seek"]["steps"] == 1
    assert "seek_recovery_level" not in checkpoint
    persisted_cursor = scanner.checkpoint["continuous_cursor"]
    assert persisted_cursor["transition"]["state"] == "effect_or_state_ambiguous"
    assert persisted_cursor["relocalization_failed"] is True


@pytest.mark.parametrize("identity_change", ["capture_instance", "generation"])
def test_unconfirmed_seek_never_retries_across_capture_identity_change(
    tmp_path, monkeypatch, identity_change
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    durations = []

    async def pulse(axis, direction, duration, **_options):
        durations.append(duration)
        _record_pending_seek_transition(
            scanner,
            cursor,
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        raise PanoramaCaptureError("movement_unconfirmed")

    async def confirm_stop():
        scanner.physical_state = "stopped"
        scanner.last_frame = await camera.frame()
        if identity_change == "capture_instance":
            scanner.last_frame["capture_instance"] = "replacement-decoder"
        else:
            scanner.last_frame["generation"] += 1

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 0.99, "displacement": 0.0},
    )
    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2]
    assert cursor["seek"]["steps"] == 1
    assert "seek_recovery_level" not in checkpoint
    assert cursor["transition"]["state"] == "effect_or_state_ambiguous"


def test_unconfirmed_seek_has_only_one_persisted_retry(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor["seek"]["duration"] = 1.2
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    durations = []

    async def pulse(axis, direction, duration, **_options):
        durations.append(duration)
        _record_pending_seek_transition(
            scanner,
            cursor,
            scanner.last_frame,
            axis=axis,
            direction=direction,
            duration=duration,
        )
        raise PanoramaCaptureError("movement_unconfirmed")

    async def confirm_stop():
        scanner.physical_state = "stopped"
        scanner.last_frame = await camera.frame()

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 0.99, "displacement": 0.0},
    )
    with pytest.raises(PanoramaCaptureError, match="movement_unconfirmed"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert durations == [1.2, 0.6]
    assert cursor["seek"]["steps"] == 2
    assert cursor["seek"]["command_failures"] == 1
    assert scanner.checkpoint["seek_recovery_level"]["pan"] == 1


def test_independent_last_seek_slot_cannot_be_reused_by_retry_or_restart(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["continuous_cursor"]["seek"]["steps"] = 63
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    pulses = []

    async def failed_pulse(axis, direction, duration, **_options):
        pulses.append(duration)
        raise PanoramaCaptureError("motion_not_observed")

    scanner._pulse = failed_pulse
    with pytest.raises(PanoramaCaptureError, match="pan_row_limit_unconfirmed"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=0.3))
    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert saved["continuous_cursor"]["seek"]["steps"] == 64
    assert saved["seek_recovery_level"]["pan"] == 1
    resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, saved)
    resumed.last_frame, resumed._pulse = frame, failed_pulse
    assert asyncio.run(resumed._seek("pan", 1, row=0, duration=0.3))[0] == "budget"
    assert pulses == [0.3]


def test_observation_retry_uses_a_fresh_stopped_origin_within_motion_floor(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    expected_frames = []
    durations = []
    stopped_frame = None

    async def pulse(_axis, _direction, duration, *, expected_frame=None):
        expected_frames.append(expected_frame)
        durations.append(duration)
        if len(expected_frames) == 1:
            raise PanoramaCaptureError("motion_not_observed")
        assert expected_frame is stopped_frame
        return {
            "frame": await camera.frame(),
            "pose": camera._position(),
            "stable": False,
            "stationary": True,
            "match": {"verified": True, "overlap": 1.0, "displacement": 0.0},
            "precondition_match": {
                "verified": True,
                "overlap": 1.0,
                "displacement": 0.1,
            },
        }

    async def confirm_stop():
        nonlocal stopped_frame
        stopped_frame = await camera.frame()
        scanner.last_frame = stopped_frame
        scanner.physical_state = "stopped"

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 0.99, "displacement": 1.5},
    )

    outcome, duration = asyncio.run(scanner._seek("pan", 1, row=0, duration=0.12))

    assert outcome == "limit" and duration == pytest.approx(0.24)
    assert durations == [0.12, 0.24]
    assert expected_frames[0]["image"] is not stopped_frame["image"]
    assert cursor["seek"]["duration_adaptation"] == {
        "reason": "device_no_effect",
        "from_seconds": 0.12,
        "to_seconds": 0.24,
        "step": 1,
    }
    assert cursor["seek"]["recovery_anchor_precondition"] == {
        "verified": True,
        "identity_verified": True,
        "overlap": 0.99,
        "displacement": 1.5,
        "code": None,
        "evidence": "visual_no_effect",
    }


def test_observation_retry_accepts_native_no_effect_at_a_sparse_endpoint(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    calls = 0

    async def pulse(_axis, _direction, _duration, *, expected_frame=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PanoramaCaptureError("motion_not_observed")
        raise PanoramaCaptureError("lease_lost")

    async def confirm_stop():
        scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"

    async def native_readback():
        return {
            "native_pan": 0.0,
            "native_tilt": None,
            "move_status": "IDLE",
            "error": "",
        }

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    scanner._observational_readback = native_readback
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": False, "code": "correspondences_not_distributed"},
    )
    monkeypatch.setattr(
        scan,
        "_no_effect_match",
        lambda *_: {
            "verified": False,
            "code": "correspondences_not_distributed",
        },
    )

    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert calls == 2
    assert checkpoint["continuous_cursor"]["seek"]["recovery_anchor_precondition"] == {
        "verified": True,
        "identity_verified": True,
        "overlap": None,
        "displacement": None,
        "code": "correspondences_not_distributed",
        "evidence": "native_pose_unchanged",
        "native_stationary": {
            "verified": True,
            "axes": {"pan": {"before": 0.0, "after": 0.0}},
            "move_status": "IDLE",
        },
    }


@pytest.mark.parametrize("displacement", [2.0001, 12.0])
def test_observation_retry_never_moves_from_a_view_beyond_motion_floor(
    tmp_path, monkeypatch, displacement
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities, scanner.last_frame = camera.capabilities, frame
    pulses = []

    async def pulse(*_args, **_options):
        pulses.append(_options.get("expected_frame"))
        raise PanoramaCaptureError("motion_not_observed")

    async def confirm_stop():
        scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"

    scanner._pulse, scanner._confirm_stop = pulse, confirm_stop
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.99,
            "displacement": displacement,
        },
    )

    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))

    assert len(pulses) == 1
    assert cursor["relocalization_failed"] is True


def test_independent_stale_valid_anchor_uses_fresh_observation_without_motion(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.last_frame, scanner.physical_state = frame, "stopped"
    anchor = copy.deepcopy(checkpoint["continuous_cursor"]["anchor"])
    sequence = frame["sequence"]
    camera.now += 1.1
    assert asyncio.run(scanner._current_anchor(checkpoint["continuous_cursor"], row=0))
    assert camera.sequence >= sequence + 5
    assert camera.now - scanner.last_frame["received_monotonic"] <= 1.0
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))
    assert scanner.checkpoint["continuous_cursor"]["anchor"] == anchor


def test_independent_active_budget_survives_a_long_pause_without_charging_it(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, _ = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["active_seconds"] = 1190.0
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = camera.capabilities
    camera.now += 4.0
    asyncio.run(scanner._persist())
    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert saved["active_seconds"] == pytest.approx(1194.0)
    camera.now += 86400.0
    resumed = scan._Scan(camera, tmp_path, _progress, lambda: False, saved)
    resumed.capabilities = camera.capabilities
    assert resumed._active_elapsed() == pytest.approx(1194.0)
    resumed._check()
    camera.now += 6.0
    with pytest.raises(PanoramaCaptureError, match="scan_budget_exhausted"):
        resumed._check()
    asyncio.run(resumed._persist())
    final = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert final["active_seconds"] == pytest.approx(1200.0)
    camera.now += 86400.0
    final_scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, final)
    with pytest.raises(PanoramaCaptureError, match="scan_budget_exhausted"):
        final_scanner._check()
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


@pytest.mark.parametrize("cursor_version", [2, 3])
def test_independent_return_only_survives_an_exhausted_capture_budget(
    tmp_path, monkeypatch, cursor_version
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, _ = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["active_seconds"] = scan.MAX_JOB_SECONDS
    checkpoint["continuous_cursor"]["version"] = cursor_version
    camera.pan = 0.15
    camera.target = (camera.pan, camera.tilt)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    result = asyncio.run(scanner.run(return_only=True))
    assert result["physical_state"] == "restored"
    assert _event_names(camera).count("return") == 1
    assert "velocity" not in _event_names(camera)
    assert not any(issue["code"] == "scan_budget_exhausted" for issue in result["issues"])
    assert result["checkpoint"]["active_seconds"] < scan.MAX_JOB_SECONDS + 5.0
    assert camera.lease is None and len(result["captures"]) == 1


def test_independent_exhausted_acquisition_still_runs_final_restore(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, _ = _independent_budget_checkpoint(tmp_path, camera)
    checkpoint["active_seconds"] = scan.MAX_JOB_SECONDS
    camera.pan = 0.15
    camera.target = (camera.pan, camera.tilt)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)

    async def already_relocalized(_current):
        pass

    scanner._relocalize = already_relocalized
    result = asyncio.run(scanner.run(return_only=False))
    assert result["physical_state"] == "restored"
    assert any(issue["code"] == "scan_budget_exhausted" for issue in result["issues"])
    assert _event_names(camera).count("return") == 1
    assert len([event for event in camera.events if event[0] == "absolute"]) == 1
    assert "velocity" not in _event_names(camera)
    assert camera.lease is None and len(result["captures"]) == 1


@pytest.mark.parametrize("at_reference", [False, True])
def test_resume_of_pending_recovery_requires_observed_destination_without_replaying_return(
    tmp_path, monkeypatch, at_reference
):
    scanner, camera, cursor, reference, local = _recovery_scan(tmp_path, monkeypatch)
    cursor.update(
        stage="return_reference", after_reference_stage="step", after_reference_direction=-1
    )
    cursor["recovery_attempts"] = {"step:0:-1": 1}
    cursor["recovery"] = {"destination": "step:0:-1", "state": "pending"}
    cursor["transition"] = {
        "state": "pending",
        "stage": "return_reference",
        "row": cursor["row"],
        "direction": cursor["direction"],
        "branch": cursor["branch"],
    }
    current = reference if at_reference else local
    scanner.checkpoint["captures"] = scanner.captures

    async def scenario():
        if not at_reference:
            with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
                await scanner._relocalize(current)
            return
        await scanner._relocalize(current)
        assert cursor["recovery"]["state"] == "confirmed"
        assert cursor["transition"]["state"] == "confirmed"
        await scanner._return_to_reference_band(cursor)
        assert cursor["stage"] == "step" and cursor["row"] == 0
        assert cursor["anchor"]["capture_id"] == "anchor-0"

    asyncio.run(scenario())
    assert camera.events == []
    assert cursor["recovery_attempts"] == {"step:0:-1": 1}


def test_resume_of_pending_seek_halves_amplitude_after_proving_the_old_anchor(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch, direction=-1)
    cursor["seek"] = {
        "axis": "pan",
        "direction": -1,
        "row": 0,
        "origin_path": cursor["anchor"]["path"],
        "progress": True,
        "steps": 2,
        "stationary_count": 0,
        "origin_excursion": False,
        "duration": 1.2,
    }
    _record_pending_seek_transition(
        scanner,
        cursor,
        local,
        axis="pan",
        direction=-1,
        duration=1.2,
    )
    scanner.checkpoint["captures"] = scanner.captures
    current = asyncio.run(camera.frame())
    asyncio.run(scanner._relocalize(current))
    assert cursor["transition"]["state"] == "no_effect_verified"
    assert cursor["transition"]["outcome"] == "stopped_baseline_unchanged"
    assert cursor["transition"]["recovery"] == {
        "method": "causal_baseline_verified",
        "previous_duration": 1.2,
        "next_duration": 0.6,
    }
    assert cursor["seek"]["duration"] == pytest.approx(0.6)
    assert cursor["seek"]["command_failures"] == 1
    assert scanner.checkpoint["seek_recovery_level"]["pan"] == 1
    persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
    assert persisted["continuous_cursor"]["seek"]["duration"] == pytest.approx(0.6)
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_resume_of_pending_seek_continues_from_a_verified_local_graph_without_replay(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, local = _recovery_scan(
        tmp_path, monkeypatch, direction=1
    )
    cursor["seek"] = {
        "axis": "pan",
        "direction": 1,
        "row": 0,
        "origin_path": cursor["anchor"]["path"],
        "progress": True,
        "steps": 2,
        "stationary_count": 0,
        "origin_excursion": False,
        "duration": 1.2,
    }
    _record_pending_seek_transition(
        scanner,
        cursor,
        local,
        axis="pan",
        direction=1,
        duration=1.2,
    )
    scanner.checkpoint["captures"] = scanner.captures
    camera.pan = 0.22
    camera.target = (camera.pan, camera.tilt)
    current = asyncio.run(camera.frame())

    asyncio.run(scanner._relocalize(current))

    assert cursor["transition"]["state"] == "relocalized"
    assert cursor["transition"]["outcome"] == "stopped_graph_relocalized"
    assert cursor["seek"]["origin_uncertain_since_anchor"] is True
    assert scanner.checkpoint["relocalizations"][-1]["row"] == 0
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))

    async def observe_dispatch_frame(axis, direction, duration, **options):
        assert (axis, direction, duration) == ("pan", 1, 1.2)
        assert options["expected_frame"] is current
        raise PanoramaCaptureError("lease_lost")

    scanner._pulse = observe_dispatch_frame
    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))


def test_resume_rejects_legacy_pending_seek_without_causal_baseline(tmp_path, monkeypatch):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch, direction=-1)
    cursor["seek"] = {
        "axis": "pan",
        "direction": -1,
        "row": 0,
        "origin_path": cursor["anchor"]["path"],
        "progress": True,
        "steps": 2,
        "stationary_count": 0,
        "origin_excursion": False,
        "duration": 1.2,
    }
    cursor["transition"] = {
        "state": "pending",
        "stage": "pan",
        "row": 0,
        "direction": -1,
        "branch": -1,
    }
    scanner.checkpoint["captures"] = scanner.captures
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(scanner._relocalize(local))
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_resume_rejects_pending_seek_identity_mismatch(tmp_path, monkeypatch):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch, direction=-1)
    cursor["seek"] = {
        "axis": "pan",
        "direction": -1,
        "row": 0,
        "origin_path": cursor["anchor"]["path"],
        "progress": True,
        "steps": 2,
        "stationary_count": 0,
        "origin_excursion": False,
        "duration": 1.2,
    }
    _record_pending_seek_transition(
        scanner,
        cursor,
        local,
        axis="pan",
        direction=-1,
        duration=1.2,
    )
    cursor["transition"]["intent"]["direction"] = 1
    scanner.checkpoint["captures"] = scanner.captures
    with pytest.raises(PanoramaCaptureError, match="continuous_resume_unavailable"):
        asyncio.run(scanner._relocalize(local))
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_resume_after_intermediate_vertical_capture_preserves_origin_and_remaining_pulses(
    tmp_path, monkeypatch
):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    cursor["stage"] = "step"
    pulses = []

    async def pulse(axis, side, duration, **_options):
        pulses.append((axis, side))
        previous = scanner.last_frame
        camera.tilt = -0.3 if len(pulses) == 1 else -0.95
        frame = await camera.frame()
        scanner.last_frame, scanner.physical_state = frame, "stopped"
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": scan._match(previous["image"], frame["image"]),
        }

    async def pause_after_capture(event):
        if len(scanner.captures) == 3:
            raise scan._Stopped

    scanner._pulse, scanner.progress = pulse, pause_after_capture
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._next_band(cursor))
    checkpoint = copy.deepcopy(scanner.checkpoint)
    original_hashes = [item.get("sha256") for item in scanner.captures]
    assert cursor["row"] == 0 and cursor["anchor"]["row"] == -1
    assert cursor["vertical_seek"]["row"] == 0 and cursor["vertical_seek"]["steps"] == 1
    camera.now += 86400
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = {"continuous_supported": True}
    scanner._pulse = pulse
    current = asyncio.run(camera.frame())
    scanner.last_pose = camera._position()
    asyncio.run(scanner._relocalize(current))
    cursor = scanner.checkpoint["continuous_cursor"]
    assert cursor["vertical_seek"]["steps"] == 1 and cursor["row"] == 0
    assert asyncio.run(scanner._next_band(cursor))
    assert cursor["row"] == -1 and cursor["stage"] == "pan"
    assert len(pulses) == 2
    assert [item.get("sha256") for item in scanner.captures[:3]] == original_hashes
    assert scanner.checkpoint["active_seconds"] < 10


@pytest.mark.parametrize("view_changes", [False, True])
def test_work_destination_is_saved_at_qualified_view_and_rechecked(tmp_path, monkeypatch, view_changes):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    reference = scanner.captures[-1]
    cursor["reference_path"] = reference["path"]
    cursor.pop("reference_destination")
    before = copy.deepcopy(scanner.saved_return)
    save = camera.save_return

    async def save_then_observe(role="original", **options):
        destination = await save(role=role, **options)
        if view_changes:
            camera.pan, camera.target = 0.17, (0.17, 0.0)
        return destination

    camera.save_return = save_then_observe
    if view_changes:
        with pytest.raises(PanoramaCaptureError, match="working_reference_changed"):
            asyncio.run(scanner._save_reference_destination(cursor))
        assert "reference_destination" not in cursor
        assert scanner.checkpoint["pending_returns"][0]["pan"] == pytest.approx(0.08)
        diagnostic = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
        assert diagnostic["outcome"] == "anchor_unconfirmed"
        assert diagnostic["frame_age_after_matching_seconds"] <= 1
        rejected = scanner.checkpoint["rejected_motion_pairs"][-1]
        assert rejected["qualified_capture"] is False
        assert Path(rejected["before"]["path"]).is_file()
        assert Path(rejected["after"]["path"]).is_file()
    else:
        asyncio.run(scanner._save_reference_destination(cursor))
        destination = cursor["reference_destination"]
        assert destination["pan"] == pytest.approx(0.08)
        assert destination["capture_id"] == reference["id"]
        persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
        assert persisted["continuous_cursor"]["reference_destination"] == destination
        assert not scanner.checkpoint["pending_returns"]
    assert scanner.saved_return == before
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_missing_work_destination_keeps_local_photos_without_returning_to_original(tmp_path, monkeypatch):
    scanner, camera, cursor, _, local = _recovery_scan(tmp_path, monkeypatch)
    cursor["reference_path"] = scanner.captures[-1]["path"]
    cursor.pop("reference_destination")
    preserved = copy.deepcopy(scanner.captures)

    async def unavailable(**options):
        raise PanoramaCaptureError("return_unavailable")

    camera.save_return = unavailable
    asyncio.run(scanner._save_reference_destination(cursor))
    assert "reference_destination" not in cursor
    assert scanner.captures == preserved
    assert scanner.issues[-1]["code"] == "working_reference_unavailable"
    scanner.last_frame = {**local, "image": np.zeros_like(local["image"])}

    async def stopped():
        scanner.physical_state = "stopped"

    scanner._confirm_stop = stopped
    assert not asyncio.run(scanner._recover(cursor, PanoramaCaptureError("coverage_connection_unverified"), axis="pan"))
    assert scanner.captures == preserved
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_original_preset_intent_is_durable_before_creation(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    intent = {"kind": "pending_preset", "role": "original", "preset_name": "owned", "owner_id": camera.owner_id}

    async def ambiguous_creation(*, before_create):
        await before_create(intent)
        persisted = json.loads((tmp_path / "scan-manifest.json").read_text())
        assert persisted["pending_returns"] == [intent]
        assert Path(persisted["initial_path"]).is_file()
        raise PanoramaCaptureError("return_creation_unconfirmed")

    async def unresolved_cleanup(_saved):
        raise PanoramaCaptureError("return_cleanup_unconfirmed")

    camera.save_return = ambiguous_creation
    camera.remove_return = unresolved_cleanup
    result = asyncio.run(scanner.run(return_only=False))
    assert result["checkpoint"]["pending_returns"] == [intent]
    assert any(issue["code"] == "return_creation_unconfirmed" for issue in result["issues"])
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))
    assert camera.lease is None


@pytest.mark.parametrize("view_changes", [False, True])
def test_original_destination_is_associated_after_persistence_and_creation(tmp_path, monkeypatch, view_changes):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    frame = asyncio.run(camera.frame())
    path = tmp_path / "original.jpg"
    cv2.imwrite(str(path), frame["image"])
    scanner.checkpoint["initial_path"] = str(path)
    scanner.last_frame, scanner.physical_state = frame, "stopped"
    save = camera.save_return

    async def save_at_changed_view(**options):
        if view_changes:
            camera.pan, camera.target = 0.12, (0.12, 0.0)
        return await save(**options)

    camera.save_return = save_at_changed_view
    if view_changes:
        with pytest.raises(PanoramaCaptureError, match="original_reference_changed"):
            asyncio.run(scanner._save_original_destination())
        assert scanner.saved_return is None
        assert scanner.checkpoint["pending_returns"][0]["pan"] == pytest.approx(0.12)
    else:
        asyncio.run(scanner._save_original_destination())
        assert scanner.saved_return["pan"] == pytest.approx(0.0)
        assert not scanner.checkpoint["pending_returns"]
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_control_verification_refuses_to_move_without_a_return_destination(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

    async def unavailable_return(**_options):
        camera.events.append(("save_return", "unavailable"))
        raise PanoramaCaptureError("return_capacity_unavailable")

    camera.save_return = unavailable_return
    result = asyncio.run(scanner.run(return_only=False, verify_control=True))

    assert result["physical_state"] == "stopped"
    assert {issue["code"] for issue in result["issues"]} >= {
        "return_reference_unavailable",
    }
    assert any(
        issue.get("reason") == "return_capacity_unavailable"
        for issue in result["issues"]
    )
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))
    assert "control_verification" not in result["checkpoint"]


@pytest.mark.parametrize("return_only", [False, True])
@pytest.mark.parametrize("terminal", ["done", "relocalization_failed", "budget"])
def test_terminal_partial_releases_owned_presets_after_verified_return(tmp_path, monkeypatch, return_only, terminal):
    scanner, camera, cursor, reference, _ = _recovery_scan(tmp_path, monkeypatch)
    camera.capabilities.update(absolute_supported=False, continuous_supported=True)
    scanner.capabilities = camera.capabilities
    camera.pan, camera.target = 0.0, (0.0, 0.0)
    original_path = tmp_path / "original.jpg"
    cv2.imwrite(str(original_path), reference["image"])
    scanner.checkpoint["initial_path"] = str(original_path)
    scanner.saved_return.update(kind="preset", preset_token="original", owner_id=camera.owner_id)
    cursor["reference_destination"].update(kind="preset", preset_token="work", owner_id=camera.owner_id)
    scanner.last_frame, scanner.physical_state = reference, "stopped"
    asyncio.run(scanner._persist())
    checkpoint = copy.deepcopy(scanner.checkpoint)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)

    async def observed(_frame):
        pass

    async def finish_partial():
        active = scanner.checkpoint["continuous_cursor"]
        if terminal == "done":
            active["stage"] = "done"
        elif terminal == "relocalization_failed":
            active["relocalization_failed"] = True
        else:
            scanner.active_seconds = scan.MAX_JOB_SECONDS

    scanner._relocalize, scanner._continuous_scan = observed, finish_partial
    if return_only:
        asyncio.run(finish_partial())
    result = asyncio.run(scanner.run(return_only=return_only))
    removed = [saved["preset_token"] for name, saved in camera.events if name == "remove_return"]
    assert sorted(removed) == ["original", "work"]
    assert result["physical_state"] == "restored"
    assert result["checkpoint"]["return"] is None
    assert "reference_destination" not in result["checkpoint"]["continuous_cursor"]
    assert len(result["captures"]) == 2


def test_run_changed_original_stops_and_cleans_unassociated_owned_preset(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    save = camera.save_return

    async def shifted_preset(**options):
        camera.pan, camera.target = 0.12, (0.12, 0.0)
        destination = await save(**options)
        return {**destination, "kind": "preset", "preset_token": "unassociated-original", "owner_id": camera.owner_id}

    camera.save_return = shifted_preset
    result = asyncio.run(scan._Scan(camera, tmp_path, _progress, lambda: False, None).run(return_only=False))
    assert result["physical_state"] == "stopped"
    assert any(issue["code"] == "original_reference_changed" for issue in result["issues"])
    assert result["checkpoint"]["return"] is None and not result["checkpoint"]["pending_returns"]
    assert not result["captures"]
    assert [saved["preset_token"] for name, saved in camera.events if name == "remove_return"] == ["unassociated-original"]
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))
    assert camera.lease is None


def test_delayed_video_does_not_turn_real_tilt_motion_into_a_stationary_boundary(tmp_path, monkeypatch):
    class DelayedVideoCamera(SimulatedCamera):
        def __init__(self):
            super().__init__()
            self.delayed_images = deque()

        async def frame(self, **kwargs):
            sample = await super().frame(**kwargs)
            self.delayed_images.append(sample["image"])
            if len(self.delayed_images) > 25:
                sample["image"] = self.delayed_images.popleft()
            else:
                noise = np.random.default_rng(self.sequence).normal(0, 0.5, self.texture.shape)
                image = np.clip(self.texture.astype(np.float64) + noise, 0, 255).astype(np.uint8)
                sample["image"] = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            return sample

    camera = DelayedVideoCamera()
    _clock(monkeypatch, camera)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        return await scanner._move(
            lambda: camera.move_velocity(tilt=.2, timeout_s=.5),
            duration=.5, allow_stationary=True,
        )

    result = asyncio.run(scenario())
    assert result["stable"] is True
    assert result["stationary"] is False
    assert result["match"]["displacement"] > 2
    attempt = json.loads((tmp_path / "scan-diagnostics.json").read_text())["attempts"][-1]
    assert attempt["first_motion_transition_seconds"] > 2
    assert attempt["stop_accepted_seconds"] < 1
    assert camera.now < 100 + scan.ATTEMPT_SECONDS, "Observed movement still finishes as soon as video settles"


@pytest.mark.parametrize("stop_failure", [False, True])
def test_video_observation_continues_during_slow_stop_response(tmp_path, monkeypatch, stop_failure):
    class SlowStopCamera(SimulatedCamera):
        def __init__(self):
            super().__init__()
            self.delayed_images = deque()
            self.stop_started = None
            self.stop_ready = asyncio.Event()
            self.stop_observations = 0

        async def frame(self, **kwargs):
            sample = await super().frame(**kwargs)
            self.delayed_images.append(sample["image"])
            if len(self.delayed_images) > 12:
                sample["image"] = self.delayed_images.popleft()
            else:
                noise = np.random.default_rng(self.sequence).normal(0, .5, self.texture.shape)
                sample["image"] = cv2.cvtColor(
                    np.clip(self.texture.astype(float) + noise, 0, 255).astype(np.uint8),
                    cv2.COLOR_GRAY2BGR,
                )
            if self.stop_started is not None:
                self.stop_observations += 1
                if self.now - self.stop_started >= 3.2:
                    self.stop_ready.set()
            return sample

        async def stop(self):
            receipt = await super().stop()
            self.stop_started = self.now
            # The guard detects a deadlock if the observer awaits this response.
            await asyncio.wait_for(self.stop_ready.wait(), timeout=2)
            if stop_failure:
                raise PanoramaCaptureError("stop_unconfirmed")
            return receipt

    camera = SlowStopCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)

    async def scenario():
        return await scanner._move(
            lambda: camera.move_velocity(tilt=.2, timeout_s=.5),
            duration=.5, allow_stationary=True,
        )

    if stop_failure:
        with pytest.raises(PanoramaCaptureError, match="stop_unconfirmed"):
            asyncio.run(scenario())
        assert scanner.physical_state == "stop_unconfirmed"
    else:
        result = asyncio.run(scenario())
        assert result["stable"] and not result["stationary"]
        attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
        assert attempt["stop_requested_seconds"] < attempt["first_motion_transition_seconds"]
        assert attempt["first_motion_transition_seconds"] < attempt["stop_accepted_seconds"]
        assert result["frame"]["received_monotonic"] > 100 + attempt["stop_accepted_seconds"]
    assert camera.stop_observations >= 32
    assert _event_names(camera).count("velocity") == 1
    assert _event_names(camera).count("stop") == 1


def test_no_motion_uses_the_full_bounded_observation_before_stationary_evidence(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    camera.pan = .2
    camera.target = (.2, 0)
    _clock(monkeypatch, camera)

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        result = await scanner._move(
            lambda: camera.move_velocity(pan=.2, timeout_s=.5),
            duration=.5, allow_stationary=True,
        )
        assert scanner.captures == []
        return result

    result = asyncio.run(scenario())
    assert result["stationary"] is True and result["stable"] is False
    assert result["evidence"]["has_motion_transition"] is False
    assert scan.ATTEMPT_SECONDS <= camera.now - 100 <= scan.ATTEMPT_SECONDS + .3


def test_native_position_corroborates_stationary_head_despite_scene_motion(
    tmp_path, monkeypatch
):
    class NativeBoundaryCamera(SimulatedCamera):
        def _position(self):
            return {
                **super()._position(),
                "native_pan": 18.0,
                "native_tilt": 200.0,
            }

        async def move_velocity(self, **options):
            self.events.append(("velocity", options))
            self.remaining_motion_frames = 0
            return {"accepted": True}

    camera = NativeBoundaryCamera()
    _clock(monkeypatch, camera)
    monkeypatch.setattr(
        scan,
        "_no_effect_match",
        lambda *_: {
            "verified": True,
            "support_scope": "distributed_scene",
            "overlap": 0.984,
            "displacement": 8.0,
            "shift_x": 2.2,
            "shift_y": 2.6,
        },
    )

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        scanner.last_pose = await camera.position()
        return scanner, await scanner._move(
            lambda: camera.move_velocity(pan=-0.1, timeout_s=1.2),
            duration=1.2,
            allow_stationary=True,
            requested_velocity={"pan": -0.1, "tilt": 0.0},
        )

    scanner, result = asyncio.run(scenario())

    assert result["stationary"] is True
    assert result["stable"] is False
    assert result["native_stationary"] == {
        "verified": True,
        "axes": {"pan": {"before": 18.0, "after": 18.0}},
        "move_status": "IDLE",
    }
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["native_stationary_evidence"] == result["native_stationary"]


def test_movement_honors_a_larger_explicit_observation_budget(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    camera.pan = .2
    camera.target = (.2, 0)
    _clock(monkeypatch, camera)
    attempt_seconds = scan.ATTEMPT_SECONDS + 7

    async def scenario():
        scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
        result = await scanner._move(
            lambda: camera.move_velocity(pan=.2, timeout_s=.5),
            duration=.5,
            allow_stationary=True,
            attempt_seconds=attempt_seconds,
        )
        return scanner, result

    scanner, result = asyncio.run(scenario())
    assert result["stationary"] is True
    assert attempt_seconds <= camera.now - 100 <= attempt_seconds + .3
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert attempt["total_budget_seconds"] == attempt_seconds


def test_repeated_stationary_limit_uses_the_latest_stopped_frame_as_precondition(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    anchor_frame = asyncio.run(camera.frame())
    anchor_path = tmp_path / "stationary-limit-anchor.jpg"
    cv2.imwrite(str(anchor_path), anchor_frame["image"])
    scanner.captures = [{
        "id": "anchor",
        "path": str(anchor_path),
        "row_index": 0,
        "quality": {"stable": True},
    }]
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "pan",
        "row": 0,
        "direction": -1,
        "branch": -1,
        "bands": {"0": {"complete": False, "edges": {}}},
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {"capture_id": "anchor", "path": str(anchor_path), "row": 0},
        "seek": {
            "axis": "pan",
            "direction": -1,
            "row": 0,
            "origin_path": str(anchor_path),
            "progress": True,
            "steps": 0,
            "stationary_count": 0,
            "origin_excursion": False,
        },
    }
    scanner.checkpoint["continuous_cursor"] = cursor
    scanner.last_frame = anchor_frame
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    stopped_frames = []

    async def stationary_pulse(*_args, expected_frame=None, **_options):
        if stopped_frames:
            assert expected_frame is stopped_frames[-1]
        frame = await camera.frame()
        stopped_frames.append(frame)
        return {
            "frame": frame,
            "pose": camera._position(),
            "match": {"verified": True, "overlap": 1.0, "displacement": 0.0},
            "evidence": {},
            "stable": False,
            "stationary": True,
        }

    scanner._pulse = stationary_pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_images: {
            "verified": True,
            "overlap": 0.99,
            "displacement": 2.0,
            "shift_x": 2.0,
            "shift_y": 0.0,
        },
    )
    outcome, _duration = asyncio.run(
        scanner._seek("pan", -1, row=0, duration=0.6)
    )
    assert outcome == "limit"
    assert len(stopped_frames) == 2


@pytest.mark.parametrize("anchor_displacement,command_displacement", [(4.09, .03), (25, .03), (4.09, 2)])
def test_stationary_vertical_probe_distinguishes_anchor_residual_from_local_motion(
    tmp_path, monkeypatch, anchor_displacement, command_displacement
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.last_frame = asyncio.run(camera.frame())
    path = tmp_path / "band.jpg"
    cv2.imwrite(str(path), scanner.last_frame["image"])
    scanner.captures.append({"id": "anchor", "path": str(path), "row_index": 1, "quality": {"stable": True}})
    scanner.physical_state = "stopped"
    cursor = {"version": scan.CONTINUOUS_CURSOR_VERSION, "branch": 1, "row": 1,
              "bands": {"1": {}}, "anchor": {"capture_id": "anchor", "path": str(path), "row": 1}}
    scanner.checkpoint["continuous_cursor"] = cursor
    async def pulse(*args):
        return {"stationary": True, "stable": False, "frame": await camera.frame(),
                "match": {"verified": True, "displacement": command_displacement, "overlap": 1},
                "pose": camera._position(), "evidence": {}}
    scanner._pulse = pulse
    monkeypatch.setattr(scan, "_match", lambda *_: {"verified": True, "overlap": .95, "displacement": anchor_displacement})
    if anchor_displacement <= 15 and command_displacement <= .5:
        assert asyncio.run(scanner._next_band(cursor)) is False
        assert scanner.boundaries["tilt_max"]["confirmed"]
    else:
        with pytest.raises(PanoramaCaptureError, match="vertical_connection_unverified"):
            asyncio.run(scanner._next_band(cursor))
        assert "tilt_max" not in scanner.boundaries
    assert len(scanner.captures) == 1


@pytest.mark.parametrize("finished", [[-1], [-1, 1]])
def test_finished_vertical_branches_do_not_leave_a_resumable_step(tmp_path, monkeypatch, finished):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.captures = [{"id": "a"}, {"id": "b"}]
    scanner.checkpoint.update(mode="continuous", continuous_cursor={
        "version": scan.CONTINUOUS_CURSOR_VERSION, "stage": "step", "finished_branches": finished,
    })
    monkeypatch.setattr(scanner, "_reference_destination", lambda _: {"kind": "preset"})
    assert scanner._has_pending_capture() is (len(finished) == 1)


def test_anchor_comparison_expiring_during_work_observes_new_frame_once(tmp_path, monkeypatch):
    scanner, camera, cursor, _, _ = _recovery_scan(tmp_path, monkeypatch)
    camera.now += .93
    original = scan._match
    calls = []
    def matching(first, second):
        result = original(first, second)
        camera.now += .09
        calls.append(camera.now)
        return result
    monkeypatch.setattr(scan, "_match", matching)
    assert asyncio.run(scanner._current_anchor(cursor, row=0))
    diagnostic = scanner.checkpoint["diagnostics"]["attempts"][-1]
    assert diagnostic["expired_comparison_age_seconds"] > 1
    assert diagnostic["frame_age_after_matching_seconds"] <= 1
    assert len(calls) >= 2
    assert not {"velocity", "absolute", "return"}.intersection(_event_names(camera))


def test_fine_pulse_never_uses_unqualified_smaller_velocity(tmp_path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True},
    }
    with pytest.raises(PanoramaCaptureError, match='visual_control_resolution_unverified'):
        asyncio.run(scanner._pulse('pan', -1, .012))
    assert not camera.events


def test_fine_pulse_can_explicitly_qualify_a_lower_bounded_velocity(tmp_path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True},
        "spaces": {
            "continuous": [
                {
                    "uri": "velocity-space",
                    "normalized": True,
                    "x": {"min": -1.0, "max": 1.0},
                    "y": {"min": -1.0, "max": 1.0},
                }
            ]
        },
        "defaults": {"continuous": "velocity-space"},
    }
    movement_options = {}

    async def move(command, **options):
        movement_options.update(options)
        await command()
        return {"frame": await camera.frame()}

    scanner._move = move
    asyncio.run(scanner._pulse("pan", -1, 0.05, speed=0.025))
    event = next(payload for name, payload in camera.events if name == "velocity")
    assert event == {"pan": -0.025, "tilt": 0.0, "timeout_s": 0.05}
    assert movement_options["requested_velocity"] == {"pan": -0.025, "tilt": 0.0}


def test_fine_pulse_rejects_a_velocity_that_the_advertised_space_would_clamp(tmp_path):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {
        "velocity_supported": True,
        "axes": {"pan": True},
        "spaces": {
            "continuous": [
                {
                    "uri": "velocity-space",
                    "normalized": True,
                    "x": {"min": 0.05, "max": 1.0},
                    "y": {"min": -1.0, "max": 1.0},
                }
            ]
        },
        "defaults": {"continuous": "velocity-space"},
    }
    with pytest.raises(PanoramaCaptureError, match="visual_control_resolution_unverified"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, speed=0.025))
    assert not camera.events


@pytest.mark.parametrize(
    "invalidity",
    ["duplicate", "not_normalized", "missing_bounds", "default_mismatch"],
)
def test_lower_velocity_requires_one_exact_normalized_default_space(tmp_path, invalidity):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    selected = {
        "uri": "velocity-space",
        "normalized": True,
        "x": {"min": -1.0, "max": 1.0},
        "y": {"min": -1.0, "max": 1.0},
    }
    spaces = [selected]
    default = "velocity-space"
    if invalidity == "duplicate":
        spaces.append(copy.deepcopy(selected))
    elif invalidity == "not_normalized":
        selected["normalized"] = False
    elif invalidity == "missing_bounds":
        selected.pop("x")
    else:
        default = "another-space"
    scanner.capabilities = {
        "velocity_supported": True,
        "axes": {"pan": True},
        "spaces": {"continuous": spaces},
        "defaults": {"continuous": default},
    }

    with pytest.raises(PanoramaCaptureError, match="visual_control_resolution_unverified"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, speed=0.025))

    assert not camera.events


@pytest.mark.parametrize("axis_state", [None, False, "missing"])
def test_pulse_never_moves_an_axis_without_verified_capability(tmp_path, axis_state):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    axes = {} if axis_state == "missing" else {"pan": axis_state}
    scanner.capabilities = {"velocity_supported": True, "axes": axes}
    with pytest.raises(PanoramaCaptureError, match="axis_movement_unavailable"):
        asyncio.run(scanner._pulse("pan", 1, 0.05))
    assert not camera.events


@pytest.mark.parametrize("direction", [-2, 0, 2, True])
def test_pulse_rejects_a_direction_outside_the_signed_unit_domain(tmp_path, direction):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    with pytest.raises(PanoramaCaptureError, match="invalid_velocity"):
        asyncio.run(scanner._pulse("pan", direction, 0.05))
    assert not camera.events


@pytest.mark.parametrize("duration", [-0.1, 0.0, 2.01, float("nan")])
def test_relative_pulse_rejects_invalid_duration_without_moving(tmp_path, duration):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {
        "relative_supported": True,
        "velocity_supported": False,
        "axes": {"pan": True},
    }
    with pytest.raises(PanoramaCaptureError, match="invalid_movement_duration"):
        asyncio.run(scanner._pulse("pan", 1, duration))
    assert not camera.events


def test_correction_precondition_is_checked_before_issuing_velocity(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    scanner.physical_state = "stopped"
    expected = asyncio.run(camera.frame())
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 2.0},
    )
    with pytest.raises(PanoramaCaptureError, match="correction_precondition_changed"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, expected_frame=expected))
    assert "velocity" not in _event_names(camera)
    assert scanner.physical_state == "unknown"
    assert len(scanner.checkpoint["rejected_motion_pairs"]) == 1


def test_changed_precondition_reobserves_once_before_dispatch(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    expected = asyncio.run(camera.frame())
    comparisons = iter(
        [
            {"verified": True, "overlap": 1.0, "displacement": 1.45},
            {"verified": True, "overlap": 1.0, "displacement": 0.2},
        ]
    )
    monkeypatch.setattr(scan, "_match", lambda *_: next(comparisons))

    async def stopped_window(*, timeout=3.0):
        assert timeout <= 3.0
        return await camera.frame()

    issued = 0

    async def rejected_command():
        nonlocal issued
        issued += 1
        raise PanoramaCaptureError("lease_lost")

    scanner._reference_window = stopped_window
    with pytest.raises(PanoramaCaptureError, match="lease_lost"):
        asyncio.run(
            scanner._move(
                rejected_command,
                duration=0.05,
                expected_frame=expected,
                requested_velocity={"pan": 0.1, "tilt": 0.0},
            )
        )

    assert issued == 1
    assert len(scanner.checkpoint["rejected_motion_pairs"]) == 1
    attempt = scanner.checkpoint["diagnostics"]["attempts"][-1]
    reobservation = attempt["precondition_reobservation"]
    assert reobservation["attempted"] is True
    assert reobservation["verified"] is True
    assert reobservation["reason"] == "same_stream_stopped_window"
    assert reobservation["initial"]["displacement"] == pytest.approx(1.45)
    assert attempt["correction_precondition"]["displacement"] == pytest.approx(0.2)


def test_failed_precondition_reobservation_cannot_be_replayed_after_restart(
    tmp_path, monkeypatch
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, expected = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.physical_state = "stopped"
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 1.45},
    )

    async def stopped_window(*, timeout=3.0):
        return await camera.frame()

    async def forbidden_command():
        raise AssertionError("the rejected movement must never be issued")

    scanner._reference_window = stopped_window
    cursor = scanner.checkpoint["continuous_cursor"]
    with pytest.raises(PanoramaCaptureError, match="correction_precondition_changed"):
        asyncio.run(
            scanner._move(
                forbidden_command,
                duration=0.12,
                expected_frame=expected,
                requested_velocity={"pan": 0.1, "tilt": 0.0},
                movement_intent={
                    "type": "seek_pulse",
                    "axis": "pan",
                    "direction": cursor["direction"],
                    "duration": 0.12,
                },
            )
        )

    saved = json.loads((tmp_path / "scan-manifest.json").read_text())
    marker = saved["continuous_cursor"]["precondition_reobservation"]
    assert marker["state"] == "consumed"
    assert re.fullmatch(r"[0-9a-f]{32}", marker["movement_id"])
    assert SourcePanoramaService._resume_unavailable_code(
        {"_checkpoint": saved}
    ) == "relocalization_required"


@pytest.mark.parametrize("field", ["overlap", "displacement"])
def test_non_finite_correction_precondition_never_issues_a_motor_command(
    tmp_path, monkeypatch, field
):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    expected = asyncio.run(camera.frame())
    comparison = {"verified": True, "overlap": 1.0, "displacement": 0.0}
    comparison[field] = float("nan")
    monkeypatch.setattr(scan, "_match", lambda *_: comparison)

    with pytest.raises(PanoramaCaptureError, match="correction_precondition_changed"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, expected_frame=expected))

    assert "velocity" not in _event_names(camera)


def test_cancellation_during_correction_match_never_issues_a_motor_command(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    cancelled = False
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: cancelled, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    expected = asyncio.run(camera.frame())

    def match(*_):
        nonlocal cancelled
        cancelled = True
        return {"verified": True, "overlap": 1.0, "displacement": 0.0}

    monkeypatch.setattr(scan, "_match", match)
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._pulse("pan", 1, 0.05, expected_frame=expected))

    assert "velocity" not in _event_names(camera)


def test_stale_correction_baseline_never_issues_a_motor_command(tmp_path, monkeypatch):
    class StaleFrameCamera(SimulatedCamera):
        async def frame(self, **options):
            frame = await super().frame(**options)
            frame["received_monotonic"] -= 2.0
            return frame

    camera = StaleFrameCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    expected = asyncio.run(camera.frame())
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )

    with pytest.raises(PanoramaCaptureError, match="correction_precondition_changed"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, expected_frame=expected))

    assert "velocity" not in _event_names(camera)


def test_correction_precondition_is_rechecked_after_slow_observation(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {"velocity_supported": True, "axes": {"pan": True}}
    expected = asyncio.run(camera.frame())
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {"verified": True, "overlap": 1.0, "displacement": 0.0},
    )
    original_observation = scanner._observation

    def slow_observation(*args):
        result = original_observation(*args)
        camera.now += 2.0
        return result

    scanner._observation = slow_observation
    with pytest.raises(PanoramaCaptureError, match="correction_precondition_changed"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, expected_frame=expected))

    assert "velocity" not in _event_names(camera)


@pytest.mark.parametrize("speed", [0, -0.01, 0.1001, float("nan")])
def test_fine_pulse_rejects_unbounded_velocity_without_moving(tmp_path, speed):
    camera = SimulatedCamera()
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, {})
    scanner.capabilities = {
        "continuous_supported": True,
        "velocity_supported": True,
        "axes": {"pan": True},
    }
    with pytest.raises(PanoramaCaptureError, match="invalid_velocity"):
        asyncio.run(scanner._pulse("pan", 1, 0.05, speed=speed))
    assert not camera.events


def test_connection_halfstep_step_and_pending_intent_are_persisted_atomically(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, frame = _independent_budget_checkpoint(tmp_path, camera)
    cursor = checkpoint["continuous_cursor"]
    cursor["seek"].update(steps=1, duration=0.6, subdivided_steps=[1])
    cursor["connection_recovery"] = {
        "version": 1,
        "state": "anchor_confirmed",
        "attempt": 1,
        "identity": {
            "axis": "pan",
            "direction": 1,
            "row": 0,
            "stage": "pan",
            "branch": -1,
            "step": 1,
            "anchor": dict(cursor["anchor"]),
        },
        "target": {"pan": 0.0, "tilt": 0.0},
        "failed_duration": 1.2,
        "retry_duration": 0.6,
        "retry_step": 2,
        "failure_code": "correspondences_not_distributed",
    }
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        axes={"pan": True, "tilt": True},
    )
    scanner.last_frame = frame
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    snapshots = []

    async def interrupted_command():
        durable = json.loads((tmp_path / "scan-manifest.json").read_text())[
            "continuous_cursor"
        ]
        snapshots.append(
            (
                durable["seek"]["steps"],
                durable["connection_recovery"]["state"],
                durable["transition"]["state"],
                durable["transition"]["intent"]["step"],
                durable["transition"].get("movement_id"),
            )
        )
        raise scan._Stopped

    with pytest.raises(scan._Stopped):
        asyncio.run(
            scanner._move(
                interrupted_command,
                duration=0.6,
                allow_stationary=True,
                movement_intent={
                    "type": "connection_halfstep",
                    "axis": "pan",
                    "direction": 1,
                    "duration": 0.6,
                    "step": 2,
                },
                connection_recovery_phase="retry",
            )
        )
    assert snapshots[0][:4] == (2, "retry_pending", "pending", 2)
    assert scan._movement_attempt_id(snapshots[0][4]) == snapshots[0][4]
    assert scanner.checkpoint["diagnostics"]["attempts"][-1]["movement_id"] == (
        snapshots[0][4]
    )
    with pytest.raises(ValueError, match="connection recovery"):
        scan._pending_seek_intent(scanner.checkpoint["continuous_cursor"])


def test_vertical_connection_recovery_uses_the_actual_anchor_row(tmp_path, monkeypatch):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        axes={"pan": True, "tilt": True},
    )
    camera.tilt = -0.05
    anchor_frame = asyncio.run(camera.frame())
    anchor_path = tmp_path / "vertical-current-anchor.jpg"
    cv2.imwrite(str(anchor_path), anchor_frame["image"])
    anchor_capture = {
        "id": "vertical-anchor",
        "path": str(anchor_path),
        "row_index": -1,
        "quality": {"stable": True},
        "pose": camera._position(),
    }
    scanner.captures = [anchor_capture]
    cursor = {
        "version": scan.CONTINUOUS_CURSOR_VERSION,
        "stage": "step",
        "row": 0,
        "direction": 1,
        "branch": -1,
        "bands": {"0": {"complete": True}, "-1": {"complete": False}},
        "finished_branches": [],
        "recovery_attempts": {},
        "anchor": {"capture_id": "vertical-anchor", "path": str(anchor_path), "row": -1},
        "vertical_seek": {
            "row": 0,
            "branch": -1,
            "anchor": {"capture_id": "origin", "path": str(anchor_path), "row": 0},
            "steps": 1,
            "progress": True,
            "stationary": 0,
            "duration": 1.2,
        },
    }
    scanner.checkpoint.update(
        mode="continuous",
        active_seconds=0.0,
        continuous_cursor=cursor,
        captures=scanner.captures,
    )
    camera.tilt = -0.09
    failed_frame = asyncio.run(camera.frame())
    camera.tilt = -0.07
    retry_frame = asyncio.run(camera.frame())
    camera.tilt = -0.05
    scanner.last_frame = failed_frame
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    anchor_rows = []
    targets = []

    async def confirm_stop():
        scanner.physical_state = "stopped"

    async def absolute(target, **_options):
        targets.append(copy.deepcopy(target))
        recovery = cursor["connection_recovery"]
        recovery["state"] = "return_pending"
        cursor["transition"] = {
            "state": "pending",
            "intent": {"type": "connection_anchor_return"},
        }
        scanner.last_frame = anchor_frame
        scanner.physical_state = "stopped"
        return {"frame": anchor_frame, "stable": True}

    async def current_anchor(_cursor, *, row, **_options):
        anchor_rows.append(row)
        return row == -1

    async def pulse(axis, direction, duration, **options):
        recovery = cursor["connection_recovery"]
        assert (axis, direction, duration) == ("tilt", -1, pytest.approx(0.6))
        assert options["intent_step"] == 2
        cursor["vertical_seek"]["steps"] = 2
        recovery["state"] = "retry_pending"
        cursor["transition"] = {
            "state": "pending",
            "intent": {
                "type": "connection_halfstep",
                "axis": "tilt",
                "direction": -1,
                "row": 0,
                "step": 2,
                "duration": 0.6,
                "anchor": dict(cursor["anchor"]),
            },
        }
        return {
            "frame": retry_frame,
            "pose": {**camera._position(), "tilt": -0.07},
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": {"verified": True, "overlap": 0.8, "displacement": 35},
        }

    scanner._confirm_stop = confirm_stop
    scanner._absolute = absolute
    scanner._current_anchor = current_anchor
    scanner._pulse = pulse
    monkeypatch.setattr(
        scan,
        "_match",
        lambda *_: {
            "verified": True,
            "overlap": 0.8,
            "displacement": 35,
            "shift_x": 0,
            "shift_y": 35,
        },
    )
    failed_result = {
        "frame": failed_frame,
        "pose": {**camera._position(), "tilt": -0.09},
        "stable": True,
        "stationary": False,
        "evidence": {"stable": True},
        "match": {"verified": True},
    }
    result, connection, duration = asyncio.run(
        scanner._subdivide_failed_connection(
            cursor,
            cursor["vertical_seek"],
            axis="tilt",
            direction=-1,
            row=0,
            failed_duration=1.2,
            failed_result=failed_result,
            failed_connection={
                "verified": False,
                "code": "correspondences_not_distributed",
            },
            step_limit=8,
            error_code="vertical_connection_unverified",
        )
    )
    assert duration == pytest.approx(0.6) and connection["verified"]
    assert targets == [{"pan": 0.0, "tilt": -0.05}]
    assert anchor_rows == [-1]
    asyncio.run(scanner._accept(result, row=-1, role="row_connection"))
    assert cursor["connection_recovery"]["state"] == "complete"


def test_verified_anchor_after_observation_retry_reenables_later_connection_subdivision(
    tmp_path, monkeypatch
):
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    checkpoint, anchor_frame = _independent_budget_checkpoint(tmp_path, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, checkpoint)
    scanner.capabilities = copy.deepcopy(camera.capabilities)
    scanner.capabilities.update(
        continuous_supported=True,
        velocity_supported=True,
        axes={"pan": True, "tilt": True},
    )
    scanner.last_frame = anchor_frame
    scanner.last_pose = camera._position()
    scanner.physical_state = "stopped"
    cursor = scanner.checkpoint["continuous_cursor"]
    cursor["seek"]["duration"] = 1.2
    pulse_calls = []
    connection_calls = []
    subdivision_calls = []

    async def pulse(_axis, _direction, duration, **_options):
        pulse_calls.append(duration)
        if len(pulse_calls) == 1:
            raise PanoramaCaptureError("motion_not_observed")
        camera.pan = 0.05 * len(pulse_calls)
        frame = await camera.frame()
        scanner.last_frame = frame
        scanner.physical_state = "stopped"
        return {
            "frame": frame,
            "pose": camera._position(),
            "stable": True,
            "stationary": False,
            "evidence": {"stable": True},
            "match": {
                "verified": True,
                "overlap": 0.8,
                "displacement": 30,
                "shift_x": 30,
                "shift_y": 0,
            },
        }

    def matching(first, second):
        if camera.pan == 0:
            return {
                "verified": True,
                "overlap": 1.0,
                "displacement": 0.0,
                "shift_x": 0.0,
                "shift_y": 0.0,
            }
        connection_calls.append(True)
        if len(connection_calls) == 1:
            return {
                "verified": True,
                "overlap": 0.8,
                "displacement": 30,
                "shift_x": 30,
                "shift_y": 0,
            }
        return {
            "verified": False,
            "code": "correspondences_not_distributed",
        }

    async def subdivision(_cursor, seek_state, **_options):
        subdivision_calls.append(seek_state["origin_uncertain_since_anchor"])
        raise scan._Stopped

    async def confirm_stop():
        scanner.last_frame = await camera.frame()
        scanner.physical_state = "stopped"

    scanner._pulse = pulse
    scanner._confirm_stop = confirm_stop
    scanner._subdivide_failed_connection = subdivision
    monkeypatch.setattr(scan, "_match", matching)
    with pytest.raises(scan._Stopped):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert pulse_calls == [1.2, 1.2, 1.2]
    assert subdivision_calls == [False]
    assert len(scanner.captures) == 2


def test_failed_connection_recovery_is_archived_before_navigation_changes(tmp_path, monkeypatch):
    scanner, cursor, _, _ = _connection_subdivision_fixture(
        tmp_path, monkeypatch, anchor_verified=False
    )
    with pytest.raises(PanoramaCaptureError, match="coverage_connection_unverified"):
        asyncio.run(scanner._seek("pan", 1, row=0, duration=1.2))
    assert cursor["connection_recovery"]["state"] == "failed"

    async def confirm_stop():
        scanner.physical_state = "stopped"

    scanner._confirm_stop = confirm_stop
    recovered = asyncio.run(
        scanner._recover(
            cursor,
            PanoramaCaptureError("coverage_connection_unverified"),
            axis="pan",
        )
    )
    assert recovered is False
    assert "connection_recovery" not in cursor
    assert cursor["connection_recovery_history"][-1]["state"] == "failed"
