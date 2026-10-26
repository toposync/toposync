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
    MAXIMUM_FOLLOWING_WINDOW_SECONDS,
    MINIMUM_CONTINUOUS_PULSE_SECONDS,
    _Scan,
    _exact_velocity_supported,
    _append_return_correction_command,
    _match,
    _next_continuous_seek_duration,
    _return_correction_commands,
)
from .processing.panorama_mapping import _rotation_basis, image_pixel_to_ray, ray_to_image_pixel, ray_to_panorama_pixel
from .panorama_target_motion import TargetMotion
from .processing.panorama_localization import (
    ANALYSIS_WIDTH,
    frame_identity,
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


def _matched_target_displacement(matching: dict, target: Any, lens: dict, located: dict) -> float | None:
    """Compare the same target position instead of a scene-wide displacement percentile."""
    if matching.get("verified") is not True or matching.get("support_scope") != "distributed_scene":
        return None
    try:
        size = np.asarray(matching["analysis_size"], dtype=float)
        source_size = np.asarray([lens["width"], lens["height"]], dtype=float)
        matrix = np.asarray(matching["homography"], dtype=float)
        if (size.shape != (2,) or matrix.shape != (3, 3)
                or not np.isfinite(np.r_[size, source_size, matrix.ravel()]).all()
                or np.any(size <= 0) or np.any(source_size <= 0)
                or abs(np.linalg.det(matrix)) < 1e-12):
            return None
        pixel = ray_to_image_pixel(target, lens, rotation_matrix=located["rotation_matrix"])
        if pixel is None:
            return None
        point = np.asarray(pixel) * size / source_size
        projected = matrix @ np.r_[point, 1.]
        if not np.isfinite(projected).all() or abs(projected[2]) < 1e-9:
            return None
        endpoint = projected[:2] / projected[2]
        if np.any(point < 0) or np.any(point >= size) or np.any(endpoint < 0) or np.any(endpoint >= size):
            return None
        return float(np.linalg.norm(endpoint - point) * ANALYSIS_WIDTH / size[0])
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        return None


async def _arrival_stop_is_current(
    scanner: Any, camera: Any, result: Any, *, maximum_error_pixels: float = MAXIMUM_LIVE_CENTER_ERROR_PIXELS,
) -> bool:
    """Reuse fresh visual arrival only while its accepted Stop still owns control."""
    frame = scanner.last_frame or {}
    evidence = frame.get("capture_evidence") or {}
    receipt = getattr(scanner, "last_stop_receipt", None)
    accepted = getattr(scanner, "last_stop_accepted_monotonic", None)
    received = frame.get("received_monotonic")
    if (not isinstance(result, dict) or result.get("verified") is not True
            or result.get("kind") != "visual_aim_verified"
            or scanner.physical_state != "stopped"
            or not evidence.get("capture_instance")
            or type(evidence.get("generation")) is not int
            or type(evidence.get("sequence")) is not int
            or result.get("capture_evidence") != evidence
            or type(accepted) not in (float, int) or type(received) not in (float, int)
            or not accepted <= received <= time.monotonic()
            or not 0 <= time.monotonic() - received <= 1.0
            or type(maximum_error_pixels) not in (float, int)
            or not 0 < maximum_error_pixels <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS):
        return False
    error = (result.get("measurement") or {}).get("center_error_pixels")
    if type(error) not in (float, int) or not 0 <= error <= maximum_error_pixels:
        return False
    try:
        current = await camera.stop_receipt_is_current(receipt)
    except Exception:
        return False
    # The control lookup can wait; never renew the image's freshness with it.
    return current is True and 0 <= time.monotonic() - received <= 1.0


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
                "after_rotation": last.tolist(), "velocity": velocity[axes[0]],
                "before_capture_id": before["id"], "after_capture_id": after["id"],
                "capture_instance": before["capture_instance"], "generation": before["generation"],
                "before_sequence": before["sequence"], "after_sequence": after["sequence"]})
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


def arrival_frame_geometry(frame: dict, located: dict, lens: dict, ray: Any) -> dict | None:
    """Snapshot qualified image geometry; the caller supplies Stop/arrival proof."""
    capture = frame.get("capture_evidence") or {}
    image = frame.get("image")
    if (not isinstance(capture.get("capture_instance"), str) or not capture["capture_instance"]
            or type(capture.get("generation")) is not int or capture["generation"] < 0
            or type(capture.get("sequence")) is not int or capture["sequence"] < 1
            or image is None or image.shape[:2] != (lens.get("height"), lens.get("width"))
            or not 0 <= time.monotonic() - frame.get("received_monotonic", 0) <= 1
            or localized_axis_measurement(ray, lens, located) is None):
        return None
    return deepcopy({"capture_evidence": capture, "lens": lens,
        **{key: located.get(key) for key in ("rotation_matrix", "analysis_width", "validation_matches",
            "validation_p95_pixels", "inlier_fraction", "reference_id")}})


def group_framing_update_valid(previous: dict, updated: dict | None) -> bool:
    if updated is None or not set(previous["members"]).issubset(updated["members"]):
        return False
    for name, old in previous["members"].items():
        new = updated["members"][name]
        if (any(old[key] != new[key] for key in ("capture_instance", "generation"))
                or new["sequence"] < old["sequence"] or new["published_at"] < old["published_at"]
                or (new["sequence"] == old["sequence"] and new != old)
                or (new["sequence"] > old["sequence"] and new["published_at"] <= old["published_at"])):
            return False
    return True


def group_framing_rays(group: dict) -> np.ndarray:
    """Center a tangent-plane envelope of member history, without predicting motion."""
    corners = np.asarray([ray for member in group["members"].values()
                          for ray in member["panorama"]["corner_rays"]], dtype=float)
    if (corners.ndim != 2 or corners.shape[1] != 3 or len(corners) == 0
            or not np.isfinite(corners).all() or np.any(abs(np.linalg.norm(corners, axis=1) - 1) > 1e-6)):
        raise ValueError("invalid group directions")
    axis = corners.sum(axis=0)
    if np.linalg.norm(axis) < 1e-8:
        raise ValueError("group has no common viewing hemisphere")
    axis /= np.linalg.norm(axis)
    depths = corners @ axis
    if np.any(depths <= 1e-3):
        raise ValueError("group has no common viewing hemisphere")
    right = np.cross(axis, [0, 0, 1] if abs(axis[2]) < .9 else [0, 1, 0])
    right /= np.linalg.norm(right)
    up = np.cross(right, axis)
    points = np.column_stack((corners @ right, corners @ up)) / depths[:, None]
    middle = (points.min(axis=0) + points.max(axis=0)) / 2
    center = axis + middle[0] * right + middle[1] * up
    center /= np.linalg.norm(center)
    return np.vstack((center, corners))


def _projected_extent_fit(box: list[float], lens: dict) -> dict:
    size = np.asarray([lens["width"], lens["height"]])
    low, high = np.asarray(box).reshape(2, 2) * size
    extent = (high - low) / (size - 1)
    inside = bool(np.all(low >= 0) and np.all(high <= size - 1))
    # Outside projections extrapolate the lens model; another pose can change the extent.
    return {"status": "extent_exceeds_frame" if np.any(extent > 1) else (
        "inside_frame" if inside else "offset_within_current_extent"),
        "extent_ratio": extent.tolist(), "basis": "current_pose_projection",
        "reaim_feasibility_verified": False}


def project_group_framing(group: dict | None, evidence: dict) -> dict | None:
    if group is None:
        return None
    projections = {name: project_arrival_framing(member, evidence) for name, member in group["members"].items()}
    result = {"kind": "projected_member_history", "subject_visibility_verified": False,
        "physical_capture_verified": False, "members": projections, "status": "member_outside_or_unqualified"}
    if projections and all(item and "unclipped_bbox01" in item for item in projections.values()):
        boxes = np.asarray([item["unclipped_bbox01"] for item in projections.values()])
        box = [*boxes[:, :2].min(axis=0).tolist(), *boxes[:, 2:].max(axis=0).tolist()]
        result.update(unclipped_bbox01=box, fit=_projected_extent_fit(box, evidence["frame_geometry"]["lens"]))
        if all(item["status"] == "projected" for item in projections.values()):
            result.update(status="projected", bbox01=box, minimum_margin01=min(*box[:2], 1-box[2], 1-box[3]))
    return result


def project_arrival_framing(framing: dict | None, evidence: dict) -> dict | None:
    """Project observed corner directions; this does not detect the subject again."""
    if framing is None:
        return None
    geometry = evidence.get("frame_geometry")
    result = {"kind": "projected_observed_corners", "subject_visibility_verified": False,
        "physical_capture_verified": False, "status": "geometry_unavailable"}
    if not isinstance(geometry, dict) or geometry.get("capture_evidence") != evidence.get("capture_evidence"):
        return result
    try:
        lens = geometry["lens"]
        panorama = framing["panorama"]
        scope = {key: framing[key] for key in ("camera_id", "source_id")} | {
            key: panorama[key] for key in ("job_id", "revision", "composition_id", "source_artifact_id", "geometry_digest")}
        if geometry.get("source_scope") != scope:
            return {**result, "status": "reference_mismatch"}
        frame_center = image_pixel_to_ray((lens["width"]-1)/2, (lens["height"]-1)/2,
            lens, rotation_matrix=geometry["rotation_matrix"])
        if localized_axis_measurement(frame_center, lens, geometry) is None:
            return result
        corners = panorama["corner_rays"]
        if len(corners) != 4:
            return result
        pixels = [ray_to_image_pixel(ray, lens, rotation_matrix=geometry["rotation_matrix"],
            allow_outside=True) for ray in corners]
        if any(pixel is None for pixel in pixels):
            return {**result, "status": "corner_behind_or_unprojectable", "sampled_corners_inside": False}
        points = np.asarray(pixels) / np.asarray([lens["width"], lens["height"]])
        low, high = points.min(axis=0), points.max(axis=0)
        same_capture = all(framing.get(key) == geometry["capture_evidence"].get(key)
            for key in ("capture_instance", "generation", "sequence"))
        box = [*low.tolist(), *high.tolist()]
        fit = _projected_extent_fit(box, lens)
        inside = fit["status"] == "inside_frame"
        result.update(status="projected" if inside else "projected_outside", sampled_corners_inside=inside,
            unclipped_bbox01=box, fit=fit)
        if inside:
            result.update(bbox01=box, minimum_margin01=float(min(*low, *(1 - high))))
        return {**result,
            "same_capture_as_observation": same_capture, "observation_boundary_contact": framing.get("boundary_contact"),
            "capture_evidence": deepcopy(geometry["capture_evidence"])}
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        return result


def _small_return_probe(entry: dict, return_epoch: str) -> bool:
    """A stopped, improving small probe permits one equal probe, never a gain."""
    if (not isinstance(entry, dict) or entry.get("return_epoch") != return_epoch or entry.get("kind") != "probe"
            or entry.get("modality") != "visual_continuous" or entry.get("state") != "observed"
            or entry.get("axis") not in {"pan", "tilt"}
            or entry.get("response_qualified") is not False
            or entry.get("reason") != "visual_response_unavailable"
            or entry.get("verified") is not True or entry.get("before_match_verified") is not True
            or entry.get("match_verified") is not True):
        return False
    try:
        before, after = np.asarray(entry["before_shift"], dtype=float), np.asarray(entry["after_shift"], dtype=float)
        values = [entry[key] for key in ("amount", "velocity", "before_pixels", "after_pixels", "before_overlap", "after_overlap")]
        if before.shape != (2,) or after.shape != (2,) or not np.isfinite([*before, *after, *values]).all():
            return False
        amount, speed, before_pixels, after_pixels, before_overlap, after_overlap = values
        return bool(abs(amount) == .08 and speed == DEFAULT_CONTINUOUS_PULSE_SPEED
            and min(before_overlap, after_overlap) >= .85 and after_pixels > 3
            and before_pixels - after_pixels >= max(.25, before_pixels * .01)
            and .5 <= np.linalg.norm(after - before) < 2
            and np.linalg.norm(after) < np.linalg.norm(before))
    except (KeyError, TypeError, ValueError):
        return False


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
    small_probes = [entry for entry in trace if isinstance(entry, dict) and _small_return_probe(entry, return_epoch)]
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
            if len(small_probes) == 1 and entry is small_probes[0]:
                # Preserve the rejected gain in the ledger; it grants no response model.
                if entry is trace[-1] and np.linalg.norm(
                        np.asarray(entry["after_shift"]) - np.asarray(measurement[2:])) > 1:
                    return responses, directions, speeds, "return_response_view_changed"
                continue
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
    correction, use bounded velocity levels and legal durations up to 150 ms.
    The resulting image is still required to qualify the device's
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
    candidates: list[tuple[bool, float, float, float, float]] = []
    for speed in FINE_CONTINUOUS_SPEEDS:
        duration = float(np.clip(abs(requested_amount) * DEFAULT_CONTINUOUS_PULSE_SPEED / speed,
                                MINIMUM_CONTINUOUS_PULSE_SECONDS, .15))
        equivalent_amount = (
            direction
            * duration
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
                    float(error @ (error + predicted_delta)) < -1e-12,
                    predicted_error,
                    speed,
                    duration,
                    equivalent_amount,
                )
            )
    if not candidates:
        return None
    # Prefer an improving pulse that does not cross the target when one is observable.
    _, _, speed, duration, equivalent_amount = min(candidates)
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
    """Choose one direction probe from the causal outbound observation.

    This is deliberately only a direction probe: a camera may have backlash,
    so the inverse displacement is not promoted to a reusable response model
    until the resulting frame demonstrates improvement. A zero amount means
    the seed predicts no improving probe; None means no usable seed.
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
    # Unmeasured directions are dispatched as 80 ms probes below. Predict
    # that same command, rather than the shorter measured-correction floor.
    probe_seconds = 0.08
    inverse_delta = -shift * (probe_seconds / duration)
    current_norm = float(np.linalg.norm(error))
    improvement_limit = current_norm - max(0.25, current_norm * 0.01)
    amount = float(-direction * probe_seconds)
    if float(np.linalg.norm(error + inverse_delta)) > improvement_limit:
        # Native recall may land on the other side of the reference. If
        # neither direction can improve it, do not fall back to a blind probe.
        amount = (
            float(direction * probe_seconds)
            if float(np.linalg.norm(error - inverse_delta)) <= improvement_limit
            else 0.0
        )
    return axis, amount, {
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


class _TargetDispatchChanged(PanoramaCaptureError):
    def __init__(self, reason: str, frame_age_seconds: float, target_shift_pixels: float | None = None):
        super().__init__("attention_target_changed_before_dispatch")
        self.details = {"reason": reason, "frame_age_seconds": frame_age_seconds,
                        "target_shift_pixels": target_shift_pixels}



class MotionFrameObservations:
    """Latest-only visual pose preparation; never a Stop or arrival certificate.

    The scanner's synchronous offer does no localization and cannot hold up
    its Stop path. One worker and one replaceable pending frame bound memory
    and work. Close drains the worker before its localizer can be released.
    """

    def __init__(self, localizer: Any, records: list[dict], command: int):
        self.localizer, self.records, self.command = localizer, records, command
        self.pending = None
        self.latest = None
        self.qualified_rate = None
        self.worker = None
        self.closed = False
        self.identity = None
        self.last_sequence = -1
        self.last_offered = -float("inf")

    def offer(self, frame: dict) -> None:
        if self.closed:
            return
        identity = frame_identity(frame.get("capture_evidence"))
        now = time.monotonic()
        received = frame.get("received_monotonic")
        if (identity is None or type(received) not in (int, float)
                or not 0 <= now - received <= 1):
            return
        if self.identity is not None and identity[:2] != self.identity:
            # A decoder change invalidates this movement window. The caller
            # must qualify the next operation instead of joining two epochs.
            self.closed = True
            self.pending = None
            return
        self.identity = identity[:2]
        if identity[2] <= self.last_sequence or now - self.last_offered < .1:
            return
        self.last_sequence, self.last_offered = identity[2], now
        self.pending = {**frame, "capture_evidence": deepcopy(frame["capture_evidence"])}
        if frame.get("image_geometry") is not None:
            self.pending["image_geometry"] = deepcopy(frame["image_geometry"])
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self.pending is not None and not self.closed:
            frame, self.pending = self.pending, None
            started = time.monotonic()
            if not 0 <= started - frame["received_monotonic"] <= 1:
                continue
            record = {"kind": "visual_motion_observation", "command": self.command,
                "arrival_verified": False, "capture_evidence": frame["capture_evidence"],
                "frame_received_monotonic": frame["received_monotonic"],
                "physical_capture_verified": False, "timing_basis": "local_observation",
                "localization_model_sha256": getattr(self.localizer, "model_digest", None)}
            try:
                located = await asyncio.to_thread(
                    self.localizer.locate,
                    frame["image"], frame["capture_evidence"],
                    **({"image_geometry": frame["image_geometry"]} if frame.get("image_geometry") else {}),
                )
                record.update(status=located.get("status"), reason=located.get("reason"))
                if located.get("status") == "localized" and located.get("capture_evidence") != frame["capture_evidence"]:
                    record.update(status="unlocalized", reason="panorama_frame_binding_mismatch")
                elif located.get("status") == "localized":
                    ray = _rotation_basis(located["rotation_matrix"])[:, 2]
                    geometry = arrival_frame_geometry(frame, located, self.localizer.lens, ray)
                    if geometry is not None and not self.closed:
                        record["frame_geometry"] = geometry
                        if frame.get("image_geometry") is not None:
                            record["image_geometry"] = deepcopy(frame["image_geometry"])
                    else:
                        reason = ("panorama_frame_not_recent"
                            if not 0 <= time.monotonic() - frame["received_monotonic"] <= 1
                            else "motion_frame_geometry_unqualified")
                        record.update(status="unlocalized", reason=reason)
            except Exception:
                # Optional preparation must not alter physical cleanup or leak
                # arbitrary exception text into persisted diagnostics.
                record.update(status="unlocalized", reason="motion_observation_failed")
            record["elapsed_seconds"] = time.monotonic() - started
            if not self.closed:
                self.latest = record
                self.records.append(record)
                del self.records[:-32]

    async def close(self) -> None:
        self.closed = True
        self.pending = None
        if self.worker is not None:
            cancelled = False
            while not self.worker.done():
                try:
                    await asyncio.shield(self.worker)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            self.worker.result()


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
        self.dispatch_guard: Callable[[], None] | None = None
        self.observation_guard: Callable[[], None] | None = None
        self.motion_feedback_target: Callable[[], tuple[Any, float]] | None = None
        self.following_seconds: float | None = None
        self.motion_prediction: Callable[[dict, float], tuple | None] | None = None
        self.following_observer: Callable[[dict], None] | None = None
        self.following_capture = None
        self.last_pulse_feedback_stop = None
        self.localization_replay = LocalizationReplay()
        self.probe_durations: dict[str, float] = {}
        self.last_pulse_stationary = False
        self.pulse_limits: dict[str, float] = {}
        self.last_pulse_match: dict[str, Any] = {}
        self.direction_responses: dict[tuple[str, int], tuple[float, np.ndarray]] = {}
        self.stationary_durations: dict[tuple[str, int], float] = {}

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
            started = time.monotonic()
            completed = False
            try:
                frame = await self.scanner._reference_window()
                self.scanner.last_frame = frame
                completed = True
            finally:
                self._record_timing("localization_reference_refresh", started, completed=completed)
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
                            localizer_timing=result.get("diagnostics", {}).get("call_timing"),
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
                for direction in (-1, 1):
                    self.direction_responses.pop((axis, direction), None)
                    self.stationary_durations.pop((axis, direction), None)
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

    def _response_pixel_delta(self, before: np.ndarray, after: np.ndarray) -> float:
        return float(np.linalg.norm((np.tan(after) - np.tan(before))
            * np.array([self.localizer.lens["fx"], self.localizer.lens["fy"]])
            * ANALYSIS_WIDTH / self.localizer.lens["width"]))

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
        self.direction_responses.clear()
        self.stationary_durations.clear()
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

    def _motion_stop_reason(self, observer: MotionFrameObservations, axis: str, direction: int) -> str | None:
        """Interrupt an obsolete correction, never certify arrival or learn a gain."""
        if self.motion_feedback_target is None:
            return None
        if observer.closed:
            return "visual_feedback_capture_changed"
        # Validate the live target even while the first pose is still processing.
        ray, maximum_error = self.motion_feedback_target()
        record = observer.latest
        if record is None or record.get("command") != observer.command:
            return None
        if not 0 <= time.monotonic() - record.get("frame_received_monotonic", 0) <= 1:
            return "visual_feedback_expired"
        geometry = record.get("frame_geometry")
        if record.get("status") != "localized" or not isinstance(geometry, dict):
            return "visual_feedback_unavailable"
        if geometry.get("capture_evidence") != record.get("capture_evidence"):
            return "visual_feedback_binding_changed"
        measured = localized_axis_measurement(ray, self.localizer.lens, geometry)
        if measured is None:
            return "visual_feedback_target_unavailable"
        if self.following_observer is not None and record["capture_evidence"] != self.following_capture:
            self.following_capture = deepcopy(record["capture_evidence"])
            self.following_observer({"kind": "visual_following_progress", "arrival_verified": False,
                "subject_visibility_verified": False, "stop_confirmed": False,
                "capture_evidence": deepcopy(record["capture_evidence"]), "frame_geometry": deepcopy(geometry),
                "target_ray": np.asarray(ray).tolist(), "measurement": measured,
                "frame_received_monotonic": record["frame_received_monotonic"]})
        predicted = self.motion_prediction(geometry, .1) if self.following_seconds is not None and self.motion_prediction else None
        control_ray = predicted[0]["ray"] if predicted is not None else ray
        control_error = self._error(control_ray, geometry)
        predicted_shift = self._response_pixel_delta(self._error(ray, geometry), control_error)
        prediction_noise = max(4., 2 * geometry["validation_p95_pixels"] * ANALYSIS_WIDTH / geometry["analysis_width"])
        if measured["center_error_pixels"] <= maximum_error and (
            predicted is None or predicted_shift < prediction_noise
        ):
            return "visual_feedback_target_reached"
        response_rotation = self.response_rotations.get(axis)
        response = direction * self.response[axis] if axis in self.response else None
        rate = getattr(observer, "qualified_rate", None)
        if self.following_seconds is not None and rate is not None:
            origin = rate["observation"]
            # This is a direction anchor inside the same finite window, not a
            # fresh speed estimate. Current pose and view-angle checks still apply.
            if (0 <= time.monotonic() - origin["frame_received_monotonic"] <= MAXIMUM_FOLLOWING_WINDOW_SECONDS
                    and all(origin["capture_evidence"].get(key) == record["capture_evidence"].get(key)
                            for key in ("capture_instance", "generation"))):
                response, response_rotation = rate["rate"], np.asarray(origin["frame_geometry"]["rotation_matrix"])
        if response_rotation is not None:
            rotation = np.asarray(geometry["rotation_matrix"])
            angle = np.arccos(np.clip((np.trace(response_rotation.T @ rotation) - 1) / 2, -1, 1))
            if angle > np.radians(MAXIMUM_RESPONSE_VIEW_CHANGE_DEGREES):
                return "visual_feedback_response_changed"
        if response is not None and float(control_error @ response) >= 0:
            return "visual_feedback_direction_changed"
        return None

    def _velocity_feedback(self, observer: MotionFrameObservations, axis: str, direction: int, speed: float):
        previous = None
        previous_rate = None
        previous_rate_origin = None
        segment = None
        segment_velocity = None
        rate_valid_after = None

        def plan(context):
            nonlocal previous, previous_rate, previous_rate_origin, segment, segment_velocity, rate_valid_after
            if segment != context["accepted_monotonic"]:
                if "window_deadline_monotonic" not in context or segment_velocity != context["velocity"]:
                    previous = previous_rate = None
                    rate_valid_after = context["accepted_monotonic"] + (
                        context.get("observed_response_seconds", 0) if segment is not None else 0)
                segment = context["accepted_monotonic"]
                segment_velocity = dict(context["velocity"])
            record = observer.latest
            now = time.monotonic()
            if (record is None or observer.closed or record.get("status") != "localized"
                    or record["frame_received_monotonic"] < rate_valid_after
                    or not 0 <= now - record["frame_received_monotonic"] <= .35
                    or record is previous):
                return None
            geometry = record.get("frame_geometry")
            if not isinstance(geometry, dict) or geometry.get("capture_evidence") != record.get("capture_evidence"):
                return None
            before, previous = previous, record
            if before is None:
                return None
            interval = record["frame_received_monotonic"] - before["frame_received_monotonic"]
            if not .08 <= interval <= .5:
                previous_rate = None
                return None
            origin = before["frame_geometry"]
            fixed_ray = _rotation_basis(origin["rotation_matrix"])[:, 2]
            old_error, new_error = self._error(fixed_ray, origin), self._error(fixed_ray, geometry)
            rate = (new_error - old_error) / interval
            noise = max(4., 2 * max(item["validation_p95_pixels"] * ANALYSIS_WIDTH / item["analysis_width"]
                for item in (origin, geometry)))
            if self._response_pixel_delta(old_error, new_error) < noise:
                previous_rate = None
                return None
            last_rate, previous_rate = previous_rate, rate
            rate_origin, previous_rate_origin = previous_rate_origin, before["capture_evidence"]
            # Two agreeing intervals at the current commanded velocity qualify
            # a local rate estimate. Finite-pulse gains never stand in for it.
            qualified_rate = bool(last_rate is not None
                and rate @ last_rate >= .9 * np.linalg.norm(rate) * np.linalg.norm(last_rate)
                and .5 <= np.linalg.norm(rate) / max(np.linalg.norm(last_rate), 1e-8) <= 2)
            if qualified_rate:
                observer.qualified_rate = {"rate": rate.copy(), "observation": record}
            ray, maximum_error = self.motion_feedback_target()
            error = self._error(ray, geometry)
            remaining = context["deadline_monotonic"] - now
            renewal = "window_deadline_monotonic" in context
            duration = min(context["maximum_duration_seconds"], context["window_deadline_monotonic"] - now - .2) if renewal else remaining
            if renewal and remaining >= .1:
                remaining = duration
            if remaining < .2:
                return None
            continuation = not qualified_rate
            current_speed = abs(context["velocity"][axis])
            if continuation:
                response = self.response.get(axis)
                # Acceleration may consume the first pulse before a steady rate
                # is measurable. Continue only its already-qualified direction
                # and speed, with another full correction still ahead.
                if (not renewal or self.following_seconds is None or current_speed != speed
                        or (axis, direction) not in self.direction_responses or response is None
                        or rate @ (direction * response) < .9 * np.linalg.norm(rate) * np.linalg.norm(response)
                        or self._motion_stop_reason(observer, axis, direction) is not None):
                    return None
                _, amount = self._correction_plan([axis], error, False)
                if direction * amount < duration or float(error @ rate) >= 0:
                    return None
                proposed = current_speed
            else:
                target_rate = np.zeros(2)
                prediction = self.motion_prediction(geometry, interval) if renewal and self.motion_prediction else None
                if prediction is not None:
                    forecast = prediction[0]
                    target_rate = (self._error(forecast["ray"], geometry) - error) / forecast["horizon_seconds"]
                # Tracking responds on the observed feedback cadence, not on the
                # duration of a stopped arrival. World-target motion is separate.
                correction_seconds = max(.2, 2 * interval, 2 * context.get("observed_response_seconds", 0)) if renewal else remaining
                desired_rate = -target_rate - .8 * error / correction_seconds
                factor = float(desired_rate @ rate) / float(rate @ rate)
                if factor <= 0:
                    return None
                proposed = float(np.clip(current_speed * factor, .025, speed))
            velocity = {"pan": 0., "tilt": 0., axis: direction * proposed}
            if abs(proposed - current_speed) < .01:
                if (not renewal or context["deadline_monotonic"] - now > duration * .6
                        or context["deadline_monotonic"] - now >= duration - .1):
                    return None
                velocity[axis] = direction * current_speed
            if not _exact_velocity_supported(self.scanner.capabilities, velocity):
                return None
            capture = deepcopy(record["capture_evidence"])
            def guard():
                if observer.closed or not 0 <= time.monotonic() - record["frame_received_monotonic"] <= .35:
                    raise PanoramaCaptureError("visual_feedback_expired")
                reason = self._motion_stop_reason(observer, axis, direction)
                if reason is not None:
                    raise PanoramaCaptureError(reason)
                latest, _ = self.motion_feedback_target()
                shift = self._response_pixel_delta(error, self._error(latest, geometry))
                if shift > maximum_error / 2:
                    raise PanoramaCaptureError("visual_feedback_target_changed")
                if continuation:
                    _, amount = self._correction_plan([axis], self._error(latest, geometry), False)
                    if direction * amount < duration:
                        raise PanoramaCaptureError("visual_feedback_target_changed")
            def reserve(entry):
                if self.commands >= self.maximum_commands:
                    raise PanoramaCaptureError("visual_navigation_budget_exhausted")
                self.commands += 1
                entry["navigation_command"] = self.commands
                self.trace[-1].setdefault("velocity_updates", []).append(entry)
            return {"velocity": velocity,
                **({"duration_seconds": duration} if renewal else {}),
                "measurement": ({"kind": "visual_direction_continuation", "timing_basis": "local_frame_receipt",
                    "interval_seconds": interval, "displacement_pixels": self._response_pixel_delta(old_error, new_error),
                    "planned_amount": amount, "capture_evidence": [deepcopy(before["capture_evidence"]), capture]}
                    if continuation else {"kind": "visual_rate_estimate", "timing_basis": "local_frame_receipt",
                    "rate_radians_per_second": rate.tolist(), "comparison_rate_radians_per_second": last_rate.tolist(),
                    "target_rate_radians_per_second": target_rate.tolist(), "correction_seconds": correction_seconds,
                    "interval_seconds": interval, "capture_evidence": [deepcopy(rate_origin),
                        deepcopy(before["capture_evidence"]), capture]}),
                "capture_evidence": capture, "frame_received_monotonic": record["frame_received_monotonic"],
                "dispatch_guard": guard, "reserve": reserve}
        return plan

    async def _pulse(self, axis: str, amount: float, *, speed: float = DEFAULT_CONTINUOUS_PULSE_SPEED) -> None:
        started = time.monotonic()
        if self.commands >= self.maximum_commands:
            raise PanoramaCaptureError("visual_navigation_budget_exhausted")
        self.scanner._check()
        if (
            not self.scanner.last_frame
            or not 0 <= time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) <= 1
        ):
            if (self.scanner.last_frame
                    and time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) > 1):
                raise _ExpiredQualifiedFrame()
            raise PanoramaCaptureError("panorama_frame_not_recent")
        self.commands += 1
        self.last_pulse_stationary = False
        self.last_pulse_feedback_stop = None
        self.last_pulse_match = {}
        self.trace.append({
            **self.scanner.checkpoint.get("navigation_step", {}),
            "axis": axis, "amount": amount, "speed": speed, "state": "pending",
        })
        self.scanner.checkpoint["navigation_commands"] = self.trace
        await self.scanner._persist()
        motion_observations = None
        if self.dispatch_guard is not None and self.localizer is not None:
            motion_observations = MotionFrameObservations(self.localizer,
                self.scanner.checkpoint.setdefault("navigation_motion_observations", []), self.commands)
        previous_guard = None
        if self.dispatch_guard is not None:
            previous_guard = self.scanner.camera.dispatch_guard
            self.scanner.camera.dispatch_guard = self.dispatch_guard
        try:
            if time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) > 1:
                raise _ExpiredQualifiedFrame()
            capabilities = self.scanner.capabilities
            if capabilities.get("velocity_supported") or capabilities.get("relative_supported"):
                options = {"speed": speed} if speed != DEFAULT_CONTINUOUS_PULSE_SPEED else {}
                if self.observation_guard is not None:
                    options["observation_guard"] = self.observation_guard
                if motion_observations is not None:
                    options["frame_observer"] = motion_observations.offer
                    if self.motion_feedback_target is not None:
                        options["motion_stop_requested"] = lambda: self._motion_stop_reason(
                            motion_observations, axis, 1 if amount > 0 else -1)
                        if capabilities.get("velocity_supported"):
                            options["motion_velocity_update"] = self._velocity_feedback(
                                motion_observations, axis, 1 if amount > 0 else -1, speed)
                            if self.following_seconds is not None:
                                options["following_seconds"] = self.following_seconds
                transition_estimator = getattr(self.localizer, "calibrated_transition", None)
                if callable(transition_estimator):
                    options["transition_estimator"] = transition_estimator
                if abs(amount) > MAXIMUM_NAVIGATION_PULSE_SECONDS or speed > DEFAULT_CONTINUOUS_PULSE_SPEED:
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
                result = await self.scanner._absolute(target, allow_stationary=True,
                    **({"observation_guard": self.observation_guard} if self.observation_guard else {}))
            else:
                raise PanoramaCaptureError("visual_control_unavailable")
        except asyncio.CancelledError:
            self.trace[-1].update(state="interrupted", failure_code="capture_cancelled")
            raise
        except PanoramaCaptureError as error:
            self.trace[-1].update(state="rejected_before_transport" if isinstance(error, (_TargetDispatchChanged, _ExpiredQualifiedFrame)) else "failed",
                failure_code=error.code)
            if isinstance(error, _TargetDispatchChanged):
                self.trace[-1]["dispatch_rejection"] = error.details
            raise
        finally:
            if self.dispatch_guard is not None:
                self.scanner.camera.dispatch_guard = previous_guard
            if motion_observations is not None:
                await motion_observations.close()
        self.scanner.last_frame = result.get("observation_frame", result["frame"])
        self.scanner.last_pose = result.get("pose", {})
        updates = result.get("motion_velocity_updates", [])
        self.trace[-1]["velocity_updates"] = updates
        self.last_pulse_feedback_stop = result.get("motion_feedback_stop") or (
            "visual_feedback_velocity_updated" if updates else None)
        self.trace[-1]["motion_feedback_stop"] = self.last_pulse_feedback_stop
        if (result.get("stable") is True and self.scanner.physical_state == "stopped"
                and speed >= DEFAULT_CONTINUOUS_PULSE_SPEED):
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

    async def qualify_velocity(self, axis: str, direction: int, speed: float,
                               duration_seconds: float = DEFAULT_NAVIGATION_PROBE_SECONDS) -> dict[str, Any]:
        """Measure one short probe without replacing the default-speed model."""
        if (axis not in {"pan", "tilt"} or isinstance(direction, bool) or direction not in {-1, 1}
                or not isinstance(speed, (int, float)) or isinstance(speed, bool)
                or not DEFAULT_CONTINUOUS_PULSE_SPEED < speed <= .2
                or not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool)
                or not DEFAULT_NAVIGATION_PROBE_SECONDS <= duration_seconds <= .24):
            raise PanoramaCaptureError("invalid_velocity")
        if self.scanner.physical_state != "stopped":
            raise PanoramaCaptureError("stop_unconfirmed")
        located = await self.locate()
        target = _rotation_basis(located["rotation_matrix"])[:, 2]
        before = self._error(target, located)
        amount = direction * duration_seconds
        self.scanner.checkpoint["navigation_step"] = {"kind": "velocity_qualification",
            "response_qualified": False, "axis": axis, "speed": speed, "direction": direction}
        await self._pulse(axis, amount, speed=speed)
        after = await self.locate()
        updated = self._error(target, after)
        delta = updated - before
        pixel_delta = self._response_pixel_delta(before, updated)
        displacement = _matched_target_displacement(self.last_pulse_match, target, self.localizer.lens, located)
        qualified = bool(self.scanner.physical_state == "stopped"
            and not self.last_pulse_stationary and np.isfinite(delta).all()
            and np.linalg.norm(delta / amount) >= .01
            and pixel_delta >= MINIMUM_QUALIFIED_RESPONSE_PIXELS
            and self.last_pulse_match.get("overlap", 0) >= .85
            and displacement is not None and displacement >= MINIMUM_QUALIFIED_RESPONSE_PIXELS)
        result = {"axis": axis, "direction": direction, "speed": speed,
            "duration_seconds": abs(amount), "response": (delta / amount).tolist(),
            "rotation_matrix": np.asarray(after["rotation_matrix"]).tolist(),
            "displacement_pixels": displacement, "response_pixel_delta": pixel_delta, "response_qualified": qualified,
            "capture_evidence": deepcopy(self.scanner.last_frame.get("capture_evidence", {}))}
        self.trace[-1].update(result)
        if not qualified:
            self.trace[-1]["failure_code"] = "visual_response_unavailable"
        await self.scanner._persist()
        if not qualified:
            raise PanoramaCaptureError("visual_response_unavailable")
        return result

    async def _target_measurement(self, target: np.ndarray, located: dict[str, Any]) -> dict[str, Any] | None:
        started = time.monotonic()
        result = None
        completed = False
        try:
            measured = await asyncio.to_thread(
                self.localizer.measure_target, self.scanner.last_frame["image"], located, target
            )
            result = measured or localized_axis_measurement(target, self.localizer.lens, located)
            completed = True
        finally:
            self._record_timing("target_measurement", started, completed=completed,
                                method=result.get("method") if result else None)
        if not 0 <= time.monotonic() - self.scanner.last_frame.get("received_monotonic", 0) <= 1:
            raise _ExpiredQualifiedFrame()
        return result

    def _probe_plan(self, target, located, axes, error, near):
        axis = max(axes, key=lambda name: abs(error[0 if name == "pan" else 1]))
        direction = reference_probe_direction(
            axis, np.asarray(target), np.asarray(located["rotation_matrix"]),
            getattr(self.localizer, "probe_observations", []),
        )
        amount = self.probe_durations.get(axis, DEFAULT_NAVIGATION_PROBE_SECONDS) * (direction if direction is not None else 1)
        from .panorama_approach import approach_pulse
        approximation = getattr(self.localizer, "approach", {})
        predicted = (approach_pulse(approximation, axis, np.asarray(target), np.asarray(located["rotation_matrix"]))
                     if approximation.get("status") == "qualified" else None)
        if predicted is not None and not near:
            amount = predicted
        return axis, amount, direction, predicted

    def _movement_axes(self, measured, error, final, maximum_error_pixels):
        axes = [
            axis
            for axis in ("pan", "tilt")
            if self.scanner.capabilities.get("axes", {}).get(axis) is True
        ]
        if final and measured and axes and len(measured.get("error_pixels", [])) == 2:
            component_limit = maximum_error_pixels / np.sqrt(2)
            needed = [
                axis
                for index, axis in enumerate(("pan", "tilt"))
                if axis in axes and abs(float(measured["error_pixels"][index])) > component_limit
            ]
            if not needed and measured["center_error_pixels"] > maximum_error_pixels:
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
        return axes, missing, deferred_axes

    def _correction_plan(self, axes, error, fine_control):
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
        return axis, amount

    def _predict_target(self, motion, located, maximum_error_pixels, seconds):
        candidate = motion.predict(time.monotonic(), seconds)
        coverage = getattr(self.localizer, "coverage_mask", None)
        if candidate is None or getattr(coverage, "ndim", None) != 2 or not coverage.size:
            return None
        estimates = []
        for key in ("ray", "comparison_ray"):
            u, v = ray_to_panorama_pixel(candidate[key])
            if (not coverage[min(coverage.shape[0] - 1, int(v * coverage.shape[0])),
                    min(coverage.shape[1] - 1, int(u * coverage.shape[1]))]
                    or self.localizer.target_reference(candidate[key]) is None):
                return None
            estimate = localized_axis_measurement(candidate[key], self.localizer.lens, located)
            if estimate is None:
                return None
            estimates.append(estimate)
        uncertainty = float(np.linalg.norm(np.asarray(estimates[0]["error_pixels"]) - estimates[1]["error_pixels"]))
        if uncertainty > maximum_error_pixels / 2:
            return None
        return {**candidate, "velocity_disagreement_pixels": uncertainty}, estimates[0]

    async def aim(
        self, ray: Any, *, maximum_error_pixels: float = MAXIMUM_LIVE_CENTER_ERROR_PIXELS,
        target_provider: Callable[[], list[float]] | None = None,
        target_observation_provider: Callable[[], dict | None] | None = None,
        following_observer: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]:
        if type(maximum_error_pixels) not in (int, float) or not 0 < maximum_error_pixels <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS:
            raise PanoramaCaptureError("invalid_arrival_limit")
        self.following_observer = following_observer
        self.following_capture = None
        motion = TargetMotion()

        def read_target():
            updated = target_provider()
            if target_observation_provider is not None:
                motion.observe(target_observation_provider(), updated, time.monotonic())
            return updated

        located = await self.locate()
        if target_provider is not None:
            # Localization can outlast the destination; route only the latest qualified observation.
            ray = np.asarray(read_target())
        reference = self.localizer.target_reference(ray)
        if reference is None:
            raise PanoramaCaptureError("visual_target_unreachable")
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
        overshoot_recovered = False
        self.scanner.checkpoint["navigation_target_observations"] = observations
        cycle_seconds = 0.

        def target_is_current() -> bool:
            if target_provider is None or np.allclose(read_target(), ray, atol=1e-7, rtol=0):
                return True
            self.scanner.checkpoint["navigation_target_replans"] = self.scanner.checkpoint.get("navigation_target_replans", 0) + 1
            return False

        while waypoints:
            located = await self.locate()
            if target_provider is not None:
                updated = np.asarray(read_target())
                if not np.allclose(updated, ray, atol=1e-7, rtol=0):
                    ray = updated
                    reference = self.localizer.target_reference(ray)
                    if reference is None:
                        raise PanoramaCaptureError("visual_target_unreachable")
                    waypoints = []
                    if ray_to_image_pixel(ray, self.localizer.lens, rotation_matrix=located["rotation_matrix"]) is None:
                        route = reference_path(self.localizer, located["reference_id"], reference["id"])
                        if route is None:
                            raise PanoramaCaptureError("visual_route_unavailable")
                        indexed = {item["id"]: item for item in self.localizer.references}
                        waypoints = [_rotation_basis(indexed[item]["rotation_matrix"])[:, 2] for item in route[1:]]
                    waypoints.append(ray)
                    last_error = None
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
            measured_frame = self.scanner.last_frame
            measured_capture = deepcopy((measured_frame or {}).get("capture_evidence", {}))
            measurement_started = time.monotonic()
            try:
                measured = await self._target_measurement(target, located) if final else None
                if final and target_provider is not None:
                    updated = np.asarray(read_target())
                    if not np.allclose(updated, ray, atol=1e-7, rtol=0):
                        rebased = localized_axis_measurement(updated, self.localizer.lens, located)
                        same_frame = (measured_frame is self.scanner.last_frame and measured_capture
                            and measured_capture == self.scanner.last_frame.get("capture_evidence"))
                        if (rebased is not None and same_frame
                                and getattr(self.scanner, "physical_state", None) == "stopped"):
                            if not 0 <= time.monotonic() - measured_frame.get("received_monotonic", 0) <= 1:
                                raise _ExpiredQualifiedFrame()
                            if self.localizer.target_reference(updated) is None:
                                raise PanoramaCaptureError("visual_target_unreachable")
                            # The same qualified camera pose can project a newer subject direction without matching the old patch again.
                            ray = target = updated
                            waypoints = [updated]
                            measured = rebased
                            measured["target_update"] = "projected_with_same_stopped_frame"
                            error = self._error(target, located)
                            last_error = None
                            self.scanner.checkpoint["navigation_target_rebases"] = self.scanner.checkpoint.get("navigation_target_rebases", 0) + 1
            except _ExpiredQualifiedFrame as error:
                # Measurement may consume the last milliseconds of a valid pose.
                # Reobserve and replan while stopped, never dispatch its old pulse
                # or accept its old arrival. Bound this separately from motion.
                expired_measurements += 1
                if expired_measurements > 2:
                    raise
                self.scanner.last_frame = await self._renew_localization_frame(error)
                continue
            measurement_seconds = time.monotonic() - measurement_started
            if final:
                observations.append({
                    "commands": self.commands,
                    "target_ray": np.asarray(target).tolist(),
                    "capture_evidence": self.scanner.last_frame.get("capture_evidence", {}),
                    "measurement": measured,
                })
            if (
                final
                and measured
                and measured["center_error_pixels"] <= maximum_error_pixels
            ):
                if not target_is_current():
                    continue
                final_measurement = measured
                final_geometry = arrival_frame_geometry(measured_frame, located, self.localizer.lens, ray)
                break
            if not final and not intermediate and np.linalg.norm(error) < 0.08:
                waypoints.pop(0)
                last_error = None
                continue
            prediction = None
            supported_axes = [axis for axis in ("pan", "tilt") if self.scanner.capabilities.get("axes", {}).get(axis) is True]
            if final and cycle_seconds > 0 and supported_axes and all(axis in self.response for axis in supported_axes):
                # Arrival also waits for target measurement after the motor/localization cycle.
                forecast = self._predict_target(motion, located, maximum_error_pixels, cycle_seconds + measurement_seconds)
                if forecast is not None:
                    candidate, measured = forecast
                    prediction = {**candidate, "motion_cycle_seconds": cycle_seconds, "measurement_seconds": measurement_seconds}
                    target = np.asarray(candidate["ray"])
                    error = self._error(target, located)
                    self.scanner.checkpoint["navigation_target_predictions"] = self.scanner.checkpoint.get("navigation_target_predictions", 0) + 1
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
            axes, missing, deferred_axes = self._movement_axes(measured, error, final, maximum_error_pixels)
            fine_control = bool(final and measured
                and measured["center_error_pixels"] <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS * 4
                and self.scanner.capabilities.get("velocity_supported"))
            if missing:
                axis, amount, probe_direction, predicted = self._probe_plan(target, located, missing, error, near)
            else:
                axis, amount = self._correction_plan(axes, error, fine_control)
            pulse_amount, pulse_speed = amount, DEFAULT_CONTINUOUS_PULSE_SPEED
            direction_key = (axis, 1 if amount > 0 else -1)
            directional = self.direction_responses.get(direction_key)
            direction_probe = bool(not missing and directional is None
                and (any(key[0] == axis for key in self.direction_responses)
                     or direction_key in self.stationary_durations)
                and any(key[0] == axis for key in self.stationary_durations))
            fine_response_qualified = (fine_control and axis in self.response
                and _fine_correction_has_measured_response(axis, amount, error, target, self.trace))
            if direction_probe:
                # A reversal has its own startup response; never reuse the other
                # direction's gain for an unobserved long correction.
                pulse_amount = float(np.copysign(
                    min(.48, max(DEFAULT_NAVIGATION_PROBE_SECONDS,
                        self.stationary_durations.get(direction_key, 0) * 2)), amount))
                amount = pulse_amount
            elif directional is not None and direction_key in self.stationary_durations:
                observed_duration, observed_delta = directional
                stationary_duration = self.stationary_durations.get(direction_key, 0)
                if observed_duration <= stationary_duration:
                    raise PanoramaCaptureError("visual_response_unavailable")
                fraction = -.8 * float(error @ observed_delta) / float(observed_delta @ observed_delta)
                if fraction <= 0:
                    raise PanoramaCaptureError("visual_control_resolution_unverified")
                duration = stationary_duration + fraction * (observed_duration - stationary_duration)
                # Identification stays capped at .48 s; measured travel uses the existing overlap-qualified limit.
                limit = self.pulse_limits.get(axis, MAXIMUM_NAVIGATION_PULSE_SECONDS)
                duration = float(np.clip(duration, MINIMUM_CONTINUOUS_PULSE_SECONDS,
                    min(.48, limit) if fine_control else limit))
                pulse_amount = amount = float(np.copysign(duration, amount))
            elif (fine_control and axis in self.response
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
            if not target_is_current():
                continue
            if near:
                fine += 1
            before = self._error(target, located)
            self.scanner.checkpoint["navigation_step"] = {
                "command": self.commands + 1,
                "target_kind": "final" if final else "intermediate",
                "target_ray": np.asarray(target).tolist(),
                "observed_target_ray": np.asarray(ray).tolist(),
                "prediction": prediction,
                "before_error_radians": before.tolist(),
                "planning_error_radians": error.tolist(),
                "planning_response": self.response[axis].tolist() if axis in self.response else None,
                "fine_control": fine_control,
                "missing_response_axes": missing,
                "deferred_response_axes": deferred_axes,
                "kind": "probe" if missing or direction_probe else "correction",
                "axis": axis,
                "amount": pulse_amount,
                "speed": pulse_speed,
                "fine_response_qualified": fine_response_qualified,
                "reference_probe_direction": probe_direction if missing else None,
                "automatic_approach": bool(missing and predicted is not None and not near),
                "response_direction": direction_key[1],
                "stationary_duration_seconds": self.stationary_durations.get(direction_key),
            }
            planned_frame_received = measured_frame.get("received_monotonic", 0)
            def guard_target_dispatch():
                updated = np.asarray(read_target())
                frame_age = time.monotonic() - planned_frame_received
                if not 0 <= frame_age <= 1:
                    raise _TargetDispatchChanged("frame_expired", frame_age)
                if not np.allclose(updated, ray, atol=1e-7, rtol=0):
                    old = localized_axis_measurement(ray, self.localizer.lens, located)
                    new = localized_axis_measurement(updated, self.localizer.lens, located)
                    if old is None or new is None:
                        raise _TargetDispatchChanged("target_projection_unavailable", frame_age)
                    shift = float(np.linalg.norm(np.asarray(new["error_pixels"]) - old["error_pixels"]))
                    if shift > maximum_error_pixels / 2:
                        # Recompute through the same planner; a changed target is safe only for an identical coarse command.
                        if (final and not near and not fine_control and prediction is None
                                and not self.stationary_durations
                                and new["center_error_pixels"] > MAXIMUM_LIVE_CENTER_ERROR_PIXELS * 4
                                and self.localizer.target_reference(updated) is not None):
                            updated_error = self._error(updated, located)
                            replanned = None
                            if np.linalg.norm(updated_error) >= .04:
                                updated_axes, updated_missing, _ = self._movement_axes(
                                    new, updated_error, True, maximum_error_pixels)
                                if not missing and not updated_missing:
                                    replanned = self._correction_plan(updated_axes, updated_error, False)
                                elif (missing and updated_missing and not self.response
                                        and not self.direction_responses and predicted is None
                                        and probe_direction is not None):
                                    new_axis, new_amount, new_direction, new_approach = self._probe_plan(
                                        updated, located, updated_missing, updated_error, False)
                                    if new_direction == probe_direction and new_approach is None:
                                        replanned = new_axis, new_amount
                            if (replanned == (axis, pulse_amount)
                                    and pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED
                                    and 0 <= time.monotonic() - planned_frame_received <= 1):
                                self.scanner.checkpoint["navigation_step"]["dispatch_replan"] = {
                                    "target_ray": updated.tolist(), "target_shift_pixels": shift,
                                    "frame_age_seconds": frame_age, "same_command": True}
                                if self.trace:
                                    self.trace[-1]["dispatch_replan"] = self.scanner.checkpoint["navigation_step"]["dispatch_replan"]
                                return
                        raise _TargetDispatchChanged("target_shifted", frame_age, shift)
            self.dispatch_guard = guard_target_dispatch if target_provider is not None else None
            # Independent of response qualification, transport and pulse speed.
            self.observation_guard = (lambda: read_target()) if target_provider is not None else None
            self.motion_feedback_target = (lambda: (read_target(), maximum_error_pixels)) if (
                target_provider is not None and final and not missing and not direction_probe
                and pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED) else None
            # Start only from a full, overlap-qualified correction that already
            # leaves time to measure rate. Probes and fine positioning stay finite.
            self.following_seconds = MAXIMUM_FOLLOWING_WINDOW_SECONDS if (
                self.motion_feedback_target is not None and not fine_control
                and self.scanner.capabilities.get("velocity_supported")
                and directional is not None and 1 <= abs(pulse_amount) <= self.pulse_limits.get(axis, 0)
            ) else None
            self.motion_prediction = lambda geometry, seconds: self._predict_target(
                motion, geometry, maximum_error_pixels, seconds)
            pulse_started = time.monotonic()
            try:
                if pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED:
                    await self._pulse(axis, pulse_amount)
                else:
                    await self._pulse(axis, pulse_amount, speed=pulse_speed)
            except _ExpiredQualifiedFrame as error:
                if near:
                    fine -= 1
                expired_measurements += 1
                if expired_measurements > 2:
                    raise
                self.scanner.last_frame = await self._renew_localization_frame(error)
                continue
            except _TargetDispatchChanged:
                if near:
                    fine -= 1
                self.scanner.checkpoint["navigation_target_replans"] = self.scanner.checkpoint.get("navigation_target_replans", 0) + 1
                continue
            finally:
                self.dispatch_guard = None
                self.observation_guard = None
                self.motion_feedback_target = None
                self.following_seconds = None
                self.motion_prediction = None
            if self.last_pulse_feedback_stop:
                # The command ended before its planned duration. Its endpoint
                # cannot qualify the full-pulse gain or a stationary boundary.
                self.trace[-1]["response_qualified"] = False
                self.trace[-1]["stationary_verified"] = False
                last_error = None
                continue
            self.trace[-1]["response_equivalent_amount"] = amount
            expired_measurements = 0
            after = await self.locate()
            cycle_seconds = time.monotonic() - pulse_started
            updated = self._error(target, after)
            response = (updated - before) / amount
            pixel_delta = self._response_pixel_delta(before, updated)
            self.trace[-1].update(
                observed_error_radians=updated.tolist(), response=response.tolist(),
                response_pixel_delta=float(pixel_delta),
                motion_match={key: self.last_pulse_match.get(key)
                    for key in ("verified", "displacement", "overlap", "analysis_size", "homography", "support_scope")},
            )
            if pulse_speed != DEFAULT_CONTINUOUS_PULSE_SPEED:
                # Match the return controller's check before reusing a scaled velocity model.
                expected_delta = self.response[axis] * amount
                observed_delta = updated - before
                ratio = float(np.linalg.norm(observed_delta) / max(np.linalg.norm(expected_delta), 1e-6))
                self.trace[-1].update(response_ratio=ratio, response=response.tolist(), response_qualified=False)
                if np.dot(observed_delta, expected_delta) <= 0 or not 0.4 <= ratio <= 2.5:
                    if final and prediction is None and getattr(self.scanner, "physical_state", None) == "stopped":
                        measured = await self._target_measurement(target, after)
                        if measured and measured["center_error_pixels"] <= maximum_error_pixels and target_is_current():
                            # Pixels can prove arrival even when the actuator model failed; never retain that model.
                            self.response.pop(axis, None)
                            self.response_rotations.pop(axis, None)
                            self.pulse_limits.pop(axis, None)
                            for direction in (-1, 1):
                                self.direction_responses.pop((axis, direction), None)
                                self.stationary_durations.pop((axis, direction), None)
                            self.trace[-1]["arrival_after_unmodeled_velocity"] = True
                            final_measurement = measured
                            final_geometry = arrival_frame_geometry(self.scanner.last_frame, after, self.localizer.lens, ray)
                            break
                    self.trace[-1]["failure_code"] = "visual_response_inconsistent"
                    raise PanoramaCaptureError("visual_response_inconsistent")
            if (self.last_pulse_stationary or np.linalg.norm(response) < 0.01
                    or pixel_delta < MINIMUM_QUALIFIED_RESPONSE_PIXELS):
                self.trace[-1]["response_qualified"] = False
                if final:
                    try:
                        measured = await self._target_measurement(target, after)
                    except _ExpiredQualifiedFrame as error:
                        # A no-effect window can consume the observation's age
                        # budget. Renew while stopped, never repeat its pulse.
                        self.scanner.last_frame = await self._renew_localization_frame(error)
                        after = await self.locate()
                        measured = await self._target_measurement(target, after)
                    observations.append({
                        "commands": self.commands,
                        "capture_evidence": self.scanner.last_frame.get("capture_evidence", {}),
                        "measurement": measured,
                    })
                    if (
                        measured
                        and measured["center_error_pixels"] <= maximum_error_pixels
                        and prediction is None
                    ):
                        if not target_is_current():
                            continue
                        final_measurement = measured
                        final_geometry = arrival_frame_geometry(self.scanner.last_frame, after, self.localizer.lens, ray)
                        break
                if (not self.last_pulse_stationary and axis in self.response
                        and pixel_delta < MINIMUM_QUALIFIED_RESPONSE_PIXELS):
                    # Subpixel drift must not replace a measured gain and amplify the next pulse.
                    continue
                matched_displacement = self.last_pulse_match.get("displacement", float("inf"))
                if matched_displacement >= MINIMUM_QUALIFIED_RESPONSE_PIXELS:
                    target_displacement = _matched_target_displacement(
                        self.last_pulse_match, target, self.localizer.lens, located)
                    self.trace[-1]["matched_target_displacement_pixels"] = target_displacement
                    if target_displacement is not None:
                        matched_displacement = target_displacement
                if (axis in missing and abs(amount) < .48
                        and self.scanner.capabilities.get("velocity_supported")
                        and getattr(self.scanner, "physical_state", None) == "stopped"
                        and self.last_pulse_match.get("verified") is True
                        and self.last_pulse_match.get("overlap", 0) >= .85
                        and 0 < matched_displacement < MINIMUM_QUALIFIED_RESPONSE_PIXELS
                        and 0 < pixel_delta < MINIMUM_QUALIFIED_RESPONSE_PIXELS):
                    # A causal, stopped small effect can extend identification, not qualify a gain.
                    self.probe_durations[axis] = min(.48, abs(amount) * 2)
                    self.trace[-1].update(next_probe_seconds=self.probe_durations[axis],
                                         small_effect_observed=True)
                    continue
                if (self.last_pulse_stationary and pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED
                        and self.scanner.capabilities.get("velocity_supported")):
                    self.stationary_durations[direction_key] = max(
                        self.stationary_durations.get(direction_key, 0), abs(pulse_amount))
                    if directional is not None and directional[0] > abs(pulse_amount):
                        continue
                    if abs(pulse_amount) < .48 and any(key[0] == axis for key in self.direction_responses):
                        # A verified dead zone needs identification in this direction, even if only the opposite was measured.
                        self.direction_responses.pop(direction_key, None)
                        continue
                if ((axis in missing or direction_probe) and self.last_pulse_stationary
                        and abs(amount) < 0.48
                        and self.scanner.capabilities.get("velocity_supported")):
                    # Some axes start after 0.24 s. Each increase needs a new
                    # causal no-effect observation; never infer a motor gain.
                    self.probe_durations[axis] = min(0.48, abs(amount) * 2)
                    self.trace[-1]["next_probe_seconds"] = self.probe_durations[axis]
                    continue
                raise PanoramaCaptureError("visual_response_unavailable")
            # A device need not scale motion linearly with requested velocity.
            if pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED:
                self.response[axis] = response
                self.direction_responses[direction_key] = (abs(pulse_amount), updated - before)
                self.response_rotations[axis] = np.asarray(after["rotation_matrix"])
                self.pulse_limits[axis] = _qualified_navigation_pulse_limit(pulse_amount, self.last_pulse_match)
            magnitude = float(np.linalg.norm(updated))
            self.trace[-1].update(error_radians=magnitude, response=response.tolist(),
                                 response_qualified=pulse_speed == DEFAULT_CONTINUOUS_PULSE_SPEED,
                                 next_pulse_limit=self.pulse_limits.get(axis, MAXIMUM_NAVIGATION_PULSE_SECONDS))
            if not missing and not direction_probe and last_error is not None and magnitude > last_error * 1.2:
                index = 0 if axis == "pan" else 1
                observed_pixels = np.linalg.norm(np.tan(updated)
                    * np.array([self.localizer.lens["fx"], self.localizer.lens["fy"]])
                    * ANALYSIS_WIDTH / self.localizer.lens["width"])
                if (overshoot_recovered or not final or before[index] * updated[index] >= 0
                        or observed_pixels > MAXIMUM_LIVE_CENTER_ERROR_PIXELS):
                    raise PanoramaCaptureError("visual_correction_diverged")
                # One observed crossing inside the arrival envelope can be corrected; oscillation cannot.
                overshoot_recovered = True
                self.trace[-1]["bounded_overshoot_recovery"] = True
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
            "frame_geometry": final_geometry,
            "commands": self.commands,
            "target_ray": np.asarray(ray).tolist(),
        }


async def correct_reference(scanner: _Scan, reference: np.ndarray, result: dict, *, maximum_error_pixels: float = 3) -> dict:
    """Use the remaining shared return budget for visually observed pulses."""
    if type(maximum_error_pixels) not in (int, float) or not 0 < maximum_error_pixels <= MAXIMUM_LIVE_CENTER_ERROR_PIXELS:
        raise PanoramaCaptureError("invalid_target")
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
        if comparison_displacement <= maximum_error_pixels:
            terminal_state = "complete"
            break
        if not model_loaded:
            small_probe = (trace[-1] if trace and _small_return_probe(trace[-1], return_epoch)
                and sum(_small_return_probe(item, return_epoch) for item in trace) == 1
                and getattr(scanner, "physical_state", None) == "stopped"
                and np.linalg.norm(np.asarray(trace[-1]["after_shift"]) - np.asarray(measurement[2:])) <= 1
                else None)
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
            if previous_state in {"failed", "rejected", "resume_unavailable"} and not (
                    previous_state == "rejected" and small_probe is not None):
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
                if amount == 0:
                    trace.append({
                        "axis": axis, "state": "rejected", "verified": False,
                        "reason": "visual_control_resolution_unverified",
                        "outbound_seed": outbound_seed, "return_epoch": return_epoch,
                    })
                    terminal_state = "rejected"
                    if callable(persist):
                        await persist()
                    break
                command_kind = (
                    "outbound_inverse_probe"
                    if np.sign(amount) != outbound_seed["outbound_direction"]
                    else "outbound_direction_probe"
                )
            else:
                axis = max(
                    missing,
                    key=lambda name: abs(residual[0 if name == "pan" else 1]),
                )
                amount = 0.08
                command_kind = "probe"
        if (trace and _small_return_probe(trace[-1], return_epoch)
                and sum(_small_return_probe(item, return_epoch) for item in trace) == 1
                and trace[-1]["axis"] in missing):
            axis, amount, command_kind = trace[-1]["axis"], trace[-1]["amount"], "probe"
        if axis is None:
            break
        direction = 1 if amount > 0 else -1
        measured_direction = axis in responses and response_directions[axis] == direction
        if not measured_direction:
            # Reversing an axis requires its own short observation; do not apply
            # the gain measured in the other direction to a larger correction.
            amount = direction * 0.08
            if command_kind not in {"outbound_inverse_probe", "outbound_direction_probe"}:
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
            if (_small_return_probe(entry, return_epoch)
                    and getattr(scanner, "physical_state", None) == "stopped"
                    and sum(_small_return_probe(item, return_epoch) for item in trace) == 1):
                # Repeat the same bounded identification pulse once; do not extrapolate its tiny effect.
                continue
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
        if observed_displacement <= maximum_error_pixels:
            terminal_state = "complete"
            break
    else:
        if remaining_corrections:
            terminal_state = "budget_exhausted"
    scanner.checkpoint["return_correction_state"] = terminal_state
    replay = getattr(scanner, "_last_return_replay", None)
    if terminal_state in {"rejected", "budget_exhausted"} and replay is not None:
        try:
            replay.value["return_correction_failure"] = trace[-1].get("reason", terminal_state) if trace else terminal_state
            await scanner._preserve_replay(replay, correction_failed=True)
            if replay.value.get("return_failure_replay_id"):
                scanner.checkpoint["return_failure_replay"] = replay.value["return_failure_replay_id"]
            elif not scanner.checkpoint.get("return_failure_replay"):
                scanner.checkpoint["return_failure_replay_unavailable"] = True
        except Exception:
            scanner.checkpoint["return_failure_replay_unavailable"] = True
    if replay is not None:
        scanner._last_return_replay = None
    if callable(persist):
        await persist()
    return result
