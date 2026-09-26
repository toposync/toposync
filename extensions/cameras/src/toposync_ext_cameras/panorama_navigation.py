"""Bounded visual PTZ navigation using the existing scanner and fenced camera.

No durations are treated as angles. Axis response is measured locally after
Stop and new video. A missing observation never permits a blind next pulse.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from copy import deepcopy
from typing import Any, Callable

import numpy as np

from .panorama_capture import PanoramaCaptureError
from .panorama_scan import (
    CORRECTION_PRECONDITION_PIXELS,
    DEFAULT_CONTINUOUS_PULSE_SPEED,
    FULL_PULSE_VISUAL_SUPPORT_INLIERS,
    MAXIMUM_RETURN_CORRECTIONS,
    MINIMUM_CONTINUOUS_PULSE_SECONDS,
    _Scan,
    _append_return_correction_command,
    _match,
    _next_continuous_seek_duration,
    _return_correction_commands,
)
from .processing.panorama_mapping import _rotation_basis, ray_to_image_pixel
from .processing.panorama_localization import (
    ANALYSIS_WIDTH,
    MAXIMUM_ERROR_PIXELS as MAXIMUM_LOCALIZATION_ERROR_PIXELS,
)
from .processing.panorama_localization_replay import LocalizationReplay

MAXIMUM_NAVIGATION_COMMANDS = 64
MAXIMUM_FINE_CORRECTIONS = MAXIMUM_RETURN_CORRECTIONS
MAXIMUM_LIVE_FINE_CORRECTIONS = 6
MAXIMUM_LIVE_CENTER_ERROR_PIXELS = 12.0
MAXIMUM_NAVIGATION_PULSE_SECONDS = 0.6
DEFAULT_NAVIGATION_PROBE_SECONDS = 0.12
FINE_CONTINUOUS_SPEEDS = (0.025, 0.05, DEFAULT_CONTINUOUS_PULSE_SPEED)
MINIMUM_QUALIFIED_RESPONSE_PIXELS = 2.0
MAXIMUM_PROBE_ERROR_GROWTH_RATIO = 1.2
MAXIMUM_RESPONSE_VIEW_CHANGE_DEGREES = 15.0


def reference_probe_observations(model: dict, photographs: list, commands: list, attempts: list) -> list:
    """Recover command direction, never motor gain, from causal reference pairs."""
    if not isinstance(commands, list) or not isinstance(attempts, list):
        return []
    rotations = {item["id"]: item["rotation_matrix"] for item in model.get("captures", [])}
    indexed = {item.get("id"): (index, item) for index, item in enumerate(commands) if isinstance(item, dict)}
    recorded = {item["command_receipt"].get("command_id"): item for item in attempts
                if isinstance(item, dict) and isinstance(item.get("command_receipt"), dict)}
    observations = []
    for before, after in zip(photographs, photographs[1:]):
        entry = indexed.get(after.get("region_command_id"))
        previous = indexed.get(before.get("region_command_id"))
        if entry is None or entry[0] != (previous[0] + 1 if previous else 0):
            continue
        command = entry[1]
        attempt = recorded.get(after.get("control_command_id"), {})
        receipt = attempt.get("command_receipt", {})
        precondition = attempt.get("correction_precondition") or {}
        baseline = command.get("baseline") or {}
        velocity = attempt.get("requested_velocity", {}) or {}
        try:
            axes = [axis for axis in ("pan", "tilt") if velocity.get(axis, 0) != 0]
            if (len(axes) != 1 or velocity.get("zoom", 0) != 0
                    or abs(velocity[axes[0]]) != DEFAULT_CONTINUOUS_PULSE_SPEED
                    or command.get("state") != "observed" or command.get("returning") is not False
                    or command.get("outcome") != "capture_stable" or attempt.get("outcome") != "capture_stable"
                    or receipt.get("command_kind") != "continuous_move"
                    or receipt.get("accepted") is not True or receipt.get("stale_after_execution") is not False
                    or precondition.get("verified") is not True or not .85 <= precondition.get("overlap", 0) <= 1
                    or not 0 <= precondition.get("displacement", float("inf")) <= CORRECTION_PRECONDITION_PIXELS
                    or not before["capture_instance"] == baseline["capture_instance"] == after["capture_instance"]
                    or not before["generation"] == baseline["generation"] == after["generation"]
                    or not before["sequence"] <= baseline["sequence"] < after["sequence"]):
                continue
            first, last = _rotation_basis(rotations[before["id"]]), _rotation_basis(rotations[after["id"]])
            angle = np.arccos(np.clip((np.trace(first.T @ last) - 1) / 2, -1, 1))
            if not np.radians(.25) <= angle <= np.radians(MAXIMUM_RESPONSE_VIEW_CHANGE_DEGREES):
                continue
            observations.append({"axis": axes[0], "direction": int(np.sign(velocity[axes[0]])),
                "command_id": receipt["command_id"], "before_rotation": first.tolist(),
                "after_rotation": last.tolist()})
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return observations


def reference_probe_direction(axis: str, target: np.ndarray, rotation: np.ndarray, observations: list) -> int | None:
    """Two nearby observed commands may choose a probe sign, not its duration."""
    rotation = _rotation_basis(rotation)
    local = rotation.T @ target
    if local.shape != (3,) or not np.isfinite(local).all() or local[2] <= .1:
        return None
    error = np.arctan2(local[:2], local[2])
    votes = {}
    for item in observations:
        if item.get("axis") != axis:
            continue
        first, last = np.asarray(item["before_rotation"]), np.asarray(item["after_rotation"])
        if any(np.arccos(np.clip((np.trace(reference.T @ rotation) - 1) / 2, -1, 1)) > np.radians(MAXIMUM_RESPONSE_VIEW_CHANGE_DEGREES)
               for reference in (first, last)):
            continue
        change = first.T @ last
        vector = np.array([change[2, 1] - change[1, 2], change[0, 2] - change[2, 0], change[1, 0] - change[0, 1]])
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or norm < 1e-6:
            continue
        derivative = -np.cross(vector / norm * item["direction"], local)
        angular = (local[2] * derivative[:2] - local[:2] * derivative[2]) / (local[:2] ** 2 + local[2] ** 2)
        dot = float(error @ angular)
        # An almost perpendicular response cannot reliably select a direction.
        if abs(dot) <= .2 * np.linalg.norm(error) * np.linalg.norm(angular) or abs(dot) < 1e-6:
            continue
        votes[item["command_id"]] = -1 if dot > 0 else 1
    return next(iter(votes.values())) if len(votes) >= 2 and len(set(votes.values())) == 1 else None


def _qualified_navigation_pulse_limit(amount: float, matching: dict[str, Any]) -> float:
    """Reuse the scanner's overlap adaptation only after a full coarse pulse."""
    if (
        abs(amount) < MAXIMUM_NAVIGATION_PULSE_SECONDS
        or matching.get("verified") is not True
        or matching.get("support_scope") != "distributed_scene"
        or matching.get("inliers", 0) < FULL_PULSE_VISUAL_SUPPORT_INLIERS
        or matching.get("overlap", 0) < .75
        or not isinstance(matching.get("source_features"), int)
        or not isinstance(matching.get("target_features"), int)
        or matching["source_features"] <= 0
    ):
        return MAXIMUM_NAVIGATION_PULSE_SECONDS
    size = matching.get("analysis_size", [])
    if len(size) != 2 or min(size) <= 0:
        return MAXIMUM_NAVIGATION_PULSE_SECONDS
    # Use total image displacement and the smaller dimension: diagonal motion
    # must not earn a larger pulse by hiding its cross-axis component.
    shift = matching.get("displacement", 0)
    if not np.isfinite(shift) or shift <= 2:
        return MAXIMUM_NAVIGATION_PULSE_SECONDS
    duration, reason = _next_continuous_seek_duration(
        abs(amount), image_extent=min(size), image_shift=shift, connection=matching,
    )
    return duration if reason is None else MAXIMUM_NAVIGATION_PULSE_SECONDS


def _visual_measurement(value: dict) -> tuple[float, float, float, float] | None:
    """Return finite overlap, displacement and shift values from a verified match."""
    if value.get("verified") is not True:
        return None
    try:
        measurements = tuple(
            float(value[name]) for name in ("overlap", "displacement", "shift_x", "shift_y")
        )
    except (KeyError, TypeError, ValueError):
        return None
    return measurements if np.isfinite(measurements).all() else None


def localized_axis_measurement(
    ray: Any, lens: dict[str, Any], located: dict[str, Any]
) -> dict[str, Any] | None:
    """Measure optical-axis error from a qualified fresh-frame localization.

    This fallback remains in image space: the target ray is projected with the
    panorama model and compared with the calibrated optical centre.  It never
    treats a reconstructed angle as a motor unit.  Held-out, distributed image
    correspondences must independently qualify the frame before it can confirm
    arrival.
    """
    try:
        analysis_width = float(located["analysis_width"])
        validation_p95 = float(located["validation_p95_pixels"])
        validation_matches = int(located["validation_matches"])
        inlier_fraction = float(located["inlier_fraction"])
        width = float(lens["width"])
        centre = np.asarray([lens["cx"], lens["cy"]], dtype=np.float64)
        pixel = ray_to_image_pixel(ray, lens, rotation_matrix=located["rotation_matrix"])
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        return None
    if (
        pixel is None
        or not np.isfinite(
            [analysis_width, validation_p95, inlier_fraction, width, *centre, *pixel]
        ).all()
        or analysis_width <= 0
        or width <= 0
        or not 0.55 <= inlier_fraction <= 1
        or validation_matches < 12
        or not 0 <= validation_p95 <= MAXIMUM_LOCALIZATION_ERROR_PIXELS
    ):
        return None
    scale = analysis_width / width
    error = (np.asarray(pixel, dtype=np.float64) - centre) * scale
    return {
        "method": "localized_optical_axis",
        "evidence": "held_out_feature_localization",
        "error_pixels": error.tolist(),
        "center_error_pixels": float(np.linalg.norm(error)),
        "analysis_width": analysis_width,
        "validation_matches": validation_matches,
        "validation_p95_pixels": validation_p95,
        "inlier_fraction": inlier_fraction,
        "reference_id": located.get("reference_id"),
    }


def _persisted_return_responses(
    trace: list[dict[str, Any]],
    measurement: tuple[float, float, float, float],
    *,
    return_epoch: str,
) -> tuple[dict[str, np.ndarray], dict[str, int], dict[str, float], str | None]:
    """Recover only a response model bound to the currently observed view.

    A planned or failed entry may already have reached the camera, so no later
    command is safe after a restart.  An observed model is reusable only when
    the current reference displacement agrees with its persisted terminal
    measurement.  The next command still carries the live frame precondition
    enforced by ``_Scan._move``.
    """
    responses: dict[str, np.ndarray] = {}
    directions: dict[str, int] = {}
    speeds: dict[str, float] = {}
    latest_model: dict[str, Any] | None = None
    for entry in trace:
        if not isinstance(entry, dict):
            return responses, directions, speeds, "return_correction_trace_invalid"
        state = entry.get("state")
        if state in {"planned", "failed"}:
            return responses, directions, speeds, "return_correction_outcome_uncertain"
        if entry.get("response_qualified") is False and (
            isinstance(entry.get("reason"), str)
            or entry.get("kind") in {"probe", "direction_probe", "correction"}
        ):
            return responses, directions, speeds, "return_correction_previously_rejected"
        vector_value = entry.get("response_vector")
        if entry.get("response_qualified") is True and vector_value is None:
            if "return_epoch" in entry:
                return responses, directions, speeds, "return_response_model_invalid"
            continue
        if entry.get("response_qualified") is not True:
            continue
        if entry.get("return_epoch") != return_epoch:
            continue
        axis = entry.get("axis")
        direction = entry.get("response_direction")
        speed = entry.get("response_velocity")
        try:
            vector = np.asarray(vector_value, dtype=np.float64)
            numeric_speed = float(speed)
        except (TypeError, ValueError):
            return responses, directions, speeds, "return_response_model_invalid"
        if (
            axis not in {"pan", "tilt"}
            or isinstance(direction, bool)
            or direction not in {-1, 1}
            or vector.shape != (2,)
            or not np.isfinite(vector).all()
            or not np.isfinite(numeric_speed)
            or numeric_speed <= 0
        ):
            return responses, directions, speeds, "return_response_model_invalid"
        responses[axis] = vector
        directions[axis] = direction
        speeds[axis] = numeric_speed
        latest_model = entry
    if latest_model is None:
        return responses, directions, speeds, None
    after_shift = latest_model.get("after_shift")
    try:
        saved_shift = np.asarray(after_shift, dtype=np.float64)
    except (TypeError, ValueError):
        return responses, directions, speeds, "return_response_model_invalid"
    current_shift = np.asarray(measurement[2:], dtype=np.float64)
    if (
        saved_shift.shape != (2,)
        or not np.isfinite(saved_shift).all()
        or np.linalg.norm(current_shift - saved_shift) > CORRECTION_PRECONDITION_PIXELS
    ):
        return responses, directions, speeds, "return_response_view_changed"
    return responses, directions, speeds, None


def _return_history_failure(
    trace: list[dict[str, Any]], commands: list[dict[str, Any]]
) -> str | None:
    """Validate the durable physical-command history before another pulse."""
    if any(not isinstance(entry, dict) for entry in trace + commands):
        return "return_correction_trace_invalid"
    visual_commands = [
        entry
        for entry in commands
        if entry.get("modality") in {"visual_continuous", "visual_relative"}
    ]
    if len(visual_commands) != len(trace):
        return "return_correction_ledger_mismatch"
    for saved, command in zip(trace, visual_commands, strict=True):
        if any(saved.get(key) != command.get(key) for key in ("axis", "state")):
            return "return_correction_ledger_mismatch"
        if saved.get("state") in {"planned", "failed", "rejected"}:
            return "return_correction_outcome_uncertain"
    return None


def _qualified_response_counts(trace: list[dict[str, Any]], *, return_epoch: str) -> dict[str, int]:
    """Count only observed response samples usable in the current return.

    One image can establish a direction, but it cannot establish a linear
    displacement model.  The count is deliberately derived from the durable
    correction trace so a restart cannot reset this physical confidence.
    """
    counts = {"pan": 0, "tilt": 0}
    for entry in trace:
        if (
            isinstance(entry, dict)
            and entry.get("return_epoch") == return_epoch
            and entry.get("response_qualified") is True
            and entry.get("axis") in counts
        ):
            counts[entry["axis"]] += 1
    return counts


def correction_step(
    jacobian: np.ndarray,
    error: np.ndarray,
    *,
    maximum: float = MAXIMUM_NAVIGATION_PULSE_SECONDS,
    minimum: float = 0.0,
) -> np.ndarray:
    """Damped local error correction, in the measured signed pulse units."""
    if (
        jacobian.ndim != 2
        or jacobian.shape[0] != 2
        or not 1 <= jacobian.shape[1] <= 2
        or not np.isfinite(jacobian).all()
        or not np.isfinite(error).all()
    ):
        raise PanoramaCaptureError("visual_response_unavailable")
    singular = np.linalg.svd(jacobian, compute_uv=False)
    if singular.min() < 0.01 or singular.max() / singular.min() > 50:
        raise PanoramaCaptureError("visual_response_unavailable")
    regularization = max(1e-6, float(singular.max()) ** 2 * 0.001)
    command = -0.8 * np.linalg.solve(
        jacobian.T @ jacobian + regularization * np.eye(jacobian.shape[1]), jacobian.T @ error
    )
    command = np.clip(command, -maximum, maximum)
    for index, amount in enumerate(command):
        if 0 < abs(amount) < minimum:
            candidate = float(np.copysign(minimum, amount))
            # Quantize only if the observed local response predicts improvement.
            # This never changes velocity or pretends the motor is continuous.
            command[index] = candidate if np.linalg.norm(error + jacobian[:, index] * candidate) < np.linalg.norm(error) else 0
    return command


def _continuous_pulse_plan(
    response: np.ndarray,
    error: np.ndarray,
    requested_amount: float,
) -> tuple[float, float, float] | None:
    """Choose a legal pulse and its equivalent response unit.

    Response is measured in pixels per second at the default continuous
    velocity. ONVIF defines continuous displacement as velocity multiplied by
    time, up to acceleration and positional inaccuracies. For a sub-floor
    correction, use one of a few bounded velocity levels for the legal minimum
    duration. The resulting image is still required to qualify the device's
    actual response before another correction can use it.
    """
    if (
        response.shape != (2,)
        or error.shape != (2,)
        or not np.isfinite(response).all()
        or not np.isfinite(error).all()
        or not np.isfinite(requested_amount)
        or abs(requested_amount) < 0.001
    ):
        return None
    direction = 1.0 if requested_amount > 0 else -1.0
    if abs(requested_amount) >= MINIMUM_CONTINUOUS_PULSE_SECONDS:
        duration = min(abs(requested_amount), 0.15)
        return duration, DEFAULT_CONTINUOUS_PULSE_SPEED, direction * duration

    current_error = float(np.linalg.norm(error))
    minimum_improvement = max(0.25, current_error * 0.05)
    candidates: list[tuple[float, float, float, float]] = []
    for speed in FINE_CONTINUOUS_SPEEDS:
        equivalent_amount = (
            direction
            * MINIMUM_CONTINUOUS_PULSE_SECONDS
            * speed
            / DEFAULT_CONTINUOUS_PULSE_SPEED
        )
        predicted_delta = response * equivalent_amount
        predicted_error = float(np.linalg.norm(error + predicted_delta))
        if (
            np.linalg.norm(predicted_delta) >= MINIMUM_QUALIFIED_RESPONSE_PIXELS
            and predicted_error <= current_error - minimum_improvement
        ):
            candidates.append(
                (
                    predicted_error,
                    speed,
                    MINIMUM_CONTINUOUS_PULSE_SECONDS,
                    equivalent_amount,
                )
            )
    if not candidates:
        return None
    _, speed, duration, equivalent_amount = min(candidates, key=lambda item: item[0])
    return duration, speed, equivalent_amount


def _fine_correction_has_measured_response(
    axis: str, amount: float, error: np.ndarray, target: np.ndarray, trace: list[dict],
) -> bool:
    """Keep a measured correction when two recent coarse pulses agree.

    An initial weak probe or a reversal must still use the conservative fine
    planner. Only consecutive, same-direction, overlap-qualified corrections
    to this target can avoid splitting an already bounded legal pulse.
    """
    if (len(trace) < 2 or not np.isfinite(amount) or error.shape != (2,)
            or abs(amount) < MINIMUM_CONTINUOUS_PULSE_SECONDS):
        return False
    recent = trace[-2:]
    for item in recent:
        if (item.get("state") != "observed" or item.get("axis") != axis
                or item.get("kind") != "correction"
                or item.get("speed") != DEFAULT_CONTINUOUS_PULSE_SPEED
                or item.get("next_pulse_limit", 0) <= MAXIMUM_NAVIGATION_PULSE_SECONDS
                or item.get("target_ray") != np.asarray(target).tolist()
                or np.sign(item.get("amount", 0)) != np.sign(amount)
                or abs(amount) > abs(item.get("amount", 0))):
            return False
    responses = [np.asarray(item.get("response", []), dtype=float) for item in recent]
    if any(response.shape != (2,) or not np.isfinite(response).all() for response in responses):
        return False
    scale = min(float(np.linalg.norm(response)) for response in responses)
    return bool(scale >= .01 and np.linalg.norm(responses[0] - responses[1]) <= scale * .15
                and all(np.linalg.norm(error + response * amount) < np.linalg.norm(error)
                        for response in responses))


def _outbound_inverse_probe(
    checkpoint: dict[str, Any],
    error: np.ndarray,
    *,
    return_epoch: str,
    missing_axes: list[str],
) -> tuple[str, float, dict[str, Any]] | None:
    """Choose one inverse probe from the causal outbound observation.

    This is deliberately only a direction probe: a camera may have backlash,
    so the inverse displacement is not promoted to a reusable response model
    until the resulting frame demonstrates improvement.
    """
    seed = checkpoint.get("return_outbound_seed")
    if not isinstance(seed, dict) or seed.get("return_epoch") != return_epoch:
        return None
    axis, direction = seed.get("axis"), seed.get("direction")
    try:
        duration = float(seed.get("duration_seconds"))
        overlap = float(seed.get("overlap"))
        displacement = float(seed.get("displacement"))
        shift = np.asarray(seed.get("observed_shift"), dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if (
        axis not in missing_axes
        or isinstance(direction, bool)
        or direction not in {-1, 1}
        or not 0.05 <= duration <= 1.0
        or not np.isfinite((overlap, displacement)).all()
        or overlap < 0.85
        or displacement < MINIMUM_QUALIFIED_RESPONSE_PIXELS
        or shift.shape != (2,)
        or not np.isfinite(shift).all()
    ):
        return None
    inverse_delta = -shift * (MINIMUM_CONTINUOUS_PULSE_SECONDS / duration)
    current_norm = float(np.linalg.norm(error))
    if float(np.linalg.norm(error + inverse_delta)) > current_norm - max(0.25, current_norm * 0.01):
        return None
    return axis, float(-direction * MINIMUM_CONTINUOUS_PULSE_SECONDS), {
        "axis": axis,
        "outbound_direction": direction,
        "duration_seconds": duration,
        "observed_shift": shift.tolist(),
    }


def reference_path(localizer: Any, start: str, target: str) -> list[str] | None:
    """A proposed route uses only previously verified photograph overlaps."""
    identifiers = {reference["id"] for reference in localizer.references}
    links = localizer.model.get("overlap_links", [])
    graph = {identifier: set() for identifier in identifiers}
    for link in links:
        if isinstance(link, list) and len(link) == 2 and all(item in graph for item in link):
            graph[link[0]].add(link[1])
            graph[link[1]].add(link[0])
    queue = deque([(start, [start])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
    return None


def select_direct_native_reference(localizer: Any, ray: Any, references: list[dict], departure: dict | None = None) -> dict | None:
    """Select a qualified destination already centred on the requested ray.

    Native recall does not require the departure orientation. Only destinations
    within the existing image-space arrival tolerance qualify here; arbitrary
    travel still requires a located departure. Recall never certifies arrival.
    The caller supplies binding-qualified references and rebinds before moving.
    """
    try:
        target = np.asarray(ray, dtype=float)
        if (target.shape != (3,) or not np.isfinite(target).all()
                or abs(np.linalg.norm(target) - 1) > 1e-6
                or localizer.target_reference(target) is None):
            return None
        width = float(localizer.lens["width"])
        centre_pixel = np.asarray([localizer.lens["cx"], localizer.lens["cy"]], dtype=float)
        if width <= 0 or not np.isfinite([width, *centre_pixel]).all():
            return None
        if departure is not None:
            pixel = ray_to_image_pixel(target, localizer.lens, rotation_matrix=departure["rotation_matrix"])
            if pixel is not None and np.linalg.norm(np.asarray(pixel) - centre_pixel) * min(width, ANALYSIS_WIDTH) / width <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS:
                # This hint only avoids a redundant recall. The ordinary
                # navigator must still measure arrival from a fresh stopped frame.
                return None
    except (KeyError, TypeError, ValueError):
        return None
    selected, best = None, MAXIMUM_LIVE_CENTER_ERROR_PIXELS
    for record in references:
        try:
            centre = np.asarray(record["ray"], dtype=float)
            rotation = _rotation_basis(record["rotation_matrix"])
            if (centre.shape != (3,) or not np.isfinite(centre).all()
                    or abs(np.linalg.norm(centre) - 1) > 1e-6
                    or np.linalg.norm(rotation[:, 2] - centre) > 1e-6
                    or record["destination"].get("preserve_zoom") is not True
                    or localizer.target_reference(centre) is None):
                continue
            pixel = ray_to_image_pixel(target, localizer.lens, rotation_matrix=record["rotation_matrix"])
            if pixel is None:
                continue
            error = float(np.linalg.norm(np.asarray(pixel) - centre_pixel) * min(width, ANALYSIS_WIDTH) / width)
            if np.isfinite(error) and error <= best:
                selected, best = record, error
        except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
            continue
    return selected


def select_native_reference(localizer: Any, located: dict, ray: Any, references: list[dict]) -> dict | None:
    """Choose useful native travel in panorama rays, never motor coordinates."""
    target = np.asarray(ray, dtype=float)
    current = _rotation_basis(located["rotation_matrix"])[:, 2]
    distance = float(np.arccos(np.clip(current @ target, -1, 1)))
    target_reference = localizer.target_reference(target)
    if target_reference is None:
        return None
    selected, best = None, distance
    for record in references:
        try:
            centre = np.asarray(record["ray"], dtype=float)
            rotation = _rotation_basis(record["rotation_matrix"])
            if (centre.shape != (3,) or not np.isfinite(centre).all()
                    or abs(np.linalg.norm(centre) - 1) > 1e-6
                    or np.linalg.norm(rotation[:, 2] - centre) > 1e-6):
                continue
            remaining = float(np.arccos(np.clip(centre @ target, -1, 1)))
            # The native point must remove most of the travel, with a useful
            # angular gain. Nearby clicks retain proportional visual control.
            if remaining >= min(best, distance * .5) or distance - remaining < np.radians(2):
                continue
            anchor = localizer.target_reference(centre)
            if anchor is None:
                continue
            if (ray_to_image_pixel(target, localizer.lens, rotation_matrix=record["rotation_matrix"]) is None
                    and reference_path(localizer, anchor["id"], target_reference["id"]) is None):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        selected, best = record, remaining
    return selected


class _ExpiredQualifiedFrame(PanoramaCaptureError):
    """Recognition established support, but cannot authorize movement yet."""

    def __init__(self):
        super().__init__("panorama_frame_not_recent")


class VisualNavigator:
    def __init__(self, scanner: _Scan, localizer: Any, *, maximum_commands: int = MAXIMUM_NAVIGATION_COMMANDS,
                 stable_frame_observer: Callable[[dict], None] | None = None):
        self.scanner, self.localizer = scanner, localizer
        self.stable_frame_observer = stable_frame_observer
        self.commands = 0
        self.maximum_commands = min(MAXIMUM_NAVIGATION_COMMANDS, maximum_commands)
        self.response: dict[str, np.ndarray] = {}
        self.response_rotations: dict[str, np.ndarray] = {}
        self.trace: list[dict[str, Any]] = []
        self.localization_replay = LocalizationReplay()
        self.probe_durations: dict[str, float] = {}
        self.last_pulse_stationary = False
        self.pulse_limits: dict[str, float] = {}
        self.last_pulse_match: dict[str, Any] = {}
        self.selected_native_reference: dict | None = None

    async def prepare_stopped_view(self) -> None:
        """Read position while qualifying the stopped image, before any pulse."""
        self.scanner._check()
        if self.scanner.physical_state != "stopped":
            raise PanoramaCaptureError("stop_unconfirmed")
        observation = asyncio.create_task(self.locate())
        try:
            self.scanner.last_pose = await self.scanner.camera.position()
            await observation
        finally:
            if not observation.done():
                observation.cancel()
            await asyncio.gather(observation, return_exceptions=True)

    def _record_timing(self, stage: str, started: float, **details: Any) -> None:
        records = self.scanner.checkpoint.setdefault("navigation_observation_timings", [])
        records.append({"stage": stage, "commands": self.commands,
                        "elapsed_seconds": time.monotonic() - started, **details})
        del records[:-96]

    async def locate(self) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return await self._locate_current()
            except PanoramaCaptureError as error:
                if attempt == 2 and isinstance(error, _ExpiredQualifiedFrame):
                    # A newly recovered anchor can qualify a fresh image cheaply.
                    # Allow one verification, never the expired pose itself and
                    # never a further retry if this fresh observation also fails.
                    self.scanner.last_frame = await self._renew_localization_frame(error)
                    return await self._locate_current()
                if attempt == 2 or error.code not in {
                    "panorama_visual_localization_failed", "panorama_visual_support_insufficient",
                    "panorama_frame_not_recent",
                }:
                    raise
                # Observe again while stopped; never reuse the last good pose or
                # issue a movement to recover localization. Ambiguity stays fatal.
                self.scanner.last_frame = await self._renew_localization_frame(error)
        raise PanoramaCaptureError("panorama_visual_localization_failed")

    async def _renew_localization_frame(self, error: PanoramaCaptureError) -> dict:
        # Recognition may outlive the image while the motor remains stopped.
        # Extend that observed endpoint through newer, matching decoder frames;
        # the scanner falls back to full stability qualification on any change.
        # Failed geometry is not an endpoint-continuity shortcut.
        if (isinstance(error, _ExpiredQualifiedFrame)
                and getattr(self.scanner, "physical_state", None) == "stopped"):
            return await self.scanner.refresh_stopped_frame()
        return await self.scanner._reference_window()

    async def _locate_current(self) -> dict[str, Any]:
        self.scanner._check()
        frame = self.scanner.last_frame
        if frame is None:
            raise PanoramaCaptureError("panorama_frame_unavailable")
        # A repeated lookup can use the original cached decision in milliseconds.
        # Do not open another stationary window before the actual one-second
        # fence. The same fence is checked after lookup and before every pulse;
        # slow recognition can still expire this frame and require observation.
        if not 0 <= time.monotonic() - frame.get("received_monotonic", 0) <= 1.0:
            frame = await self.scanner._reference_window()
            self.scanner.last_frame = frame
        started = time.monotonic()
        initial_age = started - frame.get("received_monotonic", 0)
        result = await asyncio.to_thread(
            getattr(self.localizer, "locate_diagnostic", self.localizer.locate),
            frame["image"], frame.get("capture_evidence", {})
        )
        # Keep compact raw observations in the existing bounded timing record.
        # They are not a calibrated pair: localization can outlive its frame,
        # and position may precede a refreshed image or lack a native axis.
        pose = getattr(self.scanner, "last_pose", None) or {}
        observation = {
            "rotation_matrix": deepcopy(result.get("rotation_matrix")),
            "capture_evidence": deepcopy(frame.get("capture_evidence", {})),
            "frame_received_monotonic": frame.get("received_monotonic"),
            "localized_monotonic": time.monotonic(),
            "physical_state": getattr(self.scanner, "physical_state", None),
            "position": deepcopy({key: pose[key] for key in (
                "pan", "tilt", "zoom", "native_pan", "native_tilt", "pan_tilt_space",
                "zoom_space", "move_status", "error", "observed_monotonic",
                "position_provenance", "native_position_unavailable",
            ) if key in pose}),
            "calibrated_pair": False,
        }
        self._record_timing("localization", started, initial_frame_age_seconds=initial_age,
                            final_frame_age_seconds=time.monotonic() - frame.get("received_monotonic", 0),
                            status=result.get("status"), reason=result.get("reason"),
                            photometry=result.get("diagnostics", {}).get("photometry"),
                            observation=observation)
        self.localization_replay.observe(frame, result, elapsed_seconds=time.monotonic() - started)
        result = {key: value for key, value in result.items() if key != "diagnostics"}
        if not 0 <= time.monotonic() - frame.get("received_monotonic", 0) <= 1.0:
            if result.get("status") == "localized":
                raise _ExpiredQualifiedFrame()
            raise PanoramaCaptureError("panorama_frame_not_recent")
        if result.get("status") != "localized":
            raise PanoramaCaptureError(result.get("reason", "panorama_visual_localization_failed"))
        rotation = np.asarray(result["rotation_matrix"])
        for axis, observed_rotation in list(self.response_rotations.items()):
            angle = np.arccos(
                np.clip((np.trace(observed_rotation.T @ rotation) - 1) / 2, -1, 1)
            )
            if angle > np.radians(MAXIMUM_RESPONSE_VIEW_CHANGE_DEGREES):
                self.response.pop(axis, None)
                self.response_rotations.pop(axis, None)
                self.pulse_limits.pop(axis, None)
                self.scanner.checkpoint.setdefault("navigation_response_invalidations", []).append({
                    "commands": self.commands,
                    "axis": axis,
                    "reason": "view_rotation_exceeded",
                    "angle_degrees": float(np.degrees(angle)),
                })
        return result

    def _error(self, ray: Any, located: dict) -> np.ndarray:
        local = _rotation_basis(located["rotation_matrix"]).T @ np.asarray(ray)
        if local[2] <= 0.1:
            raise PanoramaCaptureError("visual_route_unavailable")
        return np.arctan2(local[:2], local[2])

    async def approach_reference(self, destination: dict, ray: Any) -> dict:
        """Recall a prepared native point and verify it before visual refinement.

        The caller owns reference selection/provenance and the operation's
        existing finally/Stop path. A native receipt is never arrival evidence.
        """
        self.scanner._check()
        target = np.asarray(ray, dtype=float)
        if target.shape != (3,) or not np.isfinite(target).all() or not np.isclose(np.linalg.norm(target), 1):
            raise PanoramaCaptureError("visual_target_unreachable")
        if destination.get("preserve_zoom") is not True:
            raise PanoramaCaptureError("return_optical_policy_unverified")
        if self.commands >= self.maximum_commands:
            raise PanoramaCaptureError("visual_navigation_budget_exhausted")
        if self.scanner.physical_state != "stopped":
            raise PanoramaCaptureError("stop_unconfirmed")
        if (not self.scanner.last_frame
                or not 0 <= time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) <= 1):
            raise PanoramaCaptureError("panorama_frame_not_recent")
        started = time.monotonic()
        elapsed_seconds = {}
        self.commands += 1
        step = {"command": self.commands, "kind": "native_reference", "state": "pending",
                "target_ray": target.tolist(), "destination_kind": destination.get("kind"),
                "elapsed_seconds": elapsed_seconds}
        self.trace.append(step)
        self.scanner.checkpoint["navigation_commands"] = self.trace
        await self.scanner._persist()
        self.scanner._check()
        # A recall may reverse either axis. Old local pulse gains must not
        # authorize refinement after an uncertain or completed native move.
        self.response.clear()
        self.response_rotations.clear()
        self.pulse_limits.clear()
        transition_estimator = getattr(self.localizer, "calibrated_transition", None)
        result = await self.scanner._move(
            lambda: self.scanner.camera.return_to(destination), allow_stationary=True,
            target={axis: destination[axis] for axis in ("pan", "tilt")}
            if destination.get("kind") == "absolute" else None,
            **({"transition_estimator": transition_estimator} if callable(transition_estimator) else {}),
            **({"stable_frame_observer": self.stable_frame_observer} if self.stable_frame_observer else {}),
        )
        elapsed_seconds["movement_observed"] = time.monotonic() - started
        self.scanner.last_frame = result.get("observation_frame", result["frame"])
        self.scanner.last_pose = result.get("pose", {})
        if result.get("stable") is not True or self.scanner.physical_state != "stopped":
            raise PanoramaCaptureError("stop_unconfirmed")
        # Both reads qualify the observed stop. Geometry waits for both; an
        # optical failure must drain the frame task before releasing control.
        observation = asyncio.create_task(self.scanner.refresh_stopped_frame())
        try:
            await self.scanner.camera.verify_return_optical_state(destination)
            elapsed_seconds["optical_state_verified"] = time.monotonic() - started
            self.scanner._check()
            self.scanner.last_frame = await observation
        finally:
            if not observation.done():
                observation.cancel()
            await asyncio.gather(observation, return_exceptions=True)
        elapsed_seconds["stopped_frame_refreshed"] = time.monotonic() - started
        for attempt in range(2):
            self.scanner._check()
            located = await self.locate()
            try:
                measurement = await self._target_measurement(target, located)
                elapsed_seconds["target_measured"] = time.monotonic() - started
                break
            except _ExpiredQualifiedFrame as error:
                if attempt == 1:
                    raise
                # The motor is already stopped. Renew only the observation;
                # expiry never authorizes another recall or an old-frame pass.
                self.scanner.last_frame = await self._renew_localization_frame(error)
        error = measurement.get("center_error_pixels") if isinstance(measurement, dict) else None
        if (not isinstance(error, (int, float)) or isinstance(error, bool)
                or not 0 <= error <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS):
            raise PanoramaCaptureError("visual_native_reference_unconfirmed")
        step.update(state="observed", measurement=measurement)
        await self.scanner._persist()
        self.scanner._check()
        return located

    async def _pulse(self, axis: str, amount: float, *, speed: float = DEFAULT_CONTINUOUS_PULSE_SPEED) -> None:
        started = time.monotonic()
        if self.commands >= self.maximum_commands:
            raise PanoramaCaptureError("visual_navigation_budget_exhausted")
        self.scanner._check()
        if (
            not self.scanner.last_frame
            or not 0 <= time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) <= 1
        ):
            raise PanoramaCaptureError("panorama_frame_not_recent")
        self.commands += 1
        self.last_pulse_stationary = False
        self.last_pulse_match = {}
        self.trace.append({
            **self.scanner.checkpoint.get("navigation_step", {}),
            "axis": axis, "amount": amount, "speed": speed, "state": "pending",
        })
        self.scanner.checkpoint["navigation_commands"] = self.trace
        await self.scanner._persist()
        capabilities = self.scanner.capabilities
        if capabilities.get("velocity_supported") or capabilities.get("relative_supported"):
            options = {"speed": speed} if speed != DEFAULT_CONTINUOUS_PULSE_SPEED else {}
            if abs(amount) > MAXIMUM_NAVIGATION_PULSE_SECONDS:
                # Reuse the scanner's fresh baseline comparison before spending
                # an overlap-qualified larger pulse; external motion cancels it.
                options["expected_frame"] = self.scanner.last_frame
            result = await self.scanner._pulse(axis, 1 if amount >= 0 else -1, abs(amount), **options)
        elif capabilities.get("absolute_supported"):
            pose = await self.scanner.camera.position()
            limits = capabilities.get("limits", {}).get(axis)
            if not limits or any(
                not isinstance(pose.get(name), (float, int)) for name in ("pan", "tilt")
            ):
                raise PanoramaCaptureError("visual_response_unavailable")
            target = {name: pose[name] for name in ("pan", "tilt")}
            target[axis] = float(np.clip(target[axis] + 0.1 * amount, limits["min"], limits["max"]))
            if abs(target[axis] - pose[axis]) < 1e-8:
                raise PanoramaCaptureError("visual_target_unreachable")
            result = await self.scanner._absolute(target, allow_stationary=True)
        else:
            raise PanoramaCaptureError("visual_control_unavailable")
        self.scanner.last_frame = result.get("observation_frame", result["frame"])
        self.scanner.last_pose = result.get("pose", {})
        if (result.get("stable") is True and self.scanner.physical_state == "stopped"
                and speed == DEFAULT_CONTINUOUS_PULSE_SPEED):
            self.last_pulse_match = result.get("match", {})
        # Only the scanner's full causal no-effect window and observed Stop
        # can qualify a larger identification probe. A tiny fitted delta alone
        # cannot establish that the camera stayed still.
        self.last_pulse_stationary = bool(result.get("stationary") is True
            and self.scanner.physical_state == "stopped")
        self.trace[-1]["stationary_verified"] = self.last_pulse_stationary
        self.trace[-1]["state"] = "observed"
        self._record_timing("movement_cycle", started, axis=axis, amount=amount)
        await self.scanner._persist()

    async def _target_measurement(self, target: np.ndarray, located: dict[str, Any]) -> dict[str, Any] | None:
        started = time.monotonic()
        measured = await asyncio.to_thread(
            self.localizer.measure_target, self.scanner.last_frame["image"], located, target
        )
        result = measured or localized_axis_measurement(target, self.localizer.lens, located)
        self._record_timing("target_measurement", started,
                            method=result.get("method") if result else None)
        if not 0 <= time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) <= 1:
            raise _ExpiredQualifiedFrame()
        return result

    async def aim(
        self, ray: Any, *, native_destination: dict | None = None, native_ray: Any = None,
        native_references: list[dict] | None = None,
    ) -> dict[str, Any]:
        if (native_destination is None) != (native_ray is None):
            raise PanoramaCaptureError("visual_native_reference_unconfirmed")
        if native_destination is None:
            located = await self.locate()
        reference = self.localizer.target_reference(ray)
        if reference is None:
            raise PanoramaCaptureError("visual_target_unreachable")
        if native_destination is None and native_references:
            selected = select_native_reference(self.localizer, located, ray, native_references)
            if selected is not None:
                self.selected_native_reference = selected
                native_destination, native_ray = selected["destination"], selected["ray"]
                await self.scanner.camera.bind_reference_destination(native_destination)
        if native_destination is not None:
            # The prepared point only shortens travel. The final ray still
            # goes through the ordinary independent visual measurement below.
            located = await self.approach_reference(native_destination, native_ray)
        route = reference_path(self.localizer, located["reference_id"], reference["id"])
        visible = (
            ray_to_image_pixel(ray, self.localizer.lens, rotation_matrix=located["rotation_matrix"])
            is not None
        )
        if not visible and route is None:
            raise PanoramaCaptureError("visual_route_unavailable")
        waypoints = []
        if not visible:
            indexed = {item["id"]: item for item in self.localizer.references}
            # Only intermediate directions in the current optical support can
            # be followed. Each new view must re-localize before advancing.
            for identifier in route[1:]:
                waypoints.append(_rotation_basis(indexed[identifier]["rotation_matrix"])[:, 2])
        waypoints.append(np.asarray(ray))
        fine = 0
        last_error: float | None = None
        final_measurement = None
        observations = []
        expired_measurements = 0
        self.scanner.checkpoint["navigation_target_observations"] = observations
        while waypoints:
            located = await self.locate()
            if len(waypoints) > 1 and ray_to_image_pixel(
                ray, self.localizer.lens, rotation_matrix=located["rotation_matrix"]
            ) is not None:
                # Overlap waypoints only acquire sight of the requested ray.
                # Once freshly visible, aim at it instead of visiting centres
                # of every remaining reference photograph.
                waypoints = [np.asarray(ray)]
                last_error = None
            target = waypoints[0]
            final = len(waypoints) == 1
            intermediate = False
            if (
                ray_to_image_pixel(
                    target, self.localizer.lens, rotation_matrix=located["rotation_matrix"]
                )
                is None
                and not final
            ):
                middle = _rotation_basis(located["rotation_matrix"])[:, 2] + target
                if np.linalg.norm(middle) < 0.1:
                    raise PanoramaCaptureError("visual_route_unavailable")
                middle /= np.linalg.norm(middle)
                if (
                    ray_to_image_pixel(
                        middle, self.localizer.lens, rotation_matrix=located["rotation_matrix"]
                    )
                    is None
                ):
                    raise PanoramaCaptureError("visual_route_unavailable")
                target = middle
                intermediate = True
            error = self._error(target, located)
            pixel = ray_to_image_pixel(
                target, self.localizer.lens, rotation_matrix=located["rotation_matrix"]
            )
            if pixel is None:
                raise PanoramaCaptureError("visual_route_unavailable")
            try:
                measured = await self._target_measurement(target, located) if final else None
            except _ExpiredQualifiedFrame as error:
                # Measurement may consume the last milliseconds of a valid pose.
                # Reobserve and replan while stopped, never dispatch its old pulse
                # or accept its old arrival. Bound this separately from motion.
                expired_measurements += 1
                if expired_measurements > 2:
                    raise
                self.scanner.last_frame = await self._renew_localization_frame(error)
                continue
            if final:
                observations.append({
                    "commands": self.commands,
                    "capture_evidence": self.scanner.last_frame.get("capture_evidence", {}),
                    "measurement": measured,
                })
            if (
                final
                and measured
                and measured["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS
            ):
                final_measurement = measured
                break
            if not final and not intermediate and np.linalg.norm(error) < 0.08:
                waypoints.pop(0)
                last_error = None
                continue
            near = final and np.linalg.norm(error) < 0.04
            if near and fine >= MAXIMUM_LIVE_FINE_CORRECTIONS:
                raise PanoramaCaptureError("visual_arrival_unconfirmed")
            if near and measured is not None:
                # The image measurement corrects the pose prediction locally.
                scale = measured["analysis_width"] / self.localizer.lens["width"]
                error = np.arctan(
                    np.asarray(measured["error_pixels"])
                    / (np.array([self.localizer.lens["fx"], self.localizer.lens["fy"]]) * scale)
                )
            axes = [
                axis
                for axis in ("pan", "tilt")
                if self.scanner.capabilities.get("axes", {}).get(axis) is True
            ]
            if final and measured and axes and len(measured.get("error_pixels", [])) == 2:
                component_limit = MAXIMUM_LIVE_CENTER_ERROR_PIXELS / np.sqrt(2)
                needed = [
                    axis
                    for index, axis in enumerate(("pan", "tilt"))
                    if axis in axes and abs(float(measured["error_pixels"][index])) > component_limit
                ]
                if not needed and measured["center_error_pixels"] > MAXIMUM_LIVE_CENTER_ERROR_PIXELS:
                    needed = [
                        max(
                            axes,
                            key=lambda axis: abs(
                                float(measured["error_pixels"][0 if axis == "pan" else 1])
                            ),
                        )
                    ]
                axes = needed
            if not axes:
                raise PanoramaCaptureError("visual_control_resolution_unverified")
            missing = [axis for axis in axes if axis not in self.response]
            deferred_axes = []
            known = [axis for axis in axes if axis in self.response]
            if missing and known:
                # A missing minor-axis response need not interrupt useful motion
                # on a freshly qualified axis. Re-evaluate after every observed
                # pulse; probe the missing axis once known motion cannot remove
                # at least a tenth of the remaining error. Never reuse its stale gain.
                known_matrix = np.column_stack([self.response[axis] for axis in known])
                known_step = correction_step(known_matrix, error)
                if np.linalg.norm(error + known_matrix @ known_step) <= np.linalg.norm(error) * .9:
                    deferred_axes, missing, axes = missing, [], known
            fine_control = bool(final and measured
                and measured["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS * 4
                and self.scanner.capabilities.get("velocity_supported"))
            if missing:
                axis = max(missing, key=lambda name: abs(error[0 if name == "pan" else 1]))
                amount = self.probe_durations.get(axis, DEFAULT_NAVIGATION_PROBE_SECONDS)
                probe_direction = reference_probe_direction(
                    axis, np.asarray(target), np.asarray(located["rotation_matrix"]),
                    getattr(self.localizer, "probe_observations", []),
                )
                amount *= probe_direction if probe_direction is not None else 1
            else:
                matrix = np.column_stack([self.response[axis] for axis in axes])
                commands = correction_step(
                    matrix, error,
                    maximum=max(self.pulse_limits.get(axis, MAXIMUM_NAVIGATION_PULSE_SECONDS) for axis in axes),
                    minimum=0.05 if self.scanner.capabilities.get("velocity_supported") and not fine_control else 0,
                )
                for component, name in enumerate(axes):
                    limit = self.pulse_limits.get(name, MAXIMUM_NAVIGATION_PULSE_SECONDS)
                    commands[component] = np.clip(commands[component], -limit, limit)
                # Motor time is not image error. Prefer the axis whose measured
                # response leaves the smallest centre error; a slow minor axis
                # must not precede the correction that can already reach it.
                index = int(np.argmin([
                    np.linalg.norm(error + matrix[:, component] * command)
                    for component, command in enumerate(commands)
                ]))
                axis, amount = axes[index], float(commands[index])
                if abs(amount) < 0.001:
                    raise PanoramaCaptureError("visual_control_resolution_unverified")
            if near:
                fine += 1
            pulse_amount, pulse_speed = amount, DEFAULT_CONTINUOUS_PULSE_SPEED
            fine_response_qualified = (fine_control and axis in self.response
                and _fine_correction_has_measured_response(axis, amount, error, target, self.trace))
            if (fine_control and axis in self.response
                    and not fine_response_qualified):
                # Reuse the qualified return controller's fine pulse planner.
                # Convert the observed angular response to local image units;
                # neither radians nor pixels are sent as native motor positions.
                scale = measured["analysis_width"] / self.localizer.lens["width"]
                pixel_derivative = np.array([self.localizer.lens["fx"], self.localizer.lens["fy"]]) * scale / np.cos(error) ** 2
                plan = _continuous_pulse_plan(self.response[axis] * pixel_derivative,
                    np.asarray(measured["error_pixels"]), amount)
                if plan is None:
                    raise PanoramaCaptureError("visual_control_resolution_unverified")
                duration, pulse_speed, equivalent_amount = plan
                pulse_amount = float(np.copysign(duration, amount))
                amount = equivalent_amount
            before = self._error(target, located)
            self.scanner.checkpoint["navigation_step"] = {
                "command": self.commands + 1,
                "target_kind": "final" if final else "intermediate",
                "target_ray": np.asarray(target).tolist(),
                "before_error_radians": before.tolist(),
                "missing_response_axes": missing,
                "deferred_response_axes": deferred_axes,
                "kind": "probe" if missing else "correction",
                "axis": axis,
                "amount": pulse_amount,
                "speed": pulse_speed,
                "fine_response_qualified": fine_response_qualified,
                "reference_probe_direction": probe_direction if missing else None,
            }
            if pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED:
                await self._pulse(axis, pulse_amount)
            else:
                await self._pulse(axis, pulse_amount, speed=pulse_speed)
            self.trace[-1]["response_equivalent_amount"] = amount
            expired_measurements = 0
            after = await self.locate()
            updated = self._error(target, after)
            response = (updated - before) / amount
            if np.linalg.norm(response) < 0.01:
                if final:
                    measured = await self._target_measurement(target, after)
                    observations.append({
                        "commands": self.commands,
                        "capture_evidence": self.scanner.last_frame.get("capture_evidence", {}),
                        "measurement": measured,
                    })
                    if (
                        measured
                        and measured["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS
                    ):
                        final_measurement = measured
                        break
                if (axis in missing and self.last_pulse_stationary
                        and axis not in self.probe_durations
                        and self.scanner.capabilities.get("velocity_supported")):
                    # One bounded identification step after proven no-effect;
                    # do not repeat corrections, relax Stop, or assume a gain.
                    self.probe_durations[axis] = 0.24
                    self.trace[-1]["next_probe_seconds"] = 0.24
                    continue
                raise PanoramaCaptureError("visual_response_unavailable")
            self.response[axis] = response
            self.response_rotations[axis] = np.asarray(after["rotation_matrix"])
            self.pulse_limits[axis] = _qualified_navigation_pulse_limit(pulse_amount, self.last_pulse_match)
            magnitude = float(np.linalg.norm(updated))
            self.trace[-1].update(error_radians=magnitude, response=response.tolist(),
                                 next_pulse_limit=self.pulse_limits[axis])
            if not missing and last_error is not None and magnitude > last_error * 1.2:
                raise PanoramaCaptureError("visual_correction_diverged")
            last_error = magnitude
        if final_measurement is None:
            raise PanoramaCaptureError("visual_arrival_unconfirmed")
        return {
            "verified": True,
            "kind": "visual_aim_verified",
            "timing_basis": "local_observation",
            "physical_capture_verified": False,
            "capture_evidence": self.scanner.last_frame.get("capture_evidence", {}),
            "measurement": final_measurement,
            "commands": self.commands,
        }


async def correct_reference(scanner: _Scan, reference: np.ndarray, result: dict) -> dict:
    """Use the remaining shared return budget for visually observed pulses."""
    responses: dict[str, np.ndarray] = {}
    response_directions: dict[str, int] = {}
    response_speeds: dict[str, float] = {}
    previous = scanner.checkpoint.get("return_corrections", [])
    # The checkpoint belongs to one panorama job. Every return-only invocation
    # therefore sees the same physical-command budget. An uncertain or rejected
    # invocation is terminal; a new job gets a new checkpoint and fresh budget.
    trace_is_valid = isinstance(previous, list)
    trace = previous if trace_is_valid else []
    previous_state = scanner.checkpoint.get("return_correction_state")
    epoch_value = scanner.checkpoint.get("return_epoch", "final")
    return_epoch = epoch_value if isinstance(epoch_value, str) and epoch_value else "final"
    response_samples = _qualified_response_counts(trace, return_epoch=return_epoch)
    scanner.checkpoint["return_corrections"] = trace
    persist = getattr(scanner, "_persist", None)
    command_trace = _return_correction_commands(scanner.checkpoint)
    history_failure = (
        _return_history_failure(trace, command_trace)
        if trace_is_valid
        else "return_correction_trace_invalid"
    )
    remaining_corrections = max(0, MAXIMUM_FINE_CORRECTIONS - len(command_trace))
    terminal_state = (
        "complete"
        if not remaining_corrections and previous_state == "complete"
        else "budget_exhausted"
        if not remaining_corrections
        else "stopped"
    )
    model_loaded = False
    for _ in range(remaining_corrections):
        comparison = await asyncio.to_thread(_match, reference, result["frame"]["image"])
        measurement = _visual_measurement(comparison)
        if measurement is None or measurement[0] < 0.85:
            terminal_state = "rejected"
            break
        _, comparison_displacement, shift_x, shift_y = measurement
        if comparison_displacement <= 3:
            terminal_state = "complete"
            break
        if not model_loaded:
            (
                responses,
                response_directions,
                response_speeds,
                resume_failure,
            ) = (
                _persisted_return_responses(
                    trace,
                    measurement,
                    return_epoch=return_epoch,
                )
                if history_failure is None
                else ({}, {}, {}, history_failure)
            )
            model_loaded = True
            if resume_failure is not None:
                scanner.checkpoint["return_correction_resume"] = {
                    "state": "blocked",
                    "reason": resume_failure,
                    "return_epoch": return_epoch,
                }
                terminal_state = "resume_unavailable"
                break
            scanner.checkpoint["return_correction_resume"] = {
                "state": "verified" if responses else "not_required",
                "axes": sorted(responses),
                "return_epoch": return_epoch,
            }
            if previous_state in {"failed", "rejected", "resume_unavailable"}:
                terminal_state = "resume_unavailable"
                scanner.checkpoint["return_correction_resume"].update(
                    state="blocked",
                    reason="return_correction_terminal_state",
                )
                break
        error = np.array([shift_x, shift_y])
        axes = [
            axis
            for axis in ("pan", "tilt")
            if scanner.capabilities.get("axes", {}).get(axis) is True
        ]
        missing = [axis for axis in axes if axis not in responses]
        known = [axis for axis in axes if axis in responses]
        matrix = (
            np.column_stack([responses[axis] for axis in known])
            if known
            else np.empty((2, 0))
        )
        residual = (
            error - matrix @ np.linalg.lstsq(matrix, error, rcond=None)[0]
            if known
            else error
        )
        axis = None
        amount = 0.0
        command_kind = "probe"
        outbound_seed: dict[str, Any] | None = None
        # Once a probe has measured an axis, consume that information before
        # probing another unknown sign. This is coordinate descent over the
        # observed local Jacobian and prevents a sequence of unrelated blind
        # pulses such as pan+, tilt+.
        if known:
            try:
                # A single measured probe establishes direction only.  Do not
                # extrapolate it into a larger pulse until a second observed
                # response in this return epoch has confirmed local behavior.
                maximum = (
                    0.15
                    if all(response_samples.get(candidate_axis, 0) >= 2 for candidate_axis in known)
                    else 0.08
                )
                command = correction_step(matrix, error, maximum=maximum)
            except PanoramaCaptureError:
                command = np.zeros(len(known), dtype=np.float64)
            current_norm = float(np.linalg.norm(error))
            minimum_improvement = max(0.25, current_norm * 0.01)
            candidates = []
            for index, candidate_axis in enumerate(known):
                candidate_amount = float(command[index])
                if abs(candidate_amount) < 0.001:
                    continue
                predicted = float(
                    np.linalg.norm(error + responses[candidate_axis] * candidate_amount)
                )
                if predicted <= current_norm - minimum_improvement:
                    candidates.append((predicted, candidate_axis, candidate_amount))
            if candidates:
                _, axis, amount = min(candidates)
                command_kind = "correction"
        if axis is None and missing and np.linalg.norm(residual) > 2:
            seeded = _outbound_inverse_probe(
                scanner.checkpoint, error, return_epoch=return_epoch, missing_axes=missing
            )
            if seeded is not None:
                axis, amount, outbound_seed = seeded
                command_kind = "outbound_inverse_probe"
            else:
                axis = max(
                    missing,
                    key=lambda name: abs(residual[0 if name == "pan" else 1]),
                )
                amount = 0.08
                command_kind = "probe"
        if axis is None:
            break
        direction = 1 if amount > 0 else -1
        measured_direction = axis in responses and response_directions[axis] == direction
        if not measured_direction:
            # Reversing an axis requires its own short observation; do not apply
            # the gain measured in the other direction to a larger correction.
            amount = direction * 0.08
            if command_kind != "outbound_inverse_probe":
                command_kind = "direction_probe" if axis in responses else "probe"
        pulse_seconds = abs(amount)
        pulse_speed = DEFAULT_CONTINUOUS_PULSE_SPEED
        effective_amount = amount
        if measured_direction and scanner.capabilities.get("velocity_supported"):
            plan = _continuous_pulse_plan(responses[axis], error, amount)
            if plan is None:
                trace.append(
                    {
                        "axis": axis,
                        "amount": amount,
                        "before_pixels": comparison_displacement,
                        "direction_measured": True,
                        "verified": False,
                        "state": "rejected",
                        "reason": "visual_control_resolution_unverified",
                    }
                )
                if callable(persist):
                    await persist()
                terminal_state = "rejected"
                break
            pulse_seconds, pulse_speed, effective_amount = plan
        velocity_measured = (
            measured_direction
            and response_speeds.get(axis, DEFAULT_CONTINUOUS_PULSE_SPEED) == pulse_speed
        )
        expected_delta = responses[axis] * effective_amount if measured_direction else None
        entry = {
            "kind": command_kind,
            "axis": axis,
            "amount": effective_amount,
            "requested_amount": amount,
            "pulse_seconds": pulse_seconds,
            "velocity": pulse_speed,
            "before_pixels": comparison_displacement,
            "before_overlap": measurement[0],
            "before_shift": [shift_x, shift_y],
            "before_match_verified": True,
            "expected_shift_delta": (
                expected_delta.tolist() if expected_delta is not None else None
            ),
            "direction_measured": measured_direction,
            "velocity_measured": velocity_measured,
            "qualified_response_samples": response_samples.get(axis, 0),
            "prediction_basis": (
                "measured_velocity"
                if velocity_measured
                else "onvif_velocity_time_equivalence"
                if measured_direction
                else None
            ),
            "outbound_seed": outbound_seed,
            "return_epoch": return_epoch,
            "expected_frame_evidence": dict(
                result["frame"].get("capture_evidence", {})
            ),
            "verified": False,
            "match_verified": False,
            "response_qualified": False,
            "state": "planned",
        }
        _append_return_correction_command(
            scanner.checkpoint,
            trace,
            entry,
            modality=(
                "visual_continuous"
                if scanner.capabilities.get("velocity_supported") is True
                else "visual_relative"
            ),
        )
        scanner.checkpoint["return_correction_state"] = "active"
        if callable(persist):
            await persist()
        try:
            pulse_options = (
                {"speed": pulse_speed}
                if pulse_speed != DEFAULT_CONTINUOUS_PULSE_SPEED
                else {}
            )
            moved = await scanner._pulse(
                axis,
                direction,
                pulse_seconds,
                expected_frame=result["frame"],
                **pulse_options,
            )
        except PanoramaCaptureError as error:
            entry.update(reason=error.code, state="failed")
            scanner.checkpoint["return_correction_state"] = "failed"
            if callable(persist):
                await persist()
            raise
        observed = await asyncio.to_thread(_match, reference, moved["frame"]["image"])
        result = moved
        observed_measurement = _visual_measurement(observed)
        entry.update(
            after_pixels=(observed_measurement[1] if observed_measurement is not None else None),
            after_overlap=(observed_measurement[0] if observed_measurement is not None else None),
            after_shift=(
                [observed_measurement[2], observed_measurement[3]]
                if observed_measurement is not None
                else None
            ),
            after_code=(observed.get("code") if isinstance(observed.get("code"), str) else None),
            verified=observed_measurement is not None,
            match_verified=observed_measurement is not None,
            state="observed",
        )
        if callable(persist):
            await persist()
        if observed_measurement is None:
            entry["reason"] = observed.get("code", "visual_response_unavailable")
            if callable(persist):
                await persist()
            terminal_state = "rejected"
            break
        observed_overlap, observed_displacement, observed_shift_x, observed_shift_y = (
            observed_measurement
        )
        if observed_overlap < 0.85:
            entry["reason"] = "insufficient_overlap"
            if callable(persist):
                await persist()
            terminal_state = "rejected"
            break
        updated = np.array([observed_shift_x, observed_shift_y])
        if np.linalg.norm(updated - error) < 2:
            entry["reason"] = "visual_response_unavailable"
            if callable(persist):
                await persist()
            terminal_state = "rejected"
            break
        if (
            command_kind == "probe"
            and observed_displacement
            > comparison_displacement * MAXIMUM_PROBE_ERROR_GROWTH_RATIO
        ) or (
            command_kind != "probe"
            and observed_displacement > comparison_displacement
        ):
            entry["reason"] = "visual_error_increased"
            if callable(persist):
                await persist()
            terminal_state = "rejected"
            break
        if expected_delta is not None:
            ratio = float(np.linalg.norm(updated - error) / max(np.linalg.norm(expected_delta), 1e-6))
            entry["response_ratio"] = ratio
            if np.dot(updated - error, expected_delta) <= 0 or not 0.4 <= ratio <= 2.5:
                entry["reason"] = "visual_response_inconsistent"
                if callable(persist):
                    await persist()
                terminal_state = "rejected"
                break
        minimum_observed_improvement = max(0.25, comparison_displacement * 0.01)
        if (
            command_kind != "probe"
            and observed_displacement
            > comparison_displacement - minimum_observed_improvement
        ):
            entry["reason"] = "visual_error_not_reduced"
            if callable(persist):
                await persist()
            terminal_state = "rejected"
            break
        responses[axis] = (updated - error) / effective_amount
        response_directions[axis] = direction
        response_speeds[axis] = pulse_speed
        response_samples[axis] = response_samples.get(axis, 0) + 1
        entry.update(
            response_qualified=True,
            response_vector=responses[axis].tolist(),
            response_direction=direction,
            response_velocity=pulse_speed,
            after_frame_evidence=dict(moved["frame"].get("capture_evidence", {})),
        )
        if callable(persist):
            await persist()
        if observed_displacement <= 3:
            terminal_state = "complete"
            break
    else:
        if remaining_corrections:
            terminal_state = "budget_exhausted"
    scanner.checkpoint["return_correction_state"] = terminal_state
    if callable(persist):
        await persist()
    return result
