"""Automatic, bounded acquisition for a camera source's visual panorama.

Only PanoramaCamera performs I/O. Device coordinates and pulse durations remain
device coordinates and durations, never angular geometry. Every accepted image
has its own visual stability evidence. A stopped request never triggers a return
movement. Unknown limits and failed photographs produce explicit partial results.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import cv2
import numpy as np

from .panorama_capture import PanoramaCaptureError
from .processing.panorama_stability import VisualStabilityDetector
from .panorama_region import acquire_region, region_policy, region_progress, region_resume_error

MAX_CAPTURES = 256
MAX_JOB_SECONDS = 1200.0
MAX_CONTINUOUS_STEPS = 64
MAX_ROWS = 12
MAX_ATTEMPTS = 3
FRAME_BUFFER_BYTES = 96 * 1024**2
ATTEMPT_SECONDS = 12.0
MAX_RETURN_ATTEMPT_SECONDS = 60.0
DEVICE_ARRIVAL_TOLERANCE = 0.018
ANALYSIS_WIDTH = 960
CORRECTION_PRECONDITION_PIXELS = 1.0
RECOVERY_ANCHOR_PRECONDITION_PIXELS = 2.0
ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS = 15.0
MAX_CORRECTION_FRAME_AGE_SECONDS = 1.0
ENDPOINT_COMMIT_WINDOW_SECONDS = 0.12
ENDPOINT_COMMIT_MAX_SECONDS = 2.0
ENDPOINT_COMMIT_MINIMUM_FRAMES = 2
MAXIMUM_RETURN_CORRECTIONS = 4
MAX_DIAGNOSTIC_ATTEMPTS = 32
MAX_TELEMETRY_SAMPLES = 128
MAX_REJECTED_OBSERVATIONS = 16
MAX_REPLAY_BYTES = 64 * 1024**2
MAX_REPLAYS_PER_OUTCOME = 2
MAX_FIRST_REFUSAL_BYTES = 64 * 1024**2
MAX_FIRST_REFUSAL_METADATA_BYTES = 1024**2
CONTINUOUS_CURSOR_VERSION = 4
DEFAULT_CONTINUOUS_PULSE_SPEED = 0.1
MINIMUM_CONTINUOUS_PULSE_SECONDS = 0.05
MINIMUM_SEEK_PULSE_SECONDS = 0.12
UNCONFIRMED_NO_EFFECT_PIXELS = 0.5
UNCONFIRMED_NO_EFFECT_OVERLAP = 0.95
NATIVE_CORROBORATED_NO_EFFECT_PIXELS = 15.0
MAX_UNCONFIRMED_RETRIES_PER_SEEK = 1
CONNECTION_RECOVERY_VERSION = 1
VISUAL_BAND_RETURN_VERSION = 1
MAX_VISUAL_BAND_RETURN_COMMANDS = 64
MAX_VISUAL_BAND_RETURN_CORRECTIONS = 3
MINIMUM_VISUAL_BAND_RETURN_PULSE_SECONDS = 0.6
GRID_TARGET_OVERLAP = 0.68
ABSOLUTE_GRID_VERSION = 3
MAX_GRID_PILOT_CYCLES_PER_AXIS = 3
DISTRIBUTED_CONNECTION_MINIMUM_INLIERS = 24
FULL_PULSE_VISUAL_SUPPORT_INLIERS = 4 * DISTRIBUTED_CONNECTION_MINIMUM_INLIERS
FULL_PULSE_MINIMUM_FEATURE_RETENTION = 0.8


def _movement_attempt_id(value: Any) -> str | None:
    """Return the canonical durable identifier for one physical attempt."""
    if not isinstance(value, str) or len(value) != 32:
        return None
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError:
        return None
    return value if parsed.hex == value else None


def _visual_band_return(
    cursor: Any,
    *,
    captures: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Validate the durable route back to one photographed band origin.

    The route is a chain of already accepted photographs.  It never treats
    pulse duration or a device position as proof of arrival; those values only
    choose the next bounded command.  Arrival is established later by a fresh
    visual match against a node in this immutable chain.
    """
    if not isinstance(cursor, dict):
        raise ValueError("continuous cursor is unavailable")
    route = cursor.get("visual_band_return")
    if route is None:
        return None
    capture_ids = route.get("capture_ids") if isinstance(route, dict) else None
    current_index = route.get("current_index") if isinstance(route, dict) else None
    commands = route.get("commands") if isinstance(route, dict) else None
    corrections = route.get("corrections") if isinstance(route, dict) else None
    if (
        not isinstance(route, dict)
        or type(route.get("version")) is not int
        or route["version"] != VISUAL_BAND_RETURN_VERSION
        or route.get("state") not in {"active", "correcting"}
        or type(route.get("row")) is not int
        or route.get("after_stage") != "step"
        or route.get("after_direction") not in {-1, 1}
        or route.get("after_branch") not in {-1, 1}
        or not isinstance(capture_ids, list)
        or not 2 <= len(capture_ids) <= MAX_CAPTURES
        or any(not isinstance(capture_id, str) or not capture_id for capture_id in capture_ids)
        or len(set(capture_ids)) != len(capture_ids)
        or type(current_index) is not int
        or not 0 <= current_index < len(capture_ids)
        or type(commands) is not int
        or not 0 <= commands <= MAX_VISUAL_BAND_RETURN_COMMANDS
        or type(corrections) is not int
        or not 0 <= corrections <= MAX_VISUAL_BAND_RETURN_CORRECTIONS
        or route.get("origin_capture_id") != capture_ids[-1]
        or cursor.get("stage") != "pan"
        or cursor.get("row") != route.get("row")
        or cursor.get("branch") != route.get("after_branch")
        or cursor.get("anchor", {}).get("capture_id") != capture_ids[current_index]
        or cursor.get("anchor", {}).get("row") != route.get("row")
    ):
        raise ValueError("visual band return is invalid")
    if captures is None:
        return route
    by_identifier = {
        capture.get("id"): capture
        for capture in captures
        if isinstance(capture, dict) and isinstance(capture.get("id"), str)
    }
    route_captures = [by_identifier.get(capture_id) for capture_id in capture_ids]
    if any(
        not isinstance(capture, dict)
        or capture.get("row_index") != route["row"]
        or capture.get("quality", {}).get("stable") is not True
        or not isinstance(capture.get("path"), str)
        or not capture["path"]
        for capture in route_captures
    ):
        raise ValueError("visual band return capture is invalid")
    for child, parent in zip(route_captures[:-1], route_captures[1:], strict=True):
        movement = child.get("movement")
        if (
            not isinstance(movement, dict)
            or movement.get("axis") != "pan"
            or movement.get("direction") not in {-1, 1}
            or _finite(movement.get("duration")) is None
            or not MINIMUM_SEEK_PULSE_SECONDS <= movement["duration"] <= 2.0
            or movement.get("anchor_capture_id") != parent.get("id")
        ):
            raise ValueError("visual band return connection is invalid")
    current_capture = route_captures[current_index]
    if (
        cursor.get("anchor", {}).get("path") != current_capture.get("path")
        or route.get("origin_path") != route_captures[-1].get("path")
    ):
        raise ValueError("visual band return anchor is invalid")
    return route


def _visual_band_return_correction_plan(
    before: dict, after: dict, *, direction: int, duration: float
) -> dict | None:
    """Estimate one pan correction from its observed effect in a fixed target view.

    Fit all nine homography grid displacements, not just the image center. A
    residual across the grid rejects tilt, rotation or a changed scene that a
    single pan command cannot explain. Duration chooses a bounded command;
    only a subsequent ordinary anchor match can prove arrival.
    """
    if (
        type(direction) is not int or direction not in {-1, 1}
        or _finite(duration) is None or not MINIMUM_SEEK_PULSE_SECONDS <= duration <= 2.0
    ):
        return None
    fields = []
    for matching in (before, after):
        if (
            matching.get("verified") is not True
            or matching.get("support_scope") != "distributed_scene"
            or (_finite(matching.get("overlap")) or 0) < 0.65
            or (_finite(matching.get("inliers")) or 0)
            < DISTRIBUTED_CONNECTION_MINIMUM_INLIERS
        ):
            return None
        try:
            width, height = matching["analysis_size"]
            homography = np.asarray(matching["homography"], dtype=np.float64)
            if (
                not np.isfinite([width, height]).all()
                or min(width, height) <= 0
                or homography.shape != (3, 3)
                or not np.isfinite(homography).all()
            ):
                return None
            grid = np.array(
                [[width * x, height * y] for y in (0.2, 0.5, 0.8) for x in (0.2, 0.5, 0.8)],
                dtype=np.float64,
            )
            projected = np.column_stack((grid, np.ones(len(grid)))) @ homography.T
            if np.any(np.abs(projected[:, 2]) < 1e-9):
                return None
            field = projected[:, :2] / projected[:, 2:] - grid
            if not np.isfinite(field).all():
                return None
            fields.append(field)
        except (KeyError, TypeError, ValueError):
            return None
    if before["analysis_size"] != after["analysis_size"]:
        return None
    initial, remaining = fields
    response = remaining - initial
    response_energy = float(np.sum(response * response))
    if (
        response_energy < len(response) * 2.0**2
        or np.linalg.norm(remaining) >= np.linalg.norm(initial)
    ):
        return None
    fraction = -float(np.sum(remaining * response)) / response_energy
    residual = remaining + fraction * response
    residual_pixels = float(np.percentile(np.linalg.norm(residual, axis=1), 95))
    if residual_pixels > ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS:
        return None
    # Undertravel continues the measured direction; overtravel reverses it.
    # Damping and the previous pulse bound limit extrapolation and backlash.
    seconds = min(duration, 1.2, max(MINIMUM_SEEK_PULSE_SECONDS, abs(fraction) * duration * 0.8))
    signed_fraction = math.copysign(seconds / duration, fraction)
    predicted = remaining + signed_fraction * response
    remaining_pixels = float(np.percentile(np.linalg.norm(remaining, axis=1), 95))
    predicted_pixels = float(np.percentile(np.linalg.norm(predicted, axis=1), 95))
    if predicted_pixels > remaining_pixels - max(1.0, remaining_pixels * 0.05):
        return None
    return {
        "direction": direction if fraction > 0 else -direction,
        "duration": seconds,
        "before_pixels": float(np.percentile(np.linalg.norm(initial, axis=1), 95)),
        "remaining_pixels": remaining_pixels,
        "predicted_pixels": predicted_pixels,
        "residual_pixels": residual_pixels,
    }


def _connection_recovery(
    cursor: Any,
    *,
    captures: list[dict[str, Any]] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate the durable, single-subdivision recovery for one visual gap."""
    if not isinstance(cursor, dict):
        raise ValueError("continuous cursor is unavailable")
    recovery = cursor.get("connection_recovery")
    if recovery is None:
        return None
    states = {
        "planned",
        "return_pending",
        "anchor_confirmed",
        "retry_pending",
        "halfstep_observed",
        "complete",
        "failed",
    }
    identity = recovery.get("identity") if isinstance(recovery, dict) else None
    anchor = identity.get("anchor") if isinstance(identity, dict) else None
    target = recovery.get("target") if isinstance(recovery, dict) else None
    return_method = recovery.get("return_method", "absolute") if isinstance(recovery, dict) else None
    mode = recovery.get("mode", "subdivide") if isinstance(recovery, dict) else None
    return_duration = (
        _finite(recovery.get("return_duration")) if isinstance(recovery, dict) else None
    )
    failed_duration = _finite(recovery.get("failed_duration")) if isinstance(recovery, dict) else None
    retry_duration = _finite(recovery.get("retry_duration")) if isinstance(recovery, dict) else None
    if (
        not isinstance(recovery, dict)
        or type(recovery.get("version")) is not int
        or recovery["version"] != CONNECTION_RECOVERY_VERSION
        or recovery.get("state") not in states
        or type(recovery.get("attempt")) is not int
        or recovery["attempt"] != 1
        or not isinstance(identity, dict)
        or identity.get("axis") not in {"pan", "tilt"}
        or type(identity.get("direction")) is not int
        or identity["direction"] not in {-1, 1}
        or type(identity.get("row")) is not int
        or identity.get("stage") not in {"pan", "step"}
        or type(identity.get("branch")) is not int
        or identity["branch"] not in {-1, 1}
        or type(identity.get("step")) is not int
        or identity["step"] <= 0
        or not isinstance(anchor, dict)
        or not isinstance(anchor.get("capture_id"), str)
        or not anchor["capture_id"]
        or not isinstance(anchor.get("path"), str)
        or not anchor["path"]
        or type(anchor.get("row")) is not int
        or return_method not in {"absolute", "relative_pulse"}
        or mode not in {"subdivide", "return_only"}
        or (
            return_method == "absolute"
            and (
                not isinstance(target, dict)
                or any(_finite(target.get(axis)) is None for axis in ("pan", "tilt"))
                or any(not -1 <= float(target[axis]) <= 1 for axis in ("pan", "tilt"))
            )
        )
        or (
            return_method == "relative_pulse"
            and (
                target is not None
                or return_duration is None
                or abs(return_duration - failed_duration) > 1e-9
            )
        )
        or failed_duration is None
        or not MINIMUM_SEEK_PULSE_SECONDS <= failed_duration <= 2.0
        or (
            mode == "subdivide"
            and (
                failed_duration <= MINIMUM_SEEK_PULSE_SECONDS
                or retry_duration is None
                or abs(
                    retry_duration
                    - max(MINIMUM_SEEK_PULSE_SECONDS, failed_duration / 2)
                )
                > 1e-9
            )
        )
        or (mode == "return_only" and retry_duration is not None)
        or not isinstance(recovery.get("failure_code"), str)
        or not recovery["failure_code"]
    ):
        raise ValueError("connection recovery is invalid")
    if captures is not None:
        anchor_capture = next(
            (
                capture
                for capture in captures
                if isinstance(capture, dict)
                and capture.get("id") == anchor["capture_id"]
                and capture.get("path") == anchor["path"]
                and capture.get("row_index") == anchor["row"]
                and capture.get("quality", {}).get("stable") is True
            ),
            None,
        )
        anchor_pose = anchor_capture.get("pose", {}) if anchor_capture is not None else {}
        if capabilities is not None:
            anchor_pose = _normalized_absolute_pose(anchor_pose, capabilities) or {}
        if anchor_capture is None or (
            return_method == "absolute"
            and any(
                _finite(anchor_pose.get(axis)) is None
                or abs(float(anchor_pose[axis]) - float(target[axis])) > 1e-9
                for axis in ("pan", "tilt")
            )
        ):
            raise ValueError("connection recovery target is not bound to its anchor")
    if recovery["state"] == "failed":
        if (
            not isinstance(recovery.get("failure_reason"), str)
            or not recovery["failure_reason"]
        ):
            raise ValueError("failed connection recovery is invalid")
        return recovery
    if recovery["state"] == "complete":
        retry_step = recovery.get("retry_step")
        recovered_connection = recovery.get("recovered_connection")
        if (
            type(retry_step) is not int
            or retry_step != identity["step"] + 1
            or recovery.get("stationary") is not False
            or not isinstance(recovered_connection, dict)
            or recovered_connection.get("verified") is not True
            or _finite(recovered_connection.get("overlap")) is None
            or recovered_connection["overlap"] < 0.25
            or not isinstance(recovery.get("capture_id"), str)
            or not recovery["capture_id"]
            or recovery["capture_id"] == anchor["capture_id"]
            or not isinstance(recovery.get("capture_path"), str)
            or not recovery["capture_path"]
        ):
            raise ValueError("completed connection recovery is invalid")
        if captures is not None:
            expected_row = (
                identity["row"]
                if identity["axis"] == "pan"
                else identity["row"] + identity["direction"]
            )
            if not any(
                isinstance(capture, dict)
                and capture.get("id") == recovery["capture_id"]
                and capture.get("path") == recovery["capture_path"]
                and capture.get("row_index") == expected_row
                and capture.get("quality", {}).get("stable") is True
                for capture in captures
            ):
                raise ValueError("completed connection recovery capture is invalid")
        return recovery

    state_key = "seek" if identity["axis"] == "pan" else "vertical_seek"
    seek = cursor.get(state_key)
    if (
        cursor.get("stage") != identity["stage"]
        or cursor.get("row") != identity["row"]
        or cursor.get("branch") != identity["branch"]
        or not isinstance(seek, dict)
        or type(seek.get("steps")) is not int
        or seek["steps"] < identity["step"]
        or cursor.get("anchor") != anchor
    ):
        raise ValueError("connection recovery does not match the cursor")
    if identity["axis"] == "pan" and (
        cursor.get("direction") != identity["direction"]
        or seek.get("axis") != "pan"
        or seek.get("direction") != identity["direction"]
        or seek.get("row") != identity["row"]
    ):
        raise ValueError("horizontal connection recovery identity is invalid")
    if identity["axis"] == "tilt" and (
        identity["stage"] != "step"
        or seek.get("branch") != identity["direction"]
        or seek.get("row") != identity["row"]
    ):
        raise ValueError("vertical connection recovery identity is invalid")
    retry_step = recovery.get("retry_step")
    if recovery["state"] in {"retry_pending", "halfstep_observed"}:
        if (
            type(retry_step) is not int
            or retry_step != identity["step"] + 1
            or seek["steps"] != retry_step
        ):
            raise ValueError("connection halfstep identity is invalid")
    elif seek["steps"] != identity["step"]:
        raise ValueError("connection recovery step is invalid")
    return recovery


def _causal_failed_connection_retry(cursor: Any, recovery: Any) -> bool:
    """Recognize one terminal half-step failure without authorizing movement."""
    if not isinstance(cursor, dict) or not isinstance(recovery, dict):
        return False
    identity = recovery.get("identity")
    transition = cursor.get("transition")
    intent = transition.get("intent") if isinstance(transition, dict) else None
    if not isinstance(identity, dict):
        return False
    state_key = "seek" if identity.get("axis") == "pan" else "vertical_seek"
    seek = cursor.get(state_key)
    movement_id = (
        _movement_attempt_id(transition.get("movement_id"))
        if isinstance(transition, dict)
        else None
    )
    subdivisions = seek.get("subdivided_steps") if isinstance(seek, dict) else None
    retry_step = recovery.get("retry_step")
    retry_duration = _finite(recovery.get("retry_duration"))
    failure_reason = recovery.get("failure_reason")
    if (
        recovery.get("state") != "failed"
        or failure_reason not in {
            "halfstep_motion_not_observed",
            "halfstep_stability_timeout",
        }
        or cursor.get("relocalization_failed") is not True
        or type(retry_step) is not int
        or retry_step != identity.get("step", -2) + 1
        or retry_duration is None
        or movement_id is None
        or not isinstance(seek, dict)
        or not isinstance(subdivisions, list)
        or any(type(step) is not int or step <= 0 for step in subdivisions)
        or seek.get("steps") != retry_step
        or _finite(seek.get("duration")) != retry_duration
        or cursor.get("stage") != identity.get("stage")
        or cursor.get("row") != identity.get("row")
        or cursor.get("branch") != identity.get("branch")
        or cursor.get("anchor") != identity.get("anchor")
        or not isinstance(transition, dict)
        or transition.get("state") != "accepted"
        or transition.get("outcome") != "halfstep_observation_failed"
        or any(
            transition.get(key) != cursor.get(key)
            for key in ("stage", "row", "direction", "branch")
        )
        or not isinstance(intent, dict)
        or intent.get("type") != "connection_halfstep"
        or intent.get("axis") != identity.get("axis")
        or intent.get("direction") != identity.get("direction")
        or intent.get("row") != identity.get("row")
        or intent.get("step") != retry_step
        or _finite(intent.get("duration")) != retry_duration
        or intent.get("anchor") != identity.get("anchor")
    ):
        return False
    if identity["axis"] == "pan":
        return bool(
            cursor.get("direction") == identity.get("direction")
            and seek.get("axis") == "pan"
            and seek.get("direction") == identity.get("direction")
            and seek.get("row") == identity.get("row")
            and identity.get("step") in subdivisions
        )
    return bool(
        identity.get("axis") == "tilt"
        and identity.get("stage") == "step"
        and seek.get("branch") == identity.get("direction")
        and seek.get("row") == identity.get("row")
        and identity.get("step") in subdivisions
    )


def _pending_limit_probe_intent(cursor: Any) -> dict[str, Any] | None:
    """Validate a reserved opposite pulse that must never be replayed."""
    if not isinstance(cursor, dict):
        raise ValueError("continuous cursor is unavailable")
    intent = cursor.get("limit_probe_intent")
    if intent is None:
        return None
    stage = cursor.get("stage")
    if stage is None and isinstance(intent, dict):
        stage = intent.get("stage")
    if not isinstance(intent, dict) or intent.get("type") != "limit_probe":
        raise ValueError("limit probe intent is invalid")
    if intent.get("state") != "pending" or stage not in {"pan", "step"}:
        raise ValueError("limit probe intent is not pending seek work")
    state_key = "seek" if stage == "pan" else "vertical_seek"
    state = cursor.get(state_key)
    axis = "pan" if stage == "pan" else "tilt"
    main_direction = state.get("direction") if stage == "pan" and isinstance(state, dict) else (
        state.get("branch") if isinstance(state, dict) else None
    )
    row = state.get("row") if isinstance(state, dict) else None
    step = state.get("steps") if isinstance(state, dict) else None
    duration = _finite(intent.get("duration"))
    anchor = cursor.get("anchor")
    normalized_anchor = (
        {key: anchor.get(key) for key in ("capture_id", "path", "row")}
        if isinstance(anchor, dict)
        else None
    )
    if (
        type(main_direction) is not int
        or main_direction not in {-1, 1}
        or type(row) is not int
        or type(step) is not int
        or not 1 <= step <= MAX_CONTINUOUS_STEPS
        or duration is None
        or not MINIMUM_SEEK_PULSE_SECONDS <= duration <= 2.0
        or not isinstance(normalized_anchor, dict)
        or not isinstance(normalized_anchor.get("capture_id"), str)
        or not normalized_anchor["capture_id"]
        or not isinstance(normalized_anchor.get("path"), str)
        or not normalized_anchor["path"]
        or type(normalized_anchor.get("row")) is not int
    ):
        raise ValueError("limit probe seek state is invalid")
    normalized = {
        "type": "limit_probe",
        "state": "pending",
        "stage": stage,
        "axis": axis,
        "direction": -main_direction,
        "row": row,
        "step": step,
        "duration": duration,
        "anchor": normalized_anchor,
        "state_key": state_key,
    }
    if any(intent.get(key) != normalized[key] for key in (
        "type", "state", "stage", "axis", "direction", "row", "step"
    )):
        raise ValueError("limit probe identity does not match seek state")
    if intent.get("anchor") != normalized_anchor:
        raise ValueError("limit probe duration or anchor is invalid")
    return normalized


def _pending_seek_intent(cursor: Any) -> dict[str, Any] | None:
    """Validate and normalize a durable pending continuous-scan pulse.

    A pulse is resumable only when its exact identity and causal pre-command
    frame were persisted. Older pending transitions remain useful as evidence,
    but are not safe to replay.
    """
    if not isinstance(cursor, dict):
        raise ValueError("continuous cursor is unavailable")
    connection_recovery = _connection_recovery(cursor)
    if connection_recovery is not None and connection_recovery["state"] != "complete":
        if (
            connection_recovery["state"] == "failed"
            and _causal_failed_connection_retry(cursor, connection_recovery)
            and cursor.get("limit_probe_intent") is None
        ):
            return None
        # Recovery movements have different causal proofs than an ordinary seek
        # pulse. An interrupted one must never be replayed through this path.
        raise ValueError("connection recovery cannot resume as a seek pulse")
    if _pending_limit_probe_intent(cursor) is not None:
        raise ValueError("pending limit probe cannot resume as a seek pulse")
    transition = cursor.get("transition")
    stage = cursor.get("stage")
    recovery = cursor.get("recovery")
    if (
        stage == "return_reference"
        and isinstance(recovery, dict)
        and recovery.get("state") == "pending"
        and (
            not isinstance(transition, dict)
            or transition.get("state") != "pending"
        )
    ):
        raise ValueError("pending return has no atomic transition")
    if not isinstance(transition, dict) or transition.get("state") != "pending":
        return None
    if stage == "return_reference":
        if (
            transition.get("intent") is None
            and isinstance(recovery, dict)
            and recovery.get("state") == "pending"
            and all(
                transition.get(key) == cursor.get(key)
                for key in ("stage", "row", "direction", "branch")
            )
        ):
            return None
        raise ValueError("pending return transition is invalid")
    if stage not in {"pan", "step"}:
        raise ValueError("pending transition is not a seek pulse")
    for key in ("stage", "row", "direction", "branch"):
        if transition.get(key) != cursor.get(key):
            raise ValueError("pending transition does not match the cursor")

    state_key = "seek" if stage == "pan" else "vertical_seek"
    state = cursor.get(state_key)
    if not isinstance(state, dict):
        raise ValueError("pending transition has no seek state")
    axis = "pan" if stage == "pan" else "tilt"
    direction = state.get("direction") if stage == "pan" else state.get("branch")
    row = state.get("row")
    step = state.get("steps")
    duration = _finite(state.get("duration"))
    anchor = cursor.get("anchor")
    if (
        direction not in {-1, 1}
        or type(row) is not int
        or type(step) is not int
        or not 1 <= step <= MAX_CONTINUOUS_STEPS
        or duration is None
        or not MINIMUM_SEEK_PULSE_SECONDS <= duration <= 2.0
        or not isinstance(anchor, dict)
        or not isinstance(anchor.get("capture_id"), str)
        or not anchor["capture_id"]
        or not isinstance(anchor.get("path"), str)
        or not anchor["path"]
        or type(anchor.get("row")) is not int
    ):
        raise ValueError("pending transition seek state is invalid")
    if stage == "pan" and (row != cursor.get("row") or direction != cursor.get("direction")):
        raise ValueError("pending horizontal seek does not match the cursor")
    if stage == "step" and (row != cursor.get("row") or direction != cursor.get("branch")):
        raise ValueError("pending vertical seek does not match the cursor")

    intent = transition.get("intent")
    normalized = {
        "axis": axis,
        "direction": direction,
        "row": row,
        "step": step,
        "duration": duration,
        "anchor": {
            "capture_id": anchor["capture_id"],
            "path": anchor["path"],
            "row": anchor["row"],
        },
        "state_key": state_key,
    }
    if not isinstance(intent, dict) or intent.get("type") != "seek_pulse":
        raise ValueError("pending pulse identity is invalid")
    for key in ("axis", "direction", "row", "step"):
        if intent.get(key) != normalized[key]:
            raise ValueError("pending pulse identity does not match seek state")
    intent_duration = _finite(intent.get("duration"))
    intent_anchor = intent.get("anchor")
    baseline = transition.get("baseline")
    if (
        intent_duration is None
        or abs(intent_duration - duration) > 1e-9
        or not isinstance(intent_anchor, dict)
        or any(intent_anchor.get(key) != normalized["anchor"][key] for key in normalized["anchor"])
        or not isinstance(baseline, dict)
        or not isinstance(baseline.get("path"), str)
        or not baseline["path"]
        or not isinstance(baseline.get("sha256"), str)
        or len(baseline["sha256"]) != 64
        or not isinstance(baseline.get("capture_instance"), str)
        or not baseline["capture_instance"].strip()
        or type(baseline.get("generation")) is not int
        or baseline["generation"] < 0
        or type(baseline.get("sequence")) is not int
        or baseline["sequence"] <= 0
    ):
        raise ValueError("pending pulse duration, anchor or baseline is invalid")
    normalized["baseline"] = baseline
    return normalized


def _return_correction_commands(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the persisted, shared physical-command ledger for one return.

    Older checkpoints kept absolute adjustments and visual pulses in separate
    lists.  Migrate both conservatively: an already planned entry may have
    reached the camera before interruption, so it consumes the shared budget.
    """
    existing = checkpoint.get("return_correction_commands")
    if isinstance(existing, list):
        return existing
    commands: list[dict[str, Any]] = []
    for modality, key in (
        ("absolute", "absolute_return_corrections"),
        ("visual_continuous", "return_corrections"),
    ):
        legacy = checkpoint.get(key)
        if not isinstance(legacy, list):
            continue
        for index, value in enumerate(legacy):
            if isinstance(value, dict):
                commands.append(
                    {
                        **value,
                        "modality": value.get("modality", modality),
                        "legacy_trace": key,
                        "legacy_index": index,
                    }
                )
    checkpoint["return_correction_commands"] = commands
    return commands


def _append_return_correction_command(
    checkpoint: dict[str, Any],
    legacy_trace: list[dict[str, Any]],
    entry: dict[str, Any],
    *,
    modality: str,
) -> None:
    """Persist one intent in its diagnostic trace and the shared budget ledger."""
    commands = _return_correction_commands(checkpoint)
    if len(commands) >= MAXIMUM_RETURN_CORRECTIONS:
        raise PanoramaCaptureError("return_correction_budget_exhausted")
    entry.update(
        modality=modality,
        correction_command_index=len(commands) + 1,
    )
    legacy_trace.append(entry)
    # Keep the same object while this process is active so before/after evidence
    # is updated atomically in both views before each persistence operation.
    commands.append(entry)
    strategy = checkpoint.get("return_correction_strategy")
    if isinstance(strategy, dict):
        strategy["commands_already_planned"] = len(commands)


def _pilot_axes_authorized_for_absolute_finish(checkpoint: dict[str, Any]) -> set[str]:
    """Return axes whose complete pilot history has qualified visual closure."""
    response = checkpoint.get("pilot_response")
    attempts = checkpoint.get("pilot_attempts")
    if not isinstance(response, dict) or not isinstance(attempts, list):
        return set()
    axes = {axis for axis in ("pan", "tilt") if _finite(response.get(axis)) is not None}
    authorized: set[str] = set()
    for axis in axes:
        axis_attempts = [
            item
            for item in attempts
            if isinstance(item, dict)
            and item.get("axis") == axis
        ]
        if not axis_attempts or any(
            not isinstance(item.get("cycle_closure"), dict)
            or item["cycle_closure"].get("visually_closed") is not True
            or item["cycle_closure"].get("match_verified") is not True
            or _finite(item["cycle_closure"].get("displacement")) is None
            or _finite(item["cycle_closure"].get("displacement")) > 3
            or _finite(item["cycle_closure"].get("overlap")) is None
            or _finite(item["cycle_closure"].get("overlap")) < 0.85
            for item in axis_attempts
        ):
            continue
        authorized.add(axis)
    return authorized


def _return_epoch_has_coarse_intent(
    checkpoint: dict[str, Any], epoch: str
) -> bool:
    """Return whether a persisted recall may already have reached the camera."""
    recalls = checkpoint.get("return_coarse_recalls")
    if "return_coarse_recalls" in checkpoint:
        if not isinstance(recalls, dict) or epoch in recalls:
            return True
    if "return_coarse_recall" in checkpoint:
        recall = checkpoint.get("return_coarse_recall")
        recall_epoch = recall.get("epoch", "final") if isinstance(recall, dict) else "final"
        if recall_epoch == epoch:
            return True
    strategy = checkpoint.get("return_correction_strategy")
    return bool(
        epoch == "final"
        and isinstance(strategy, dict)
        and strategy.get("coarse") == "saved_destination"
    )


class _AttemptDiagnostic:
    """Bounded telemetry and private, lossless observations for causal replay."""

    _RESULT_KEYS = (
        "code",
        "state",
        "stable",
        "timing_basis",
        "evidence",
        "confidence",
        "motion_pixels",
        "speed_px_s",
        "drift_pixels",
        "sharpness",
        "tracks",
        "inliers",
        "occupied_cells",
        "inlier_fraction",
        "has_motion_transition",
        "sequence",
        "generation",
        "window_frames",
        "window_media_seconds",
        "window_observation_seconds",
        "recovery_code",
        "recovery_metrics",
        "distributed_transition",
    )

    def __init__(
        self,
        kind: str,
        *,
        window_seconds: float | None = None,
        total_seconds: float = ATTEMPT_SECONDS,
    ) -> None:
        self.start = time.monotonic()
        self.first_observation: float | None = None
        self.last_receive: float | None = None
        self.last_media: float | None = None
        self.last_generation: int | None = None
        self.replay: list[tuple[np.ndarray, dict]] = []
        self.replay_bytes = 0
        self.value: dict[str, Any] = {
            "kind": kind,
            "frame_count": 0,
            "code_counts": {},
            "last_result": {},
            "window_budget_seconds": window_seconds,
            "total_budget_seconds": total_seconds,
            "first_frame_wait_seconds": None,
            "decoder_generation_changes": 0,
            "receive_intervals": {},
            "media_intervals": {},
            "frame_age_seconds": {},
            "frame_wait_seconds": {},
            "comparison_count": 0,
            "outcome": "in_progress",
        }
        if kind == "movement":
            self.value["samples"] = []

    @staticmethod
    def _summary_add(summary: dict, value: float | None) -> None:
        if value is None or not math.isfinite(value):
            return
        count = summary.get("count", 0)
        summary.update(
            count=count + 1,
            minimum=min(summary.get("minimum", value), value),
            maximum=max(summary.get("maximum", value), value),
            mean=(summary.get("mean", 0.0) * count + value) / (count + 1),
        )

    def frame(
        self,
        frame: dict,
        result: dict,
        *,
        wait_seconds: float | None = None,
        pose: dict | None = None,
    ) -> None:
        now = time.monotonic()
        if self.first_observation is None:
            self.first_observation = now
            self.value["first_frame_wait_seconds"] = now - self.start
        self.value["frame_count"] += 1
        if self.value["frame_count"] == 1:
            self.value["capture_transport"] = frame.get("transport", "unknown")
            self.value["capture_instance"] = frame.get("capture_instance")
            self.value["capture_generation"] = frame.get("generation")
            self.value["first_sequence"] = frame.get("sequence")
            if frame.get("transport_fallback_reason"):
                self.value["capture_transport_fallback_reason"] = frame[
                    "transport_fallback_reason"
                ]
            self.value["physical_timestamp_verified"] = bool(frame.get("physical_timestamp_verified", False))
        code = str(result.get("code", "unknown"))
        counts = self.value["code_counts"]
        counts[code] = counts.get(code, 0) + 1
        self.value["last_result"] = {key: result[key] for key in self._RESULT_KEYS if key in result}
        received, media = _finite(frame.get("received_monotonic")), _finite(frame.get("media_time"))
        generation = frame.get("generation")
        generation_changed = self.last_generation is not None and generation != self.last_generation
        if generation_changed:
            self.value["decoder_generation_changes"] += 1
        if received is not None and self.last_receive is not None:
            self._summary_add(self.value["receive_intervals"], received - self.last_receive)
        if media is not None and self.last_media is not None and not generation_changed:
            self._summary_add(self.value["media_intervals"], media - self.last_media)
        self._summary_add(
            self.value["frame_age_seconds"], now - received if received is not None else None
        )
        self._summary_add(self.value["frame_wait_seconds"], wait_seconds)
        self.last_receive, self.last_media, self.last_generation = received, media, generation
        if self.value["kind"] == "movement" and isinstance(frame.get("image"), np.ndarray):
            gray = _gray(frame["image"])
            encoded_ok, encoded = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            if encoded_ok and self.replay_bytes + encoded.nbytes <= MAX_REPLAY_BYTES and len(self.replay) < 256:
                self.replay.append((encoded, {
                    "observed_seconds": now - self.start,
                    "received_monotonic": received,
                    "media_time": media,
                    "sequence": frame.get("sequence"),
                    "generation": generation,
                    "physical_timestamp_verified": frame.get("physical_timestamp_verified", False),
                    "pose": {key: _finite(pose.get(key)) for key in (
                        "pan", "tilt", "zoom", "native_pan", "native_tilt",
                    )} if pose else None,
                    "result": dict(self.value["last_result"]),
                }))
                self.replay_bytes += encoded.nbytes
            else:
                self.value["replay_truncated"] = True
        if "samples" in self.value:
            state = result.get("state")
            sample = {
                "elapsed_seconds": max(0.0, now - self.start),
                "motion_pixels": _finite(result.get("motion_pixels")),
                "speed_px_s": (
                    _finite(result.get("speed_px_s"))
                    if result.get("timing_basis") == "media" and media is not None
                    else None
                ),
                "media_time": media,
                "drift_pixels": _finite(result.get("drift_pixels")),
                "confidence": _finite(result.get("confidence")),
                "state": state if state in {"observing", "stable", "timeout"} else "unknown",
                "pose": (
                    {
                        key: _finite(pose.get(key))
                        for key in ("pan", "tilt", "native_pan", "native_tilt")
                    }
                    if pose
                    else None
                ),
            }
            samples = self.value["samples"]
            samples.append(sample)
            if len(samples) > MAX_TELEMETRY_SAMPLES:
                # Deterministic thinning retains the initial and latest frame.
                # Elapsed coordinates preserve the resulting uneven intervals.
                self.value["samples"] = samples[:1] + samples[1:-1:2] + samples[-1:]

    def telemetry(self) -> dict | None:
        if self.value["kind"] != "movement":
            return None
        outcome = self.value.get("outcome")
        return {
            "kind": "movement",
            "outcome": (
                "accepted"
                if outcome in {"capture_stable", "stationary_boundary_observed"}
                else (
                    "timeout"
                    if outcome
                    in {"frame_acquisition_timeout", "motion_not_observed", "stability_timeout"}
                    else "inconclusive"
                )
            ),
            "timing_basis": self.value["last_result"].get("timing_basis", "local_observation"),
            "analysis_width": ANALYSIS_WIDTH,
            "samples": self.value["samples"],
            **{
                key: self.value[key]
                for key in (
                    "command_accepted_seconds",
                    "first_target_readback_seconds",
                    "first_motion_transition_seconds",
                    "stop_requested_seconds",
                    "stop_accepted_seconds",
                )
                if _finite(self.value.get(key)) is not None
            },
        }

    def comparison(self, result: dict) -> None:
        self.value["comparison_count"] += 1
        self.value["last_comparison"] = {
            key: result[key]
            for key in (
                "verified",
                "code",
                "displacement",
                "overlap",
                "inliers",
                "model_candidates",
            )
            if key in result
        }

    def finish(self) -> dict:
        now = time.monotonic()
        return {
            **self.value,
            "total_elapsed_seconds": now - self.start,
            "observed_elapsed_seconds": (
                now - self.first_observation if self.first_observation is not None else 0.0
            ),
        }


class _Stopped(Exception):
    pass


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) else None


def _native_axes_unchanged(
    before: Any, after: Any, requested_velocity: Any
) -> dict[str, Any] | None:
    """Corroborate a stopped velocity command with its device position."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if not isinstance(requested_velocity, dict):
        return None
    commanded_axes = [
        axis
        for axis in ("pan", "tilt")
        if abs(_finite(requested_velocity.get(axis)) or 0.0) > 1e-9
    ]
    if not commanded_axes:
        return None
    positions: dict[str, dict[str, float]] = {}
    for axis in commanded_axes:
        key = f"native_{axis}"
        start, finish = _finite(before.get(key)), _finite(after.get(key))
        if start is None or finish is None or abs(finish - start) > 1e-9:
            return None
        positions[axis] = {"before": start, "after": finish}
    status = str(after.get("move_status") or "").upper()
    if status and status not in {"IDLE", "UNKNOWN"}:
        return None
    if str(after.get("error") or "").strip():
        return None
    return {"verified": True, "axes": positions, "move_status": status or None}


def _axis_direction_evidence(
    captures: Any, axis: str, direction: int, matching: Any
) -> dict[str, Any] | None:
    """Verify a late endpoint against the observed camera-to-image direction."""
    if axis not in {"pan", "tilt"} or direction not in {-1, 1}:
        return None
    if not isinstance(captures, list) or not isinstance(matching, dict):
        return None
    primary_key = "shift_x" if axis == "pan" else "shift_y"
    cross_key = "shift_y" if axis == "pan" else "shift_x"
    primary, cross = _finite(matching.get(primary_key)), _finite(matching.get(cross_key))
    if (
        matching.get("verified") is not True
        or matching.get("overlap", 0) < 0.25
        or matching.get("displacement", 0) < 2.0
        or primary is None
        or cross is None
        or abs(primary) < 2.0
        or abs(primary) < abs(cross) * 1.25
    ):
        return None
    response_signs: list[int] = []
    for capture in captures[-12:]:
        movement = capture.get("movement") if isinstance(capture, dict) else None
        observed = capture.get("previous_overlap") if isinstance(capture, dict) else None
        if not isinstance(movement, dict) or not isinstance(observed, dict):
            continue
        sample_direction = movement.get("direction")
        sample_primary = _finite(observed.get(primary_key))
        sample_cross = _finite(observed.get(cross_key))
        if (
            movement.get("axis") != axis
            or sample_direction not in {-1, 1}
            or observed.get("verified") is not True
            or sample_primary is None
            or sample_cross is None
            or abs(sample_primary) < 2.0
            or abs(sample_primary) < abs(sample_cross) * 1.25
        ):
            continue
        response_signs.append((1 if sample_primary > 0 else -1) * sample_direction)
    if len(response_signs) < 3:
        return None
    positive = response_signs.count(1)
    negative = response_signs.count(-1)
    response_sign = 1 if positive >= negative else -1
    agreeing = max(positive, negative)
    required = math.ceil(len(response_signs) * 2 / 3)
    actual_sign = 1 if primary > 0 else -1
    expected_sign = response_sign * direction
    if agreeing < required or actual_sign != expected_sign:
        return None
    return {
        "verified": True,
        "axis": axis,
        "direction": direction,
        "samples": len(response_signs),
        "agreeing_samples": agreeing,
        "expected_image_shift_sign": expected_sign,
        "observed_image_shift": primary,
    }


def _native_axis_direction_evidence(
    captures: Any,
    axis: str,
    direction: int,
    before: Any,
    after: Any,
) -> dict[str, Any] | None:
    """Verify a late endpoint direction from the camera's learned native response."""
    if axis not in {"pan", "tilt"} or direction not in {-1, 1}:
        return None
    if not isinstance(captures, list) or not isinstance(before, dict) or not isinstance(after, dict):
        return None
    native_key = f"native_{axis}"
    start, finish = _finite(before.get(native_key)), _finite(after.get(native_key))
    if start is None or finish is None or abs(finish - start) <= 2.0:
        return None
    response_signs: list[int] = []
    previous: dict[str, Any] | None = None
    for capture in captures[-16:]:
        if not isinstance(capture, dict):
            continue
        movement = capture.get("movement")
        current = _finite(capture.get(f"native_{axis}_position"))
        if current is None:
            pose = capture.get("pose")
            current = _finite(pose.get(native_key)) if isinstance(pose, dict) else None
        if previous is not None and isinstance(movement, dict):
            prior = previous.get("native")
            sample_direction = movement.get("direction")
            if (
                movement.get("axis") == axis
                and sample_direction in {-1, 1}
                and prior is not None
                and current is not None
                and abs(current - prior) > 2.0
            ):
                response_signs.append(
                    (1 if current - prior > 0 else -1) * sample_direction
                )
        if current is not None:
            previous = {"native": current}
    if len(response_signs) < 3:
        return None
    positive = response_signs.count(1)
    negative = response_signs.count(-1)
    response_sign = 1 if positive >= negative else -1
    agreeing = max(positive, negative)
    required = math.ceil(len(response_signs) * 2 / 3)
    actual_sign = 1 if finish - start > 0 else -1
    expected_sign = response_sign * direction
    if agreeing < required or actual_sign != expected_sign:
        return None
    return {
        "verified": True,
        "axis": axis,
        "direction": direction,
        "samples": len(response_signs),
        "agreeing_samples": agreeing,
        "expected_native_delta_sign": expected_sign,
        "native_before": start,
        "native_after": finish,
        "native_delta": finish - start,
    }


def _normalized_absolute_pose(
    pose: dict[str, Any], capabilities: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a pose only when its coordinates share the normalized absolute space."""
    if not isinstance(pose, dict):
        return None
    limits = capabilities.get("limits", {})
    default_space = capabilities.get("defaults", {}).get("absolute")
    reported_space = str(pose.get("pan_tilt_space") or "")
    if reported_space not in {"", default_space}:
        return None
    for axis in ("pan", "tilt"):
        value = _finite(pose.get(axis))
        bounds = limits.get(axis)
        lower = _finite(bounds.get("min")) if isinstance(bounds, dict) else None
        upper = _finite(bounds.get("max")) if isinstance(bounds, dict) else None
        limit_space = bounds.get("space") if isinstance(bounds, dict) else None
        if (
            value is None
            or not -1 <= value <= 1
            or lower is None
            or upper is None
            or not lower <= value <= upper
            or (limit_space and default_space and limit_space != default_space)
        ):
            return None
    return pose


def _canonical_absolute_pilot_limits(limits: Any) -> dict[str, dict[str, Any]]:
    """Return the finite normalized bounds that constrain every pilot command."""
    if not isinstance(limits, dict):
        raise PanoramaCaptureError("pilot_resume_unavailable")
    canonical: dict[str, dict[str, Any]] = {}
    for axis in ("pan", "tilt"):
        bounds = limits.get(axis)
        minimum = _finite(bounds.get("min")) if isinstance(bounds, dict) else None
        maximum = _finite(bounds.get("max")) if isinstance(bounds, dict) else None
        space = bounds.get("space") if isinstance(bounds, dict) else None
        if (
            minimum is None
            or maximum is None
            or not -1 <= minimum <= maximum <= 1
            or space is not None
            and (not isinstance(space, str) or not space)
        ):
            raise PanoramaCaptureError("pilot_resume_unavailable")
        canonical[axis] = {"min": minimum, "max": maximum, "space": space}
    return canonical


def _validate_absolute_pilot_limits(
    checkpoint: dict[str, Any], *, limits: Any
) -> None:
    stored = _canonical_absolute_pilot_limits(checkpoint.get("absolute_pilot_limits"))
    current = _canonical_absolute_pilot_limits(limits)
    if stored != current:
        raise PanoramaCaptureError("pilot_resume_unavailable")


def _absolute_grid_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible") from error
    if len(encoded) > 256_000:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_absolute_grid_axis_samples(
    values: Any, *, qualified_only: bool = True
) -> list[dict[str, Any]]:
    """Canonicalize the bounded optical evidence authenticated by grid v3."""
    if values is None:
        values = []
    if not isinstance(values, list) or len(values) > 2 * MAX_GRID_PILOT_CYCLES_PER_AXIS:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
    canonical = []
    for sample in values:
        if not isinstance(sample, dict) or sample.get("kind") not in {
            "pilot_outward",
            "pilot_return",
        }:
            continue
        device_delta = _finite(sample.get("device_delta"))
        overlap = _finite(sample.get("overlap"))
        motion_gain = _finite(sample.get("motion_gain"))
        homography = _finite_homography(sample.get("homography"))
        image_delta = sample.get("image_delta")
        gain_vector = sample.get("gain_vector")
        size = sample.get("analysis_size")
        cycle_id = sample.get("pilot_cycle_id")
        if (
            sample.get("verified") is not True
            or sample.get("cycle_closed") is not True
            or device_delta is None
            or abs(device_delta) < 1e-8
            or overlap is None
            or not 0 <= overlap <= 1
            or motion_gain is None
            or motion_gain <= 0
            or homography is None
            or not isinstance(cycle_id, str)
            or not cycle_id
            or not isinstance(image_delta, list | tuple)
            or len(image_delta) != 2
            or any(_finite(value) is None for value in image_delta)
            or not isinstance(gain_vector, list | tuple)
            or len(gain_vector) != 2
            or any(_finite(value) is None for value in gain_vector)
            or not isinstance(size, list | tuple)
            or len(size) != 2
            or any(_finite(value) is None or _finite(value) <= 0 for value in size)
        ):
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
        if qualified_only and overlap < GRID_TARGET_OVERLAP:
            continue
        canonical.append(
            {
                "cycle_id": cycle_id,
                "kind": sample["kind"],
                "device_delta": device_delta,
                "image_delta": [_finite(value) for value in image_delta],
                "gain_vector": [_finite(value) for value in gain_vector],
                "motion_gain": motion_gain,
                "overlap": overlap,
                "homography": homography.tolist(),
                "analysis_size": [_finite(value) for value in size],
            }
        )
    return canonical


def _absolute_grid_contract(
    *,
    capabilities: dict[str, Any],
    limits: dict[str, Any],
    plan: Any,
    grid_geometry: Any,
    pilot_steps: Any,
    optical_samples: Any,
) -> dict[str, Any]:
    """Bind a persisted absolute grid to its coordinates and finite evidence."""
    axes: dict[str, dict[str, Any]] = {}
    for axis in ("pan", "tilt"):
        bounds = limits.get(axis)
        lower = _finite(bounds.get("min")) if isinstance(bounds, dict) else None
        upper = _finite(bounds.get("max")) if isinstance(bounds, dict) else None
        if lower is None or upper is None:
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
        axes[axis] = {
            "min": lower,
            "max": upper,
            "space": bounds.get("space") if isinstance(bounds, dict) else None,
        }
    if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_CAPTURES:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
    canonical_plan = []
    for target in plan:
        pan = _finite(target.get("pan")) if isinstance(target, dict) else None
        tilt = _finite(target.get("tilt")) if isinstance(target, dict) else None
        row = target.get("row") if isinstance(target, dict) else None
        if (
            pan is None
            or tilt is None
            or not -1 <= pan <= 1
            or not -1 <= tilt <= 1
            or type(row) is not int
            or not 0 <= row < MAX_CAPTURES
        ):
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
        canonical_plan.append({"pan": pan, "tilt": tilt, "row": row})

    canonical_geometry: dict[str, dict[str, Any]] = {}
    canonical_samples: dict[str, list[dict[str, Any]]] = {}
    for axis in ("pan", "tilt"):
        geometry = grid_geometry.get(axis) if isinstance(grid_geometry, dict) else None
        count = geometry.get("count") if isinstance(geometry, dict) else None
        sample_count = geometry.get("optical_sample_count") if isinstance(geometry, dict) else None
        if (
            type(count) is not int
            or not 1 <= count <= MAX_CAPTURES
            or type(sample_count) is not int
            or not 0 <= sample_count <= 2 * MAX_GRID_PILOT_CYCLES_PER_AXIS
        ):
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
        canonical_geometry[axis] = {"count": count, "optical_sample_count": sample_count}
        for key in (
            "step",
            "span",
            "target_overlap",
            "predicted_overlap",
            "observed_step_limit",
        ):
            if key not in geometry:
                continue
            number = _finite(geometry.get(key))
            if number is None or number < 0:
                raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
            canonical_geometry[axis][key] = number
        persisted_step = (
            _finite(pilot_steps.get(axis)) if isinstance(pilot_steps, dict) else None
        )
        if persisted_step is None or persisted_step != canonical_geometry[axis].get("step"):
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")

        values = optical_samples.get(axis) if isinstance(optical_samples, dict) else None
        canonical_samples[axis] = _canonical_absolute_grid_axis_samples(values)
        if len(canonical_samples[axis]) != sample_count:
            raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")

    basis = {
        "version": ABSOLUTE_GRID_VERSION,
        "absolute_space": capabilities.get("defaults", {}).get("absolute"),
        "limits": axes,
        "target_overlap": GRID_TARGET_OVERLAP,
        "plan_count": len(canonical_plan),
        "plan_digest": _absolute_grid_digest(canonical_plan),
        "geometry_digest": _absolute_grid_digest(canonical_geometry),
        "steps_digest": _absolute_grid_digest(
            {axis: canonical_geometry[axis]["step"] for axis in ("pan", "tilt")}
        ),
        "optical_evidence_digest": _absolute_grid_digest(canonical_samples),
    }
    return {**basis, "signature": _absolute_grid_digest(basis)}


def _validate_absolute_grid_checkpoint(
    checkpoint: dict[str, Any], *, capabilities: dict[str, Any]
) -> None:
    """Reject a legacy or stale absolute plan before control is acquired."""
    contract = checkpoint.get("absolute_grid")
    if not isinstance(contract, dict) or contract.get("version") != ABSOLUTE_GRID_VERSION:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")
    expected = _absolute_grid_contract(
        capabilities=capabilities,
        limits=capabilities.get("limits", {}),
        plan=checkpoint.get("plan"),
        grid_geometry=checkpoint.get("grid_geometry"),
        pilot_steps=checkpoint.get("pilot_steps"),
        optical_samples=checkpoint.get("pilot_observations"),
    )
    if contract != expected:
        raise PanoramaCaptureError("absolute_grid_checkpoint_incompatible")


def _finite_homography(value: Any) -> np.ndarray | None:
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        return None
    denominator = float(matrix[2, 2])
    if abs(denominator) < 1e-12:
        return None
    matrix = matrix / denominator
    return matrix if np.isfinite(matrix).all() else None


def _homography_overlap(
    homography: np.ndarray, *, width: float, height: float
) -> float | None:
    """Measure the two-dimensional overlap of one directly observed homography."""
    if not all(
        value is not None and math.isfinite(value) and value > 0
        for value in (_finite(width), _finite(height))
    ):
        return None
    corners = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    transformed = cv2.perspectiveTransform(corners[None], homography)[0]
    if not np.isfinite(transformed).all() or not cv2.isContourConvex(transformed):
        return None
    intersection, _ = cv2.intersectConvexConvex(corners, transformed.astype(np.float32))
    overlap = float(intersection / (width * height))
    return float(np.clip(overlap, 0, 1)) if math.isfinite(overlap) else None


def _axis_overlap_geometry(
    *,
    span: float,
    optical_samples: list[dict[str, Any]],
    maximum_count: int,
) -> dict[str, Any]:
    """Plan one device axis from verified two-dimensional optical transforms."""
    finite_span = _finite(span)
    if (
        finite_span is None
        or finite_span < 0
        or isinstance(maximum_count, bool)
        or not isinstance(maximum_count, int)
        or maximum_count < 1
    ):
        raise PanoramaCaptureError("pilot_geometry_unconfirmed")
    if finite_span == 0:
        return {
            "count": 1,
            "step": 0.0,
            "span": 0.0,
            "target_overlap": GRID_TARGET_OVERLAP,
            "predicted_overlap": 1.0,
            "optical_sample_count": 0,
        }
    if maximum_count < 2 or not optical_samples:
        raise PanoramaCaptureError(
            "coverage_exceeds_capture_budget" if maximum_count < 2 else "pilot_geometry_unconfirmed"
        )
    samples: dict[int, list[tuple[float, float]]] = {-1: [], 1: []}
    for sample in optical_samples:
        homography = _finite_homography(sample.get("homography")) if isinstance(sample, dict) else None
        device_delta = _finite(sample.get("device_delta")) if isinstance(sample, dict) else None
        overlap = _finite(sample.get("overlap")) if isinstance(sample, dict) else None
        size = sample.get("analysis_size") if isinstance(sample, dict) else None
        width = _finite(size[0]) if isinstance(size, list | tuple) and len(size) == 2 else None
        height = _finite(size[1]) if isinstance(size, list | tuple) and len(size) == 2 else None
        measured = (
            _homography_overlap(homography, width=width, height=height)
            if homography is not None and width is not None and height is not None
            else None
        )
        if (
            sample.get("verified") is not True
            or homography is None
            or device_delta is None
            or abs(device_delta) < 1e-8
            or overlap is None
            or not 0 <= overlap <= 1
            or measured is None
            or abs(measured - overlap) > 0.01
        ):
            raise PanoramaCaptureError("pilot_geometry_unconfirmed")
        if overlap >= GRID_TARGET_OVERLAP:
            samples[1 if device_delta > 0 else -1].append((abs(device_delta), overlap))
    if any(not direction_samples for direction_samples in samples.values()):
        raise PanoramaCaptureError("pilot_geometry_unconfirmed")
    direction_evidence = [
        max(direction_samples, key=lambda item: item[0])
        for direction_samples in samples.values()
    ]
    observed_step_limit = min(device_delta for device_delta, _ in direction_evidence)
    interval_count = max(1, math.ceil(finite_span / observed_step_limit))
    if interval_count + 1 > maximum_count:
        raise PanoramaCaptureError("coverage_exceeds_capture_budget")
    step = finite_span / interval_count
    if step > observed_step_limit + 1e-12:
        raise PanoramaCaptureError("pilot_geometry_unconfirmed")
    observed_overlap = min(
        overlap for _, overlap in direction_evidence
    )
    return {
        "count": interval_count + 1,
        "step": step,
        "span": finite_span,
        "target_overlap": GRID_TARGET_OVERLAP,
        "predicted_overlap": observed_overlap,
        "overlap_basis": "direct_pilot_observation",
        "observed_step_limit": observed_step_limit,
        "optical_sample_count": sum(len(values) for values in samples.values()),
    }


def _pilot_optical_samples(checkpoint: dict[str, Any], axis: str) -> list[dict[str, Any]]:
    """Return only the verified optical samples committed by closed pilot cycles."""
    observations = checkpoint.get("pilot_observations", {}).get(axis, [])
    return [
        observation
        for observation in observations
        if isinstance(observation, dict) and observation.get("cycle_closed") is True
    ] if isinstance(observations, list) else []


def _absolute_grid_plan(
    *,
    limits: dict[str, Any],
    axis_samples: dict[str, list[dict[str, Any]]],
    captures_used: int,
    maximum_captures: int,
) -> tuple[list[dict[str, float | int]], dict[str, dict[str, Any]]]:
    """Build a full-limit serpentine grid without weakening optical overlap."""
    if (
        isinstance(captures_used, bool)
        or not isinstance(captures_used, int)
        or captures_used < 0
        or isinstance(maximum_captures, bool)
        or not isinstance(maximum_captures, int)
        or maximum_captures < captures_used
    ):
        raise PanoramaCaptureError("coverage_exceeds_capture_budget")
    available = maximum_captures - captures_used
    geometry: dict[str, dict[str, Any]] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for axis in ("pan", "tilt"):
        axis_limits = limits.get(axis)
        minimum = _finite(axis_limits.get("min")) if isinstance(axis_limits, dict) else None
        maximum = _finite(axis_limits.get("max")) if isinstance(axis_limits, dict) else None
        if minimum is None or maximum is None or maximum < minimum:
            raise PanoramaCaptureError("pilot_geometry_unconfirmed")
        bounds[axis] = (minimum, maximum)
        geometry[axis] = _axis_overlap_geometry(
            span=maximum - minimum,
            optical_samples=axis_samples.get(axis, []),
            maximum_count=available,
        )
    planned_count = geometry["pan"]["count"] * geometry["tilt"]["count"]
    if planned_count > available:
        raise PanoramaCaptureError("coverage_exceeds_capture_budget")
    pans = np.linspace(*bounds["pan"], geometry["pan"]["count"])
    plan: list[dict[str, float | int]] = []
    for row, tilt in enumerate(np.linspace(*bounds["tilt"], geometry["tilt"]["count"])):
        for pan in pans if row % 2 == 0 else pans[::-1]:
            plan.append({"pan": float(pan), "tilt": float(tilt), "row": row})
    return plan, geometry


def _control_reference_evidence(comparison: dict, frame: dict) -> tuple[dict, str | None]:
    """Keep a finite, ordered record of the reference gate used before movement."""
    overlap = _finite(comparison.get("overlap"))
    displacement = _finite(comparison.get("displacement"))
    received = _finite(frame.get("received_monotonic"))
    age = time.monotonic() - received if received is not None else None
    age = age if age is not None and math.isfinite(age) and age >= 0 else None
    verified = comparison.get("verified") is True
    if not verified:
        reason = "reference_match_unverified"
    elif overlap is None:
        reason = "reference_overlap_unavailable"
    elif overlap < 0.85:
        reason = "reference_overlap_insufficient"
    elif displacement is None:
        reason = "reference_displacement_unavailable"
    elif displacement > 3:
        reason = "reference_displacement_exceeded"
    else:
        reason = None
    source_code = comparison.get("code")
    sequence = frame.get("sequence")
    generation = frame.get("generation")
    inliers = comparison.get("inliers")
    return {
        "verified": verified,
        "overlap": overlap,
        "displacement": displacement,
        "shift_x": _finite(comparison.get("shift_x")),
        "shift_y": _finite(comparison.get("shift_y")),
        "inliers": inliers if type(inliers) is int and inliers >= 0 else None,
        "code": source_code if isinstance(source_code, str) else None,
        "reason": reason,
        "frame_sequence": sequence if type(sequence) is int and sequence >= 0 else None,
        "frame_generation": generation if type(generation) is int and generation >= 0 else None,
        "frame_age_seconds": age,
    }, reason


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_image(path: Path, image: np.ndarray, maximum_bytes: int | None = None) -> str:
    temporary = path.with_name(path.stem + ".partial.jpg")
    try:
        if not cv2.imwrite(str(temporary), image, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise PanoramaCaptureError("capture_write_failed")
        if maximum_bytes is not None and temporary.stat().st_size > maximum_bytes:
            raise PanoramaCaptureError("region_budget_exhausted")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_capture_frame(frames: Any, evidence: dict, identity: dict) -> dict | None:
    """Bind evidence to the sharpest retained original from the approved window.

    Selection happens after buffer eviction. Returning the existing frame keeps
    its original alive through persistence without copying or growing the buffer.
    Evidence without a qualified window still requires its exact selected frame.
    """
    candidates = evidence.get("qualified_frames")
    if candidates is None:
        candidates = [{"sequence": evidence.get("best_sequence"), "generation": identity.get("generation")}]
    elif evidence.get("stable") is not True or not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if (not isinstance(candidate, dict) or type(candidate.get("sequence")) is not int
                or candidate.get("generation") != identity.get("generation")):
            continue
        selected = next((frame for frame in frames
                         if frame.get("sequence") == candidate["sequence"]
                         and frame.get("capture_instance") == identity.get("capture_instance")
                         and frame.get("generation") == candidate["generation"]
                         and frame.get("image_representation", "source") == "source"), None)
        if selected is not None:
            evidence["best_sequence"] = selected["sequence"]
            return selected
    return None


def _gray(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.resize(gray, (ANALYSIS_WIDTH, round(gray.shape[0] * ANALYSIS_WIDTH / gray.shape[1])))


def _texture_support(image: np.ndarray) -> dict:
    """Screen-local detail cannot justify extra travel into an unobservable area."""
    gray = _gray(image)
    keys = cv2.SIFT_create(nfeatures=2000, contrastThreshold=0.025).detect(gray, None)
    points = np.float32([key.pt for key in keys]).reshape(-1, 2)
    height, width = gray.shape
    cells = {(min(3, int(x * 4 / width)), min(2, int(y * 3 / height))) for x, y in points}
    area = float(cv2.contourArea(cv2.convexHull(points))) if len(points) >= 3 else 0.0
    return {
        "distributed": len(points) >= 24 and len(cells) >= 4 and area >= width * height * 0.12,
        "features": len(points),
        "occupied_cells": len(cells),
        "hull_fraction": area / (width * height),
    }


def _deterministic_homography(
    source: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Estimate one reproducible robust model for identical correspondence input."""
    parameters = cv2.UsacParams()
    parameters.confidence = 0.999
    parameters.maxIterations = 10_000
    parameters.threshold = 2.5
    parameters.isParallel = False
    parameters.randomGeneratorState = 0
    parameters.sampler = cv2.SAMPLING_UNIFORM
    parameters.score = cv2.SCORE_METHOD_RANSAC
    parameters.loMethod = cv2.LOCAL_OPTIM_INNER_LO
    return cv2.findHomography(source, target, parameters)


def _correspondence_match(
    first: np.ndarray,
    second: np.ndarray,
    *,
    localized_command_support: bool,
    minimum_inliers: int | None = None,
    minimum_occupied_cells: int | None = None,
    minimum_hull_fraction: float | None = None,
) -> dict[str, Any]:
    """Match two views with global or command-bound spatial support."""
    first_gray, second_gray = _gray(first), _gray(second)
    if first_gray.shape != second_gray.shape:
        return {"verified": False, "code": "dimensions_changed"}
    sift = cv2.SIFT_create(nfeatures=2000, contrastThreshold=0.025)
    first_keys, first_descriptors = sift.detectAndCompute(first_gray, None)
    second_keys, second_descriptors = sift.detectAndCompute(second_gray, None)
    if first_descriptors is None or second_descriptors is None:
        return {"verified": False, "code": "insufficient_texture"}
    pairs = cv2.BFMatcher().knnMatch(first_descriptors, second_descriptors, k=2)
    matches = [
        pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7
    ]
    minimum_inliers = (
        minimum_inliers
        if minimum_inliers is not None
        else (
            80
            if localized_command_support
            else DISTRIBUTED_CONNECTION_MINIMUM_INLIERS
        )
    )
    minimum_occupied_cells = (
        minimum_occupied_cells
        if minimum_occupied_cells is not None
        else 4
    )
    minimum_hull_fraction = (
        minimum_hull_fraction
        if minimum_hull_fraction is not None
        else (0.06 if localized_command_support else 0.12)
    )
    if len(matches) < minimum_inliers:
        return {"verified": False, "code": "insufficient_correspondences"}
    source = np.float32([first_keys[match.queryIdx].pt for match in matches])
    target = np.float32([second_keys[match.trainIdx].pt for match in matches])
    height, width = first_gray.shape
    candidates = []
    for candidate in range(3):
        if len(source) < minimum_inliers:
            return {
                "verified": False,
                "code": "correspondences_not_distributed",
                "model_candidates": candidates,
            }
        homography, inliers = _deterministic_homography(source, target)
        if homography is None or inliers is None or not np.isfinite(homography).all():
            return {
                "verified": False,
                "code": "correspondence_model_failed",
                "model_candidates": candidates,
            }
        inlier_mask = inliers.ravel().astype(bool)
        support = source[inlier_mask]
        cells = {(min(3, int(x * 4 / width)), min(2, int(y * 3 / height))) for x, y in support}
        supported_area = float(cv2.contourArea(cv2.convexHull(support))) if len(support) >= 3 else 0
        candidates.append(
            {
                "inliers": len(support),
                "occupied_cells": len(cells),
                "hull_fraction": supported_area / (width * height),
            }
        )
        if (
            len(support) >= minimum_inliers
            and len(cells) >= minimum_occupied_cells
            and supported_area >= width * height * minimum_hull_fraction
        ):
            # USAC selects the robust consensus; an ordinary least-squares fit
            # over that fixed inlier set removes seed-dependent model jitter
            # and gives static scenes a near-identity transform.
            refined, _ = cv2.findHomography(
                source[inlier_mask], target[inlier_mask], method=0
            )
            if refined is not None and np.isfinite(refined).all():
                homography = refined
            break
        # A distorted lens can produce a larger, spatially localized consensus
        # than the useful background. Remove only a spatially rejected consensus
        # and try at most two more candidates, retaining every original gate.
        source, target = source[~inlier_mask], target[~inlier_mask]
    else:
        return {
            "verified": False,
            "code": "correspondences_not_distributed",
            "model_candidates": candidates,
        }
    corners = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    transformed = cv2.perspectiveTransform(corners[None], homography)[0]
    if not cv2.isContourConvex(transformed):
        return {"verified": False, "code": "correspondence_model_folded"}
    # OpenCV's convex intersection can lose most of an otherwise identical
    # polygon when floating-point homography noise puts one corner a few
    # femtopixels outside the frame. Snap only numerical boundary residue; real
    # out-of-frame geometry remains untouched.
    for coordinate, maximum in ((0, width - 1), (1, height - 1)):
        values = transformed[:, coordinate]
        values[np.abs(values) <= 1e-3] = 0
        values[np.abs(values - maximum) <= 1e-3] = maximum
    intersection, _ = cv2.intersectConvexConvex(corners, transformed)
    grid = np.float32([[width * x, height * y] for y in (0.2, 0.5, 0.8) for x in (0.2, 0.5, 0.8)])
    displaced = cv2.perspectiveTransform(grid[None], homography)[0] - grid
    return {
        "verified": True,
        "support_scope": (
            "localized_command_transition"
            if localized_command_support
            else "distributed_scene"
        ),
        "source_features": len(first_keys),
        "target_features": len(second_keys),
        "inliers": len(support),
        "model_candidates": candidates,
        "analysis_size": [width, height],
        "overlap": float(np.clip(intersection / (width * height), 0, 1)),
        "shift_x": float(np.median(displaced[:, 0])),
        "shift_y": float(np.median(displaced[:, 1])),
        "displacement": float(np.percentile(np.linalg.norm(displaced, axis=1), 95)),
        "homography": homography.tolist(),
    }


def _match(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Independent distributed image correspondence for geometry and navigation."""
    return _correspondence_match(first, second, localized_command_support=False)


def _command_match(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Use localized support only inside a causal, stopped PTZ command fence."""
    matching = _match(first, second)
    if matching.get("verified") is True:
        return matching
    localized = _correspondence_match(
        first, second, localized_command_support=True
    )
    if localized.get("verified") is True:
        return localized
    # Some useful views contain texture in one broad horizontal strip (for
    # example a wall edge below a blank sky). Three occupied cells can still
    # prove a command-bound transition when it retains the same high inlier
    # floor and almost twice the required convex-hull area. Keep this evidence
    # in a distinct scope so it cannot silently weaken global geometry.
    banded = _correspondence_match(
        first,
        second,
        localized_command_support=True,
        minimum_inliers=80,
        minimum_occupied_cells=3,
        minimum_hull_fraction=0.10,
    )
    if banded.get("verified") is True:
        banded = {**banded, "support_scope": "banded_command_transition"}
        return banded
    # At an extreme PTZ pose, a wide-angle or strongly distorted image can
    # leave only a modest number of repeatable features even though those
    # features span the scene. Accept four fewer inliers only when their
    # support remains as broad as the ordinary distributed-scene contract.
    # This scope is command-bound: it cannot make an arbitrary pair of images
    # eligible for navigation, anchoring or a stationary-boundary decision.
    sparse_distributed = _correspondence_match(
        first,
        second,
        localized_command_support=True,
        minimum_inliers=20,
        minimum_occupied_cells=4,
        minimum_hull_fraction=0.12,
    )
    if sparse_distributed.get("verified") is True:
        return {
            **sparse_distributed,
            "support_scope": "sparse_distributed_command_transition",
        }
    return sparse_distributed


def _axis_command_match(
    first: np.ndarray,
    second: np.ndarray,
    *,
    axis: str | None,
) -> dict[str, Any]:
    """Qualify a command-bound endpoint without weakening global geometry.

    A camera movement can leave useful texture in one broad band while sky,
    ground or a nearby wall occupies the rest of the frame.  The ordinary
    command matcher deliberately rejects that support when its convex hull is
    slightly smaller than the generic band contract.  Inside a causal, stopped
    single-axis command we can use a narrower contract: the model still needs
    high correspondence support across three grid cells and its measured
    displacement must be predominantly on the commanded axis.  The distinct
    support scope keeps this evidence ineligible for navigation, anchoring,
    return validation and panorama geometry by itself.
    """
    matching = _command_match(first, second)
    if matching.get("verified") is True or axis not in {"pan", "tilt"}:
        return matching
    axis_banded = _correspondence_match(
        first,
        second,
        localized_command_support=True,
        minimum_inliers=120 if axis == "tilt" else 100,
        minimum_occupied_cells=3,
        minimum_hull_fraction=0.075 if axis == "tilt" else 0.07,
    )
    primary_key, cross_key = (
        ("shift_y", "shift_x") if axis == "tilt" else ("shift_x", "shift_y")
    )
    primary_shift = _finite(axis_banded.get(primary_key))
    cross_shift = _finite(axis_banded.get(cross_key))
    if (
        axis_banded.get("verified") is not True
        or axis_banded.get("overlap", 0) < 0.25
        or axis_banded.get("displacement", 0) < 2.0
        or primary_shift is None
        or cross_shift is None
        or abs(primary_shift) < 2.0
        or abs(primary_shift) < abs(cross_shift) * 1.25
    ):
        return matching
    return {
        **axis_banded,
        "support_scope": f"{axis}_banded_command_transition",
    }


async def _temporal_command_match(
    first: np.ndarray,
    frames: list[dict[str, Any]],
    *,
    axis: str,
) -> dict[str, Any]:
    """Require repeatable sparse geometry across a stopped endpoint window."""
    if axis not in {"pan", "tilt"}:
        return {"verified": False, "code": "temporal_endpoint_axis_invalid"}
    unique: list[dict[str, Any]] = []
    digests: set[bytes] = set()
    for frame in frames:
        image = frame.get("image") if isinstance(frame, dict) else None
        if not isinstance(image, np.ndarray) or image.size == 0:
            continue
        digest = hashlib.blake2b(
            np.ascontiguousarray(image).data, digest_size=16
        ).digest()
        if digest in digests:
            continue
        digests.add(digest)
        unique.append(frame)
    if len(unique) < 5:
        return {
            "verified": False,
            "code": "temporal_endpoint_frames_insufficient",
            "temporal_consensus": {"distinct_frames": len(unique)},
        }
    sampled_indices = sorted(
        set(np.linspace(0, len(unique) - 1, min(7, len(unique)), dtype=int).tolist())
    )

    async def estimate(index: int) -> tuple[int, dict[str, Any]]:
        matching = await asyncio.to_thread(
            _correspondence_match,
            first,
            unique[index]["image"],
            localized_command_support=True,
            minimum_inliers=24,
            minimum_occupied_cells=2,
            minimum_hull_fraction=0.03,
        )
        return index, matching

    estimates = await asyncio.gather(*(estimate(index) for index in sampled_indices))
    primary_key = "shift_x" if axis == "pan" else "shift_y"
    cross_key = "shift_y" if axis == "pan" else "shift_x"
    eligible: list[tuple[int, dict[str, Any], float]] = []
    for index, matching in estimates:
        primary = _finite(matching.get(primary_key))
        cross = _finite(matching.get(cross_key))
        if (
            matching.get("verified") is True
            and matching.get("overlap", 0) >= 0.25
            and matching.get("displacement", 0) >= 2.0
            and primary is not None
            and cross is not None
            and abs(primary) >= 2.0
            and abs(primary) >= abs(cross) * 1.5
        ):
            eligible.append((index, matching, primary))
    sampled = len(sampled_indices)
    required = max(5, math.ceil(sampled * 2 / 3))
    consensus = {
        "axis": axis,
        "distinct_frames": len(unique),
        "sampled_frames": sampled,
        "verified_models": len(eligible),
        "required_models": required,
    }
    if len(eligible) < required:
        return {
            "verified": False,
            "code": "temporal_endpoint_models_insufficient",
            "temporal_consensus": consensus,
        }
    positive = [item for item in eligible if item[2] > 0]
    negative = [item for item in eligible if item[2] < 0]
    directional = positive if len(positive) >= len(negative) else negative
    direction_sign = 1 if directional is positive else -1
    consensus.update(
        direction_sign=direction_sign,
        direction_consistent_models=len(directional),
    )
    if len(directional) < required:
        return {
            "verified": False,
            "code": "temporal_endpoint_consensus_unverified",
            "temporal_consensus": consensus,
        }
    median_primary = float(np.median([item[2] for item in directional]))
    tolerance = max(8.0, abs(median_primary) * 0.20)
    consistent = [
        item
        for item in directional
        if abs(item[2] - median_primary) <= tolerance
    ]
    required_magnitude = max(4, math.ceil(len(directional) / 2))
    maximum_deviation = max(
        (abs(item[2] - median_primary) for item in consistent), default=math.inf
    )
    consensus.update(
        consistent_models=len(consistent),
        required_magnitude_models=required_magnitude,
        median_primary_shift_pixels=median_primary,
        maximum_primary_deviation_pixels=maximum_deviation,
        allowed_primary_deviation_pixels=tolerance,
    )
    if len(consistent) < required_magnitude:
        return {
            "verified": False,
            "code": "temporal_endpoint_consensus_unverified",
            "temporal_consensus": consensus,
        }
    representative_index, representative, _primary = max(
        consistent, key=lambda item: item[0]
    )
    representative_frame = unique[representative_index]
    return {
        **representative,
        "support_scope": "temporal_command_transition",
        "temporal_consensus": consensus,
        "endpoint_frame_identity": {
            key: representative_frame.get(key)
            for key in (
                "capture_instance",
                "sequence",
                "generation",
                "received_monotonic",
            )
            if key in representative_frame
        },
    }


def _anchor_match(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Match one expected anchor with bounded localized support as a fallback."""
    matching = _match(first, second)
    if matching.get("verified") is True:
        return matching
    localized = _correspondence_match(first, second, localized_command_support=True)
    if localized.get("verified") is True:
        # Keep anchor evidence ineligible for command-transition arming. The
        # call site still imposes the narrow overlap and displacement gates for
        # the single expected destination.
        localized = {**localized, "support_scope": "localized_anchor"}
    return localized


def _no_effect_match(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Measure an expected stationary endpoint with narrow geometric support.

    This fallback is only evidence about displacement after a causal command.
    Its distinct scope keeps it ineligible for transition arming, capture and
    panorama geometry. Callers must additionally require the full observation
    budget, verified transport, high overlap and near-zero displacement.
    """
    matching = _match(first, second)
    if matching.get("verified") is True:
        return matching
    localized = _correspondence_match(
        first,
        second,
        localized_command_support=True,
        minimum_inliers=48,
        minimum_occupied_cells=2,
        minimum_hull_fraction=0.03,
    )
    if localized.get("verified") is True:
        return {**localized, "support_scope": "localized_no_effect"}
    # A low-detail endpoint can have fewer correspondences spread across a
    # much larger portion of the view. This second shape is stricter in area
    # and cell coverage, and remains limited to proving subpixel no-effect
    # after the complete command and transport observation budget.
    distributed_sparse = _correspondence_match(
        first,
        second,
        localized_command_support=True,
        minimum_inliers=24,
        minimum_occupied_cells=3,
        minimum_hull_fraction=0.10,
    )
    if distributed_sparse.get("verified") is True:
        return {
            **distributed_sparse,
            "support_scope": "sparse_distributed_no_effect",
        }
    return distributed_sparse


def _observational_connection_failure(matching: dict[str, Any]) -> str | None:
    """Identify a recoverable lack of visual support without weakening its gate."""
    if matching.get("verified") is True:
        overlap = _finite(matching.get("overlap"))
        return "connection_overlap_insufficient" if overlap is not None and overlap < 0.25 else None
    code = matching.get("code")
    if code in {
        "insufficient_texture",
        "insufficient_correspondences",
        "correspondences_not_distributed",
        "correspondence_model_failed",
        "correspondence_model_folded",
    }:
        return str(code)
    return None


def _next_continuous_seek_duration(
    duration: float,
    *,
    image_extent: int,
    image_shift: float,
    connection: dict[str, Any],
) -> tuple[float, str | None]:
    """Adapt the next pulse while preserving overlap through weak visual areas."""
    chained_methods = {
        "verified_precondition_chain",
        "fresh_seek_precondition_chain",
        "verified_recovery_chain",
        "native_pose_recovery_chain",
        "vertical_precondition_chain",
    }
    if connection.get("connection_method") in chained_methods:
        return max(MINIMUM_SEEK_PULSE_SECONDS, duration / 2), "chained_connection"
    source_features = connection.get("source_features")
    target_features = connection.get("target_features")
    if (
        type(source_features) is int
        and source_features > 0
        and type(target_features) is int
        and target_features / source_features < FULL_PULSE_MINIMUM_FEATURE_RETENTION
    ):
        return max(MINIMUM_SEEK_PULSE_SECONDS, duration / 2), "declining_visual_support"
    inliers = connection.get("inliers")
    if type(inliers) is int and inliers < FULL_PULSE_VISUAL_SUPPORT_INLIERS:
        return max(MINIMUM_SEEK_PULSE_SECONDS, duration / 2), "low_visual_support"
    if image_shift <= 2:
        return duration, None
    return (
        min(
            duration * 2,
            float(
                np.clip(
                    duration * image_extent * 0.4 / image_shift,
                    MINIMUM_SEEK_PULSE_SECONDS,
                    1.2,
                )
            ),
        ),
        None,
    )


def _vertical_band_spacing(
    state: dict[str, Any],
    *,
    connection: dict[str, Any],
    origin_connection: dict[str, Any],
    image_height: int,
) -> tuple[bool, dict[str, Any]]:
    """Accumulate verified tilt travel until the next row has useful overlap.

    A direct comparison with the row origin may disappear because that origin is
    untextured even while every adjacent movement remains well constrained.  Its
    absence therefore cannot mean that a new row is far enough away.  Consecutive
    verified vertical shifts provide the causal fallback without treating pulse
    duration or native position units as geometry.
    """
    previous = _finite(state.get("vertical_displacement_pixels")) or 0.0
    shift = _finite(connection.get("shift_y"))
    if connection.get("verified") is not True or shift is None:
        raise ValueError("A verified vertical connection with finite shift is required")
    accumulated = previous + abs(shift)
    target = max(1.0, float(image_height) * (1.0 - GRID_TARGET_OVERLAP))
    direct_overlap = (
        _finite(origin_connection.get("overlap"))
        if origin_connection.get("verified") is True
        else None
    )
    reached_by_origin = direct_overlap is not None and direct_overlap <= GRID_TARGET_OVERLAP
    reached_by_chain = accumulated >= target
    evidence = {
        "method": (
            "verified_origin_overlap" if reached_by_origin else "accumulated_vertical_shift"
        ),
        "vertical_displacement_pixels": accumulated,
        "target_displacement_pixels": target,
        "origin_overlap": direct_overlap,
        "origin_verified": origin_connection.get("verified") is True,
    }
    return reached_by_origin or reached_by_chain, evidence


class _Scan:
    def __init__(
        self,
        camera: Any,
        output_dir: Path,
        progress: Callable[[dict], Awaitable[Any]],
        cancelled: Callable[[], bool],
        checkpoint: dict | None,
        acquisition_policy: dict | None = None,
    ) -> None:
        self.camera = camera
        self.directory = Path(output_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.progress, self.cancelled = progress, cancelled
        self.checkpoint = dict(checkpoint or {})
        recorded_policy = self.checkpoint.get("acquisition_policy")
        self.region_policy = region_policy(recorded_policy if recorded_policy is not None else acquisition_policy)
        if recorded_policy is not None and acquisition_policy is not None and recorded_policy != acquisition_policy:
            raise PanoramaCaptureError("region_policy_incompatible")
        if self.checkpoint.get("physical_state") == "restored":
            epoch_value = self.checkpoint.get("return_epoch", "final")
            if isinstance(epoch_value, str) and epoch_value:
                # Preserve legacy restoration proof before an observational
                # resume persists its current stopped state.
                self.checkpoint["restored_return_epoch"] = epoch_value
        self._resuming = bool(checkpoint)
        self.captures = list(self.checkpoint.get("captures", []))
        self.issues: list[dict] = [
            issue
            for issue in self.checkpoint.get("issues", [])
            if issue.get("code") != "capture_interrupted"
        ]
        self.capabilities: dict = {}
        self.last_frame: dict | None = None
        self.last_pose: dict = {}
        self.saved_return: dict | None = self.checkpoint.get("return")
        self.physical_state = "unknown"
        self.started = time.monotonic()
        self.active_seconds = _finite(self.checkpoint.get("active_seconds")) or 0.0
        self.active_finished: float | None = None
        self.acquired = False
        self.returning = False
        self.boundaries: dict = dict(self.checkpoint.get("boundaries", {}))
        self.coverage: dict = {"kind": "attempted_reachable_domain", "boundaries": self.boundaries}
        self.complete = False
        self._stop_failed = False
        self._persistence_failed = False
        self._pending_telemetry: dict | None = None
        self.checkpoint.pop("last_settled_absolute", None)

    def _diagnostic(self, attempt: _AttemptDiagnostic) -> None:
        telemetry = attempt.telemetry()
        if telemetry is not None:
            self._pending_telemetry = telemetry
        diagnostics = self.checkpoint.setdefault("diagnostics", {"version": 1, "attempts": []})
        attempts = diagnostics.setdefault("attempts", [])
        attempts.append(attempt.finish())
        del attempts[:-MAX_DIAGNOSTIC_ATTEMPTS]
        try:
            _write_json(self.directory / "scan-diagnostics.json", diagnostics)
        except OSError:
            # Diagnostics must never mask the original failure or prevent Stop.
            if not any(issue.get("code") == "diagnostic_write_failed" for issue in self.issues):
                self.issues.append({"code": "diagnostic_write_failed"})

    async def _preserve_replay(self, attempt: _AttemptDiagnostic) -> None:
        """Persist only after Stop; never put private images in public telemetry."""
        outcome = "accepted" if attempt.value["outcome"] == "capture_stable" else "rejected"
        if not attempt.replay or len({path.stem for path in self.directory.glob(f"replay-{outcome}-*")}) >= MAX_REPLAYS_PER_OUTCOME:
            return
        identifier = f"replay-{outcome}-{uuid.uuid4().hex}"

        def write() -> None:
            # Decode one lossless diagnostic at a time, keeping the same NPZ
            # replay format without a second uncompressed frame collection.
            with zipfile.ZipFile(self.directory / f"{identifier}.npz", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, (encoded, _) in enumerate(attempt.replay):
                    frame = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
                    with archive.open(f"frame_{index}.npy", "w") as member:
                        np.lib.format.write_array(member, frame, allow_pickle=False)
            _write_json(self.directory / f"{identifier}.json", {
                "version": 1,
                "analysis_width": ANALYSIS_WIDTH,
                "started_monotonic": attempt.start,
                "complete": not attempt.value.get("replay_truncated", False),
                "scope": "consumed_frames_not_all_decoder_frames",
                "attempt": attempt.finish(),
                "frames": [record for _, record in attempt.replay],
            })

        await asyncio.to_thread(write)
        attempt.value["replay_id"] = identifier

    def _active_elapsed(self) -> float:
        now = self.active_finished if self.active_finished is not None and self.region_policy is None else time.monotonic()
        return self.active_seconds + max(0.0, now - self.started)

    def _check(self, *, budget: bool = True) -> None:
        if self.cancelled():
            raise _Stopped
        if self.region_policy is not None:
            maximum = self.region_policy["maximum_active_seconds"]
            if not self.returning:
                maximum -= self.region_policy["return_seconds_reserved"]
            if self._active_elapsed() >= maximum or (
                not self.returning and len(self.captures) >= self.region_policy["maximum_captures"]
            ):
                raise PanoramaCaptureError("region_budget_exhausted")
        if budget and (
            self._active_elapsed() >= MAX_JOB_SECONDS or len(self.captures) >= MAX_CAPTURES
        ):
            raise PanoramaCaptureError("scan_budget_exhausted")

    async def _emit(self, phase: str, **extra: Any) -> None:
        # Send a completed movement with the next normal progress update. No
        # extra callback can delay Stop or replace the original movement error.
        telemetry = self._pending_telemetry
        await self.progress(
            {
                "phase": phase,
                "status": "returning"
                if self.returning
                else ("exploring" if phase == "exploring" else "capturing"),
                "captures_accepted": len(self.captures),
                "physical_state": self.physical_state,
                "coverage_progress": self._coverage_progress(),
                **({"telemetry": telemetry} if telemetry is not None else {}),
                **extra,
            }
        )
        if self._pending_telemetry is telemetry:
            self._pending_telemetry = None

    async def _persist(self) -> None:
        pending_returns = getattr(self.camera, "pending_return_destinations", None)
        if pending_returns is not None:
            intents = pending_returns()
            if intents:
                recorded = self.checkpoint.setdefault("pending_returns", [])
                for intent in intents:
                    recorded[:] = [item for item in recorded if not (
                        item.get("kind") == intent.get("kind")
                        and item.get("role") == intent.get("role")
                        and item.get("preset_name") == intent.get("preset_name")
                    )]
                    recorded.append(intent)
        self.checkpoint.update(
            version=1,
            source_identity=self.capabilities.get("source_identity", {}),
            captures=self.captures,
            boundaries=self.boundaries,
            active_seconds=self._active_elapsed(),
            issues=self.issues,
            **{"return": self.saved_return},
        )
        _write_json(
            self.directory / "scan-manifest.json",
            {
                **self.checkpoint,
                "issues": self.issues,
                "physical_state": self.physical_state,
            },
        )
        await self._emit(
            "returning" if self.returning else "capturing",
            captures=self.captures,
            checkpoint=self.checkpoint,
        )

    def _record_internal_failure(self, error: Exception, *, stage: str) -> None:
        """Persist a bounded local traceback without exception messages or transports."""
        records = getattr(self, "_internal_failures", [])
        frames = []
        trace = error.__traceback__
        while trace is not None:
            name = trace.tb_frame.f_code.co_name
            frames.append(
                {
                    "file": Path(trace.tb_frame.f_code.co_filename).name[:120],
                    "function": name[:120] if re.fullmatch(r"[A-Za-z0-9_<>]+", name) else "unknown",
                    "line": max(0, int(trace.tb_lineno)),
                }
            )
            trace = trace.tb_next
        error_type = type(error).__name__
        records.append(
            {
                "stage": stage,
                "type": (
                    error_type[:120]
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", error_type)
                    else "Exception"
                ),
                "frames": frames[-12:],
            }
        )
        self._internal_failures = records[-4:]
        try:
            _write_json(
                self.directory / "internal-failure.json",
                {"failures": self._internal_failures},
            )
        except Exception:
            # This diagnostic must never interfere with physical cleanup.
            pass

    async def _stop(self) -> bool:
        try:
            await self.camera.stop()
            # A fenced accepted command is still not visual confirmation.
            self.physical_state = "unknown"
            return True
        except PanoramaCaptureError as error:
            self.physical_state = (
                "ownership_lost" if error.code == "control_lost" else "stop_unconfirmed"
            )
        except Exception:
            self.physical_state = "stop_unconfirmed"
        self._stop_failed = True
        return False

    async def _reference_window(self, *, timeout: float = ATTEMPT_SECONDS) -> dict:
        """Observe a stationary reference; never publish it as a qualified capture.

        The initial return reference has no prior causal movement. This weaker
        observational evidence is explicitly restricted to restoring framing.
        """
        detector = VisualStabilityDetector(allow_observation_timing=True)
        diagnostic = _AttemptDiagnostic("return_reference", window_seconds=timeout)
        total_deadline = diagnostic.start + ATTEMPT_SECONDS
        observation_start: float | None = None
        first = None
        count = 0
        try:
            while time.monotonic() < total_deadline and (
                observation_start is None or time.monotonic() - observation_start < timeout
            ):
                wait_start = time.monotonic()
                remaining = total_deadline - wait_start
                if remaining <= 1e-6:
                    break
                try:
                    frame = await asyncio.wait_for(
                        self.camera.frame(timeout_s=min(remaining, 3.0)),
                        timeout=remaining,
                    )
                except TimeoutError:
                    if diagnostic.value["frame_count"]:
                        diagnostic.value["budget_hit"] = "observation"
                        diagnostic.value["outcome"] = "stop_observation_unconfirmed"
                        raise PanoramaCaptureError("stop_observation_unconfirmed") from None
                    diagnostic.value["outcome"] = "frame_acquisition_timeout"
                    raise PanoramaCaptureError("frame_acquisition_timeout") from None
                except PanoramaCaptureError as error:
                    if error.code != "fresh_frame_unavailable":
                        raise
                    diagnostic.value["transient_frame_failures"] = (
                        diagnostic.value.get("transient_frame_failures", 0) + 1
                    )
                    first, count = None, 0
                    remaining = total_deadline - time.monotonic()
                    if remaining > 0:
                        # The adapter may spend one attempt on each configured
                        # transport. A transient failure can use the remaining
                        # original acquisition budget, never a restarted clock.
                        await asyncio.sleep(min(0.05, remaining))
                    continue
                now = time.monotonic()
                self.last_frame = frame
                if observation_start is None:
                    observation_start = now
                    detector.reset(now=now)
                result = await asyncio.to_thread(self._observation, detector, frame, None)
                diagnostic.frame(frame, result, wait_seconds=now - wait_start)
                if now >= total_deadline or now - observation_start >= timeout:
                    break
                eligible = result.get("code") in {
                    "motion_transition_unobserved",
                    "settling",
                    "stable",
                }
                if result.get("code") == "repeated_image":
                    # A duplicate contributes no evidence, but it also does not
                    # erase distinct candidates. The detector retains their
                    # continuity fence and rejects a prolonged freeze itself.
                    continue
                if not eligible:
                    first, count = None, 0
                    continue
                first = first or frame
                count += 1
                diagnostic.value["maximum_candidate_frames"] = max(
                    diagnostic.value.get("maximum_candidate_frames", 0),
                    count,
                )
                if count >= 5 and frame["received_monotonic"] - first["received_monotonic"] >= 0.8:
                    comparison = await asyncio.to_thread(_match, first["image"], frame["image"])
                    diagnostic.comparison(comparison)
                    if time.monotonic() >= total_deadline:
                        break
                    if comparison.get("verified") and comparison["displacement"] <= 0.5:
                        frame = {
                            **frame,
                            "return_reference_evidence": {
                                "evidence": "local_observation_only",
                                "provisional": True,
                                "window_observation_seconds": frame["received_monotonic"]
                                - first["received_monotonic"],
                                "drift_pixels": comparison["displacement"],
                            },
                        }
                        self.last_frame = frame
                        diagnostic.value["outcome"] = "reference_observed"
                        return frame
                    first, count = frame, 1
            diagnostic.value["budget_hit"] = (
                "total" if total_deadline - time.monotonic() <= 1e-6 else "observation"
            )
            if observation_start is None and diagnostic.value.get("transient_frame_failures"):
                raise PanoramaCaptureError("fresh_frame_unavailable")
            raise PanoramaCaptureError("stop_observation_unconfirmed")
        except PanoramaCaptureError as error:
            diagnostic.value["outcome"] = error.code
            raise
        finally:
            self._diagnostic(diagnostic)

    async def _confirm_stop(self) -> None:
        if not await self._stop():
            return
        try:
            await self._reference_window(timeout=3.0)
            self.physical_state = "stopped"
        except Exception:
            self.physical_state = "stop_unconfirmed"

    async def _commit_endpoint_frame(
        self,
        candidate: dict[str, Any],
        diagnostic: _AttemptDiagnostic,
        *,
        deadline: float,
    ) -> bool:
        """Confirm that a qualified endpoint survives the next stream window.

        A camera can acknowledge Stop while its encoder still publishes a
        short stationary-looking backlog. The endpoint is committed only when
        newer frames from the same decoder generation remain registered with
        the qualified candidate. This is a stream observation barrier; it does
        not infer position from a timer.
        """
        candidate_instance = candidate.get("capture_instance")
        candidate_generation = candidate.get("generation")
        candidate_sequence = candidate.get("sequence")
        started = time.monotonic()
        commit_deadline = min(deadline, started + ENDPOINT_COMMIT_MAX_SECONDS)
        first_received: float | None = None
        previous_sequence = candidate_sequence
        confirmations = 0
        last_matching: dict[str, Any] | None = None
        reason = "commit_window_unavailable"
        while time.monotonic() < commit_deadline:
            remaining = commit_deadline - time.monotonic()
            if remaining <= 1e-6:
                break
            try:
                frame = await asyncio.wait_for(
                    self.camera.frame(timeout_s=min(remaining, 0.5)),
                    timeout=remaining,
                )
            except TimeoutError:
                reason = "fresh_frame_unavailable"
                break
            except PanoramaCaptureError as error:
                if error.code != "fresh_frame_unavailable":
                    raise
                reason = error.code
                break
            self.last_frame = frame
            received = _finite(frame.get("received_monotonic"))
            sequence = frame.get("sequence")
            if (
                not isinstance(candidate_instance, str)
                or frame.get("capture_instance") != candidate_instance
                or type(candidate_generation) is not int
                or frame.get("generation") != candidate_generation
                or type(previous_sequence) is not int
                or type(sequence) is not int
                or sequence <= previous_sequence
                or received is None
                or not 0 <= time.monotonic() - received <= 1.0
            ):
                reason = "frame_identity_unverified"
                break
            previous_sequence = sequence
            last_matching = await asyncio.to_thread(
                _no_effect_match, candidate["image"], frame["image"]
            )
            if (
                last_matching.get("verified") is not True
                or last_matching.get("overlap", 0) < UNCONFIRMED_NO_EFFECT_OVERLAP
                or last_matching.get("displacement", math.inf)
                > UNCONFIRMED_NO_EFFECT_PIXELS
            ):
                reason = "endpoint_changed"
                break
            first_received = received if first_received is None else first_received
            confirmations += 1
            if (
                confirmations >= ENDPOINT_COMMIT_MINIMUM_FRAMES
                and received - first_received >= ENDPOINT_COMMIT_WINDOW_SECONDS
            ):
                diagnostic.value["endpoint_commit"] = {
                    "verified": True,
                    "frames": confirmations,
                    "window_seconds": received - first_received,
                    "last_sequence": sequence,
                    "overlap": last_matching.get("overlap"),
                    "displacement": last_matching.get("displacement"),
                }
                return True
        diagnostic.value["endpoint_commit"] = {
            "verified": False,
            "reason": reason,
            "frames": confirmations,
            **(
                {
                    "overlap": last_matching.get("overlap"),
                    "displacement": last_matching.get("displacement"),
                    "code": last_matching.get("code"),
                }
                if isinstance(last_matching, dict)
                else {}
            ),
        }
        return False

    async def _terminal_scene(self, frames: deque[dict]) -> dict | None:
        """Classify observable detail after a failed attempt, never readiness.

        A fresh ordered window is essential: a stalled decoder or outage must
        not be relabeled as an empty scene simply because its last image is gray.
        """
        if not frames:
            return None
        last = frames[-1]
        received = _finite(last.get("received_monotonic"))
        if received is None or not 0 <= time.monotonic() - received <= 1.0:
            return None
        # Each sequence is assigned only after the source decoder receives a
        # frame. A motionless low-entropy scene may therefore produce identical
        # pixels while transport remains live. This method classifies that
        # ordered transport window; it never qualifies a photograph by itself.
        raw_window = list(frames)
        media_available = all(
            _finite(frame.get("media_time")) is not None for frame in raw_window
        )
        if not media_available and any(frame.get("media_time") is not None for frame in raw_window):
            return None
        window = [raw_window[0]]
        for previous, current in zip(raw_window, raw_window[1:], strict=False):
            repeated = np.array_equal(previous["image"], current["image"])
            if (
                not isinstance(previous.get("capture_instance"), str)
                or not previous["capture_instance"].strip()
                or previous.get("capture_instance") != current.get("capture_instance")
                or previous.get("generation") != current.get("generation")
                or type(previous.get("generation")) is not int
                or type(current.get("generation")) is not int
                or current["generation"] < 0
                or type(previous.get("sequence")) is not int
                or type(current.get("sequence")) is not int
                or previous["sequence"] < 0
                or previous["sequence"] >= current["sequence"]
                or previous["image"].shape != current["image"].shape
                or previous.get("source_width", previous["image"].shape[1])
                != current.get("source_width", current["image"].shape[1])
                or previous.get("source_height", previous["image"].shape[0])
                != current.get("source_height", current["image"].shape[0])
                or not 0 < current["received_monotonic"] - previous["received_monotonic"] <= 1.0
                or (
                    media_available
                    and not (
                        0 <= current["media_time"] - previous["media_time"] <= 1.0
                        if repeated
                        else 0 < current["media_time"] - previous["media_time"] <= 1.0
                    )
                )
            ):
                return None
            if not repeated:
                window.append(current)
        first_received = _finite(raw_window[0].get("received_monotonic"))
        if len(raw_window) < 5 or first_received is None or received - first_received < 0.8:
            return None
        texture = await asyncio.to_thread(_texture_support, last["image"])
        age = time.monotonic() - received
        if not 0 <= age <= 1.0:
            return None
        return {
            "classification": "scene_texture_available"
            if texture["distributed"]
            else "scene_texture_insufficient",
            "texture_support": texture,
            "sequence": last["sequence"],
            "generation": last["generation"],
            "age_seconds": age,
            "observed_window_seconds": received - raw_window[0]["received_monotonic"],
            "distinct_frames": len(window),
            "received_frames": len(raw_window),
            "visual_change_observed": len(window) > 1,
            "transport_window_verified": True,
            "timing_basis": "media" if media_available else "local_observation",
            "evidence": "observation_only",
            "qualified_capture": False,
            "image_representation": last.get("image_representation", "source"),
            "image_width": last["image"].shape[1],
            "image_height": last["image"].shape[0],
            "source_width": last.get("source_width", last["image"].shape[1]),
            "source_height": last.get("source_height", last["image"].shape[0]),
        }

    async def _late_endpoint_capture(
        self,
        *,
        baseline: dict,
        frames: deque[dict],
        terminal_frames: deque[dict],
        transition: dict[str, Any] | None,
        diagnostic: _AttemptDiagnostic,
        command_finished: bool,
        stopped: bool,
    ) -> dict | None:
        """Qualify a missed fast transition without weakening capture gates.

        The durable command baseline proves which attempt produced the later
        endpoint. A fresh detector then replays the terminal window and still
        requires its ordinary stability, continuity and image-quality gates.
        """
        cursor = self.checkpoint.get("continuous_cursor")
        intent = transition.get("intent") if isinstance(transition, dict) else None
        saved = transition.get("baseline") if isinstance(transition, dict) else None
        movement_id = (
            _movement_attempt_id(transition.get("movement_id"))
            if isinstance(transition, dict)
            else None
        )
        if (
            self.returning
            or not command_finished
            or not stopped
            or self._stop_failed
            or not isinstance(cursor, dict)
            or cursor.get("transition") is not transition
            or not isinstance(transition, dict)
            or transition.get("state") != "pending"
            or movement_id is None
            or diagnostic.value.get("movement_id") != movement_id
            or diagnostic.value.get("command_outcome") != "accepted"
            or diagnostic.value.get("stop_command_accepted") is not True
            or not isinstance(intent, dict)
            or intent.get("type") not in {
                "seek_pulse", "connection_halfstep", "limit_probe", "reference_probe"
            }
            or not isinstance(saved, dict)
        ):
            return None
        baseline_instance = baseline.get("capture_instance")
        baseline_sequence = baseline.get("sequence")
        baseline_generation = baseline.get("generation")
        if (
            not isinstance(baseline_instance, str)
            or not baseline_instance.strip()
            or saved.get("capture_instance") != baseline_instance
            or type(baseline_sequence) is not int
            or baseline_sequence <= 0
            or saved.get("sequence") != baseline_sequence
            or type(baseline_generation) is not int
            or baseline_generation < 0
            or saved.get("generation") != baseline_generation
            or saved.get("media_time") != baseline.get("media_time")
            or saved.get("received_monotonic") != baseline.get("received_monotonic")
        ):
            diagnostic.value["late_endpoint_rejection"] = "baseline_identity_unverified"
            return None
        scene = await self._terminal_scene(terminal_frames)
        if scene is None or scene.get("classification") != "scene_texture_available":
            diagnostic.value["late_endpoint_rejection"] = "terminal_window_unverified"
            return None
        count = scene.get("received_frames")
        if type(count) is not int or not 5 <= count <= len(terminal_frames):
            diagnostic.value["late_endpoint_rejection"] = "terminal_window_unverified"
            return None
        window: list[dict[str, Any]] = list(terminal_frames)
        if len(window) != count:
            diagnostic.value["late_endpoint_rejection"] = "terminal_window_unverified"
            return None
        first_terminal, last_terminal = window[0], window[-1]
        terminal_media_available = scene.get("timing_basis") == "media"
        baseline_media = _finite(saved.get("media_time"))
        terminal_media = _finite(last_terminal.get("media_time"))
        if (
            any(frame.get("capture_instance") != baseline_instance for frame in window)
            or last_terminal.get("generation") != baseline_generation
            or type(last_terminal.get("sequence")) is not int
            or last_terminal["sequence"] <= baseline_sequence
            or _finite(first_terminal.get("received_monotonic")) is None
            or first_terminal["received_monotonic"] <= baseline["received_monotonic"]
            or (baseline_media is None) != (not terminal_media_available)
            or (baseline_media is not None and (terminal_media is None or terminal_media <= baseline_media))
        ):
            diagnostic.value["late_endpoint_rejection"] = "endpoint_identity_unverified"
            return None
        integral = next(
            (
                frame
                for frame in reversed(frames)
                if frame.get("capture_instance") == baseline_instance
                and frame.get("generation") == last_terminal["generation"]
                and frame.get("sequence") == last_terminal["sequence"]
                and frame.get("received_monotonic") == last_terminal["received_monotonic"]
            ),
            None,
        )
        if integral is None:
            diagnostic.value["late_endpoint_rejection"] = "integral_frame_unavailable"
            return None
        try:
            path = Path(saved.get("path", "")).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file():
                raise PanoramaCaptureError("invalid_checkpoint_file")
            digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
            if digest != saved.get("sha256"):
                raise PanoramaCaptureError("invalid_checkpoint_image")
            saved_image = self._private_image(str(path))
        except (OSError, PanoramaCaptureError):
            diagnostic.value["late_endpoint_rejection"] = "durable_baseline_unavailable"
            return None
        matching = await asyncio.to_thread(
            _axis_command_match,
            saved_image,
            last_terminal["image"],
            axis=intent.get("axis"),
        )
        diagnostic.comparison(matching)
        if (
            matching.get("verified") is not True
            or matching.get("overlap", 0) < 0.25
            or matching.get("displacement", 0) < 2.0
        ):
            diagnostic.value["late_endpoint_single_match"] = {
                key: matching.get(key)
                for key in (
                    "verified",
                    "code",
                    "support_scope",
                    "inliers",
                    "model_candidates",
                    "overlap",
                    "displacement",
                    "shift_x",
                    "shift_y",
                )
                if key in matching
            }
            matching = await _temporal_command_match(
                saved_image,
                window,
                axis=intent.get("axis"),
            )
            diagnostic.comparison(matching)
        diagnostic.value["late_endpoint_transition_match"] = {
            key: matching.get(key)
            for key in (
                "verified",
                "code",
                "support_scope",
                "inliers",
                "model_candidates",
                "overlap",
                "displacement",
                "shift_x",
                "shift_y",
                "temporal_consensus",
                "endpoint_frame_identity",
            )
            if key in matching
        }
        if (
            matching.get("verified") is not True
            or matching.get("overlap", 0) < 0.25
            or matching.get("displacement", 0) < 2.0
        ):
            diagnostic.value["late_endpoint_rejection"] = (
                "endpoint_transition_match_unverified"
            )
            return None
        matched_integral = integral
        if matching.get("support_scope") == "temporal_command_transition":
            endpoint_identity = matching.get("endpoint_frame_identity")
            if not isinstance(endpoint_identity, dict):
                diagnostic.value["late_endpoint_rejection"] = (
                    "temporal_endpoint_frame_unavailable"
                )
                return None
            matched_integral = next(
                (
                    frame
                    for frame in reversed(frames)
                    if all(
                        frame.get(key) == endpoint_identity.get(key)
                        for key in (
                            "capture_instance",
                            "sequence",
                            "generation",
                            "received_monotonic",
                        )
                    )
                ),
                None,
            )
            if matched_integral is None:
                diagnostic.value["late_endpoint_rejection"] = (
                    "temporal_endpoint_frame_unavailable"
                )
                return None
        detector = VisualStabilityDetector(allow_observation_timing=True)
        detector.reset(now=first_terminal["received_monotonic"])
        detector.arm_stop(now=first_terminal["received_monotonic"])
        armed = detector.arm_verified_endpoint_transition(matching)
        if not armed.get("has_motion_transition"):
            diagnostic.value["late_endpoint_rejection"] = (
                "endpoint_transition_arming_unverified"
            )
            return None
        result: dict[str, Any] = armed
        for frame in window:
            result = await asyncio.to_thread(self._observation, detector, frame, None)
        received = _finite(matched_integral.get("received_monotonic"))
        if (
            result.get("stable") is not True
            or received is None
            or not 0 <= time.monotonic() - received <= 1.0
        ):
            diagnostic.value["late_endpoint_rejection"] = "terminal_stability_unverified"
            return None
        qualified = {
            (candidate.get("sequence"), candidate.get("generation"))
            for candidate in result.get("qualified_frames", [])
            if isinstance(candidate, dict)
        }
        # The matched integral frame is the exact source representation of the
        # endpoint already proven above. Prefer it when it belongs to the
        # approved stability window: rematching a different, slightly sharper
        # frame can select another consensus on repetitive walls and discard a
        # transition whose geometry and quality were already independently
        # proven.
        if (
            matched_integral.get("sequence"),
            matched_integral.get("generation"),
        ) in qualified:
            selected = matched_integral
            selected_match = matching
            result["best_sequence"] = matched_integral["sequence"]
            diagnostic.value["late_endpoint_selection"] = (
                "verified_temporal_frame"
                if matching.get("support_scope") == "temporal_command_transition"
                else "verified_terminal_frame"
            )
        else:
            selected = _selected_capture_frame(frames, result, {
                "capture_instance": baseline_instance, "generation": baseline_generation,
            })
            selected_match = (
                await asyncio.to_thread(
                    _axis_command_match,
                    saved_image,
                    selected["image"],
                    axis=intent.get("axis"),
                )
                if selected is not None
                else None
            )
            diagnostic.value["late_endpoint_selection"] = "ranked_qualified_frame"
        if selected is None:
            diagnostic.value["late_endpoint_rejection"] = "selected_capture_frame_unavailable"
            return None
        if (selected_match is None
                or selected_match.get("verified") is not True
                or selected_match.get("overlap", 0) < 0.25
                or selected_match.get("displacement", 0) < 2.0):
            diagnostic.value["late_endpoint_rejection"] = (
                "selected_endpoint_transition_unverified"
            )
            return None
        axis = intent.get("axis")
        primary_shift = _finite(
            selected_match.get("shift_x" if axis == "pan" else "shift_y")
        )
        cross_shift = _finite(
            selected_match.get("shift_y" if axis == "pan" else "shift_x")
        )
        if (
            axis not in {"pan", "tilt"}
            or primary_shift is None
            or cross_shift is None
            or abs(primary_shift) < 2.0
            or abs(primary_shift) < abs(cross_shift) * 1.25
        ):
            diagnostic.value["late_endpoint_rejection"] = "endpoint_axis_unverified"
            return None
        diagnostic.value.update(
            late_endpoint_transition=True,
            late_endpoint_movement_id=movement_id,
            terminal_scene=scene,
        )
        diagnostic.value.pop("late_endpoint_rejection", None)
        return {
            "frame": selected,
            "evidence": result,
            "match": selected_match,
            "stable": True,
            "stationary": False,
        }

    async def _preserve_rejected(
        self, frame: dict, scene: dict, diagnostic: _AttemptDiagnostic
    ) -> None:
        records = self.checkpoint.setdefault("rejected_observations", [])
        # Count files as well as records so a crash between image and manifest
        # persistence cannot grow retention or overwrite an orphaned reference.
        if (
            len(records) >= MAX_REJECTED_OBSERVATIONS
            or sum(1 for _ in self.directory.glob("rejected-observation-*.jpg"))
            >= MAX_REJECTED_OBSERVATIONS
        ):
            return
        identifier = f"rejected-observation-{uuid.uuid4().hex}"
        path = self.directory / f"{identifier}.jpg"
        try:
            digest = await asyncio.to_thread(_write_image, path, frame["image"])
        except Exception:
            if not any(
                issue.get("code") == "rejected_observation_write_failed" for issue in self.issues
            ):
                self.issues.append({"code": "rejected_observation_write_failed"})
            return
        records.append(
            {
                "id": identifier,
                "path": str(path),
                "sha256": digest,
                "rejection_code": diagnostic.value["outcome"],
                "nominal_target": diagnostic.value.get("requested_target"),
                **scene,
            }
        )
        diagnostic.value["rejected_observation_id"] = identifier

    @staticmethod
    def _observation(detector: VisualStabilityDetector, frame: dict, pose: dict | None) -> dict:
        return detector.observe(
            frame["image"],
            media_time=frame.get("media_time"),
            received_monotonic=frame["received_monotonic"],
            sequence=frame["sequence"],
            generation=frame["generation"],
            pose=pose,
            physical_timestamp_verified=bool(frame.get("physical_timestamp_verified", False)),
        )

    async def _preserve_motion_pair(
        self, before: dict, after: dict, diagnostic: _AttemptDiagnostic
    ) -> None:
        records = self.checkpoint.setdefault("rejected_motion_pairs", [])
        if len(records) >= 8 or sum(1 for _ in self.directory.glob("rejected-pair-*.jpg")) >= 16:
            return
        identifier = f"rejected-pair-{uuid.uuid4().hex}"
        record = {"id": identifier, "code": diagnostic.value["outcome"], "qualified_capture": False}
        for name, frame in (("before", before), ("after", after)):
            path = self.directory / f"{identifier}-{name}.jpg"
            digest = await asyncio.to_thread(_write_image, path, _gray(frame["image"]))
            record[name] = {
                "path": str(path),
                "sha256": digest,
                "capture_instance": frame.get("capture_instance"),
                "generation": frame.get("generation"),
                "sequence": frame.get("sequence"),
            }
        records.append(record)
        diagnostic.value["rejected_pair_id"] = identifier

    async def _materialize_normal_return_epoch(self) -> None:
        """Persist one epoch immediately before a normal outbound sequence."""
        if (
            self.returning
            or not self.saved_return
            or not self.checkpoint.get("initial_path")
        ):
            return
        value = self.checkpoint.get("return_epoch", "final")
        current_epoch = value if isinstance(value, str) and value else "final"
        if not _return_epoch_has_coarse_intent(self.checkpoint, current_epoch):
            # No return has been attempted for this outbound sequence. Reuse
            # its persisted epoch across every grid movement and any restart.
            return
        previously_restored = bool(
            self.checkpoint.get("restored_return_epoch") == current_epoch
            or self.checkpoint.get("physical_state") == "restored"
            or self.physical_state == "restored"
        )
        if not previously_restored:
            raise PanoramaCaptureError("return_framing_unconfirmed")
        self.checkpoint["return_epoch"] = f"normal:{uuid.uuid4().hex}"
        # A legacy physical-state value only proves the preceding epoch. The
        # dedicated restored marker is keyed, so it cannot authorize this one.
        self.checkpoint.pop("physical_state", None)
        await self._persist()
        self._check()

    def _prune_transition_baselines(self, keep: str | None) -> None:
        keep_path = Path(keep).resolve() if keep else None
        for candidate in self.directory.glob("transition-*.jpg"):
            resolved = candidate.resolve()
            if resolved == keep_path or not resolved.is_relative_to(self.directory):
                continue
            try:
                candidate.unlink()
            except OSError:
                # Retention cleanup is best effort. It must never erase the
                # durable intent or mask the camera command's actual outcome.
                pass

    async def _move(
        self,
        command: Callable[[], Awaitable[Any]],
        *,
        target: dict | None = None,
        duration: float = 0.0,
        allow_stationary: bool = False,
        requested_velocity: dict[str, float] | None = None,
        expected_frame: dict | None = None,
        movement_intent: dict[str, Any] | None = None,
        persist_recovery_return_intent: bool = False,
        connection_recovery_phase: str | None = None,
        attempt_seconds: float = ATTEMPT_SECONDS,
    ) -> dict:
        """Observe the actual movement concurrently with its controller command."""
        self._check(budget=not self.returning)
        if not self.returning:
            await self._materialize_normal_return_epoch()
        command_start_pose = copy.deepcopy(self.last_pose)
        # A progress/storage callback may wait. Finish it before starting motion,
        # then obtain a fresh baseline that causally precedes the command.
        # The previous settled pose says nothing about this new movement.
        self.physical_state = "unknown"
        await self._emit("waiting_for_stability")
        cursor = self.checkpoint.get("continuous_cursor")
        if cursor is not None:
            cursor["resume_anchor_verified"] = False
        transition: dict[str, Any] | None = None
        if cursor is not None and not self.returning:
            transition = {
                "movement_id": uuid.uuid4().hex,
                "state": "pending",
                "stage": cursor["stage"],
                "row": cursor["row"],
                "direction": cursor["direction"],
                "branch": cursor["branch"],
            }
            if movement_intent is not None:
                state_key = "seek" if movement_intent.get("axis") == "pan" else "vertical_seek"
                seek_state = cursor.get(state_key)
                anchor = cursor.get("anchor")
                if isinstance(seek_state, dict) and isinstance(anchor, dict):
                    transition["intent"] = {
                        "type": movement_intent.get("type", "seek_pulse"),
                        "axis": movement_intent.get("axis"),
                        "direction": movement_intent.get("direction"),
                        "row": seek_state.get("row"),
                        "step": movement_intent.get("step", seek_state.get("steps")),
                        "duration": movement_intent.get("duration"),
                        "anchor": {
                            key: anchor.get(key) for key in ("capture_id", "path", "row")
                        },
                    }
                    if isinstance(movement_intent.get("target"), dict):
                        transition["intent"]["target"] = {
                            axis: movement_intent["target"].get(axis)
                            for axis in ("pan", "tilt")
                        }
                elif (
                    cursor.get("stage") == "reference"
                    and movement_intent.get("type") == "reference_probe"
                    and movement_intent.get("axis") in {"pan", "tilt"}
                    and movement_intent.get("direction") in {-1, 1}
                    and _finite(movement_intent.get("duration")) is not None
                ):
                    # The first probe has no accepted anchor. Its persisted
                    # baseline can still prove a missed transition during this
                    # attempt, while a restart remains deliberately unresumable.
                    transition["intent"] = {
                        "type": "reference_probe",
                        "axis": movement_intent["axis"],
                        "direction": movement_intent["direction"],
                        "row": cursor.get("row"),
                        "duration": movement_intent["duration"],
                    }
        verification = self.checkpoint.get("control_verification")
        if verification is not None:
            # Reserve the diagnostic command before obtaining its causal frame.
            # If video or a later precondition fails, consuming budget is safer
            # than replaying a command whose dispatch is uncertain after restart.
            verification["commands_attempted"] = verification.get("commands_attempted", 0) + 1
            await self._persist()
            self._check(budget=not self.returning)
        baseline = await self.camera.frame()
        self.last_frame = baseline
        detector = VisualStabilityDetector(allow_observation_timing=True)
        start = time.monotonic()
        attempt_deadline = start + attempt_seconds
        observation_deadline = attempt_deadline
        detector.reset(now=start)
        diagnostic = _AttemptDiagnostic("movement", total_seconds=attempt_seconds)
        diagnostic.value.update(
            movement_id=(transition or {}).get("movement_id"),
            command_kind="absolute"
            if target is not None
            else ("velocity" if duration > 0 else "return"),
            command_outcome="not_issued",
            pulse_seconds=duration,
            allow_stationary=allow_stationary,
            requested_target=dict(target) if target is not None else None,
        )
        precondition: dict[str, Any] | None = None
        if requested_velocity is not None:
            diagnostic.value["requested_velocity"] = dict(requested_velocity)
        if expected_frame is not None:
            precondition = await asyncio.to_thread(
                _match, expected_frame["image"], baseline["image"]
            )
            for reobservation in range(2):
                precondition_overlap = _finite(precondition.get("overlap"))
                precondition_displacement = _finite(precondition.get("displacement"))
                baseline_received = _finite(baseline.get("received_monotonic"))
                baseline_age = (
                    time.monotonic() - baseline_received
                    if baseline_received is not None
                    else math.inf
                )
                precondition_details = {
                    "verified": precondition.get("verified") is True,
                    "overlap": precondition_overlap,
                    "displacement": precondition_displacement,
                    "code": precondition.get("code"),
                    "frame_age_seconds": baseline_age if math.isfinite(baseline_age) else None,
                }
                diagnostic.value["correction_precondition"] = precondition_details
                accepted_precondition = bool(
                    precondition.get("verified") is True
                    and precondition_overlap is not None
                    and precondition_overlap >= 0.85
                    and precondition_displacement is not None
                    and precondition_displacement <= CORRECTION_PRECONDITION_PIXELS
                    and 0 <= baseline_age <= MAX_CORRECTION_FRAME_AGE_SECONDS
                )
                if accepted_precondition:
                    break
                if reobservation:
                    self.physical_state = "stopped"
                    diagnostic.value["outcome"] = "correction_precondition_changed"
                    try:
                        await self._preserve_motion_pair(expected_frame, baseline, diagnostic)
                    except OSError:
                        diagnostic.value["rejected_pair_unavailable"] = True
                    self._diagnostic(diagnostic)
                    raise PanoramaCaptureError("correction_precondition_changed")

                # The command has not been created or dispatched. Preserve the
                # rejected evidence, then spend the same attempt's remaining
                # budget on one stopped observation before deciding again.
                diagnostic.value["outcome"] = "correction_precondition_changed"
                diagnostic.value["precondition_reobservation"] = {
                    "attempted": True,
                    "initial": dict(precondition_details),
                }
                if cursor is not None and transition is not None and transition.get("intent"):
                    cursor["precondition_reobservation"] = {
                        "version": 1,
                        "state": "consumed",
                        "movement_id": transition["movement_id"],
                        "stage": transition["stage"],
                        "row": transition["row"],
                        "direction": transition["direction"],
                        "branch": transition["branch"],
                        "intent": copy.deepcopy(transition["intent"]),
                    }
                try:
                    await self._preserve_motion_pair(expected_frame, baseline, diagnostic)
                except OSError:
                    diagnostic.value["rejected_pair_unavailable"] = True
                await self._persist()
                self._check(budget=not self.returning)
                remaining = attempt_deadline - time.monotonic()
                if remaining <= 0:
                    self.physical_state = "unknown"
                    self._diagnostic(diagnostic)
                    raise PanoramaCaptureError("correction_precondition_changed")
                first_baseline = baseline
                try:
                    baseline = await self._reference_window(timeout=min(3.0, remaining))
                except PanoramaCaptureError:
                    self.physical_state = "unknown"
                    diagnostic.value["precondition_reobservation"].update(
                        verified=False,
                        reason="stopped_window_unavailable",
                    )
                    self._diagnostic(diagnostic)
                    raise PanoramaCaptureError("correction_precondition_changed") from None
                same_stream = bool(
                    isinstance(first_baseline.get("capture_instance"), str)
                    and first_baseline.get("capture_instance")
                    == baseline.get("capture_instance")
                    and type(first_baseline.get("generation")) is int
                    and first_baseline.get("generation") == baseline.get("generation")
                    and type(first_baseline.get("sequence")) is int
                    and type(baseline.get("sequence")) is int
                    and baseline["sequence"] > first_baseline["sequence"]
                )
                diagnostic.value["precondition_reobservation"].update(
                    verified=same_stream,
                    reason=(
                        "same_stream_stopped_window"
                        if same_stream
                        else "frame_identity_changed"
                    ),
                )
                if not same_stream:
                    self.physical_state = "stopped"
                    self._diagnostic(diagnostic)
                    raise PanoramaCaptureError("correction_precondition_changed")
                self.last_frame = baseline
                precondition = await asyncio.to_thread(
                    _match, expected_frame["image"], baseline["image"]
                )
                diagnostic.value["outcome"] = "in_progress"
                detector.reset(now=time.monotonic())
            # Matching may be CPU-bound. Recheck cancellation immediately after
            # it; the fenced camera command performs the lease/fence guard before
            # any transport mutation.
            self._check(budget=not self.returning)
        if transition is not None and transition.get("intent") is not None:
            baseline_path = self.directory / f"transition-{uuid.uuid4().hex}.jpg"
            baseline_digest = await asyncio.to_thread(
                _write_image, baseline_path, baseline["image"]
            )
            transition["baseline"] = {
                "path": str(baseline_path),
                "sha256": baseline_digest,
                "capture_instance": baseline.get("capture_instance"),
                "sequence": baseline.get("sequence"),
                "generation": baseline.get("generation"),
                "media_time": baseline.get("media_time"),
                "received_monotonic": baseline.get("received_monotonic"),
            }
        observed = await asyncio.to_thread(self._observation, detector, baseline, None)
        await asyncio.to_thread(diagnostic.frame, baseline, observed, pose=self.last_pose)

        region_command: dict[str, Any] | None = None
        if self.region_policy is not None:
            self._check()
            commands = self.checkpoint.setdefault("region_commands", [])
            limit = self.region_policy["maximum_commands"]
            if not self.returning:
                limit -= self.region_policy["return_commands_reserved"]
            if not isinstance(commands, list) or len(commands) >= limit:
                raise PanoramaCaptureError("region_budget_exhausted")
            region_command = {
                "id": uuid.uuid4().hex, "state": "pending",
                "kind": diagnostic.value["command_kind"], "returning": self.returning,
                "baseline": {key: baseline.get(key) for key in
                             ("capture_instance", "generation", "sequence", "received_monotonic")},
            }
            commands.append(region_command)

        async def issue_command() -> Any:
            if transition is not None or expected_frame is not None:
                dispatch_received = _finite(baseline.get("received_monotonic"))
                dispatch_age = (
                    time.monotonic() - dispatch_received
                    if dispatch_received is not None
                    else math.inf
                )
                if not 0 <= dispatch_age <= MAX_CORRECTION_FRAME_AGE_SECONDS:
                    raise PanoramaCaptureError("correction_precondition_changed")
            if self.region_policy is not None:
                self._check()
                if not 0 <= time.monotonic() - baseline["received_monotonic"] <= MAX_CORRECTION_FRAME_AGE_SECONDS:
                    raise PanoramaCaptureError("correction_precondition_changed")
            try:
                receipt = await command()
            except PanoramaCaptureError as error:
                # The public error code is deliberately credential-free. Keep
                # enough private evidence to distinguish an unconfirmed command
                # from a command that was accepted but whose motion was not seen.
                diagnostic.value.update(
                    command_outcome=(
                        "unconfirmed" if error.code == "movement_unconfirmed" else "rejected"
                    ),
                    command_failure_code=error.code,
                )
                ptz_failure_code = getattr(error, "ptz_failure_code", None)
                ptz_failure_stage = getattr(error, "ptz_failure_stage", None)
                if ptz_failure_code is not None:
                    diagnostic.value["ptz_failure_code"] = ptz_failure_code
                if ptz_failure_stage is not None:
                    diagnostic.value["ptz_failure_stage"] = ptz_failure_stage
                raise
            except Exception:
                diagnostic.value["command_outcome"] = "unexpected_exception"
                raise
            diagnostic.value["command_accepted_seconds"] = time.monotonic() - start
            diagnostic.value["command_outcome"] = "accepted"
            if isinstance(receipt, dict):
                diagnostic.value["command_receipt"] = {
                    key: receipt[key] for key in (
                        "command_id", "command_kind", "motion_epoch", "accepted",
                        "stale_after_execution", "command_elapsed_seconds",
                        "pulse_remaining_seconds", "device_timeout_s",
                        "transport_elapsed_seconds",
                    ) if key in receipt
                }
            if (
                cursor is not None
                and not self.returning
                and cursor.get("recovery", {}).get("state") == "confirmed"
                and cursor["stage"] in {"pan", "step"}
            ):
                cursor["continued_after_recovery"] = True
            return receipt

        if region_command is not None and transition is None:
            await self._persist()
        if transition is not None:
            # The regional command ledger shares this write, avoiding another
            # progress/storage round trip between the fresh baseline and motion.
            # Persist the exact intent after all potentially failing frame work
            # and before dispatch. A crash from this point is reconciled as an
            # unknown outcome; it is never assumed not to have reached the head.
            if persist_recovery_return_intent:
                recovery = cursor.get("recovery")
                if (
                    cursor.get("stage") != "return_reference"
                    or not isinstance(recovery, dict)
                    or recovery.get("state") != "planned"
                ):
                    raise PanoramaCaptureError("continuous_resume_unavailable")
                # The return state and its command transition form one durable
                # intent. Before this write the saved state remains `planned`,
                # so a pre-dispatch interruption may safely try again. After
                # it, an interruption is uncertain and must only be reconciled
                # by observing the saved destination.
                recovery["state"] = "pending"
            if connection_recovery_phase is not None:
                connection_recovery = cursor.get("connection_recovery")
                expected_state, next_state, intent_type = {
                    "return": ("planned", "return_pending", "connection_anchor_return"),
                    "retry": ("anchor_confirmed", "retry_pending", "connection_halfstep"),
                }.get(connection_recovery_phase, (None, None, None))
                transition_intent = transition.get("intent")
                if (
                    expected_state is None
                    or not isinstance(connection_recovery, dict)
                    or connection_recovery.get("state") != expected_state
                    or not isinstance(transition_intent, dict)
                    or transition_intent.get("type") != intent_type
                ):
                    raise PanoramaCaptureError("continuous_resume_unavailable")
                if connection_recovery_phase == "return":
                    identity = connection_recovery.get("identity", {})
                    return_method = connection_recovery.get("return_method", "absolute")
                    common_invalid = (
                        transition_intent.get("axis") != identity.get("axis")
                        or transition_intent.get("direction") != -identity.get("direction", 0)
                        or transition_intent.get("row") != identity.get("row")
                        or transition_intent.get("step") != identity.get("step")
                        or transition_intent.get("anchor") != identity.get("anchor")
                    )
                    absolute_invalid = (
                        transition_intent.get("target") != connection_recovery.get("target")
                    )
                    relative_invalid = (
                        "target" in transition_intent
                        or _finite(transition_intent.get("duration"))
                        != _finite(connection_recovery.get("return_duration"))
                    )
                    if common_invalid or (
                        return_method == "absolute" and absolute_invalid
                    ) or (return_method == "relative_pulse" and relative_invalid):
                        raise PanoramaCaptureError("continuous_resume_unavailable")
                if connection_recovery_phase == "retry" and (
                    transition_intent.get("axis")
                    != connection_recovery.get("identity", {}).get("axis")
                    or transition_intent.get("direction")
                    != connection_recovery.get("identity", {}).get("direction")
                    or transition_intent.get("step") != connection_recovery.get("retry_step")
                    or _finite(transition_intent.get("duration"))
                    != _finite(connection_recovery.get("retry_duration"))
                    or transition_intent.get("row")
                    != connection_recovery.get("identity", {}).get("row")
                    or transition_intent.get("anchor")
                    != connection_recovery.get("identity", {}).get("anchor")
                ):
                    raise PanoramaCaptureError("continuous_resume_unavailable")
                if connection_recovery_phase == "retry":
                    identity = connection_recovery.get("identity", {})
                    state_key = "seek" if identity.get("axis") == "pan" else "vertical_seek"
                    seek_state = cursor.get(state_key)
                    if (
                        not isinstance(seek_state, dict)
                        or seek_state.get("steps") != identity.get("step")
                        or connection_recovery.get("retry_step")
                        != identity.get("step", -2) + 1
                    ):
                        raise PanoramaCaptureError("continuous_resume_unavailable")
                    seek_state["steps"] = connection_recovery["retry_step"]
                connection_recovery["state"] = next_state
            cursor.pop("precondition_reobservation", None)
            cursor["transition"] = transition
            await self._persist()
            await asyncio.to_thread(
                self._prune_transition_baselines,
                transition.get("baseline", {}).get("path"),
            )
            self._check(budget=not self.returning)
        if transition is not None or expected_frame is not None:
            # No callbacks or persistence follow this final gate. The task starts
            # with the same synchronous age check before entering the fenced
            # camera command, closing the scheduler gap as well.
            dispatch_received = _finite(baseline.get("received_monotonic"))
            dispatch_age = (
                time.monotonic() - dispatch_received
                if dispatch_received is not None
                else math.inf
            )
            diagnostic.value["dispatch_frame_age_seconds"] = (
                dispatch_age if math.isfinite(dispatch_age) else None
            )
            if expected_frame is not None:
                diagnostic.value["correction_precondition"][
                    "dispatch_frame_age_seconds"
                ] = diagnostic.value["dispatch_frame_age_seconds"]
            if not 0 <= dispatch_age <= MAX_CORRECTION_FRAME_AGE_SECONDS:
                self.physical_state = "stopped"
                diagnostic.value["outcome"] = "correction_precondition_changed"
                if transition is not None:
                    transition.update(state="not_issued", outcome="stale_precondition")
                    await self._persist()
                self._diagnostic(diagnostic)
                raise PanoramaCaptureError("correction_precondition_changed")
            self._check(budget=not self.returning)
        diagnostic.value["command_outcome"] = "pending"
        operation = asyncio.create_task(issue_command())
        frames: deque[dict] = deque()
        terminal_frames: deque[dict] = deque()
        buffer_bytes = 0
        terminal_bytes = 0
        stopped = False
        stop_operation: asyncio.Task[bool] | None = None
        pose_time = start - 1
        pose_matches = 0
        endpoint_probe_attempts = 0
        # Device status is quantized and sometimes leads the motor. This fixed
        # gate only establishes coarse command proximity, never angular accuracy
        # or physical stability. Those require independent optical evidence.
        diagnostic.value["arrival_policy"] = "coarse_device_arrival"
        diagnostic.value["arrival_tolerance_device_units"] = DEVICE_ARRIVAL_TOLERANCE
        no_op_requested = target is not None and all(
            _finite(self.last_pose.get(axis)) is not None
            and abs(value - self.last_pose[axis]) <= 1e-6
            for axis, value in target.items()
        )
        last_result: dict = {}

        def deadline_code() -> str:
            if diagnostic.value["frame_count"] <= 1:
                return "frame_acquisition_timeout"
            if not last_result.get("has_motion_transition"):
                return "motion_not_observed"
            return "stability_timeout"

        async def request_stop(reason: str) -> bool:
            diagnostic.value.update(
                stop_requested_seconds=time.monotonic() - start,
                stop_reason=reason,
                motion_transition_before_stop=bool(last_result.get("has_motion_transition")),
            )
            accepted = await self._stop()
            diagnostic.value["stop_command_accepted"] = accepted
            if accepted:
                diagnostic.value["stop_accepted_seconds"] = time.monotonic() - start
            return accepted

        def start_stop(reason: str) -> None:
            nonlocal stop_operation
            if stop_operation is None:
                stop_operation = asyncio.create_task(request_stop(reason))

        try:
            while time.monotonic() < observation_deadline:
                self._check(budget=not self.returning)
                if operation.done():
                    operation.result()
                if stop_operation is not None and stop_operation.done() and not stopped:
                    if not stop_operation.result():
                        raise PanoramaCaptureError("stop_unconfirmed")
                    # Keep observing during the response, but require a new
                    # stable window after its acknowledgement before capture.
                    detector.arm_stop(now=time.monotonic())
                    stopped = True
                wait_start = time.monotonic()
                remaining = observation_deadline - wait_start
                if remaining <= 1e-6:
                    diagnostic.value["budget_hit"] = "total"
                    break
                try:
                    frame = await asyncio.wait_for(
                        self.camera.frame(timeout_s=min(3.0, remaining)),
                        timeout=remaining,
                    )
                except TimeoutError:
                    diagnostic.value["budget_hit"] = "total"
                    break
                self.last_frame = frame
                now = time.monotonic()
                if now >= observation_deadline:
                    diagnostic.value["budget_hit"] = "total"
                if operation.done() and not stopped and stop_operation is None:
                    if target is not None and now - pose_time >= 0.15:
                        self.last_pose = await self._absolute_readback()
                        pose_time = now
                        diagnostic.value["arrival_delta_device_units"] = {
                            axis: self.last_pose[axis] - value
                            if _finite(self.last_pose.get(axis)) is not None
                            else None
                            for axis, value in target.items()
                        }
                        at_target = all(
                            _finite(self.last_pose.get(axis)) is not None
                            and abs(self.last_pose[axis] - value) <= DEVICE_ARRIVAL_TOLERANCE
                            for axis, value in target.items()
                        )
                        diagnostic.value["coarse_device_arrival"] = at_target
                        pose_matches = pose_matches + 1 if at_target else 0
                        if at_target:
                            diagnostic.value.setdefault(
                                "first_target_readback_seconds", now - start
                            )
                    # Absolute-position reports can lead the physical mechanism.
                    # Only finite pulses stop on their command duration; absolute
                    # travel waits for observed motion followed by settling.
                    receipt = diagnostic.value.get("command_receipt", {})
                    pulse_deadline = (
                        diagnostic.value.get("command_accepted_seconds", 0)
                        + max(0.0, receipt["pulse_remaining_seconds"])
                        if "pulse_remaining_seconds" in receipt else duration
                    )
                    stop_due = target is None and duration > 0 and now - start >= pulse_deadline
                    if stop_due:
                        start_stop("pulse_deadline")
                # Continuous/preset moves settle from video. Repeated native
                # position sessions can block that observer past its gap limit;
                # their final position is read once the visual window passes.
                if stopped and target is not None and now - pose_time >= 0.25:
                    self.last_pose = await self._absolute_readback()
                    pose_time = now
                last_result = await asyncio.to_thread(
                    self._observation, detector, frame, self.last_pose if stopped and target is not None else None
                )
                await asyncio.to_thread(
                    diagnostic.frame, frame, last_result, wait_seconds=now - wait_start, pose=self.last_pose
                )
                if last_result.get("has_motion_transition"):
                    diagnostic.value.setdefault(
                        "first_motion_transition_seconds", time.monotonic() - start
                    )
                diagnostic.value["stop_command_accepted"] = stopped
                frames.append(frame)
                buffer_bytes += frame["image"].nbytes
                # Full-resolution frames serve capture selection. Diagnostic
                # continuity needs a longer window at high resolutions, so it
                # retains only the exact 960-pixel grayscale analysis instead.
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
                    image=_gray(frame["image"]),
                    image_representation="analysis",
                    source_width=frame["image"].shape[1],
                    source_height=frame["image"].shape[0],
                )
                terminal_frames.append(terminal)
                terminal_bytes += terminal["image"].nbytes
                while (
                    len(terminal_frames) > 5
                    and terminal["received_monotonic"] - terminal_frames[1]["received_monotonic"]
                    >= 1.0
                ):
                    terminal_bytes -= terminal_frames.popleft()["image"].nbytes
                while buffer_bytes + terminal_bytes + diagnostic.replay_bytes > FRAME_BUFFER_BYTES:
                    if len(frames) > 1 or (frames and len(terminal_frames) <= 1):
                        buffer_bytes -= frames.popleft()["image"].nbytes
                    elif terminal_frames:
                        terminal_bytes -= terminal_frames.popleft()["image"].nbytes
                    else:
                        break
                diagnostic.value["frame_buffer_peak_bytes"] = max(
                    diagnostic.value.get("frame_buffer_peak_bytes", 0),
                    buffer_bytes + terminal_bytes + diagnostic.replay_bytes,
                )
                stop_accepted_seconds = _finite(
                    diagnostic.value.get("stop_accepted_seconds")
                )
                endpoint_probe_delays = (1.25, 3.0)
                if (
                    stopped
                    and operation.done()
                    and not last_result.get("has_motion_transition")
                    and stop_accepted_seconds is not None
                    and endpoint_probe_attempts < len(endpoint_probe_delays)
                    and now - start - stop_accepted_seconds
                    >= endpoint_probe_delays[endpoint_probe_attempts]
                ):
                    endpoint_probe_attempts += 1
                    late_capture = await self._late_endpoint_capture(
                        baseline=baseline,
                        frames=frames,
                        terminal_frames=terminal_frames,
                        transition=transition,
                        diagnostic=diagnostic,
                        command_finished=True,
                        stopped=True,
                    )
                    if late_capture is not None:
                        last_result = late_capture["evidence"]
                        diagnostic.value["last_result"] = {
                            key: last_result[key]
                            for key in _AttemptDiagnostic._RESULT_KEYS
                            if key in last_result
                        }
                        diagnostic.value["endpoint_recovery_timing"] = "early"
                        self.last_pose = await self._movement_readback(
                            target=target, diagnostic=diagnostic
                        )
                        if not await self._commit_endpoint_frame(
                            late_capture["frame"],
                            diagnostic,
                            deadline=attempt_deadline,
                        ):
                            detector.arm_stop(now=time.monotonic())
                            continue
                        self.physical_state = "stopped"
                        diagnostic.value["outcome"] = "capture_stable"
                        return {
                            **late_capture,
                            "pose": self.last_pose,
                            "precondition_match": precondition,
                        }
                if (
                    last_result["stable"]
                    and not stopped
                    and stop_operation is None
                    and operation.done()
                    and (target is None or allow_stationary or pose_matches >= 2)
                ):
                    start_stop("visual_settled")
                    continue
                if stopped and last_result["stable"] and time.monotonic() < observation_deadline:
                    chosen = _selected_capture_frame(frames, last_result, frame)
                    if chosen is None:
                        raise PanoramaCaptureError("selected_capture_frame_unavailable")
                    self.last_pose = await self._movement_readback(
                        target=target, diagnostic=diagnostic
                    )
                    if not await self._commit_endpoint_frame(
                        chosen,
                        diagnostic,
                        deadline=attempt_deadline,
                    ):
                        detector.arm_stop(now=time.monotonic())
                        continue
                    self.physical_state = "stopped"
                    matching = await asyncio.to_thread(
                        _axis_command_match,
                        baseline["image"],
                        chosen["image"],
                        axis=(movement_intent or {}).get("axis"),
                    )
                    diagnostic.comparison(matching)
                    diagnostic.value["outcome"] = "capture_stable"
                    return {
                        "frame": chosen,
                        "evidence": last_result,
                        "pose": self.last_pose,
                        "match": matching,
                        "precondition_match": precondition,
                        "stable": True,
                        "stationary": False,
                    }
            self._check(budget=not self.returning)
            diagnostic.value["budget_hit"] = "total"
            # A short static window cannot exclude delayed video. Only after
            # the complete attempt budget do we combine the causal command
            # baseline with a fresh, ordered terminal transport window. The
            # relaxed spatial support below can prove only near-zero motion;
            # it cannot qualify a transition, photograph or geometric edge.
            terminal_scene = await self._terminal_scene(terminal_frames)
            if terminal_scene is not None:
                diagnostic.value["terminal_scene"] = {
                    key: terminal_scene[key]
                    for key in (
                        "classification",
                        "sequence",
                        "generation",
                        "age_seconds",
                        "observed_window_seconds",
                        "distinct_frames",
                        "received_frames",
                        "visual_change_observed",
                        "transport_window_verified",
                        "timing_basis",
                    )
                    if key in terminal_scene
                }
            terminal_frame = terminal_frames[-1] if terminal_frames else None
            baseline_media = _finite(baseline.get("media_time"))
            terminal_media = (
                _finite(terminal_frame.get("media_time"))
                if terminal_frame is not None
                else None
            )
            causal_terminal_frame = bool(
                terminal_frame is not None
                and isinstance(baseline.get("capture_instance"), str)
                and baseline.get("capture_instance")
                == terminal_frame.get("capture_instance")
                and type(baseline.get("generation")) is int
                and baseline.get("generation") == terminal_frame.get("generation")
                and type(baseline.get("sequence")) is int
                and type(terminal_frame.get("sequence")) is int
                and terminal_frame["sequence"] > baseline["sequence"]
                and (
                    baseline_media is None
                    or (
                        terminal_media is not None
                        and terminal_media > baseline_media
                    )
                )
            )
            if (
                allow_stationary
                and (stopped or (no_op_requested and operation.done() and pose_matches >= 2))
                and operation.done()
                and diagnostic.value.get("command_outcome") == "accepted"
                and terminal_scene is not None
                and terminal_scene.get("transport_window_verified") is True
                and causal_terminal_frame
                and frames
                and "first_motion_transition_seconds" not in diagnostic.value
            ):
                frame = frames[-1]
                matching = await asyncio.to_thread(
                    _no_effect_match, baseline["image"], terminal_frame["image"]
                )
                diagnostic.comparison(matching)
                terminal_pose: dict[str, Any] | None = None
                native_no_effect: dict[str, Any] | None = None
                if (
                    matching.get("verified") is True
                    and matching.get("overlap", 0) >= UNCONFIRMED_NO_EFFECT_OVERLAP
                    and matching.get("displacement", math.inf)
                    <= NATIVE_CORROBORATED_NO_EFFECT_PIXELS
                ):
                    terminal_pose = await self._observational_readback()
                    native_no_effect = _native_axes_unchanged(
                        command_start_pose, terminal_pose, requested_velocity
                    )
                    diagnostic.value["native_stationary_evidence"] = (
                        native_no_effect
                        or {
                            "verified": False,
                            "before": {
                                key: _finite(command_start_pose.get(key))
                                if isinstance(command_start_pose, dict)
                                else None
                                for key in ("native_pan", "native_tilt")
                            },
                            "after": {
                                key: _finite(terminal_pose.get(key))
                                for key in ("native_pan", "native_tilt")
                            },
                        }
                    )
                if (
                    matching.get("verified") is True
                    and matching.get("overlap", 0) >= UNCONFIRMED_NO_EFFECT_OVERLAP
                    and (
                        matching.get("displacement", math.inf)
                        <= UNCONFIRMED_NO_EFFECT_PIXELS
                        or native_no_effect is not None
                    )
                ):
                    if not stopped:
                        start_stop("unchanged_return_reference")
                        if not await asyncio.shield(stop_operation):
                            raise PanoramaCaptureError("stop_unconfirmed")
                        stopped = True
                    self.last_pose = terminal_pose or await self._movement_readback(
                        target=target, diagnostic=diagnostic
                    )
                    if 0 <= time.monotonic() - frame["received_monotonic"] <= 1.0:
                        self.physical_state = "stopped"
                        diagnostic.value.update(
                            outcome="stationary_boundary_observed",
                            no_motion_observation_policy="full_attempt_budget",
                            causal_terminal_frame=True,
                        )
                        return {
                            "frame": frame, "evidence": last_result, "pose": self.last_pose,
                            "match": matching,
                            "native_stationary": native_no_effect,
                            "precondition_match": precondition,
                            "stable": False,
                            "stationary": True,
                        }
            # Qualify no-effect against the fresh full-budget window first.
            # A failed, expensive endpoint-motion analysis must not age that
            # evidence before its existing no-effect gates can inspect it.
            late_capture = await self._late_endpoint_capture(
                baseline=baseline,
                frames=frames,
                terminal_frames=terminal_frames,
                transition=transition,
                diagnostic=diagnostic,
                command_finished=operation.done(),
                stopped=stopped,
            )
            if late_capture is not None:
                last_result = late_capture["evidence"]
                diagnostic.value["last_result"] = {
                    key: last_result[key]
                    for key in _AttemptDiagnostic._RESULT_KEYS
                    if key in last_result
                }
                self.last_pose = await self._movement_readback(
                    target=target, diagnostic=diagnostic
                )
                if await self._commit_endpoint_frame(
                    late_capture["frame"],
                    diagnostic,
                    deadline=attempt_deadline + ENDPOINT_COMMIT_MAX_SECONDS,
                ):
                    diagnostic.value["endpoint_commit_extended_seconds"] = max(
                        0.0, time.monotonic() - attempt_deadline
                    )
                    self.physical_state = "stopped"
                    diagnostic.value["outcome"] = "capture_stable"
                    return {
                        **late_capture,
                        "pose": self.last_pose,
                        "precondition_match": precondition,
                    }
            raise PanoramaCaptureError(deadline_code())
        except PanoramaCaptureError as error:
            diagnostic.value["outcome"] = error.code
            raise
        except (_Stopped, asyncio.CancelledError):
            diagnostic.value["outcome"] = "interrupted"
            raise
        except Exception:
            diagnostic.value["outcome"] = "unexpected_exception"
            raise
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if stop_operation is not None:
                # Do not abandon or duplicate an in-flight Stop on timeout,
                # cancellation or observer failure. The executor bounds it.
                await asyncio.shield(stop_operation)
            elif not stopped:
                await request_stop("attempt_cleanup")
            diagnostic.value["physical_state"] = self.physical_state
            diagnostic.value["last_pose"] = {
                key: _finite(self.last_pose.get(key))
                for key in ("pan", "tilt", "zoom", "native_pan", "native_tilt")
            }
            if diagnostic.value["outcome"] in {"motion_not_observed", "stability_timeout"}:
                try:
                    if terminal_frames:
                        await self._preserve_motion_pair(baseline, terminal_frames[-1], diagnostic)
                except Exception:
                    diagnostic.value["rejected_pair_unavailable"] = True
                try:
                    diagnostic.value["terminal_frame_count"] = len(terminal_frames)
                    diagnostic.value["terminal_window_seconds"] = (
                        terminal_frames[-1]["received_monotonic"]
                        - terminal_frames[0]["received_monotonic"]
                        if terminal_frames
                        else 0.0
                    )
                    scene = await self._terminal_scene(terminal_frames)
                    if scene is not None:
                        diagnostic.value["terminal_scene"] = scene
                        await self._preserve_rejected(terminal_frames[-1], scene, diagnostic)
                except Exception:
                    # Optional inspection must not mask the movement failure;
                    # Stop and command cleanup have already completed above.
                    diagnostic.value["terminal_scene_status"] = "unavailable"
            try:
                await self._preserve_replay(diagnostic)
            except Exception:
                diagnostic.value["replay_write_failed"] = True
            self._diagnostic(diagnostic)

            if region_command is not None:
                region_command["state"] = (
                    "observed" if diagnostic.value["outcome"] in
                    {"capture_stable", "stationary_boundary_observed"} else "uncertain"
                )
                region_command["outcome"] = diagnostic.value["outcome"]

    async def _preserve_first_connection_refusal(
        self, anchor: dict, anchor_image: np.ndarray, frame: dict, matching: dict,
    ) -> None:
        """One exact pair, before recovery can replace the selected endpoint.

        NPY preserves compared pixels losslessly without constructing compressed
        copies. The existing input frame plus one loaded anchor are the only
        full-resolution arrays retained. Diagnostics never authorize movement.
        """
        key = "first_connection_refusal"
        destination = self.directory / "first-connection-refusal"
        if key in self.checkpoint or destination.exists():
            return
        record = {
            "version": 1, "status": "planned", "path": str(destination),
            "metrics": matching,
            "anchor": {k: v for k, v in anchor.items() if k != "image"},
            "observation": {k: frame.get(k) for k in (
                "capture_instance", "generation", "sequence", "received_monotonic",
                "published_at", "media_time", "physical_timestamp_verified", "captured_monotonic",
            )},
            "observed_pose": self.last_pose,
            "source_identity": self.capabilities.get("source_identity", {}),
            "transition": (self.checkpoint.get("continuous_cursor") or {}).get("transition"),
            "command": (self.checkpoint.get("region_commands") or [None])[-1],
            "diagnostic": (self.checkpoint.get("diagnostics", {}).get("attempts") or [None])[-1],
        }
        self.checkpoint[key] = record
        arrays = (anchor_image, frame["image"])
        if any(a.dtype != np.uint8 or a.ndim not in {2, 3} for a in arrays) or sum(a.nbytes for a in arrays) + 4096 > MAX_FIRST_REFUSAL_BYTES:
            record.clear()
            record.update(status="unavailable", reason="refusal_storage_limit")
            await self._persist()
            return

        # Bound diagnostics independently of the image quota, including the
        # copies later embedded in the checkpoint. Do not truncate evidence and
        # label it complete. Leave room for the two file identities below.
        # Check the object before copying/encoding it: a large string or nested
        # diagnostic must not allocate another unbounded representation.
        def bounded_metadata(value: Any, remaining: list[int], depth: int = 0) -> bool:
            if depth > 32:
                return False
            remaining[0] -= len(value) * 6 + 2 if isinstance(value, str) else 32
            if remaining[0] < 0:
                return False
            if isinstance(value, dict):
                return all(bounded_metadata(k, remaining, depth + 1)
                           and bounded_metadata(v, remaining, depth + 1) for k, v in value.items())
            if isinstance(value, (list, tuple)):
                return all(bounded_metadata(v, remaining, depth + 1) for v in value)
            return value is None or isinstance(value, (str, int, float, bool))

        if not bounded_metadata(record, [MAX_FIRST_REFUSAL_METADATA_BYTES - 4096]):
            record.clear()
            record.update(version=1, status="unavailable", reason="refusal_metadata_limit", path=str(destination))
            await self._persist()
            return
        record = copy.deepcopy(record)
        self.checkpoint[key] = record
        metadata_bytes = 4096
        for part in json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(record):
            metadata_bytes += len(part.encode("utf-8"))
            if metadata_bytes > MAX_FIRST_REFUSAL_METADATA_BYTES:
                record.clear()
                record.update(version=1, status="unavailable", reason="refusal_metadata_limit", path=str(destination))
                await self._persist()
                return

        def write() -> None:
            destination.mkdir(mode=0o700)
            files = []
            for name, array in zip(("anchor.npy", "observation.npy"), arrays):
                path = destination / name
                with path.open("xb") as stream:
                    np.save(stream, array, allow_pickle=False)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                files.append({"path": str(path), "sha256": digest.hexdigest(),
                              "shape": list(array.shape), "dtype": str(array.dtype), "bytes": path.stat().st_size})
            record.update(status="preserved", files=files)
            (destination / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))

        try:
            await asyncio.to_thread(write)
        except OSError:
            record.update(status="unavailable", reason="refusal_write_failed")
        await self._persist()

    async def _accept(
        self, result: dict, *, row: int, role: str = "grid", plan_index: int | None = None,
        command_diagnostic: dict | None = None,
    ) -> dict:
        if not result.get("stable"):
            raise PanoramaCaptureError("capture_not_stable")
        if len(self.captures) >= MAX_CAPTURES:
            raise PanoramaCaptureError("scan_budget_exhausted")
        if self.region_policy is not None:
            self._check()
        cursor = self.checkpoint.get("continuous_cursor")
        completes_connection_recovery = False
        if isinstance(cursor, dict) and isinstance(cursor.get("connection_recovery"), dict):
            try:
                connection_recovery = _connection_recovery(
                    cursor, captures=self.captures, capabilities=self.capabilities
                )
            except ValueError:
                raise PanoramaCaptureError("continuous_resume_unavailable") from None
            if (
                connection_recovery is not None
                and connection_recovery.get("state") == "halfstep_observed"
            ):
                identity = connection_recovery["identity"]
                transition = cursor.get("transition")
                intent = transition.get("intent") if isinstance(transition, dict) else None
                expected_row = (
                    identity["row"]
                    if identity["axis"] == "pan"
                    else identity["row"] + identity["direction"]
                )
                if (
                    row != expected_row
                    or not isinstance(transition, dict)
                    or transition.get("state") != "accepted"
                    or not isinstance(intent, dict)
                    or intent.get("type") != "connection_halfstep"
                    or intent.get("axis") != identity["axis"]
                    or intent.get("direction") != identity["direction"]
                    or intent.get("row") != identity["row"]
                    or intent.get("step") != connection_recovery.get("retry_step")
                    or _finite(intent.get("duration"))
                    != _finite(connection_recovery.get("retry_duration"))
                    or intent.get("anchor") != identity["anchor"]
                    or cursor.get("anchor") != identity["anchor"]
                ):
                    raise PanoramaCaptureError("continuous_resume_unavailable")
                completes_connection_recovery = True
        frame = result["frame"]
        if self.region_policy is not None and (
            not isinstance(frame.get("capture_instance"), str) or not frame["capture_instance"]
            or type(frame.get("generation")) is not int or type(frame.get("sequence")) is not int
            or frame["generation"] < 0 or frame["sequence"] < 0
        ):
            raise PanoramaCaptureError("capture_identity_unverified")
        identifier = f"capture-{len(self.captures):04d}"
        path = self.directory / f"{identifier}.jpg"
        pose = result.get("pose", {})
        original_zoom = _finite(self.checkpoint.get("initial_zoom"))
        observed_zoom = _finite(pose.get("zoom"))
        if (
            original_zoom is not None
            and observed_zoom is not None
            and abs(original_zoom - observed_zoom) > 0.001
        ):
            raise PanoramaCaptureError("optical_state_changed")
        maximum_bytes = None
        if self.region_policy is not None:
            consumed = sum(Path(photo["path"]).stat().st_size for photo in self.captures)
            maximum_bytes = min(24 * 1024**2, self.region_policy["maximum_input_bytes"] - consumed)
            if maximum_bytes <= 0:
                raise PanoramaCaptureError("region_budget_exhausted")
        digest = await asyncio.to_thread(
            _write_image, path, frame["image"],
            **({"maximum_bytes": maximum_bytes} if maximum_bytes is not None else {}),
        )
        capture = {
            "capture_schema_version": 2,
            "id": identifier,
            "path": str(path),
            "sha256": digest,
            "pose": pose,
            "native_pan_position": pose.get("native_pan"),
            "row_index": row,
            "role": role,
            "width": frame["image"].shape[1],
            "height": frame["image"].shape[0],
            "sequence": frame["sequence"],
            "generation": frame["generation"],
            "capture_instance": frame.get("capture_instance"),
            "media_time": frame.get("media_time"),
            "received_monotonic": frame["received_monotonic"],
            "published_at": frame.get("published_at"),
            "physical_timestamp_verified": frame.get("physical_timestamp_verified") is True,
            "captured_monotonic": frame.get("captured_monotonic"),
            "source_identity": copy.deepcopy(self.capabilities.get("source_identity", {})),
            "analysis": {"width": ANALYSIS_WIDTH, "matcher_version": "sift_distributed_homography_v1"},
            "quality": {**result["evidence"], "stable": True},
            "previous_overlap": result.get("match", {}),
        }
        if self.region_policy is not None:
            commands = self.checkpoint.get("region_commands", [])
            capture["region_command_id"] = commands[-1].get("id") if commands else None
            attempts = self.checkpoint.get("diagnostics", {}).get("attempts", [])
            associated = command_diagnostic if command_diagnostic is not None else (attempts[-1] if attempts else {})
            capture["control_command_id"] = associated.get("command_receipt", {}).get("command_id")
            capture["observation_replay_id"] = associated.get("replay_id")
        transition = cursor.get("transition") if isinstance(cursor, dict) else None
        intent = transition.get("intent") if isinstance(transition, dict) else None
        if isinstance(intent, dict) and intent.get("axis") in {"pan", "tilt"}:
            anchor = intent.get("anchor")
            capture["movement"] = {
                "type": intent.get("type"),
                "axis": intent["axis"],
                "direction": intent.get("direction"),
                "duration": intent.get("duration"),
                "anchor_capture_id": (
                    anchor.get("capture_id") if isinstance(anchor, dict) else None
                ),
            }
        if plan_index is not None:
            capture["plan_index"] = plan_index
        if result.get("requested_target") is not None:
            capture["requested_target"] = result["requested_target"]
        self.captures.append(capture)
        self.last_frame = frame
        self.physical_state = "stopped"
        if cursor is not None and result.get("match", {}).get("verified"):
            state_key = "seek" if cursor.get("stage") == "pan" else "vertical_seek"
            active_seek = cursor.get(state_key)
            if isinstance(active_seek, dict):
                active_seek["origin_uncertain_since_anchor"] = False
            cursor["anchor"] = {"capture_id": identifier, "path": str(path), "row": row}
            # The accepted image is an immutable observation of this exact
            # anchor, produced only after a causal stopped window. Preserve
            # that identity so an immediate change of scan stage does not
            # discard it and ask the stream to prove a second, unrelated
            # stillness window. The attestation expires with the frame and
            # cannot survive a process restart as a substitute for a new
            # visual anchor check.
            cursor["anchor_observation"] = {
                "capture_id": identifier,
                "path": str(path),
                "row": row,
                "capture_instance": frame.get("capture_instance"),
                "generation": frame.get("generation"),
                "sequence": frame.get("sequence"),
                "overlap": 1.0,
                "displacement": 0.0,
                "evidence": "accepted_capture",
            }
            if "transition" in cursor:
                cursor["transition"]["state"] = "confirmed"
            if completes_connection_recovery:
                connection_recovery = cursor["connection_recovery"]
                connection_recovery.update(
                    state="complete",
                    capture_id=identifier,
                    capture_path=str(path),
                )
        self.checkpoint["last_capture_id"] = identifier
        if cursor is not None and isinstance(cursor.get("region"), dict):
            cursor["resume_anchor_verified"] = True
            if isinstance(result.get("region_update"), dict):
                cursor["region"] = copy.deepcopy(result["region_update"])
            elif role == "reference_connection" and not cursor["region"]["views"]:
                cursor["region"]["views"] = [{"row": row, "column": 0, "capture_id": identifier}]
            cursor["row"] = row
            cursor.setdefault("bands", {}).setdefault(str(row), {"complete": False, "edges": {}})
            self.coverage["region"] = copy.deepcopy(cursor["region"])
            if cursor["region"]["version"] == 1:
                if sum(view["row"] == row for view in cursor["region"]["views"]) == 3:
                    cursor["bands"][str(row)]["region_complete"] = True
            elif cursor["region"]["version"] == 2:
                criteria = region_progress(self.checkpoint)["decision"]["criteria"]
                cursor["bands"][str(row)]["region_complete"] = criteria["first_row" if row == 0 else "second_row"]
        await self._persist()
        await self._emit("capturing", preview_path=str(path))
        return capture

    def _limits(self) -> dict | None:
        limits = self.capabilities.get("limits", {})
        if not self.capabilities.get("absolute_supported"):
            return None
        if _normalized_absolute_pose(self.last_pose, self.capabilities) is None:
            return None
        for axis in ("pan", "tilt"):
            bounds = limits.get(axis) or {}
            lower, upper = _finite(bounds.get("min")), _finite(bounds.get("max"))
            if (
                lower is None
                or upper is None
                or not -1 <= lower <= upper <= 1
                or (axis == "pan" and lower == upper)
            ):
                return None
        return limits

    async def _absolute_readback(self) -> dict[str, Any]:
        pose = await self.camera.position()
        if (
            self.capabilities.get("absolute_supported") is True
            and _normalized_absolute_pose(pose, self.capabilities) is None
        ):
            raise PanoramaCaptureError("normalized_motion_unavailable")
        return pose

    async def _observational_readback(
        self, diagnostic: _AttemptDiagnostic | None = None
    ) -> dict[str, Any]:
        """Keep image-verified movement usable when PTZ status is unavailable.

        Continuous, relative and preset paths derive acceptance from the fenced
        command, an optical transition and a stopped frame window. Position is
        supplemental there. Absolute movement continues through
        ``_absolute_readback`` and still requires position to prove arrival.
        """
        try:
            return await self.camera.position()
        except PanoramaCaptureError as error:
            if error.code != "position_unavailable":
                raise
        if diagnostic is not None:
            diagnostic.value["position_readback"] = (
                "unavailable_after_visual_stop"
            )
        return {
            "pan": None,
            "tilt": None,
            "zoom": None,
            "native_pan": None,
            "native_tilt": None,
            "pan_tilt_space": "",
            "zoom_space": "",
            "move_status": "UNKNOWN",
            "error": "",
            "observed_monotonic": time.monotonic(),
            "position_evidence": "unavailable",
        }

    async def _movement_readback(
        self,
        *,
        target: dict[str, Any] | None,
        diagnostic: _AttemptDiagnostic,
    ) -> dict[str, Any]:
        if target is not None:
            return await self._absolute_readback()
        return await self._observational_readback(diagnostic)

    async def _absolute(
        self,
        target: dict,
        *,
        allow_stationary: bool = False,
        expected_frame: dict | None = None,
        movement_intent: dict[str, Any] | None = None,
        connection_recovery_phase: str | None = None,
    ) -> dict:
        options = {"target": target, "allow_stationary": allow_stationary}
        if expected_frame is not None:
            options["expected_frame"] = expected_frame
        if movement_intent is not None:
            options["movement_intent"] = movement_intent
        if connection_recovery_phase is not None:
            options["connection_recovery_phase"] = connection_recovery_phase
        return await self._move(lambda: self.camera.move_absolute(**target), **options)

    def _axis_response_observation(
        self, axis: str, matching: dict, before: dict, after: dict, *, kind: str
    ) -> dict[str, Any] | None:
        first, last = _finite(before.get(axis)), _finite(after.get(axis))
        shift_x, shift_y = _finite(matching.get("shift_x")), _finite(matching.get("shift_y"))
        overlap = _finite(matching.get("overlap"))
        homography = _finite_homography(matching.get("homography"))
        size = matching.get("analysis_size")
        width = _finite(size[0]) if isinstance(size, list | tuple) and len(size) == 2 else None
        height = _finite(size[1]) if isinstance(size, list | tuple) and len(size) == 2 else None
        if (
            matching.get("verified") is not True
            or first is None
            or last is None
            or abs(last - first) < 1e-5
            or shift_x is None
            or shift_y is None
            or overlap is None
            or not 0 <= overlap <= 1
            or homography is None
            or width is None
            or height is None
        ):
            return None
        other = "tilt" if axis == "pan" else "pan"
        other_first, other_last = _finite(before.get(other)), _finite(after.get(other))
        if other_first is None or other_last is None:
            return None
        other_tolerance = min(0.002, abs(last - first) * 0.2)
        if (
            other_first is not None
            and other_last is not None
            and abs(other_last - other_first) > other_tolerance
        ):
            return None
        gain_vector = [shift_x / (last - first), shift_y / (last - first)]
        motion_gain = float(np.linalg.norm(gain_vector))
        if not math.isfinite(motion_gain) or motion_gain < 2 / abs(last - first):
            return None
        return {
            "device_delta": last - first,
            "image_delta": [shift_x, shift_y],
            "gain_vector": gain_vector,
            "motion_gain": motion_gain,
            "kind": kind,
            "verified": True,
            "overlap": overlap,
            "homography": homography.tolist(),
            "analysis_size": [width, height],
        }

    def _store_axis_response(
        self, axis: str, observation: dict[str, Any], *, cycle_closed: bool
    ) -> None:
        samples = self.checkpoint.setdefault("pilot_observations", {}).setdefault(axis, [])
        cycle_id = observation.get("pilot_cycle_id")
        if cycle_id is not None and any(
            item.get("pilot_cycle_id") == cycle_id
            and item.get("kind") == observation.get("kind")
            for item in samples
            if isinstance(item, dict)
        ):
            return
        samples.append({**observation, "cycle_closed": cycle_closed})
        del samples[:-6]
        self.checkpoint.setdefault("pilot_response", {})[axis] = float(
            np.median([item["motion_gain"] for item in samples])
        )
        self.checkpoint.setdefault("return_jacobian", {})[axis] = np.median(
            np.asarray([item["gain_vector"] for item in samples], dtype=np.float64), axis=0
        ).tolist()

    def _remember_axis_response(
        self, axis: str, matching: dict, before: dict, after: dict, *, kind: str
    ) -> bool:
        observation = self._axis_response_observation(axis, matching, before, after, kind=kind)
        if observation is None:
            return False
        self._store_axis_response(axis, observation, cycle_closed=kind == "return_probe")
        return True

    async def _pilot_axis(self, axis: str, origin: dict, limits: dict) -> float:
        span = limits[axis]["max"] - limits[axis]["min"]
        positive_room = limits[axis]["max"] - origin[axis]
        negative_room = origin[axis] - limits[axis]["min"]
        direction = 1 if positive_room >= negative_room else -1
        maximum_excursion = min(max(positive_room, negative_room), span * 0.2)
        amplitude = min(span * 0.05, maximum_excursion)
        history = self.checkpoint.setdefault("pilot_attempts", [])
        budget = self.checkpoint.setdefault(
            "grid_pilot_budget",
            {
                "maximum_cycles_per_axis": MAX_GRID_PILOT_CYCLES_PER_AXIS,
                "captures_per_cycle": 2,
                "cycles_planned": 0,
            },
        )
        if (
            not isinstance(history, list)
            or not isinstance(budget, dict)
            or budget.get("maximum_cycles_per_axis") != MAX_GRID_PILOT_CYCLES_PER_AXIS
            or budget.get("captures_per_cycle") != 2
            or type(budget.get("cycles_planned")) is not int
            or budget["cycles_planned"] != len(history)
        ):
            raise PanoramaCaptureError("pilot_resume_unavailable")
        axis_history = []
        for saved in history:
            if (
                not isinstance(saved, dict)
                or saved.get("axis") not in {"pan", "tilt"}
                or not isinstance(saved.get("state"), str)
            ):
                raise PanoramaCaptureError("pilot_resume_unavailable")
            state = saved["state"]
            if state in {"planned", "outward_observed", "outcome_uncertain"}:
                raise PanoramaCaptureError("pilot_resume_unavailable")
            if state in {"cycle_unconfirmed", "return_unconfirmed"}:
                raise PanoramaCaptureError("pilot_cycle_unconfirmed")
            if state not in {"cycle_closed", "stationary_no_op"}:
                raise PanoramaCaptureError("pilot_resume_unavailable")
            if saved["axis"] == axis:
                axis_history.append(saved)
        if len(axis_history) > MAX_GRID_PILOT_CYCLES_PER_AXIS:
            raise PanoramaCaptureError("pilot_resume_unavailable")
        if axis_history:
            # A completed cycle is reusable only through its persisted optical
            # evidence. A restart never manufactures another pilot command.
            for saved in axis_history:
                cycle_id = saved.get("cycle_id")
                if saved["state"] != "cycle_closed" or not isinstance(cycle_id, str):
                    continue
                for key in ("outward_observation", "return_observation"):
                    observation = saved.get(key)
                    if isinstance(observation, dict):
                        self._store_axis_response(
                            axis,
                            {**observation, "pilot_cycle_id": cycle_id},
                            cycle_closed=True,
                        )
            try:
                geometry = _axis_overlap_geometry(
                    span=span,
                    optical_samples=_pilot_optical_samples(self.checkpoint, axis),
                    maximum_count=MAX_CAPTURES,
                )
            except PanoramaCaptureError as error:
                if error.code == "coverage_exceeds_capture_budget":
                    raise
                raise PanoramaCaptureError("pilot_resume_unavailable") from error
            await self._persist()
            return geometry["step"]
        expanding = False
        last_geometry = None
        for attempt in range(len(axis_history), MAX_GRID_PILOT_CYCLES_PER_AXIS):
            if amplitude <= 0:
                raise PanoramaCaptureError("coverage_exceeds_capture_budget")
            if len(self.captures) + 2 > MAX_CAPTURES:
                if last_geometry is not None:
                    return last_geometry["step"]
                raise PanoramaCaptureError("coverage_exceeds_capture_budget")
            origin_image = self.last_frame["image"].copy() if self.last_frame is not None else None
            delta = direction * amplitude
            record = {
                "axis": axis,
                "attempt": attempt + 1,
                "cycle_id": f"{axis}:{attempt + 1}",
                "requested_delta": delta,
                "state": "planned",
            }
            history.append(record)
            budget["cycles_planned"] = int(budget.get("cycles_planned", 0)) + 1
            await self._persist()
            before = dict(self.last_pose)
            try:
                result = await self._absolute({**origin, axis: origin[axis] + delta})
            except PanoramaCaptureError as error:
                if error.code not in {"motion_not_observed", "stability_timeout"}:
                    raise
                await self._confirm_stop()
                comparison = None
                try:
                    self.last_pose = await self._absolute_readback()
                    if origin_image is not None and self.last_frame is not None:
                        comparison = await asyncio.to_thread(
                            _match, origin_image, self.last_frame["image"]
                        )
                except Exception:
                    comparison = None
                unchanged_pose = all(
                    _finite(before.get(name)) is not None
                    and _finite(self.last_pose.get(name)) is not None
                    and abs(self.last_pose[name] - before[name]) <= 1e-5
                    for name in ("pan", "tilt")
                )
                displacement = _finite(comparison.get("displacement")) if comparison else None
                overlap = _finite(comparison.get("overlap")) if comparison else None
                stationary_no_op = bool(
                    self.physical_state == "stopped"
                    and unchanged_pose
                    and comparison is not None
                    and comparison.get("verified") is True
                    and displacement is not None
                    and displacement <= 0.5
                    and overlap is not None
                    and overlap >= 0.85
                )
                record.update(
                    state="stationary_no_op" if stationary_no_op else "cycle_unconfirmed",
                    movement_error=error.code,
                    stationary_no_op=stationary_no_op,
                    no_op_displacement=displacement,
                    no_op_overlap=overlap,
                )
                await self._persist()
                if not stationary_no_op:
                    raise PanoramaCaptureError("pilot_cycle_unconfirmed") from error
                next_amplitude = min(amplitude * 2, maximum_excursion)
                if next_amplitude <= amplitude + 1e-12:
                    raise PanoramaCaptureError("pilot_cycle_unconfirmed") from error
                amplitude = next_amplitude
                expanding = True
                continue
            outward_observation = self._axis_response_observation(
                axis, result["match"], before, result["pose"], kind="pilot_outward"
            )
            outward = outward_observation is not None
            await self._accept(result, row=-1, role=f"pilot_{axis}")
            record.update({
                "state": "outward_observed",
                "outward_verified": outward,
                "outward_match_code": result["match"].get("code"),
                "outward_model_candidates": result["match"].get("model_candidates", []),
                "outward_observation": outward_observation,
            })
            await self._persist()
            try:
                home = await self._absolute(origin)
            except PanoramaCaptureError as error:
                if error.code in {"motion_not_observed", "stability_timeout"}:
                    await self._confirm_stop()
                    record.update(state="return_unconfirmed", return_error=error.code)
                    await self._persist()
                    raise PanoramaCaptureError("pilot_cycle_unconfirmed") from error
                raise
            backward_observation = self._axis_response_observation(
                axis, home["match"], result["pose"], home["pose"], kind="pilot_return"
            )
            backward = backward_observation is not None
            await self._accept(home, row=-1, role="pilot_return")
            record["return_verified"] = backward
            record["return_observation"] = backward_observation
            cycle_closed = False
            if origin_image is not None:
                closure = await asyncio.to_thread(_match, origin_image, home["frame"]["image"])
                closure_displacement = _finite(closure.get("displacement"))
                closure_overlap = _finite(closure.get("overlap"))
                closure_verified = closure.get("verified") is True
                record["cycle_closure"] = {
                    "visually_closed": bool(
                        closure_verified
                        and closure_displacement is not None
                        and closure_displacement <= 3
                        and closure_overlap is not None
                        and closure_overlap >= 0.85
                    ),
                    "displacement": closure_displacement,
                    "overlap": closure_overlap,
                    "match_verified": closure_verified,
                    "code": closure.get("code"),
                    "geometry_status": "not_validated",
                }
                cycle_closed = record["cycle_closure"]["visually_closed"]
            record["state"] = "cycle_closed" if cycle_closed else "cycle_unconfirmed"
            await self._persist()
            if not cycle_closed:
                raise PanoramaCaptureError("pilot_cycle_unconfirmed")
            if cycle_closed and (outward or backward):
                for observation in (outward_observation, backward_observation):
                    if observation is not None:
                        self._store_axis_response(
                            axis,
                            {**observation, "pilot_cycle_id": record["cycle_id"]},
                            cycle_closed=True,
                        )
                await self._persist()
                try:
                    geometry = _axis_overlap_geometry(
                        span=span,
                        optical_samples=_pilot_optical_samples(self.checkpoint, axis),
                        maximum_count=MAX_CAPTURES,
                    )
                except PanoramaCaptureError as error:
                    if error.code != "pilot_geometry_unconfirmed":
                        raise
                    if last_geometry is not None:
                        return last_geometry["step"]
                    if expanding:
                        raise
                    amplitude /= 2
                    continue
                last_geometry = geometry
                next_amplitude = min(amplitude * 2, maximum_excursion)
                if (
                    attempt + 1 >= MAX_GRID_PILOT_CYCLES_PER_AXIS
                    or next_amplitude <= amplitude + 1e-12
                    or min(
                        observation["overlap"]
                        for observation in (outward_observation, backward_observation)
                        if observation is not None
                    ) <= GRID_TARGET_OVERLAP
                ):
                    return geometry["step"]
                amplitude = next_amplitude
                expanding = True
                continue
            if last_geometry is not None:
                return last_geometry["step"]
            if expanding:
                raise PanoramaCaptureError("pilot_geometry_unconfirmed")
            amplitude /= 2
        raise PanoramaCaptureError("pilot_cycle_unconfirmed")

    def _graph_target(self, capture: dict, plan: list[dict]) -> dict | None:
        index = capture.get("plan_index")
        if isinstance(index, int) and 0 <= index < len(plan):
            return plan[index]
        target = capture.get("requested_target")
        return target if isinstance(target, dict) else None

    def _prepare_graph(self, plan: list[dict]) -> None:
        self.checkpoint.setdefault("graph_edges", [])
        self.checkpoint.setdefault("bridge_attempts", {})
        for capture in self.captures:
            if capture.get("role") != "grid" or "plan_index" in capture:
                continue
            pose = capture.get("pose", {})
            if any(_finite(pose.get(axis)) is None for axis in ("pan", "tilt")):
                continue
            candidates = [
                index
                for index, target in enumerate(plan)
                if target["row"] == capture.get("row_index")
                and all(abs(pose[axis] - target[axis]) <= 0.018 for axis in ("pan", "tilt"))
            ]
            if len(candidates) == 1:
                # This recovers only the old coarse command association. It
                # creates no visual edge or claim of calibrated PT accuracy.
                capture["plan_index"] = candidates[0]

    def _grid_neighbours(self, capture: dict, plan: list[dict]) -> list[dict]:
        target = self._graph_target(capture, plan)
        if target is None:
            return []
        latest: dict[int, dict] = {}
        for previous in self.captures:
            if (
                previous is not capture
                and previous.get("role") == "grid"
                and isinstance(previous.get("plan_index"), int)
            ):
                latest[previous["plan_index"]] = previous
        selected: list[dict] = []
        previous_visit = latest.pop(capture["plan_index"], None)
        if previous_visit is not None:
            selected.append(previous_visit)
        for axis, fixed_axis in (("pan", "tilt"), ("tilt", "pan")):
            for direction in (-1, 1):
                candidates = []
                for previous in latest.values():
                    previous_target = self._graph_target(previous, plan)
                    if (
                        previous_target is None
                        or abs(previous_target[fixed_axis] - target[fixed_axis]) > 1e-6
                    ):
                        continue
                    distance = (previous_target[axis] - target[axis]) * direction
                    if distance > 1e-6:
                        candidates.append((distance, previous))
                if candidates:
                    selected.append(min(candidates, key=lambda item: item[0])[1])
        return selected

    def _record_edge(self, first: dict, second: dict, matching: dict, *, method: str) -> bool:
        if not matching.get("verified") or matching.get("overlap", 0) < 0.25:
            return False
        key = sorted([first["id"], second["id"]])
        edges = self.checkpoint["graph_edges"]
        if not any(sorted([edge["first"], edge["second"]]) == key for edge in edges):
            edges.append(
                {"first": first["id"], "second": second["id"], "method": method, "match": matching}
            )
        return True

    def _graph_confirmation(self, plan: list[dict], visited: set[int]) -> tuple[set[int], set[int]]:
        nodes = {
            capture["id"]: capture
            for capture in self.captures
            if capture.get("role") in {"grid", "grid_bridge"}
        }
        neighbours: dict[str, set[str]] = {key: set() for key in nodes}
        for edge in self.checkpoint["graph_edges"]:
            if edge["first"] in nodes and edge["second"] in nodes:
                neighbours[edge["first"]].add(edge["second"])
                neighbours[edge["second"]].add(edge["first"])
        remaining, components = set(nodes), []
        while remaining:
            pending, component = [min(remaining)], set()
            while pending:
                identifier = pending.pop()
                if identifier in component:
                    continue
                component.add(identifier)
                pending.extend(neighbours[identifier] - component)
            remaining -= component
            components.append(component)

        def indices(component: set[str]) -> set[int]:
            return {
                nodes[key]["plan_index"]
                for key in component
                if isinstance(nodes[key].get("plan_index"), int)
            }

        largest = max(
            components,
            key=lambda component: (len(indices(component)), len(component)),
            default=set(),
        )
        confirmed = indices(largest)
        self.checkpoint["graph_component_count"] = len(components)
        self.checkpoint["graph_connected_capture_ids"] = sorted(largest)
        self.issues = [
            issue
            for issue in self.issues
            if not (
                issue.get("code") == "coverage_connection_unverified"
                and issue.get("index") in confirmed
            )
        ]
        return confirmed, visited - confirmed

    async def _bridge_pair(
        self, first: dict, second: dict, plan: list[dict], *, base_remaining: int, key: str
    ) -> bool:
        history = self.checkpoint["bridge_attempts"].setdefault(key, {"subdivisions": []})
        photographed = {
            capture.get("plan_index") for capture in self.captures if capture.get("role") == "grid"
        }
        base_remaining = max(base_remaining, len(set(range(len(plan))) - photographed))
        if (
            len(history["subdivisions"]) >= 2
            or len(self.captures) + base_remaining + 1 > MAX_CAPTURES
        ):
            return False
        first_target, second_target = (
            self._graph_target(first, plan),
            self._graph_target(second, plan),
        )
        if first_target is None or second_target is None:
            return False
        first_image, second_image = (
            self._private_image(first["path"]),
            self._private_image(second["path"]),
        )
        for capture, image in ((first, first_image), (second, second_image)):
            if "texture_support" not in capture:
                capture["texture_support"] = await asyncio.to_thread(_texture_support, image)
            if not capture["texture_support"].get("distributed"):
                history["state"] = "insufficient_texture"
                return False
        midpoint = {
            axis: (first_target[axis] + second_target[axis]) / 2 for axis in ("pan", "tilt")
        }
        if max(abs(first_target[axis] - second_target[axis]) for axis in ("pan", "tilt")) <= 0.005:
            return False
        attempt = {
            "target": midpoint,
            "state": "planned",
            "first": first["id"],
            "second": second["id"],
        }
        history["subdivisions"].append(attempt)
        await self._persist()
        await self._emit("capturing", planned_captures=len(self.captures) + base_remaining + 1)
        try:
            result = await self._absolute(midpoint)
            result["requested_target"] = midpoint
            bridge = await self._accept(result, row=second.get("row_index", -1), role="grid_bridge")
            attempt.update(state="captured", capture_id=bridge["id"])
            links = []
            for endpoint, image in ((first, first_image), (second, second_image)):
                matching = await asyncio.to_thread(_match, image, result["frame"]["image"])
                links.append(self._record_edge(endpoint, bridge, matching, method="midpoint"))
            await self._persist()
            for index, endpoint in enumerate((first, second)):
                if not links[index]:
                    links[index] = await self._bridge_pair(
                        endpoint, bridge, plan, base_remaining=base_remaining, key=key
                    )
            history["state"] = "connected" if all(links) else "unresolved"
            return all(links)
        except PanoramaCaptureError as error:
            attempt.update(state="failed", code=error.code)
            if error.code in {
                "control_lost",
                "stop_unconfirmed",
                "camera_configuration_changed",
                "source_geometry_changed",
                "optical_state_changed",
            }:
                raise
            return False

    async def _connect_grid(self, capture: dict, plan: list[dict], *, base_remaining: int) -> None:
        image = self._private_image(capture["path"])
        failed = []
        for neighbour in self._grid_neighbours(capture, plan):
            comparison = await asyncio.to_thread(
                _match, self._private_image(neighbour["path"]), image
            )
            if self._record_edge(neighbour, capture, comparison, method="grid_neighbour"):
                continue
            if comparison.get("code") in {
                "insufficient_correspondences",
                "correspondences_not_distributed",
                "correspondence_model_failed",
            } or comparison.get("verified"):
                failed.append(neighbour)
        for neighbour in failed:
            # An existing horizontal edge may belong to an isolated row. Only
            # skip an extra photograph when a verified path already joins this
            # particular pair, including links through other rows or bridges.
            connected = {capture["id"]}
            while True:
                expanded = set(connected)
                for edge in self.checkpoint["graph_edges"]:
                    if edge["first"] in connected or edge["second"] in connected:
                        expanded.update((edge["first"], edge["second"]))
                if expanded == connected:
                    break
                connected = expanded
            if neighbour["id"] in connected:
                continue
            first_index, second_index = neighbour["plan_index"], capture["plan_index"]
            if first_index == second_index:
                continue
            key = f"{min(first_index, second_index)}:{max(first_index, second_index)}"
            await self._bridge_pair(
                neighbour, capture, plan, base_remaining=base_remaining, key=key
            )

    async def _absolute_scan(self, limits: dict) -> None:
        self.checkpoint["mode"] = "absolute"
        plan = self.checkpoint.get("plan")
        if not plan:
            pilot_attempts = self.checkpoint.get("pilot_attempts")
            if pilot_attempts:
                _validate_absolute_pilot_limits(self.checkpoint, limits=limits)
            else:
                self.checkpoint["absolute_pilot_limits"] = (
                    _canonical_absolute_pilot_limits(limits)
                )
                # The actual full span is durable before a pilot intent can be
                # persisted or dispatched, so restart coverage uses the same bounds.
                await self._persist()
            origin = {axis: self.last_pose[axis] for axis in ("pan", "tilt")}
            self.checkpoint["acquisition_origin"] = origin
            steps = {}
            for axis in ("pan", "tilt"):
                if limits[axis]["min"] == limits[axis]["max"]:
                    steps[axis] = 0.0
                    continue
                steps[axis] = await self._pilot_axis(axis, origin, limits)
            plan, grid_geometry = _absolute_grid_plan(
                limits=limits,
                axis_samples={
                    axis: _pilot_optical_samples(self.checkpoint, axis)
                    for axis in ("pan", "tilt")
                },
                captures_used=len(self.captures),
                maximum_captures=MAX_CAPTURES,
            )
            steps = {axis: grid_geometry[axis]["step"] for axis in ("pan", "tilt")}
            self.checkpoint.update(
                plan=plan,
                next_index=0,
                pilot_steps=steps,
                grid_geometry=grid_geometry,
                absolute_grid=_absolute_grid_contract(
                    capabilities=self.capabilities,
                    limits=limits,
                    plan=plan,
                    grid_geometry=grid_geometry,
                    pilot_steps=steps,
                    optical_samples={
                        axis: _pilot_optical_samples(self.checkpoint, axis)
                        for axis in ("pan", "tilt")
                    },
                ),
            )
            await self._persist()
        self._prepare_graph(plan)
        start_index = int(self.checkpoint.get("next_index", 0))
        unresolved = set(self.checkpoint.get("unresolved_indices", []))
        confirmed = set(self.checkpoint.get("confirmed_indices", []))
        visited = (
            set(self.checkpoint.get("visited_indices", range(start_index))) | confirmed | unresolved
        )
        if start_index and not confirmed:
            # A pre-checkpoint-format scan cannot inherit a completion claim.
            unresolved.update(range(start_index))
        confirmed, unresolved = self._graph_confirmation(plan, visited)
        origin = self.checkpoint.get("acquisition_origin") or self.saved_return or self.last_pose
        origin_tilt = _finite(origin.get("tilt"))
        if origin_tilt is None:
            origin_tilt = self.last_pose.get("tilt", 0.0)
        pending = sorted(
            unresolved | (set(range(len(plan))) - visited),
            key=lambda index: (abs(plan[index]["tilt"] - origin_tilt), index),
        )
        if plan:
            self.checkpoint["primary_row"] = min(
                plan, key=lambda item: abs(item["tilt"] - origin_tilt)
            )["row"]
        await self._emit("capturing", planned_captures=len(pending) + len(self.captures))
        for pending_offset, index in enumerate(pending):
            self._check()
            item = plan[index]
            self.checkpoint["active_row"] = item["row"]
            target = {axis: item[axis] for axis in ("pan", "tilt")}
            accepted = False
            for attempt in range(MAX_ATTEMPTS):
                try:
                    # A retry at an unchanged pose needs a new observed excursion.
                    if attempt:
                        axis = "pan"
                        offset = self.checkpoint.get("pilot_steps", {}).get(axis, 0.05) * 0.3
                        other = float(
                            np.clip(target[axis] + offset, limits[axis]["min"], limits[axis]["max"])
                        )
                        if abs(other - target[axis]) < 0.01:
                            other = max(limits[axis]["min"], target[axis] - offset)
                        await self._absolute({**target, axis: other})
                    result = await self._absolute(target)
                    # Edges always refer to persisted photographs. A transient
                    # retry excursion never becomes a node or evidence of coverage.
                    capture = await self._accept(result, row=item["row"], plan_index=index)
                    await self._connect_grid(
                        capture, plan, base_remaining=len(pending) - pending_offset - 1
                    )
                    accepted = True
                    for axis in ("pan", "tilt"):
                        for side in ("min", "max"):
                            if abs(target[axis] - limits[axis][side]) < 1e-9:
                                self.boundaries[f"{axis}_{side}"] = {
                                    "confirmed": True,
                                    "method": "published_limit_and_observed_arrival",
                                    "position": result["pose"].get(axis),
                                }
                    break
                except PanoramaCaptureError as error:
                    if error.code in {
                        "control_lost",
                        "stop_unconfirmed",
                        "camera_configuration_changed",
                        "source_geometry_changed",
                        "optical_state_changed",
                    }:
                        raise
                    self.issues.append({"code": error.code, "index": index, "attempt": attempt + 1})
                    diagnostics = self.checkpoint.get("diagnostics", {}).get("attempts", [])
                    latest = diagnostics[-1] if diagnostics else {}
                    scene = latest.get("terminal_scene", {})
                    if (
                        error.code in {"motion_not_observed", "stability_timeout"}
                        and latest.get("outcome") == error.code
                        and latest.get("requested_target") == target
                        and scene.get("classification") == "scene_texture_insufficient"
                    ):
                        self.checkpoint.setdefault("scene_regions", {})[str(index)] = scene
                        self.issues.append(
                            {
                                "code": "scene_texture_insufficient",
                                "index": index,
                                "attempt": attempt + 1,
                            }
                        )
                        break
            if not accepted:
                unresolved.add(index)
                confirmed.discard(index)
            visited.add(index)
            confirmed, unresolved = self._graph_confirmation(plan, visited)
            if accepted and index in unresolved:
                self.issues.append({"code": "coverage_connection_unverified", "index": index})
            prefix = 0
            while prefix in visited:
                prefix += 1
            self.checkpoint["next_index"] = prefix
            self.checkpoint["visited_indices"] = sorted(visited)
            self.checkpoint["unresolved_indices"] = sorted(unresolved)
            self.checkpoint["confirmed_indices"] = sorted(confirmed)
            await self._persist()
        self.complete = (
            not unresolved
            and len(visited) == len(plan)
            and len(confirmed) == len(plan)
            and all(
                self.boundaries.get(f"{axis}_{side}", {}).get("confirmed")
                for axis in ("pan", "tilt")
                for side in ("min", "max")
            )
        )
        self.coverage.update(planned_positions=len(plan), failed_positions=len(unresolved))

    async def _pulse(
        self,
        axis: str,
        direction: int,
        duration: float,
        *,
        speed: float = DEFAULT_CONTINUOUS_PULSE_SPEED,
        expected_frame: dict | None = None,
        intent_type: str = "seek_pulse",
        intent_step: int | None = None,
        connection_recovery_phase: str | None = None,
    ) -> dict:
        # Start slowly enough to retain inter-frame correspondence on cameras
        # with fast motors. Duration adapts to measured overlap in _seek; speed
        # must not turn the first pilot pulse into an unobservable jump.
        if self.capabilities.get("axes", {}).get(axis) is not True:
            raise PanoramaCaptureError("axis_movement_unavailable")
        if isinstance(direction, bool) or direction not in {-1, 1}:
            raise PanoramaCaptureError("invalid_velocity")
        duration = _finite(duration)
        if duration is None or not 0 < duration <= 2.0:
            raise PanoramaCaptureError("invalid_movement_duration")
        speed = _finite(speed)
        if speed is None or not 0 < speed <= DEFAULT_CONTINUOUS_PULSE_SPEED:
            raise PanoramaCaptureError("invalid_velocity")
        if intent_type not in {
            "seek_pulse",
            "connection_anchor_return",
            "connection_halfstep",
            "reference_probe",
        }:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        if intent_step is not None and (
            intent_type != "connection_halfstep" or type(intent_step) is not int
        ):
            raise PanoramaCaptureError("continuous_resume_unavailable")
        movement_type = intent_type
        cursor = self.checkpoint.get("continuous_cursor")
        if (
            intent_type == "seek_pulse"
            and isinstance(cursor, dict)
            and cursor.get("stage") == "reference"
        ):
            movement_type = "reference_probe"
        if intent_type == "reference_probe" and (
            not isinstance(cursor, dict) or cursor.get("stage") != "reference"
        ):
            raise PanoramaCaptureError("continuous_resume_unavailable")
        if not self.returning and isinstance(cursor, dict):
            try:
                limit_probe = _pending_limit_probe_intent(cursor)
            except ValueError:
                raise PanoramaCaptureError("continuous_resume_unavailable") from None
            if limit_probe is not None:
                if (
                    limit_probe["axis"] != axis
                    or limit_probe["direction"] != direction
                    or abs(limit_probe["duration"] - duration) > 1e-9
                ):
                    raise PanoramaCaptureError("continuous_resume_unavailable")
                movement_type = "limit_probe"
        velocity = {"pan": 0.0, "tilt": 0.0, axis: direction * speed}
        fallback = self.checkpoint.get("absolute_grid_fallback")
        fallback_mode = (
            fallback.get("continuous_mode")
            if not self.returning
            and isinstance(fallback, dict)
            and fallback.get("state") == "active"
            else None
        )
        use_relative = bool(
            fallback_mode == "relative"
            or fallback_mode is None
            and self.capabilities.get("relative_supported")
            and not self.capabilities.get("velocity_supported")
        )
        if use_relative:
            if self.capabilities.get("relative_supported") is not True:
                raise PanoramaCaptureError("normalized_motion_unavailable")
            delta = {key: value * duration for key, value in velocity.items()}
            return await self._move(
                lambda: self.camera.move_relative(**delta),
                allow_stationary=True,
                expected_frame=expected_frame,
                movement_intent={
                    "type": movement_type,
                    "axis": axis,
                    "direction": direction,
                    "duration": duration,
                    **({"step": intent_step} if intent_step is not None else {}),
                },
                connection_recovery_phase=connection_recovery_phase,
            )
        if fallback_mode not in {None, "velocity"} or not self.capabilities.get(
            "velocity_supported"
        ):
            raise PanoramaCaptureError("normalized_motion_unavailable")
        # Keep the controller's legal duration floor. Fine return may explicitly
        # request a lower, bounded velocity and then qualify its optical effect;
        # the scanner never assumes that a device honored that velocity.
        if duration < MINIMUM_CONTINUOUS_PULSE_SECONDS:
            raise PanoramaCaptureError("visual_control_resolution_unverified")
        if speed != DEFAULT_CONTINUOUS_PULSE_SPEED:
            spaces = self.capabilities.get("spaces", {}).get("continuous", [])
            default_space = self.capabilities.get("defaults", {}).get("continuous")
            selected = [
                space
                for space in spaces
                if space.get("normalized") is True and space.get("uri") == default_space
            ]
            requested = {"x": velocity["pan"], "y": velocity["tilt"]}
            exact_velocity_supported = len(selected) == 1
            if exact_velocity_supported:
                for coordinate, value in requested.items():
                    bounds = selected[0].get(coordinate)
                    minimum = _finite(bounds.get("min")) if isinstance(bounds, dict) else None
                    maximum = _finite(bounds.get("max")) if isinstance(bounds, dict) else None
                    if minimum is None or maximum is None or not minimum <= value <= maximum:
                        exact_velocity_supported = False
                        break
            if not exact_velocity_supported:
                raise PanoramaCaptureError("visual_control_resolution_unverified")
        return await self._move(
            lambda: self.camera.move_velocity(**velocity, timeout_s=duration),
            duration=duration,
            allow_stationary=True,
            requested_velocity=velocity,
            expected_frame=expected_frame,
            movement_intent={
                "type": movement_type,
                "axis": axis,
                "direction": direction,
                "duration": duration,
                **({"step": intent_step} if intent_step is not None else {}),
            },
            connection_recovery_phase=connection_recovery_phase,
        )

    def _anchor_image(self, cursor: dict, *, row: int) -> np.ndarray:
        anchor = cursor.get("anchor") or {}
        capture = next(
            (item for item in self.captures if item.get("id") == anchor.get("capture_id")), None
        )
        if (
            capture is None
            or anchor.get("row") != row
            or capture.get("row_index") != row
            or capture.get("path") != anchor.get("path")
            or capture.get("quality", {}).get("stable") is not True
        ):
            raise PanoramaCaptureError("relocalization_required")
        return self._private_image(anchor["path"])

    def _anchor_absolute_target(self, cursor: dict, *, row: int) -> dict[str, float] | None:
        """Return the persisted absolute pose bound to the current visual anchor."""
        if self.capabilities.get("absolute_supported") is not True:
            return None
        anchor = cursor.get("anchor") or {}
        capture = next(
            (
                item
                for item in self.captures
                if item.get("id") == anchor.get("capture_id")
                and item.get("path") == anchor.get("path")
                and item.get("row_index") == row
                and item.get("quality", {}).get("stable") is True
            ),
            None,
        )
        pose = _normalized_absolute_pose(
            capture.get("pose", {}) if isinstance(capture, dict) else {},
            self.capabilities,
        )
        if pose is None:
            return None
        return {axis: float(pose[axis]) for axis in ("pan", "tilt")}

    async def _fail_connection_recovery(
        self, cursor: dict, recovery: dict, reason: str
    ) -> None:
        recovery.update(state="failed", failure_reason=reason)
        cursor["relocalization_failed"] = True
        await self._persist()

    async def _subdivide_failed_connection(
        self,
        cursor: dict,
        seek_state: dict,
        *,
        axis: str,
        direction: int,
        row: int,
        failed_duration: float,
        failed_result: dict,
        failed_connection: dict,
        step_limit: int,
        error_code: str,
        return_only: bool = False,
    ) -> tuple[dict, dict, float] | None:
        """Return to one persisted anchor and retry one accepted step at half size."""
        failure_code = _observational_connection_failure(failed_connection)
        anchor_row = (cursor.get("anchor") or {}).get("row")
        target = (
            self._anchor_absolute_target(cursor, row=anchor_row)
            if type(anchor_row) is int
            else None
        )
        original_step = seek_state.get("steps")
        retry_duration = max(MINIMUM_SEEK_PULSE_SECONDS, failed_duration / 2)
        if (
            failure_code is None
            or failed_result.get("stable") is not True
            or failed_result.get("stationary") is True
            or type(original_step) is not int
            or not 1 <= original_step < step_limit
            or (not return_only and retry_duration >= failed_duration)
        ):
            return None
        return_method = "absolute" if target is not None else "relative_pulse"
        if return_method == "relative_pulse":
            command_match = failed_result.get("match")
            if not (
                isinstance(command_match, dict)
                and command_match.get("verified") is True
                and command_match.get("overlap", 0) >= 0.25
                and command_match.get("displacement", 0) >= 2.0
            ):
                return None
        try:
            existing = _connection_recovery(
                cursor, captures=self.captures, capabilities=self.capabilities
            )
        except ValueError:
            raise PanoramaCaptureError("continuous_resume_unavailable") from None
        if existing is not None and existing.get("state") != "complete":
            raise PanoramaCaptureError(error_code)
        subdivisions = seek_state.setdefault("subdivided_steps", [])
        if (
            not isinstance(subdivisions, list)
            or any(type(step) is not int or step <= 0 for step in subdivisions)
            or len(set(subdivisions)) != len(subdivisions)
        ):
            raise PanoramaCaptureError("continuous_resume_unavailable")
        if original_step in subdivisions:
            return None
        subdivisions.append(original_step)
        recovery = {
            "version": CONNECTION_RECOVERY_VERSION,
            "state": "planned",
            "attempt": 1,
            "identity": {
                "axis": axis,
                "direction": direction,
                "row": row,
                "stage": cursor.get("stage"),
                "branch": cursor.get("branch"),
                "step": original_step,
                "anchor": dict(cursor["anchor"]),
            },
            "target": target,
            "return_method": return_method,
            "mode": "return_only" if return_only else "subdivide",
            **({"return_duration": failed_duration} if return_method == "relative_pulse" else {}),
            "failed_duration": failed_duration,
            **({"retry_duration": retry_duration} if not return_only else {}),
            "failure_code": failure_code,
        }
        cursor["connection_recovery"] = recovery
        try:
            _connection_recovery(
                cursor, captures=self.captures, capabilities=self.capabilities
            )
        except ValueError:
            raise PanoramaCaptureError("continuous_resume_unavailable") from None
        await self._persist()

        await self._confirm_stop()
        if self._stop_failed or self.physical_state != "stopped":
            await self._fail_connection_recovery(cursor, recovery, "stop_unconfirmed")
            raise PanoramaCaptureError("stop_unconfirmed")
        failed_view = self.last_frame
        if not isinstance(failed_view, dict):
            await self._fail_connection_recovery(cursor, recovery, "fresh_frame_unavailable")
            raise PanoramaCaptureError(error_code)
        return_failure: PanoramaCaptureError | None = None
        try:
            if return_method == "absolute":
                await self._absolute(
                    target,
                    allow_stationary=True,
                    expected_frame=failed_view,
                    movement_intent={
                        "type": "connection_anchor_return",
                        "axis": axis,
                        "direction": -direction,
                        "duration": 0.0,
                        "target": target,
                    },
                    connection_recovery_phase="return",
                )
            else:
                await self._pulse(
                    axis,
                    -direction,
                    failed_duration,
                    expected_frame=failed_view,
                    intent_type="connection_anchor_return",
                    connection_recovery_phase="return",
                )
        except PanoramaCaptureError as failure:
            return_failure = failure
            if failure.code not in {
                "movement_unconfirmed",
                "motion_not_observed",
                "stability_timeout",
            }:
                await self._fail_connection_recovery(cursor, recovery, failure.code)
                raise
            await self._confirm_stop()
            if self._stop_failed or self.physical_state != "stopped":
                await self._fail_connection_recovery(cursor, recovery, "stop_unconfirmed")
                raise PanoramaCaptureError("stop_unconfirmed") from None

        if not await self._current_anchor(cursor, row=anchor_row):
            reason = return_failure.code if return_failure is not None else "anchor_unconfirmed"
            await self._fail_connection_recovery(cursor, recovery, reason)
            raise PanoramaCaptureError(error_code)
        transition = cursor.get("transition")
        if isinstance(transition, dict) and transition.get("state") == "pending":
            transition.update(state="confirmed", outcome="anchor_return_observed")
        if return_only:
            recovery.update(
                state="failed",
                failure_reason="command_only_connection_returned",
            )
            cursor["relocalization_failed"] = False
            await self._persist()
            raise PanoramaCaptureError(error_code)
        recovery["state"] = "anchor_confirmed"
        recovery["retry_step"] = original_step + 1
        seek_state["duration"] = retry_duration
        await self._persist()

        if seek_state["steps"] >= step_limit:
            await self._fail_connection_recovery(cursor, recovery, "step_budget_exhausted")
            raise PanoramaCaptureError(error_code)
        anchor_frame = self.last_frame
        try:
            retry_result = await self._pulse(
                axis,
                direction,
                retry_duration,
                expected_frame=anchor_frame,
                intent_type="connection_halfstep",
                intent_step=recovery["retry_step"],
                connection_recovery_phase="retry",
            )
        except PanoramaCaptureError as failure:
            transition = cursor.get("transition")
            intent = transition.get("intent") if isinstance(transition, dict) else None
            diagnostics = self.checkpoint.get("diagnostics", {})
            attempts = diagnostics.get("attempts") if isinstance(diagnostics, dict) else None
            movement_id = (
                _movement_attempt_id(transition.get("movement_id"))
                if isinstance(transition, dict)
                else None
            )
            matching_attempts = (
                [
                    attempt
                    for attempt in attempts
                    if isinstance(attempt, dict)
                    and attempt.get("movement_id") == movement_id
                ]
                if movement_id is not None and isinstance(attempts, list)
                else []
            )
            attempt = matching_attempts[0] if len(matching_attempts) == 1 else None
            identity = recovery.get("identity", {})
            observation_failed_after_accepted_command = (
                failure.code in {"motion_not_observed", "stability_timeout"}
                and not self._stop_failed
                and self.physical_state not in {"ownership_lost", "stop_unconfirmed"}
                and recovery.get("state") == "retry_pending"
                and movement_id is not None
                and isinstance(attempt, dict)
                and attempt.get("kind") == "movement"
                and attempt.get("outcome") == failure.code
                and attempt.get("command_outcome") == "accepted"
                and attempt.get("stop_command_accepted") is True
                and isinstance(transition, dict)
                and transition.get("state") == "pending"
                and all(
                    transition.get(key) == cursor.get(key)
                    for key in ("stage", "row", "direction", "branch")
                )
                and isinstance(intent, dict)
                and intent.get("type") == "connection_halfstep"
                and intent.get("axis") == identity.get("axis")
                and intent.get("direction") == identity.get("direction")
                and intent.get("row") == identity.get("row")
                and intent.get("step") == recovery.get("retry_step")
                and _finite(intent.get("duration"))
                == _finite(recovery.get("retry_duration"))
                and intent.get("anchor") == identity.get("anchor")
                and cursor.get("anchor") == identity.get("anchor")
            )
            if observation_failed_after_accepted_command:
                transition.update(
                    state="accepted",
                    outcome="halfstep_observation_failed",
                )
                await self._fail_connection_recovery(
                    cursor,
                    recovery,
                    f"halfstep_{failure.code}",
                )
                raise PanoramaCaptureError(error_code) from failure
            else:
                # Without accepted-command and accepted-Stop evidence the
                # retry may still be in flight. Keep its pending intent so no
                # recovery or restart can replay or route around it.
                cursor["relocalization_failed"] = True
                await self._persist()
            raise
        transition = cursor.get("transition")
        if isinstance(transition, dict) and transition.get("state") == "pending":
            transition.update(state="accepted", outcome="observed_stopped")
        retry_connection = await asyncio.to_thread(
            _match,
            self._anchor_image(cursor, row=anchor_row),
            retry_result["frame"]["image"],
        )
        if (
            retry_result.get("stable") is not True
            or retry_result.get("stationary") is True
            or retry_connection.get("verified") is not True
            or retry_connection.get("overlap", 0) < 0.25
        ):
            await self._fail_connection_recovery(
                cursor,
                recovery,
                (
                    "halfstep_motion_not_observed"
                    if retry_result.get("stationary") is True
                    else _observational_connection_failure(retry_connection)
                    or "halfstep_connection_unverified"
                ),
            )
            raise PanoramaCaptureError(error_code)
        recovery.update(
            state="halfstep_observed",
            stationary=False,
            recovered_connection={
                key: retry_connection.get(key)
                for key in ("verified", "code", "overlap", "displacement", "shift_x", "shift_y")
            },
        )
        await self._persist()
        retry_result["match"] = retry_connection
        return retry_result, retry_connection, retry_duration

    async def _current_anchor(
        self,
        cursor: dict,
        *,
        row: int,
        minimum_overlap: float = 0.85,
        maximum_displacement: float = ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS,
    ) -> bool:
        image = self._anchor_image(cursor, row=row)
        frame = self.last_frame
        received = _finite(frame.get("received_monotonic")) if frame is not None else None
        if self.physical_state != "stopped":
            return False
        diagnostic = _AttemptDiagnostic("anchor_validation")
        diagnostic.value.update(row=row, anchor_capture_id=cursor["anchor"]["capture_id"])
        if received is None or not 0 <= time.monotonic() - received <= 1.0:
            self._check()
            frame = await self._reference_window()
            received = frame["received_monotonic"]
        original_zoom = _finite(self.checkpoint.get("initial_zoom"))
        observed_zoom = _finite(self.last_pose.get("zoom"))
        if (
            original_zoom is not None
            and observed_zoom is not None
            and abs(original_zoom - observed_zoom) > 0.001
        ):
            raise PanoramaCaptureError("optical_state_changed")
        diagnostic.value["frame_age_before_matching_seconds"] = time.monotonic() - received
        matching = await asyncio.to_thread(_no_effect_match, image, frame["image"])
        age = time.monotonic() - received
        if (
            matching.get("verified") and matching.get("overlap", 0) >= minimum_overlap
            and matching.get("displacement", math.inf) <= maximum_displacement
            and age > 1.0 and time.monotonic() - diagnostic.start < ATTEMPT_SECONDS - 4
        ):
            # Matching can consume the remainder of an otherwise recent
            # frame's budget. Observe and compare again once; never extend the
            # freshness limit or relabel the original comparison as current.
            diagnostic.value["expired_comparison_age_seconds"] = age
            self._check()
            frame = await self._reference_window()
            received = frame["received_monotonic"]
            diagnostic.value["frame_age_before_matching_seconds"] = time.monotonic() - received
            matching = await asyncio.to_thread(_anchor_match, image, frame["image"])
            age = time.monotonic() - received
            self.last_frame = frame
        accepted = bool(
            matching.get("verified")
            and matching.get("overlap", 0) >= minimum_overlap
            and matching.get("displacement", math.inf) <= maximum_displacement
            and 0 <= age <= 1.0
        )
        diagnostic.comparison(matching)
        diagnostic.value.update(
            outcome="anchor_verified" if accepted else "anchor_unconfirmed",
            frame_age_after_matching_seconds=age,
            frame_sequence=frame.get("sequence"),
        )
        if accepted:
            # Preserve the exact frame identity that passed ordinary anchor
            # validation. A following seek can use that attestation to fence
            # its first command even when a repeated feature-model fit varies
            # by a few pixels. The command still needs its own fresh dispatch
            # precondition and observed transition before a photograph can be
            # accepted.
            cursor["anchor_observation"] = {
                "capture_id": cursor["anchor"]["capture_id"],
                "path": cursor["anchor"]["path"],
                "row": row,
                "capture_instance": frame.get("capture_instance"),
                "generation": frame.get("generation"),
                "sequence": frame.get("sequence"),
                **{
                    key: matching.get(key)
                    for key in (
                        "support_scope",
                        "inliers",
                        "overlap",
                        "displacement",
                        "shift_x",
                        "shift_y",
                    )
                    if key in matching
                },
            }
        if not accepted:
            try:
                await self._preserve_motion_pair({"image": image}, frame, diagnostic)
            except OSError:
                diagnostic.value["rejected_pair_unavailable"] = True
        self._diagnostic(diagnostic)
        return accepted

    async def _pending_baseline_unchanged(self, intent: dict[str, Any]) -> bool:
        """Prove that an unconfirmed pulse had no visible effect.

        This is deliberately stricter than ordinary relocalization. The image is
        the causal frame persisted immediately before dispatch, not merely the
        most recent accepted panorama photograph.
        """
        baseline = intent["baseline"]
        path = Path(baseline["path"]).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise PanoramaCaptureError("invalid_checkpoint_file")
        digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        if digest != baseline["sha256"]:
            raise PanoramaCaptureError("invalid_checkpoint_image")
        image = self._private_image(str(path))
        if self.physical_state != "stopped":
            return False
        frame = self.last_frame
        received = _finite(frame.get("received_monotonic")) if frame is not None else None
        if received is None or not 0 <= time.monotonic() - received <= 1.0:
            self._check()
            frame = await self._reference_window()
            received = frame["received_monotonic"]
            self.last_frame = frame
        pose = await self._observational_readback()
        self.last_pose = pose
        original_zoom = _finite(self.checkpoint.get("initial_zoom"))
        observed_zoom = _finite(pose.get("zoom"))
        if (
            original_zoom is not None
            and observed_zoom is not None
            and abs(original_zoom - observed_zoom) > 0.001
        ):
            raise PanoramaCaptureError("optical_state_changed")
        if time.monotonic() - received > 1.0:
            self._check()
            frame = await self._reference_window()
            received = frame["received_monotonic"]
            self.last_frame = frame
        matching = await asyncio.to_thread(_anchor_match, image, frame["image"])
        age = time.monotonic() - received
        baseline_instance = baseline.get("capture_instance")
        observed_instance = frame.get("capture_instance")
        baseline_generation = baseline.get("generation")
        observed_generation = frame.get("generation")
        before_sequence = baseline.get("sequence")
        after_sequence = frame.get("sequence")
        same_capture_instance = bool(
            isinstance(baseline_instance, str)
            and baseline_instance
            and baseline_instance == observed_instance
        )
        same_generation = bool(
            type(baseline_generation) is int
            and type(observed_generation) is int
            and baseline_generation == observed_generation
        )
        causal = bool(
            same_capture_instance
            and same_generation
            and type(before_sequence) is int
            and type(after_sequence) is int
            and after_sequence > before_sequence
        )
        accepted = bool(
            causal
            and matching.get("verified") is True
            and matching.get("overlap", 0) >= UNCONFIRMED_NO_EFFECT_OVERLAP
            and matching.get("displacement", math.inf) <= UNCONFIRMED_NO_EFFECT_PIXELS
            and 0 <= age <= 1.0
        )
        diagnostic = _AttemptDiagnostic("pending_command_reconciliation")
        diagnostic.comparison(matching)
        diagnostic.value.update(
            outcome="no_effect_verified" if accepted else "effect_or_state_ambiguous",
            baseline_sequence=baseline.get("sequence"),
            observed_sequence=frame.get("sequence"),
            same_capture_instance=same_capture_instance,
            same_generation=same_generation,
            causal_frame=causal,
            frame_age_after_matching_seconds=age,
        )
        if not accepted:
            try:
                await self._preserve_motion_pair({"image": image}, frame, diagnostic)
            except OSError:
                diagnostic.value["rejected_pair_unavailable"] = True
        self._diagnostic(diagnostic)
        return accepted

    async def _finish_limit_probe_intent(
        self, cursor: dict, intent: dict[str, Any] | None = None
    ) -> None:
        """Consume a returned probe result without making the probe replayable."""
        if intent is None:
            try:
                intent = _pending_limit_probe_intent(cursor)
            except ValueError:
                raise PanoramaCaptureError("continuous_resume_unavailable") from None
        if intent is None:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        persisted_intent = cursor.get("limit_probe_intent")
        if not isinstance(persisted_intent, dict) or any(
            persisted_intent.get(key) != intent[key]
            for key in (
                "type", "state", "stage", "axis", "direction", "row", "step", "anchor"
            )
        ) or _finite(persisted_intent.get("duration")) != intent["duration"]:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        transition = cursor.get("transition")
        if isinstance(transition, dict):
            transition_intent = transition.get("intent")
            if not isinstance(transition_intent, dict) or any(
                transition_intent.get(key) != intent[key]
                for key in ("type", "axis", "direction", "row", "step", "anchor")
            ) or _finite(transition_intent.get("duration")) != intent["duration"]:
                raise PanoramaCaptureError("continuous_resume_unavailable")
            if transition.get("state") == "pending":
                transition.update(state="accepted", outcome="observed_stopped")
        cursor.pop("limit_probe_intent", None)
        await self._persist()

    async def _seek(
        self, axis: str, direction: int, *, row: int, duration: float, store: bool = True
    ) -> tuple[str, float]:
        cursor = self.checkpoint.get("continuous_cursor")
        seek_state = None
        new_seek = False
        new_seek_frame: dict | None = None
        if cursor is not None:
            try:
                connection_recovery = _connection_recovery(
                    cursor, captures=self.captures, capabilities=self.capabilities
                )
                pending_limit_probe = _pending_limit_probe_intent(cursor)
            except ValueError:
                raise PanoramaCaptureError("continuous_resume_unavailable") from None
            if connection_recovery is not None and connection_recovery["state"] != "complete":
                raise PanoramaCaptureError("continuous_resume_unavailable")
            if cursor.get("precondition_reobservation") is not None:
                raise PanoramaCaptureError("continuous_resume_unavailable")
            if pending_limit_probe is not None:
                raise PanoramaCaptureError("continuous_resume_unavailable")
            self._anchor_image(cursor, row=row)
        if cursor is not None:
            seek_state = cursor.get("seek")
            if not seek_state or any(
                seek_state.get(key) != value
                for key, value in (("axis", axis), ("direction", direction), ("row", row))
            ):
                # An accepted capture already has a causal stopped-window
                # proof. Reuse that exact, still-fresh frame when a segment
                # starts immediately afterwards; otherwise observe a fresh
                # stopped window as usual. A persisted capture observation
                # cannot pass this check after restart because the live frame
                # has a different sequence and receipt time.
                anchor = cursor.get("anchor")
                observation = cursor.get("anchor_observation")
                received = (
                    _finite(self.last_frame.get("received_monotonic"))
                    if isinstance(self.last_frame, dict)
                    else None
                )
                fresh_accepted_anchor = bool(
                    isinstance(anchor, dict)
                    and isinstance(observation, dict)
                    and observation.get("evidence") == "accepted_capture"
                    and observation.get("capture_id") == anchor.get("capture_id")
                    and observation.get("path") == anchor.get("path")
                    and observation.get("row") == row
                    and isinstance(self.last_frame, dict)
                    and observation.get("capture_instance") == self.last_frame.get("capture_instance")
                    and observation.get("generation") == self.last_frame.get("generation")
                    and observation.get("sequence") == self.last_frame.get("sequence")
                    and received is not None
                    and 0 <= time.monotonic() - received <= 1.0
                )
                if not fresh_accepted_anchor:
                    self.last_frame = await self._reference_window(timeout=3.0)
                self.last_pose = await self._observational_readback()
                if not await self._current_anchor(cursor, row=row):
                    raise PanoramaCaptureError("coverage_connection_unverified")
                origin_path = self.directory / f"seek-origin-{row}-{axis}-{direction}.jpg"
                new_seek_frame = self.last_frame
                await asyncio.to_thread(_write_image, origin_path, self.last_frame["image"])
                seek_state = {
                    "axis": axis,
                    "direction": direction,
                    "row": row,
                    "origin_path": str(origin_path),
                    "progress": False,
                    "steps": 0,
                    "stationary_count": 0,
                    "origin_excursion": False,
                }
                cursor["seek"] = seek_state
                new_seek = True
                await self._persist()
            if seek_state.get("outcome"):
                return seek_state["outcome"], seek_state.get("duration", duration)
            subdivided_steps = seek_state.get("subdivided_steps", [])
            if (
                not isinstance(subdivided_steps, list)
                or any(
                    type(subdivided_step) is not int
                    or not 1 <= subdivided_step <= seek_state.get("steps", -1)
                    for subdivided_step in subdivided_steps
                )
                or len(set(subdivided_steps)) != len(subdivided_steps)
            ):
                raise PanoramaCaptureError("continuous_resume_unavailable")
        progress_seen = bool(seek_state and seek_state.get("progress"))
        origin_excursion = bool(seek_state and seek_state.get("origin_excursion"))
        stationary_count = int(seek_state.get("stationary_count", 0)) if seek_state else 0
        origin_uncertain = (
            seek_state.get("origin_uncertain_since_anchor", False) if seek_state else False
        )
        if type(origin_uncertain) is not bool:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        duration = seek_state.get("duration", duration) if seek_state else duration
        starting_frame = (
            {"image": self._private_image(seek_state["origin_path"])}
            if seek_state
            else self.last_frame
        )
        previous_pose = await self._observational_readback()
        # A new branch may start after boundary confirmation or another long
        # observation while the persisted coverage anchor remains unchanged.
        # Use the current stopped view to fence the first physical command;
        # the immutable anchor still proves geometric continuity below.
        transition = cursor.get("transition") if cursor is not None else None
        transition_intent = transition.get("intent") if isinstance(transition, dict) else None
        resumed_from_graph = bool(
            isinstance(transition, dict)
            and transition.get("state") == "relocalized"
            and transition.get("outcome") == "stopped_graph_relocalized"
            and isinstance(transition_intent, dict)
            and transition_intent.get("type") == "seek_pulse"
            and transition_intent.get("axis") == axis
            and transition_intent.get("direction") == direction
            and transition_intent.get("row") == row
        )
        if resumed_from_graph and seek_state is not None:
            origin_uncertain = True
            seek_state["origin_uncertain_since_anchor"] = True
        stationary_precondition_frame: dict | None = (
            self.last_frame if resumed_from_graph else new_seek_frame if new_seek else None
        )

        async def pulse(
            side: int,
            seconds: float,
            *,
            limit_probe: bool = False,
            expected_frame: dict | None = None,
        ) -> dict:
            if seek_state is not None:
                if seek_state["steps"] >= MAX_CONTINUOUS_STEPS:
                    raise PanoramaCaptureError("pan_row_limit_unconfirmed")
                seek_state.update(
                    steps=seek_state["steps"] + 1,
                    duration=seconds,
                    stationary_count=stationary_count,
                    origin_excursion=origin_excursion,
                )
                if limit_probe:
                    cursor["limit_probe_intent"] = {
                        "type": "limit_probe",
                        "state": "pending",
                        "stage": cursor.get("stage", "pan"),
                        "axis": axis,
                        "direction": side,
                        "row": row,
                        "step": seek_state["steps"],
                        "duration": seconds,
                        "anchor": dict(cursor["anchor"]),
                    }
                    try:
                        _pending_limit_probe_intent(cursor)
                    except ValueError:
                        raise PanoramaCaptureError("continuous_resume_unavailable") from None
                await self._persist()
            return await self._pulse(
                axis,
                side,
                seconds,
                expected_frame=expected_frame,
            )

        first_step = int(seek_state.get("steps", 0)) if seek_state else 0
        for step in range(first_step, MAX_CONTINUOUS_STEPS):
            self._check()
            baseline = (
                {"image": self._anchor_image(cursor, row=row)}
                if cursor is not None
                else self.last_frame
            )
            recovered = False
            retry_precondition_frame = stationary_precondition_frame or baseline
            recovery_anchor_precondition: dict[str, Any] | None = None
            recovery_evidence: dict[str, Any] | None = None
            if new_seek and new_seek_frame is not None and cursor is not None:
                anchor = cursor.get("anchor")
                anchor_capture = next(
                    (
                        capture
                        for capture in self.captures
                        if isinstance(anchor, dict)
                        and capture.get("id") == anchor.get("capture_id")
                        and capture.get("path") == anchor.get("path")
                        and capture.get("row_index") == row
                    ),
                    None,
                )
                fresh_anchor_match = await asyncio.to_thread(
                    _no_effect_match,
                    baseline["image"],
                    new_seek_frame["image"],
                )
                fresh_overlap = _finite(fresh_anchor_match.get("overlap"))
                fresh_displacement = _finite(fresh_anchor_match.get("displacement"))
                direct_fresh_visual = bool(
                    fresh_anchor_match.get("verified") is True
                    and fresh_overlap is not None
                    and fresh_overlap >= 0.85
                    and fresh_displacement is not None
                    and fresh_displacement <= RECOVERY_ANCHOR_PRECONDITION_PIXELS
                )
                anchor_observation = cursor.get("anchor_observation")
                verified_anchor_observation = bool(
                    isinstance(anchor_observation, dict)
                    and isinstance(anchor, dict)
                    and anchor_observation.get("capture_id") == anchor.get("capture_id")
                    and anchor_observation.get("path") == anchor.get("path")
                    and anchor_observation.get("row") == row
                    and anchor_observation.get("capture_instance")
                    == new_seek_frame.get("capture_instance")
                    and anchor_observation.get("generation")
                    == new_seek_frame.get("generation")
                    and anchor_observation.get("sequence")
                    == new_seek_frame.get("sequence")
                    and _finite(anchor_observation.get("overlap")) is not None
                    and anchor_observation["overlap"] >= 0.85
                    and _finite(anchor_observation.get("displacement")) is not None
                    and anchor_observation["displacement"]
                    <= ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS
                )
                fresh_visual = direct_fresh_visual or verified_anchor_observation
                fresh_native = (
                    _native_axes_unchanged(
                        anchor_capture.get("pose"),
                        previous_pose,
                        {axis: float(direction)},
                    )
                    if isinstance(anchor_capture, dict)
                    and fresh_anchor_match.get("verified") is not True
                    else None
                )
                recovery_evidence = {
                    "verified": fresh_visual or fresh_native is not None,
                    "identity_verified": bool(
                        isinstance(anchor_capture, dict)
                        and anchor_capture.get("capture_instance")
                        == new_seek_frame.get("capture_instance")
                        and anchor_capture.get("generation")
                        == new_seek_frame.get("generation")
                        and type(anchor_capture.get("sequence")) is int
                        and type(new_seek_frame.get("sequence")) is int
                        and new_seek_frame["sequence"] >= anchor_capture["sequence"]
                    ),
                    "overlap": fresh_overlap,
                    "displacement": fresh_displacement,
                    "code": fresh_anchor_match.get("code"),
                    "evidence": (
                        "fresh_seek_visual_anchor"
                        if direct_fresh_visual
                        else "fresh_seek_verified_anchor"
                        if verified_anchor_observation
                        else "fresh_seek_native_pose"
                        if fresh_native is not None
                        else "insufficient"
                    ),
                }
                if fresh_native is not None:
                    recovery_evidence["native_stationary"] = fresh_native
                seek_state["origin_anchor_precondition"] = recovery_evidence
            while True:
                recovery_level = self.checkpoint.get("seek_recovery_level", {}).get(axis, 0)
                try:
                    result = await pulse(
                        direction,
                        duration,
                        expected_frame=retry_precondition_frame,
                    )
                    transition = cursor.get("transition") if cursor is not None else None
                    if isinstance(transition, dict) and transition.get("state") == "pending":
                        transition.update(state="accepted", outcome="observed_stopped")
                        await self._persist()
                    break
                except PanoramaCaptureError as error:
                    if error.code == "movement_unconfirmed":
                        # A failed controller call may have reached the device.
                        # Reconcile against its causal pre-command frame; an
                        # ordinary navigation anchor is intentionally too loose.
                        command_failures = (
                            int(seek_state.get("command_failures", 0))
                            if isinstance(seek_state, dict)
                            and type(seek_state.get("command_failures", 0)) is int
                            else MAX_UNCONFIRMED_RETRIES_PER_SEEK
                        )
                        if (
                            self._stop_failed
                            or recovery_level >= 2
                            or command_failures >= MAX_UNCONFIRMED_RETRIES_PER_SEEK
                            or duration <= MINIMUM_SEEK_PULSE_SECONDS
                            or cursor is None
                            or seek_state is None
                        ):
                            raise
                        try:
                            pending_intent = _pending_seek_intent(cursor)
                        except ValueError:
                            raise PanoramaCaptureError(
                                "continuous_resume_unavailable"
                            ) from None
                        if pending_intent is None:
                            raise PanoramaCaptureError(
                                "continuous_resume_unavailable"
                            ) from None
                        await self._confirm_stop()
                        if self._stop_failed or self.physical_state != "stopped":
                            raise PanoramaCaptureError("stop_unconfirmed") from None
                        if not await self._pending_baseline_unchanged(pending_intent):
                            transition = cursor.get("transition")
                            if isinstance(transition, dict):
                                transition.update(
                                    state="effect_or_state_ambiguous",
                                    outcome="recovery_required",
                                )
                            cursor["relocalization_failed"] = True
                            await self._persist()
                            raise PanoramaCaptureError(
                                "coverage_connection_unverified"
                            ) from None
                        transition = cursor.get("transition")
                        if isinstance(transition, dict):
                            transition.update(
                                state="no_effect_verified",
                                outcome="stopped_baseline_unchanged",
                            )
                        recovered = True
                        recovery_level += 1
                        self.checkpoint.setdefault("seek_recovery_level", {})[
                            axis
                        ] = recovery_level
                        last_verified = _finite(seek_state.get("last_verified_duration"))
                        duration = max(
                            MINIMUM_SEEK_PULSE_SECONDS,
                            min(duration / 2, last_verified or duration / 2),
                        )
                        if seek_state is not None:
                            seek_state.update(
                                duration=duration,
                                command_failures=command_failures + 1,
                            )
                        await self._persist()
                        continue
                    if (
                        self._stop_failed
                        or error.code
                        not in {
                            "motion_not_observed",
                            "stability_timeout",
                        }
                        or recovery_level >= 2
                    ):
                        raise
                    # First observe a stopped, fresh view. A residual below the
                    # smallest accepted movement may become the retry origin. A
                    # larger residual is usable only when the accepted command,
                    # transport identity, geometric link and learned axis
                    # direction form one complete causal proof.
                    if cursor is not None and seek_state is not None:
                        await self._confirm_stop()
                        if (
                            self._stop_failed
                            or self.physical_state != "stopped"
                            or not isinstance(self.last_frame, dict)
                        ):
                            raise PanoramaCaptureError("stop_unconfirmed") from None
                        anchor = cursor.get("anchor")
                        anchor_capture = next(
                            (
                                capture
                                for capture in self.captures
                                if isinstance(anchor, dict)
                                and capture.get("id") == anchor.get("capture_id")
                                and capture.get("path") == anchor.get("path")
                                and capture.get("row_index") == row
                            ),
                            None,
                        )
                        current_received = _finite(
                            self.last_frame.get("received_monotonic")
                        )
                        current_sequence = self.last_frame.get("sequence")
                        identity_verified = bool(
                            isinstance(anchor_capture, dict)
                            and isinstance(anchor_capture.get("capture_instance"), str)
                            and anchor_capture.get("capture_instance")
                            == self.last_frame.get("capture_instance")
                            and type(anchor_capture.get("generation")) is int
                            and anchor_capture.get("generation")
                            == self.last_frame.get("generation")
                            and type(anchor_capture.get("sequence")) is int
                            and type(current_sequence) is int
                            and current_sequence > anchor_capture.get("sequence")
                            and current_received is not None
                            and 0 <= time.monotonic() - current_received <= 1.0
                        )
                        # This comparison answers one narrow question: did the
                        # accepted pulse leave the stopped head at the exact
                        # causal baseline so that a bounded retry is safe?  A
                        # low-texture mechanical endpoint can prove that with
                        # localized no-effect support even when it cannot meet
                        # the stricter distributed-scene gate used to connect
                        # two different panorama views.
                        recovery_anchor_precondition = await asyncio.to_thread(
                            _no_effect_match,
                            baseline["image"],
                            self.last_frame["image"],
                        )
                        recovery_overlap = _finite(
                            recovery_anchor_precondition.get("overlap")
                        )
                        recovery_displacement = _finite(
                            recovery_anchor_precondition.get("displacement")
                        )
                        visual_no_effect = bool(
                            recovery_anchor_precondition.get("verified") is True
                            and recovery_overlap is not None
                            and recovery_overlap >= 0.85
                            and recovery_displacement is not None
                            and recovery_displacement
                            <= RECOVERY_ANCHOR_PRECONDITION_PIXELS
                        )
                        stopped_pose = await self._observational_readback()
                        native_no_effect = (
                            _native_axes_unchanged(
                                previous_pose,
                                stopped_pose,
                                {axis: float(direction)},
                            )
                            if recovery_anchor_precondition.get("verified") is not True
                            else None
                        )
                        recovery_evidence = {
                            "verified": visual_no_effect or native_no_effect is not None,
                            "identity_verified": identity_verified,
                            "overlap": recovery_overlap,
                            "displacement": recovery_displacement,
                            "code": recovery_anchor_precondition.get("code"),
                            "evidence": (
                                "visual_no_effect"
                                if visual_no_effect
                                else "native_pose_unchanged"
                                if native_no_effect is not None
                                else "insufficient"
                            ),
                        }
                        if native_no_effect is not None:
                            recovery_evidence["native_stationary"] = native_no_effect
                        recovery_history = self.checkpoint.setdefault(
                            "seek_recovery_observations", []
                        )
                        if not isinstance(recovery_history, list):
                            raise PanoramaCaptureError(
                                "continuous_resume_unavailable"
                            ) from None
                        recovery_history.append(
                            {
                                "axis": axis,
                                "direction": direction,
                                "row": row,
                                "identity_verified": identity_verified,
                                "visual_verified": recovery_anchor_precondition.get(
                                    "verified"
                                )
                                is True,
                                "visual_code": recovery_anchor_precondition.get("code"),
                                "visual_overlap": recovery_overlap,
                                "visual_displacement": recovery_displacement,
                                "before_native": _finite(previous_pose.get(f"native_{axis}")),
                                "after_native": _finite(stopped_pose.get(f"native_{axis}")),
                                "move_status": str(
                                    stopped_pose.get("move_status") or ""
                                ).upper()
                                or None,
                                "device_error": bool(
                                    str(stopped_pose.get("error") or "").strip()
                                ),
                                "native_no_effect_verified": native_no_effect is not None,
                                "retry_authorized": bool(
                                    identity_verified
                                    and (visual_no_effect or native_no_effect is not None)
                                ),
                            }
                        )
                        del recovery_history[:-12]
                        seek_state["recovery_anchor_precondition"] = recovery_evidence
                        attempts = self.checkpoint.get("diagnostics", {}).get("attempts", [])
                        transition = cursor.get("transition")
                        movement_id = (
                            transition.get("movement_id")
                            if isinstance(transition, dict)
                            else None
                        )
                        # Stop confirmation records its own observation after
                        # the failed movement. Bind recovery to the immutable
                        # command identifier instead of whichever diagnostic
                        # happened to be appended last.
                        failed_attempt = next(
                            (
                                attempt
                                for attempt in reversed(attempts)
                                if isinstance(attempt, dict)
                                and attempt.get("kind") == "movement"
                                and attempt.get("movement_id") == movement_id
                            ),
                            None,
                        )
                        direction_evidence = _axis_direction_evidence(
                            self.captures,
                            axis,
                            direction,
                            recovery_anchor_precondition,
                        )
                        native_direction_evidence = _native_axis_direction_evidence(
                            self.captures,
                            axis,
                            direction,
                            previous_pose,
                            stopped_pose,
                        )
                        accepted_late_endpoint = bool(
                            identity_verified
                            and recovery_overlap is not None
                            and recovery_overlap >= 0.25
                            and recovery_displacement is not None
                            and recovery_displacement >= 2.0
                            and isinstance(failed_attempt, dict)
                            and isinstance(transition, dict)
                            and failed_attempt.get("movement_id")
                            == transition.get("movement_id")
                            and failed_attempt.get("command_outcome") == "accepted"
                            and failed_attempt.get("stop_command_accepted") is True
                            and failed_attempt.get("outcome")
                            in {"motion_not_observed", "stability_timeout"}
                            and (
                                direction_evidence is not None
                                or native_direction_evidence is not None
                            )
                        )
                        if accepted_late_endpoint:
                            recovery_evidence.update(
                                late_endpoint=True,
                                direction=(
                                    direction_evidence
                                    if direction_evidence is not None
                                    else native_direction_evidence
                                ),
                                direction_basis=(
                                    "image_response"
                                    if direction_evidence is not None
                                    else "native_position_response"
                                ),
                                movement_id=transition["movement_id"],
                            )
                            seek_state["recovery_anchor_precondition"] = recovery_evidence
                            pose = await self._observational_readback()
                            result = {
                                "frame": self.last_frame,
                                "pose": pose,
                                "stable": True,
                                "stationary": False,
                                "match": recovery_anchor_precondition,
                                "precondition_match": failed_attempt.get(
                                    "correction_precondition"
                                ),
                                "evidence": {
                                    "stable": True,
                                    "state": "stable",
                                    "code": "stable",
                                    "evidence": "post_attempt_stopped_window",
                                    "has_motion_transition": True,
                                },
                            }
                            transition.update(
                                state="accepted",
                                outcome="late_endpoint_recovered",
                            )
                            cursor["relocalization_failed"] = False
                            self.checkpoint.setdefault(
                                "late_endpoint_recoveries", []
                            ).append(recovery_evidence)
                            self.checkpoint["late_endpoint_recoveries"] = self.checkpoint[
                                "late_endpoint_recoveries"
                            ][-8:]
                            recovered = True
                            await self._persist()
                            break
                        if (
                            not identity_verified
                            or not (visual_no_effect or native_no_effect is not None)
                        ):
                            cursor["relocalization_failed"] = True
                            await self._persist()
                            raise PanoramaCaptureError(
                                "coverage_connection_unverified"
                            ) from None
                        retry_precondition_frame = self.last_frame
                    recovered = True
                    origin_uncertain = True
                    if seek_state is not None:
                        seek_state["origin_uncertain_since_anchor"] = True
                    self.checkpoint.setdefault("seek_recovery_level", {})[axis] = recovery_level + 1
                    previous_duration = duration
                    if error.code == "motion_not_observed":
                        # A causally accepted command whose stopped endpoint is
                        # still the same view is below this camera's effective
                        # motor/transport floor. A shorter retry makes that
                        # deadband worse. Grow one bounded step and let the
                        # ordinary visual and native-motion gates decide
                        # whether it moved or reached a mechanical boundary.
                        duration = min(1.2, max(MINIMUM_SEEK_PULSE_SECONDS, duration * 2))
                        adaptation_reason = "device_no_effect"
                    else:
                        # A stability timeout can indicate an excessive move;
                        # retain the existing conservative subdivision.
                        duration = max(MINIMUM_SEEK_PULSE_SECONDS, duration / 2)
                        adaptation_reason = "stability_timeout"
                    if seek_state is not None:
                        seek_state["duration"] = duration
                        seek_state["duration_adaptation"] = {
                            "reason": adaptation_reason,
                            "from_seconds": previous_duration,
                            "to_seconds": duration,
                            "step": seek_state["steps"],
                        }
                    await self._persist()
            connection = None
            if recovered or cursor is not None:
                connection = await asyncio.to_thread(
                    _match, baseline["image"], result["frame"]["image"]
                )
                precondition = result.get("precondition_match")
                command_transition = result.get("match")
                direct_precondition_chain = bool(
                    retry_precondition_frame is baseline
                    and isinstance(precondition, dict)
                    and precondition.get("verified") is True
                    and precondition.get("overlap", 0) >= 0.85
                    and precondition.get("displacement", math.inf)
                    <= CORRECTION_PRECONDITION_PIXELS
                )
                recovery_precondition_chain = bool(
                    retry_precondition_frame is not baseline
                    and isinstance(recovery_evidence, dict)
                    and recovery_evidence.get("verified") is True
                    and recovery_evidence.get("identity_verified") is True
                    and (
                        (
                            recovery_evidence.get("evidence")
                            in {"native_pose_unchanged", "fresh_seek_native_pose"}
                            and isinstance(
                                recovery_evidence.get("native_stationary"), dict
                            )
                        )
                        or (
                            recovery_evidence.get("overlap", 0) >= 0.85
                            and (
                                recovery_evidence.get("displacement", math.inf)
                                <= RECOVERY_ANCHOR_PRECONDITION_PIXELS
                                or (
                                    recovery_evidence.get("evidence")
                                    == "fresh_seek_verified_anchor"
                                    and recovery_evidence.get(
                                        "displacement", math.inf
                                    )
                                    <= ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS
                                )
                            )
                        )
                    )
                    and isinstance(precondition, dict)
                    and precondition.get("verified") is True
                    and precondition.get("overlap", 0) >= 0.85
                    and precondition.get("displacement", math.inf)
                    <= CORRECTION_PRECONDITION_PIXELS
                )
                chained_connection = bool(
                    (direct_precondition_chain or recovery_precondition_chain)
                    and isinstance(command_transition, dict)
                    and command_transition.get("verified") is True
                    and command_transition.get("overlap", 0) >= 0.25
                )
                command_only_connection = bool(
                    chained_connection
                    and command_transition.get("support_scope")
                    == "sparse_distributed_command_transition"
                )
                if (
                    (
                        connection.get("verified") is not True
                        or connection.get("overlap", 0) < 0.25
                    )
                    and chained_connection
                    and not command_only_connection
                ):
                    connection = {
                        **command_transition,
                        "connection_method": (
                            "native_pose_recovery_chain"
                            if recovery_precondition_chain
                            and recovery_evidence.get("evidence")
                            in {"native_pose_unchanged", "fresh_seek_native_pose"}
                            else "fresh_seek_precondition_chain"
                            if recovery_precondition_chain
                            and recovery_evidence.get("evidence")
                            in {
                                "fresh_seek_visual_anchor",
                                "fresh_seek_verified_anchor",
                            }
                            else "verified_recovery_chain"
                            if recovery_precondition_chain
                            else "verified_precondition_chain"
                        ),
                        "anchor_precondition": {
                            key: (
                                recovery_evidence
                                if recovery_precondition_chain
                                else precondition
                            ).get(key)
                            for key in (
                                "support_scope",
                                "inliers",
                                "overlap",
                                "displacement",
                                "shift_x",
                                "shift_y",
                                "evidence",
                                "native_stationary",
                            )
                            if key
                            in (
                                recovery_evidence
                                if recovery_precondition_chain
                                else precondition
                            )
                        },
                    }
                    if recovery_precondition_chain:
                        connection["dispatch_precondition"] = {
                            key: precondition.get(key)
                            for key in (
                                "support_scope",
                                "inliers",
                                "overlap",
                                "displacement",
                                "shift_x",
                                "shift_y",
                            )
                            if key in precondition
                        }
                if (
                    not (result.get("stable") or result.get("stationary"))
                    or not connection.get("verified")
                    or connection.get("overlap", 0) < 0.25
                ):
                    subdivision = (
                        await self._subdivide_failed_connection(
                            cursor,
                            seek_state,
                            axis=axis,
                            direction=direction,
                            row=row,
                            failed_duration=duration,
                            failed_result=result,
                            failed_connection=connection,
                            step_limit=MAX_CONTINUOUS_STEPS,
                            error_code="coverage_connection_unverified",
                            # A causal command transition proves how to undo
                            # this one movement, but not a new navigation node.
                            # Return to the last distributed anchor and let the
                            # ordinary recovery choose another pending region.
                            return_only=command_only_connection,
                        )
                        if (
                            cursor is not None
                            and seek_state is not None
                            and not origin_uncertain
                        )
                        else None
                    )
                    if subdivision is None:
                        raise PanoramaCaptureError("coverage_connection_unverified")
                    result, connection, duration = subdivision
                    recovered = True
                if not result.get("stationary"):
                    result["match"] = connection
            command_matching = result["match"]
            matching = result["match"]
            native_axis = f"native_{axis}"
            old_native, new_native = (
                _finite(previous_pose.get(native_axis)),
                _finite(result["pose"].get(native_axis)),
            )
            native_static = (
                old_native is None or new_native is None or abs(new_native - old_native) <= 2
            )
            previous_pose = result["pose"]
            if result["stationary"]:
                # No movement belongs to the command's fresh baseline. The
                # persisted anchor separately permits the same small residual
                # already accepted when returning to that anchor.
                native_stationary = isinstance(result.get("native_stationary"), dict)
                command_no_effect = (
                    command_matching.get("verified")
                    and (
                        command_matching.get("displacement", math.inf) <= 0.5
                        or (
                            native_stationary
                            and command_matching.get("displacement", math.inf)
                            <= NATIVE_CORROBORATED_NO_EFFECT_PIXELS
                        )
                    )
                )
                if (
                    not command_no_effect
                    or (connection is not None and (
                        connection.get("displacement", math.inf) > 15
                        or connection.get("overlap", 0) < 0.85
                    ))
                ):
                    raise PanoramaCaptureError("coverage_connection_unverified")
                stationary_count = stationary_count + 1 if native_static else 0
                if seek_state is not None:
                    seek_state["stationary_count"] = stationary_count
                stationary_precondition_frame = result["frame"]
                if progress_seen and stationary_count >= 2:
                    key = f"{axis}_{'min' if direction < 0 else 'max'}"
                    self.boundaries[key] = {
                        "confirmed": True,
                        "method": "repeated_no_progress_after_motion",
                        "native_position": new_native,
                    }
                    if seek_state is not None:
                        seek_state.update(outcome="limit", duration=duration)
                        cursor["bands"][str(row)].setdefault("boundary_evidence", {})[
                            str(direction)
                        ] = {**self.boundaries[key], "capture_id": cursor["anchor"]["capture_id"]}
                    await self._persist()
                    return "limit", duration
                if stationary_count >= 2:
                    if not origin_excursion:
                        # Starting exactly at a boundary is not evidence of its
                        # location. One bounded excursion establishes progress
                        # before returning to test that same endpoint again.
                        origin_excursion = True
                        away = await pulse(
                            -direction,
                            min(duration, 0.3),
                            limit_probe=True,
                            expected_frame=result["frame"],
                        )
                        probe_confirmed = (
                            away["stable"]
                            and away["match"].get("verified")
                            and away["match"]["displacement"] > 2
                        )
                        if probe_confirmed:
                            stationary_count = 0
                            if seek_state is not None:
                                seek_state["stationary_count"] = 0
                            completed_intent = None
                            if cursor is not None:
                                try:
                                    completed_intent = _pending_limit_probe_intent(cursor)
                                except ValueError:
                                    raise PanoramaCaptureError(
                                        "continuous_resume_unavailable"
                                    ) from None
                            if store:
                                await self._accept(away, row=row, role="limit_probe")
                                stationary_precondition_frame = None
                            else:
                                stationary_precondition_frame = away["frame"]
                            previous_pose = away["pose"]
                        if cursor is not None:
                            await self._finish_limit_probe_intent(
                                cursor, completed_intent if probe_confirmed else None
                            )
                        if probe_confirmed:
                            continue
                    return "unknown", duration
                continue
            stationary_count = 0
            stationary_precondition_frame = None
            if not result["stable"] or not matching.get("verified"):
                raise PanoramaCaptureError("coverage_connection_unverified")
            progress_seen = progress_seen or matching["displacement"] >= 2
            extent = (
                ANALYSIS_WIDTH
                if axis == "pan"
                else _gray(result["frame"]["image"]).shape[0]
            )
            shift = abs(matching["shift_x" if axis == "pan" else "shift_y"])
            next_duration, adaptation_reason = _next_continuous_seek_duration(
                duration,
                image_extent=extent,
                image_shift=shift,
                connection=matching,
            )
            if seek_state is not None:
                seek_state.update(
                    progress=progress_seen,
                    duration=next_duration,
                    last_verified_duration=duration,
                )
                if adaptation_reason is not None:
                    seek_state["duration_adaptation"] = {
                        "reason": adaptation_reason,
                        "from_seconds": duration,
                        "to_seconds": next_duration,
                        "step": seek_state["steps"],
                    }
                else:
                    seek_state.pop("duration_adaptation", None)
            if store:
                await self._accept(result, row=row)
                origin_uncertain = False
                if seek_state is not None:
                    seek_state["origin_uncertain_since_anchor"] = False
            duration = next_duration
            if axis == "pan" and step >= 6 and starting_frame is not None:
                closure = await asyncio.to_thread(
                    _match, starting_frame["image"], result["frame"]["image"]
                )
                if (
                    closure.get("verified")
                    and closure["displacement"] < 15
                    and closure["overlap"] > 0.9
                ):
                    self.boundaries["pan_loop"] = {
                        "confirmed": True,
                        "method": "visual_loop_closure",
                    }
                    if seek_state is not None:
                        seek_state.update(outcome="loop", duration=duration)
                        cursor["bands"][str(row)].setdefault("boundary_evidence", {})["loop"] = {
                            **self.boundaries["pan_loop"],
                            "capture_id": cursor["anchor"]["capture_id"],
                        }
                    await self._persist()
                    return "loop", duration
        return "budget", duration

    def _coverage_progress(self) -> dict:
        if self.region_policy is not None:
            return region_progress(self.checkpoint)
        cursor = self.checkpoint.get("continuous_cursor") or {}
        bands = cursor.get("bands", {})
        if bands:
            return {
                "primary_complete": bands.get("0", {}).get("complete", False),
                "bands_completed": sum(bool(band.get("complete")) for band in bands.values()),
                "current_band": cursor.get("row"),
                "stage": cursor.get("stage"),
                "regions_pending": sum(
                    edge not in {"limit", "loop"}
                    for band in bands.values()
                    for edge in band.get("edges", {}).values()
                )
                + len(cursor.get("pending_branches", [])),
                "continued_after_recovery": bool(cursor.get("continued_after_recovery")),
            }
        plan = self.checkpoint.get("plan", [])
        confirmed = set(self.checkpoint.get("confirmed_indices", []))
        rows = {item["row"] for item in plan}
        complete = {
            row
            for row in rows
            if all(index in confirmed for index, item in enumerate(plan) if item["row"] == row)
        }
        primary = self.checkpoint.get("primary_row")
        return {
            "primary_complete": primary is not None and primary in complete,
            "bands_completed": len(complete),
            "current_band": self.checkpoint.get("active_row"),
        }

    async def _find_reference(self, cursor: dict) -> None:
        await self._emit("finding_reference")
        moves = cursor.setdefault("reference_moves", [])
        for attempt in range(6):
            self._check()
            # Translation at the image centre must accompany the pan. Near a
            # vertical pole, pan mostly rotates the image without useful width.
            moves.append({"axis": "pan", "direction": 1, "duration": 0.3})
            try:
                result = await self._pulse("pan", 1, 0.3)
                if result.get("stationary"):
                    moves.append({"axis": "pan", "direction": -1, "duration": 0.3})
                    result = await self._pulse("pan", -1, 0.3)
                match = result["match"]
                useful = (
                    result.get("stable")
                    and match.get("verified")
                    and math.hypot(match.get("shift_x", 0), match.get("shift_y", 0))
                    >= max(2, match.get("displacement", 0) * 0.3)
                )
                if useful:
                    capture = await self._accept(result, row=0, role="reference_connection")
                    cursor["reference_path"] = capture["path"]
                    return
            except PanoramaCaptureError as error:
                if (
                    error.code not in {"stability_timeout", "motion_not_observed"}
                    or self._stop_failed
                    or self.region_policy is not None
                ):
                    raise
            if self.capabilities.get("axes", {}).get("tilt") is False:
                break
            # Bounded alternating probes work with either mounting orientation.
            move = {
                "axis": "tilt",
                "direction": 1 if attempt % 2 == 0 else -1,
                "duration": min(0.3 * (attempt + 1), 1.2),
            }
            moves.append(move)
            try:
                result = await self._pulse(**move)
                if result.get("stable") and result["match"].get("verified"):
                    await self._accept(result, row=0, role="reference_probe")
            except PanoramaCaptureError as error:
                if (
                    error.code not in {"stability_timeout", "motion_not_observed"}
                    or self._stop_failed
                    or self.region_policy is not None
                ):
                    raise
        raise PanoramaCaptureError("reference_view_unobservable")

    def _reference_capture(self, cursor: dict) -> dict | None:
        return next(
            (
                capture for capture in self.captures
                if capture.get("path") == cursor.get("reference_path")
                and capture.get("row_index") == 0
                and capture.get("quality", {}).get("stable") is True
            ),
            None,
        )

    def _reference_destination(self, cursor: dict) -> dict | None:
        reference = self._reference_capture(cursor)
        destination = cursor.get("reference_destination")
        if (
            reference is None or not isinstance(destination, dict)
            or destination.get("role") != "work"
            or destination.get("kind") not in {"absolute", "preset"}
            or not isinstance(destination.get("binding"), dict)
            or destination.get("capture_id") != reference.get("id")
            or destination.get("path") != reference.get("path")
        ):
            return None
        return destination

    def _return_attempt_seconds(self, cursor: dict | None = None) -> float:
        """Bound a saved-destination recall by the travel already observed."""
        captures = self.captures
        if cursor is not None:
            reference = self._reference_capture(cursor)
            anchor_id = (cursor.get("anchor") or {}).get("capture_id")
            reference_index = next(
                (index for index, item in enumerate(captures) if item is reference), None
            )
            anchor_index = next(
                (index for index, item in enumerate(captures) if item.get("id") == anchor_id),
                None,
            )
            if reference_index is not None and anchor_index is not None:
                start, finish = sorted((reference_index, anchor_index))
                captures = captures[start + 1 : finish + 1]
        travel_seconds = sum(
            duration
            for capture in captures
            if isinstance(capture.get("movement"), dict)
            and (duration := _finite(capture["movement"].get("duration"))) is not None
            and duration > 0
        )
        return min(
            MAX_RETURN_ATTEMPT_SECONDS,
            max(ATTEMPT_SECONDS, travel_seconds + ATTEMPT_SECONDS / 2),
        )

    async def _remember_return_intent(self, intent: dict) -> None:
        self.checkpoint.setdefault("pending_returns", []).append(dict(intent))
        await self._persist()
        self._check()

    def _clear_return_intent(self, role: str) -> None:
        self.checkpoint["pending_returns"] = [
            item for item in self.checkpoint.get("pending_returns", [])
            if not (item.get("kind") == "pending_preset" and item.get("role") == role)
        ]

    async def _save_reference_destination(self, cursor: dict) -> None:
        reference = self._reference_capture(cursor)
        if reference is None or not await self._current_anchor(cursor, row=0):
            raise PanoramaCaptureError("working_reference_changed")
        try:
            destination = await self.camera.save_return(
                role="work", before_create=self._remember_return_intent
            )
        except PanoramaCaptureError as error:
            if error.code not in {"return_unavailable", "return_capacity_unavailable"}:
                raise
            self.issues.append({"code": "working_reference_unavailable", "reason": error.code})
            await self._persist()
            return
        self._clear_return_intent("work")
        # Persist ownership before observing the association: a cancelled or
        # changed view must not leave an unrecorded temporary preset behind.
        self.checkpoint.setdefault("pending_returns", []).append(destination)
        await self._persist()
        self.last_frame = await self._reference_window()
        self.last_pose = await self._observational_readback()
        if not await self._current_anchor(cursor, row=0):
            raise PanoramaCaptureError("working_reference_changed")
        cursor["reference_destination"] = {
            **destination, "capture_id": reference["id"], "path": reference["path"],
        }
        self.checkpoint["pending_returns"] = [
            item for item in self.checkpoint.get("pending_returns", []) if item != destination
        ]
        await self._persist()

    async def _save_original_destination(self) -> bool:
        try:
            destination = await self.camera.save_return(before_create=self._remember_return_intent)
        except PanoramaCaptureError as error:
            if error.code not in {"return_unavailable", "return_capacity_unavailable"}:
                raise
            self.issues.append({"code": "return_reference_unavailable", "reason": error.code})
            await self._persist()
            return False
        self._clear_return_intent("original")
        self.checkpoint.setdefault("pending_returns", []).append(destination)
        await self._persist()
        if (self.checkpoint.get("initial_reference_evidence") or {}).get("status") != "untextured_unverified":
            self.last_frame = await self._reference_window()
            self.last_pose = await self._observational_readback()
            comparison = await asyncio.to_thread(
                _match, self._private_image(self.checkpoint["initial_path"]), self.last_frame["image"]
            )
            if not comparison.get("verified") or comparison.get("displacement", math.inf) > 3:
                raise PanoramaCaptureError("original_reference_changed")
        self.saved_return = destination
        self.checkpoint["pending_returns"] = [
            item for item in self.checkpoint.get("pending_returns", []) if item != destination
        ]
        await self._persist()
        return True

    def _band_origin_capture(self, cursor: dict, *, row: int) -> dict | None:
        band = cursor.get("bands", {}).get(str(row), {})
        if row == 0:
            return self._reference_capture(cursor)
        origin_identifier = band.get("origin_capture_id")
        origin_path = band.get("start_path")
        return next(
            (
                capture
                for capture in self.captures
                if capture.get("id") == origin_identifier
                and capture.get("path") == origin_path
                and capture.get("row_index") == row
                and capture.get("quality", {}).get("stable") is True
            ),
            None,
        )

    async def _band_transition_capture(
        self, cursor: dict, *, row: int, chain: list[dict]
    ) -> dict:
        """Choose a textured interior node as the next vertical transition.

        A capture that happens to start a scan can be a mechanical endpoint,
        with enough detail for a small pan probe but too little parallax to
        connect another tilt band. The return graph already proves the
        photographed route back from the row endpoint, so select its strongest
        interior node instead of requiring an operator to choose a centre.
        """
        band = cursor["bands"][str(row)]
        known_identifier = band.get("transition_capture_id")
        known_path = band.get("transition_path")
        if isinstance(known_identifier, str) and isinstance(known_path, str):
            known = next(
                (
                    capture
                    for capture in chain
                    if capture.get("id") == known_identifier
                    and capture.get("path") == known_path
                    and capture.get("row_index") == row
                    and capture.get("quality", {}).get("stable") is True
                ),
                None,
            )
            if known is not None:
                return known

        # The current endpoint would make no return movement. Prefer an
        # interior observation, but preserve a valid origin as the fallback
        # for short rows and older checkpoints.
        candidates = chain[1:] if len(chain) > 1 else chain
        scored: list[tuple[tuple[float, int, float], dict, dict]] = []
        for capture in candidates:
            if (
                capture.get("row_index") != row
                or capture.get("quality", {}).get("stable") is not True
            ):
                continue
            support = capture.get("texture_support")
            if not isinstance(support, dict):
                support = await asyncio.to_thread(
                    _texture_support, self._private_image(capture["path"])
                )
                capture["texture_support"] = support
            features = int(support.get("features", 0))
            cells = int(support.get("occupied_cells", 0))
            hull_fraction = _finite(support.get("hull_fraction")) or 0.0
            score = float(features * cells) * hull_fraction
            scored.append(((score, cells, hull_fraction), capture, support))
        if not scored:
            raise PanoramaCaptureError("band_origin_route_unavailable")
        _, selected, support = max(scored, key=lambda item: item[0])
        band.update(
            transition_capture_id=selected["id"],
            transition_path=selected["path"],
            transition_texture_support={
                key: support.get(key)
                for key in ("distributed", "features", "occupied_cells", "hull_fraction")
            },
        )
        return selected

    def _visual_band_return_chain(
        self, cursor: dict, *, row: int, origin: dict
    ) -> list[dict]:
        anchor = cursor.get("anchor") or {}
        by_identifier = {
            capture.get("id"): capture
            for capture in self.captures
            if isinstance(capture, dict) and isinstance(capture.get("id"), str)
        }
        current = by_identifier.get(anchor.get("capture_id"))
        chain: list[dict] = []
        visited: set[str] = set()
        while isinstance(current, dict) and len(chain) < MAX_CAPTURES:
            identifier = current.get("id")
            if (
                not isinstance(identifier, str)
                or identifier in visited
                or current.get("row_index") != row
                or current.get("quality", {}).get("stable") is not True
            ):
                break
            chain.append(current)
            visited.add(identifier)
            if identifier == origin.get("id"):
                return chain
            movement = current.get("movement")
            if (
                not isinstance(movement, dict)
                or movement.get("axis") != "pan"
                or movement.get("direction") not in {-1, 1}
                or _finite(movement.get("duration")) is None
            ):
                break
            current = by_identifier.get(movement.get("anchor_capture_id"))
        raise PanoramaCaptureError("band_origin_route_unavailable")

    async def _prepare_band_origin_return(
        self, cursor: dict, *, row: int, after_direction: int
    ) -> None:
        row_origin = self._band_origin_capture(cursor, row=row)
        if row_origin is None:
            raise PanoramaCaptureError("band_origin_route_unavailable")
        chain = self._visual_band_return_chain(cursor, row=row, origin=row_origin)
        origin = await self._band_transition_capture(cursor, row=row, chain=chain)
        origin_index = next(
            index for index, capture in enumerate(chain) if capture.get("id") == origin["id"]
        )
        chain = chain[: origin_index + 1]
        origin_anchor = {
            "capture_id": origin["id"],
            "path": origin["path"],
            "row": row,
        }
        if await self._current_anchor({"anchor": origin_anchor}, row=row):
            cursor["anchor"] = origin_anchor
            cursor.update(stage="step", direction=after_direction)
            cursor.pop("seek", None)
            await self._persist()
            return
        if (
            row == 0
            and origin.get("id") == row_origin.get("id")
            and self._reference_destination(cursor) is not None
        ):
            cursor["recovery"] = {
                "destination": f"step:0:{cursor['branch']}",
                "state": "planned",
                "row": 0,
                "branch": cursor["branch"],
                "direction": after_direction,
                "action": "step",
            }
            cursor.update(
                stage="return_reference",
                after_reference_stage="step",
                after_reference_direction=after_direction,
            )
            cursor.pop("seek", None)
            cursor.pop("vertical_seek", None)
            await self._persist()
            return
        cursor["visual_band_return"] = {
            "version": VISUAL_BAND_RETURN_VERSION,
            "state": "active",
            "row": row,
            "capture_ids": [capture["id"] for capture in chain],
            "current_index": 0,
            "origin_capture_id": origin["id"],
            "origin_path": origin["path"],
            "after_stage": "step",
            "after_direction": after_direction,
            "after_branch": cursor["branch"],
            "commands": 0,
            "corrections": 0,
        }
        first_movement = chain[0].get("movement", {})
        cursor.update(
            stage="pan",
            row=row,
            direction=-first_movement["direction"],
        )
        cursor.pop("seek", None)
        cursor.pop("vertical_seek", None)
        try:
            _visual_band_return(cursor, captures=self.captures)
        except ValueError:
            cursor.pop("visual_band_return", None)
            raise PanoramaCaptureError("band_origin_route_unavailable") from None
        await self._persist()

    async def _visual_return_matches(
        self, route: dict, frame: dict
    ) -> tuple[int, dict] | None:
        by_identifier = {capture.get("id"): capture for capture in self.captures}
        start = route["current_index"] + 1
        stop = min(len(route["capture_ids"]), start + 8)
        matches: list[tuple[int, dict]] = []
        for index in range(start, stop):
            capture = by_identifier.get(route["capture_ids"][index])
            if not isinstance(capture, dict):
                continue
            matching = await asyncio.to_thread(
                _anchor_match,
                self._private_image(capture["path"]),
                frame["image"],
            )
            if (
                matching.get("verified") is True
                and matching.get("overlap", 0) >= 0.85
                and matching.get("displacement", math.inf)
                <= ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS
                and matching.get("inliers", 0) >= DISTRIBUTED_CONNECTION_MINIMUM_INLIERS
            ):
                matches.append((index, matching))
        return max(matches, key=lambda item: item[0]) if matches else None

    async def _advance_visual_band_return(
        self,
        cursor: dict,
        route: dict,
        matched: tuple[int, dict],
        frame: dict,
        *,
        outcome: str,
    ) -> bool:
        index, matching = matched
        capture_identifier = route["capture_ids"][index]
        capture = next(item for item in self.captures if item.get("id") == capture_identifier)
        route.update(current_index=index, corrections=0, state="active")
        route.pop("correction", None)
        cursor["relocalization_failed"] = False
        cursor["anchor"] = {
            "capture_id": capture["id"],
            "path": capture["path"],
            "row": route["row"],
        }
        cursor["anchor_observation"] = {
            "capture_id": capture["id"],
            "path": capture["path"],
            "row": route["row"],
            "capture_instance": frame.get("capture_instance"),
            "generation": frame.get("generation"),
            "sequence": frame.get("sequence"),
            "support_scope": matching.get("support_scope"),
            "inliers": matching.get("inliers"),
            "overlap": matching.get("overlap"),
            "displacement": matching.get("displacement"),
            "shift_x": matching.get("shift_x"),
            "shift_y": matching.get("shift_y"),
        }
        transition = cursor.get("transition")
        if isinstance(transition, dict):
            transition.update(state="confirmed", outcome=outcome)
        cursor.pop("seek", None)
        if index != len(route["capture_ids"]) - 1:
            await self._persist()
            return False
        history = self.checkpoint.setdefault("visual_band_returns", [])
        if not isinstance(history, list):
            raise PanoramaCaptureError("continuous_resume_unavailable")
        history.append(
            {
                "row": route["row"],
                "origin_capture_id": route["origin_capture_id"],
                "commands": route["commands"],
                "method": "verified_visual_capture_graph",
            }
        )
        del history[:-MAX_ROWS]
        cursor.update(
            stage=route["after_stage"],
            direction=route["after_direction"],
            branch=route["after_branch"],
            relocalization_failed=False,
        )
        cursor.pop("visual_band_return", None)
        await self._persist()
        return True

    async def _correct_visual_band_return(
        self,
        cursor: dict,
        route: dict,
        before: dict,
        result: dict,
        *,
        direction: int,
        duration: float,
    ) -> tuple[tuple[int, dict], dict] | None:
        """Approach one visible route node using measured, stopped pan feedback."""
        candidates = []
        by_identifier = {capture.get("id"): capture for capture in self.captures}
        start = route["current_index"] + 1
        for index in range(start, min(len(route["capture_ids"]), start + 8)):
            self._check()
            capture = by_identifier[route["capture_ids"][index]]
            reference = self._private_image(capture["path"])
            initial = await asyncio.to_thread(_match, reference, before["image"])
            observed = await asyncio.to_thread(_match, reference, result["frame"]["image"])
            plan = _visual_band_return_correction_plan(
                initial, observed, direction=direction, duration=duration
            )
            if plan is not None:
                candidates.append((plan["remaining_pixels"], index, reference, observed, plan))
        if not candidates:
            return None
        _, index, reference, observed, plan = min(candidates, key=lambda item: item[0])
        while (
            route["commands"] < MAX_VISUAL_BAND_RETURN_COMMANDS
            and route["corrections"] < MAX_VISUAL_BAND_RETURN_CORRECTIONS
        ):
            self._check()
            if self.physical_state != "stopped" or result.get("stable") is not True:
                return None
            # Reserve the command before dispatch. An interrupted correction is
            # not resumable from the old photographed anchor: its live baseline
            # is between nodes. Keep that distinction durable, including when
            # persistence or the command itself raises.
            route.update(
                state="correcting",
                commands=route["commands"] + 1,
                corrections=route["corrections"] + 1,
            )
            route["correction"] = {
                "target_capture_id": route["capture_ids"][index],
                "state": "pending", **plan,
            }
            cursor["relocalization_failed"] = True
            cursor["direction"] = plan["direction"]
            cursor["seek"].update(
                direction=plan["direction"], duration=plan["duration"], steps=route["commands"]
            )
            diagnostic = _AttemptDiagnostic("visual_band_return_correction")
            diagnostic.value.update(route["correction"], command_number=route["commands"])
            await self._persist()
            previous_frame = result["frame"]
            try:
                result = await self._pulse(
                    "pan", plan["direction"], plan["duration"], expected_frame=previous_frame
                )
            except PanoramaCaptureError as error:
                route["correction"].update(state="failed", reason=error.code)
                diagnostic.value["outcome"] = error.code
                self._diagnostic(diagnostic)
                await self._persist()
                raise
            diagnostic.value["movement_id"] = cursor.get("transition", {}).get("movement_id")
            if self.physical_state != "stopped" or result.get("stable") is not True:
                diagnostic.value["outcome"] = "visual_return_correction_unconfirmed"
                self._diagnostic(diagnostic)
                return None
            matched = await self._visual_return_matches(route, result["frame"])
            route["correction"]["state"] = "observed"
            if matched is not None:
                diagnostic.value["outcome"] = "visual_graph_node_verified"
                self._diagnostic(diagnostic)
                return matched, result["frame"]
            following = await asyncio.to_thread(_match, reference, result["frame"]["image"])
            next_plan = _visual_band_return_correction_plan(
                observed, following, direction=plan["direction"], duration=plan["duration"]
            )
            diagnostic.value.update(
                outcome=(
                    "visual_return_correction_observed"
                    if next_plan else "visual_return_correction_rejected"
                ),
                after_pixels=following.get("displacement"),
            )
            self._diagnostic(diagnostic)
            await self._persist()
            if next_plan is None:
                return None
            observed, plan = following, next_plan
        return None

    async def _return_to_band_origin(self, cursor: dict) -> None:
        try:
            route = _visual_band_return(cursor, captures=self.captures)
        except ValueError:
            raise PanoramaCaptureError("continuous_resume_unavailable") from None
        if route is None:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        if route["state"] == "correcting":
            # Never replay a correction from a previous invocation. Its budget
            # and uncertain effect remain available for diagnosis.
            raise PanoramaCaptureError("band_origin_return_unverified")
        await self._emit("relocalizing")
        while True:
            self._check()
            self.last_frame = await self._reference_window(timeout=3.0)
            self.last_pose = await self._observational_readback()
            transition = cursor.get("transition")
            if (
                isinstance(transition, dict)
                and transition.get("state") == "relocalized"
                and transition.get("outcome") == "stopped_graph_relocalized"
            ):
                matched = await self._visual_return_matches(route, self.last_frame)
                if matched is None:
                    raise PanoramaCaptureError("band_origin_return_unverified")
                if await self._advance_visual_band_return(
                    cursor,
                    route,
                    matched,
                    self.last_frame,
                    outcome="visual_graph_resume_verified",
                ):
                    return
                continue
            if route["commands"] >= MAX_VISUAL_BAND_RETURN_COMMANDS:
                raise PanoramaCaptureError("band_origin_return_unverified")
            current_identifier = route["capture_ids"][route["current_index"]]
            current = next(
                capture for capture in self.captures if capture.get("id") == current_identifier
            )
            movement = current.get("movement") or {}
            duration = _finite(movement.get("duration"))
            if duration is None:
                raise PanoramaCaptureError("band_origin_route_unavailable")
            direction = -movement["direction"]
            combined_short_steps = False
            if not route["corrections"] and duration < MINIMUM_VISUAL_BAND_RETURN_PULSE_SECONDS:
                # Some heads do not reliably react to the shortest pulses in
                # their own outbound path. Combine only adjacent, same-axis
                # reverse edges: the photographed route remains the arrival
                # proof, while the combined duration merely gets the head out
                # of the controller's deadband. A later frame must still
                # match a node ahead of the current one before we advance.
                by_identifier = {capture.get("id"): capture for capture in self.captures}
                index = route["current_index"]
                while duration < MINIMUM_VISUAL_BAND_RETURN_PULSE_SECONDS:
                    next_index = index + 1
                    if next_index >= len(route["capture_ids"]):
                        break
                    next_capture = by_identifier.get(route["capture_ids"][next_index])
                    next_movement = (
                        next_capture.get("movement") if isinstance(next_capture, dict) else None
                    )
                    next_duration = (
                        _finite(next_movement.get("duration"))
                        if isinstance(next_movement, dict)
                        else None
                    )
                    if (
                        not isinstance(next_movement, dict)
                        or next_movement.get("axis") != "pan"
                        or next_movement.get("direction") != movement["direction"]
                        or next_duration is None
                    ):
                        break
                    duration += next_duration
                    index = next_index
                    combined_short_steps = True
            if route["corrections"]:
                duration = min(1.2, duration * (2 ** route["corrections"]))
            elif combined_short_steps:
                duration = min(1.2, duration)
            route["commands"] += 1
            cursor["direction"] = direction
            cursor["seek"] = {
                "axis": "pan",
                "direction": direction,
                "row": route["row"],
                "origin_path": current["path"],
                "progress": True,
                "steps": route["commands"],
                "stationary_count": 0,
                "origin_excursion": True,
                "duration": duration,
            }
            await self._persist()
            previous_pose = dict(self.last_pose)
            previous_frame = self.last_frame
            try:
                result = await self._pulse(
                    "pan",
                    direction,
                    duration,
                    expected_frame=self.last_frame,
                )
            except PanoramaCaptureError as error:
                if error.code not in {"motion_not_observed", "stability_timeout"}:
                    raise
                await self._confirm_stop()
                if self._stop_failed or self.physical_state != "stopped":
                    raise PanoramaCaptureError("stop_unconfirmed") from None
                matched = await self._visual_return_matches(route, self.last_frame)
                if matched is not None:
                    if await self._advance_visual_band_return(
                        cursor,
                        route,
                        matched,
                        self.last_frame,
                        outcome="visual_graph_late_endpoint_verified",
                    ):
                        return
                    continue
                no_effect = await asyncio.to_thread(
                    _no_effect_match,
                    self._private_image(current["path"]),
                    self.last_frame["image"],
                )
                stopped_pose = await self._observational_readback()
                native_no_effect = (
                    _native_axes_unchanged(previous_pose, stopped_pose, {"pan": direction})
                    if no_effect.get("verified") is not True
                    else None
                )
                causal_attempts = self.checkpoint.get("diagnostics", {}).get("attempts", [])
                transition = cursor.get("transition")
                movement_identifier = (
                    transition.get("movement_id") if isinstance(transition, dict) else None
                )
                causal_attempt = next(
                    (
                        attempt
                        for attempt in reversed(causal_attempts)
                        if isinstance(attempt, dict)
                        and attempt.get("kind") == "movement"
                        and attempt.get("movement_id") == movement_identifier
                    ),
                    None,
                )
                no_effect_verified = bool(
                    isinstance(causal_attempt, dict)
                    and causal_attempt.get("command_outcome") == "accepted"
                    and causal_attempt.get("stop_command_accepted") is True
                    and no_effect.get("overlap", 0) >= 0.85
                    and no_effect.get("displacement", math.inf)
                    <= RECOVERY_ANCHOR_PRECONDITION_PIXELS
                    and (no_effect.get("verified") is True or native_no_effect is not None)
                )
                if (
                    not no_effect_verified
                    or route["corrections"] >= MAX_VISUAL_BAND_RETURN_CORRECTIONS
                ):
                    raise PanoramaCaptureError("band_origin_return_unverified") from None
                route["corrections"] += 1
                if isinstance(transition, dict):
                    transition.update(
                        state="no_effect_verified",
                        outcome="visual_return_baseline_unchanged",
                    )
                cursor.pop("seek", None)
                await self._persist()
                continue
            if not result.get("stable") or result.get("stationary"):
                route["corrections"] += 1
                if route["corrections"] > MAX_VISUAL_BAND_RETURN_CORRECTIONS:
                    raise PanoramaCaptureError("band_origin_return_unverified")
                transition = cursor.get("transition")
                if isinstance(transition, dict):
                    transition.update(
                        state="no_effect_verified",
                        outcome="visual_return_stationary",
                    )
                cursor.pop("seek", None)
                await self._persist()
                continue
            matched = await self._visual_return_matches(route, result["frame"])
            arrival_frame = result["frame"]
            if matched is None:
                corrected = await self._correct_visual_band_return(
                    cursor, route, previous_frame, result, direction=direction, duration=duration
                )
                if corrected is not None:
                    matched, arrival_frame = corrected
            if matched is None:
                cursor["relocalization_failed"] = True
                diagnostic = _AttemptDiagnostic("visual_band_return")
                diagnostic.value.update(
                    outcome="band_origin_return_unverified",
                    anchor_capture_id=current_identifier,
                    command_number=route["commands"],
                )
                try:
                    await self._preserve_motion_pair(previous_frame, self.last_frame, diagnostic)
                except OSError:
                    diagnostic.value["rejected_pair_unavailable"] = True
                self._diagnostic(diagnostic)
                await self._persist()
                raise PanoramaCaptureError("band_origin_return_unverified")
            if await self._advance_visual_band_return(
                cursor,
                route,
                matched,
                arrival_frame,
                outcome="visual_graph_node_verified",
            ):
                return

    async def _next_band(self, cursor: dict) -> bool:
        direction, row = cursor["branch"], cursor["row"]
        try:
            connection_recovery = _connection_recovery(
                cursor, captures=self.captures, capabilities=self.capabilities
            )
            pending_limit_probe = _pending_limit_probe_intent(cursor)
        except ValueError:
            raise PanoramaCaptureError("continuous_resume_unavailable") from None
        if connection_recovery is not None and connection_recovery["state"] != "complete":
            raise PanoramaCaptureError("continuous_resume_unavailable")
        if pending_limit_probe is not None:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        terminal_key = f"tilt_{'max' if direction > 0 else 'min'}"
        if self.boundaries.get(terminal_key, {}).get("confirmed"):
            return False
        state = cursor.get("vertical_seek")
        if state is None:
            self._anchor_image(cursor, row=row)
            state = {
                "row": row,
                "branch": direction,
                "anchor": dict(cursor["anchor"]),
                "steps": 0,
                "progress": False,
                "stationary": 0,
                "duration": cursor.get("vertical_duration", 0.3),
                "excursion": False,
            }
            cursor["vertical_seek"] = state
            await self._persist()
        if state["row"] != row or state["branch"] != direction:
            raise PanoramaCaptureError("relocalization_required")
        subdivided_steps = state.get("subdivided_steps", [])
        if (
            not isinstance(subdivided_steps, list)
            or any(
                type(subdivided_step) is not int
                or not 1 <= subdivided_step <= state.get("steps", -1)
                for subdivided_step in subdivided_steps
            )
            or len(set(subdivided_steps)) != len(subdivided_steps)
        ):
            raise PanoramaCaptureError("continuous_resume_unavailable")
        anchor = self._anchor_image({"anchor": state["anchor"]}, row=row)
        if not await self._current_anchor(cursor, row=cursor["anchor"]["row"]):
            raise PanoramaCaptureError("vertical_connection_unverified")

        async def start_band() -> bool:
            new_row = row + direction
            horizontal_duration = _finite(
                state.get("recommended_horizontal_duration")
            ) or cursor.get("horizontal_duration", 0.3)
            cursor.update(
                row=new_row,
                vertical_duration=state["duration"],
                horizontal_duration=horizontal_duration,
                stage="pan",
            )
            cursor["bands"][str(new_row)] = {
                "complete": False,
                "edges": {},
                "origin": "center",
                "start_path": cursor["anchor"]["path"],
                "origin_capture_id": cursor["anchor"]["capture_id"],
                "parent_origin_capture_id": state["anchor"]["capture_id"],
                "spacing": state.get("band_spacing_evidence"),
            }
            cursor.pop("seek", None)
            cursor.pop("vertical_seek", None)
            await self._persist()
            return True

        if state["progress"]:
            comparison = await asyncio.to_thread(_match, anchor, self.last_frame["image"])
            if (
                comparison.get("verified") is True
                and comparison.get("overlap", 1) <= GRID_TARGET_OVERLAP
            ) or (
                (_finite(state.get("vertical_displacement_pixels")) or 0.0)
                >= _gray(self.last_frame["image"]).shape[0] * (1.0 - GRID_TARGET_OVERLAP)
            ):
                return await start_band()
        while state["steps"] < 8:
            self._check()
            state["steps"] += 1
            await self._persist()
            previous = self._anchor_image(cursor, row=cursor["anchor"]["row"])
            previous_anchor = dict(cursor["anchor"])
            previous_capture = next(
                (
                    capture
                    for capture in self.captures
                    if capture.get("id") == previous_anchor.get("capture_id")
                    and capture.get("path") == previous_anchor.get("path")
                    and capture.get("row_index") == previous_anchor.get("row")
                ),
                None,
            )
            command_precondition_frame = self.last_frame
            anchor_identity_verified = bool(
                isinstance(previous_capture, dict)
                and previous_capture.get("capture_instance")
                == command_precondition_frame.get("capture_instance")
                and previous_capture.get("generation")
                == command_precondition_frame.get("generation")
                and type(previous_capture.get("sequence")) is int
                and type(command_precondition_frame.get("sequence")) is int
                and command_precondition_frame["sequence"]
                >= previous_capture["sequence"]
            )
            anchor_precondition = (
                await asyncio.to_thread(
                    _no_effect_match,
                    previous,
                    command_precondition_frame["image"],
                )
                if anchor_identity_verified
                else {"verified": False, "code": "capture_identity_unverified"}
            )
            anchor_overlap = _finite(anchor_precondition.get("overlap"))
            anchor_displacement = _finite(anchor_precondition.get("displacement"))
            while True:
                try:
                    result = await self._pulse(
                        "tilt",
                        direction,
                        state["duration"],
                        **(
                            {"expected_frame": command_precondition_frame}
                            if anchor_identity_verified
                            else {}
                        ),
                    )
                    transition = cursor.get("transition")
                    if isinstance(transition, dict) and transition.get("state") == "pending":
                        transition.update(state="accepted", outcome="observed_stopped")
                        await self._persist()
                    break
                except PanoramaCaptureError as error:
                    if error.code != "movement_unconfirmed":
                        raise
                    recovery_level = self.checkpoint.get("seek_recovery_level", {}).get(
                        "tilt", 0
                    )
                    command_failures = (
                        int(state.get("command_failures", 0))
                        if type(state.get("command_failures", 0)) is int
                        else MAX_UNCONFIRMED_RETRIES_PER_SEEK
                    )
                    if (
                        self._stop_failed
                        or recovery_level >= 2
                        or command_failures >= MAX_UNCONFIRMED_RETRIES_PER_SEEK
                        or state["duration"] <= MINIMUM_SEEK_PULSE_SECONDS
                        or state["steps"] >= 8
                    ):
                        raise
                    try:
                        pending_intent = _pending_seek_intent(cursor)
                    except ValueError:
                        raise PanoramaCaptureError(
                            "continuous_resume_unavailable"
                        ) from None
                    if pending_intent is None:
                        raise PanoramaCaptureError(
                            "continuous_resume_unavailable"
                        ) from None
                    await self._confirm_stop()
                    if self._stop_failed or self.physical_state != "stopped":
                        raise PanoramaCaptureError("stop_unconfirmed") from None
                    if not await self._pending_baseline_unchanged(pending_intent):
                        cursor["transition"].update(
                            state="effect_or_state_ambiguous",
                            outcome="recovery_required",
                        )
                        cursor["relocalization_failed"] = True
                        await self._persist()
                        raise PanoramaCaptureError(
                            "vertical_connection_unverified"
                        ) from None
                    next_duration = max(
                        MINIMUM_SEEK_PULSE_SECONDS,
                        min(
                            state["duration"] / 2,
                            _finite(state.get("last_verified_duration"))
                            or state["duration"] / 2,
                        ),
                    )
                    cursor["transition"].update(
                        state="no_effect_verified",
                        outcome="stopped_baseline_unchanged",
                    )
                    self.checkpoint.setdefault("seek_recovery_level", {})["tilt"] = (
                        recovery_level + 1
                    )
                    state.update(
                        duration=next_duration,
                        command_failures=command_failures + 1,
                        steps=state["steps"] + 1,
                    )
                    await self._persist()
            link = await asyncio.to_thread(_match, previous, result["frame"]["image"])
            command_transition = result.get("match")
            dispatch_precondition = result.get("precondition_match")
            vertical_precondition_chain = bool(
                anchor_identity_verified
                and anchor_precondition.get("verified") is True
                and anchor_overlap is not None
                and anchor_overlap >= 0.85
                and anchor_displacement is not None
                and anchor_displacement
                <= ANCHOR_VALIDATION_MAX_DISPLACEMENT_PIXELS
                and isinstance(dispatch_precondition, dict)
                and dispatch_precondition.get("verified") is True
                and dispatch_precondition.get("overlap", 0) >= 0.85
                and dispatch_precondition.get("displacement", math.inf)
                <= CORRECTION_PRECONDITION_PIXELS
                and isinstance(command_transition, dict)
                and command_transition.get("verified") is True
                and command_transition.get("overlap", 0) >= 0.25
                and command_transition.get("displacement", 0) >= 2.0
            )
            if (
                not result.get("stationary")
                and (
                    link.get("verified") is not True
                    or link.get("overlap", 0) < 0.25
                )
                and vertical_precondition_chain
            ):
                link = {
                    **command_transition,
                    "connection_method": "vertical_precondition_chain",
                    "anchor_precondition": {
                        key: anchor_precondition.get(key)
                        for key in (
                            "support_scope",
                            "inliers",
                            "overlap",
                            "displacement",
                            "shift_x",
                            "shift_y",
                        )
                        if key in anchor_precondition
                    },
                    "dispatch_precondition": {
                        key: dispatch_precondition.get(key)
                        for key in (
                            "support_scope",
                            "inliers",
                            "overlap",
                            "displacement",
                            "shift_x",
                            "shift_y",
                        )
                        if key in dispatch_precondition
                    },
                }
            if not link.get("verified") or link.get("overlap", 0) < 0.25:
                subdivision = await self._subdivide_failed_connection(
                    cursor,
                    state,
                    axis="tilt",
                    direction=direction,
                    row=row,
                    failed_duration=state["duration"],
                    failed_result=result,
                    failed_connection=link,
                    step_limit=8,
                    error_code="vertical_connection_unverified",
                )
                if subdivision is None:
                    raise PanoramaCaptureError("vertical_connection_unverified")
                result, link, _ = subdivision
            if result["stationary"]:
                if (
                    not result["match"].get("verified")
                    or result["match"].get("displacement", math.inf) > 0.5
                    or link.get("displacement", math.inf) > 15
                    or link.get("overlap", 0) < 0.85
                ):
                    raise PanoramaCaptureError("vertical_connection_unverified")
                state["stationary"] += 1
                if state["stationary"] < 2:
                    continue
                if state["progress"] or row * direction > 0:
                    self.boundaries[terminal_key] = {
                        "confirmed": True,
                        "method": "repeated_no_progress_after_motion",
                    }
                    comparison = await asyncio.to_thread(_match, anchor, result["frame"]["image"])
                    if (
                        state["progress"]
                        and comparison.get("verified")
                        and comparison.get("displacement", 0) > 3
                        and cursor["anchor"]["row"] == row + direction
                    ):
                        return await start_band()
                    cursor.pop("vertical_seek", None)
                    await self._persist()
                    return False
                if state["excursion"] or state["steps"] >= 8:
                    raise PanoramaCaptureError("tilt_terminal_limit_unconfirmed")
                probe_duration = min(state["duration"], 0.3)
                state.update(excursion=True, steps=state["steps"] + 1)
                cursor["limit_probe_intent"] = {
                    "type": "limit_probe",
                    "state": "pending",
                    "stage": cursor.get("stage", "step"),
                    "axis": "tilt",
                    "direction": -direction,
                    "row": row,
                    "step": state["steps"],
                    "duration": probe_duration,
                    "anchor": dict(cursor["anchor"]),
                }
                try:
                    _pending_limit_probe_intent(cursor)
                except ValueError:
                    raise PanoramaCaptureError("continuous_resume_unavailable") from None
                await self._persist()
                away = await self._pulse("tilt", -direction, probe_duration)
                if not away.get("stable") or not away["match"].get("verified"):
                    await self._finish_limit_probe_intent(cursor)
                    raise PanoramaCaptureError("tilt_terminal_limit_unconfirmed")
                state["stationary"] = 0
                try:
                    completed_intent = _pending_limit_probe_intent(cursor)
                except ValueError:
                    raise PanoramaCaptureError("continuous_resume_unavailable") from None
                await self._accept(away, row=row, role="limit_probe")
                await self._finish_limit_probe_intent(cursor, completed_intent)
                continue
            if not result.get("stable") or not result["match"].get("verified"):
                raise PanoramaCaptureError("vertical_connection_unverified")
            result["match"] = link
            state.update(
                progress=True,
                stationary=0,
                last_verified_duration=state["duration"],
            )
            comparison = await asyncio.to_thread(_match, anchor, result["frame"]["image"])
            try:
                spacing_reached, spacing_evidence = _vertical_band_spacing(
                    state,
                    connection=link,
                    origin_connection=comparison,
                    image_height=_gray(result["frame"]["image"]).shape[0],
                )
            except ValueError:
                raise PanoramaCaptureError("vertical_connection_unverified") from None
            state["vertical_displacement_pixels"] = spacing_evidence[
                "vertical_displacement_pixels"
            ]
            state["band_spacing_evidence"] = spacing_evidence
            horizontal_duration, horizontal_reason = _next_continuous_seek_duration(
                cursor.get("horizontal_duration", 0.3),
                image_extent=_gray(result["frame"]["image"]).shape[1],
                image_shift=0.0,
                connection=link,
            )
            state["recommended_horizontal_duration"] = horizontal_duration
            if horizontal_reason is not None:
                state["horizontal_duration_adaptation"] = {
                    "reason": horizontal_reason,
                    "from_seconds": cursor.get("horizontal_duration", 0.3),
                    "to_seconds": horizontal_duration,
                    "vertical_step": state["steps"],
                }
            await self._accept(result, row=row + direction, role="row_connection")
            # A verified direct overlap is the strongest spacing evidence.  If
            # the origin itself lacks texture, accumulate the adjacent verified
            # vertical shifts instead of mistaking a failed direct match for
            # sufficient travel.
            if spacing_reached:
                return await start_band()
            shift = abs(link.get("shift_y", 0))
            if shift > 2:
                state["duration"] = min(
                    state["duration"] * 2,
                    float(
                        np.clip(
                            state["duration"]
                            * _gray(result["frame"]["image"]).shape[0]
                            * 0.4
                            / shift,
                            MINIMUM_SEEK_PULSE_SECONDS,
                            1.2,
                        )
                    ),
                )
        raise PanoramaCaptureError("vertical_connection_unverified")

    async def _recover(
        self, cursor: dict, error: PanoramaCaptureError | None, *, axis: str
    ) -> bool:
        if error is not None and error.code not in {
            "motion_not_observed",
            "stability_timeout",
            "coverage_connection_unverified",
            "vertical_connection_unverified",
            "tilt_terminal_limit_unconfirmed",
            "pan_row_limit_unconfirmed",
            "relocalization_required",
        }:
            raise error
        if self.physical_state == "ownership_lost":
            raise PanoramaCaptureError("control_lost")
        if self._stop_failed or self._persistence_failed:
            raise PanoramaCaptureError("stop_unconfirmed")
        try:
            connection_recovery = _connection_recovery(
                cursor, captures=self.captures, capabilities=self.capabilities
            )
        except ValueError:
            raise PanoramaCaptureError("continuous_resume_unavailable") from None
        archived_connection_failure = False
        if connection_recovery is not None and connection_recovery["state"] == "failed":
            archived_connection_failure = True
            history = cursor.setdefault("connection_recovery_history", [])
            if not isinstance(history, list):
                raise PanoramaCaptureError("continuous_resume_unavailable")
            history.append(dict(connection_recovery))
            del history[:-16]
            cursor.pop("connection_recovery", None)
        elif connection_recovery is not None and connection_recovery["state"] != "complete":
            raise PanoramaCaptureError("continuous_resume_unavailable")
        self._check()
        row, direction, branch = cursor["row"], cursor["direction"], cursor["branch"]
        finished = cursor["finished_branches"]
        if error is not None:
            if axis == "pan":
                cursor["bands"][str(row)]["edges"][str(direction)] = "unconfirmed"
                self.issues.append(
                    {
                        "code": "horizontal_coverage_partial",
                        "row": row,
                        "direction": direction,
                    }
                )
            else:
                cursor.setdefault("pending_branches", []).append(branch)
                self.issues.append({"code": error.code, "branch": branch})
        if axis == "tilt" and branch not in finished:
            finished.append(branch)
        opposite_edge_missing = bool(
            axis == "pan"
            and str(-direction) not in cursor["bands"][str(row)]["edges"]
        )
        primary_retry_direction = next(
            (
                side
                for side in (-1, 1)
                if axis == "pan"
                and row == 0
                and cursor["bands"]["0"]["edges"].get(str(side)) == "unconfirmed"
                and cursor["recovery_attempts"].get(f"pan:0:{side}", 0) < 1
                and (
                    error is None
                    or (
                        side == direction
                        and not archived_connection_failure
                        and error.code
                        in {
                            "motion_not_observed",
                            "stability_timeout",
                            "coverage_connection_unverified",
                            "pan_row_limit_unconfirmed",
                        }
                    )
                )
            ),
            None,
        )
        await self._persist()
        await self._confirm_stop()
        self._check()
        if self._stop_failed or self.physical_state != "stopped":
            raise PanoramaCaptureError("stop_unconfirmed")
        primary_retry_local = False
        if primary_retry_direction is not None and error is not None:
            try:
                primary_retry_local = await self._current_anchor(cursor, row=row)
            except PanoramaCaptureError as failure:
                if failure.code != "relocalization_required":
                    raise
            if not primary_retry_local:
                primary_retry_direction = None
        if (
            axis == "pan"
            and row == 0
            and not opposite_edge_missing
            and primary_retry_direction is None
        ):
            cursor["horizontal_coverage_blocked"] = True
            cursor["direction"] = direction
            await self._persist()
            return False
        if (
            self.capabilities.get("axes", {}).get("tilt") is False
            and primary_retry_direction is None
            and not (
                axis == "pan"
                and row == 0
                and str(-direction) not in cursor["bands"]["0"]["edges"]
            )
        ):
            return False
        action, destination_row, next_direction = "step", 0, direction
        local = False
        if primary_retry_direction is not None:
            action, next_direction = "pan", primary_retry_direction
            local = primary_retry_local
        elif axis == "pan" and row == 0 and opposite_edge_missing:
            action, next_direction = "pan", -direction
            try:
                local = await self._current_anchor(cursor, row=row)
            except PanoramaCaptureError as failure:
                if failure.code != "relocalization_required":
                    raise
                local = False
        elif axis == "pan":
            try:
                local = await self._current_anchor(cursor, row=row)
            except PanoramaCaptureError as failure:
                if failure.code != "relocalization_required":
                    raise
                local = False
            if local:
                destination_row = row
                if opposite_edge_missing:
                    action, next_direction = "pan", -direction
            elif row != 0 and branch not in finished:
                finished.append(branch)
                cursor.setdefault("pending_branches", []).append(branch)
        local_resume = axis == "pan" and local
        if not local_resume and self._reference_destination(cursor) is None:
            reference = self._reference_capture(cursor)
            at_reference = reference is not None and await self._current_anchor(
                {"anchor": {"capture_id": reference["id"], "path": reference["path"], "row": 0}},
                row=0,
            )
            if not at_reference:
                self.issues.append({"code": "working_reference_unavailable"})
                return False
        if action == "step" and not local_resume:
            branches = [side for side in (-1, 1) if side not in finished]
            if not branches:
                return False
            branch = branches[0]
        destination = f"{action}:{destination_row}:{next_direction if action == 'pan' else branch}"
        attempts = cursor["recovery_attempts"]
        # ponytail: one recovery per destination; arbitrary inter-band routing
        # needs a demonstrated use case and a verified visual connection chain.
        if attempts.get(destination, 0) >= 1:
            return False
        attempts[destination] = attempts.get(destination, 0) + 1
        cursor["recovery"] = {
            "destination": destination,
            "state": "confirmed" if local_resume else "planned",
            "row": destination_row,
            "branch": branch,
            "direction": next_direction,
            "action": action,
        }
        cursor["branch"] = branch
        if local_resume:
            cursor["direction"] = next_direction
            cursor.pop("after_reference_stage", None)
            cursor.pop("after_reference_direction", None)
        else:
            cursor.update(
                after_reference_stage=action,
                after_reference_direction=next_direction,
            )
        cursor.pop("seek", None)
        cursor.pop("vertical_seek", None)
        cursor["stage"] = action if local_resume else "return_reference"
        await self._persist()
        return True

    async def _return_to_reference_band(self, cursor: dict) -> None:
        try:
            await self._reach_reference_band(cursor)
        except PanoramaCaptureError:
            cursor["relocalization_failed"] = True
            raise

    async def _reach_reference_band(self, cursor: dict) -> None:
        reference = self._reference_capture(cursor)
        if reference is None:
            raise PanoramaCaptureError("relocalization_required")
        destination = self._reference_destination(cursor)
        recovery = cursor["recovery"]
        await self._emit("relocalizing")
        reference_anchor = {
            "capture_id": reference["id"], "path": reference["path"], "row": 0,
        }
        already_there = await self._current_anchor({"anchor": reference_anchor}, row=0)
        if recovery["state"] == "pending" and not already_there:
            # A persisted intention does not prove that its command was sent.
            # Resume must observe the destination instead of replaying it.
            raise PanoramaCaptureError("relocalization_required")
        if recovery["state"] != "confirmed" and not already_there:
            if destination is None:
                raise PanoramaCaptureError("working_reference_unavailable")
            cursor["resume_anchor_verified"] = False
            try:
                await self._move(
                    lambda: self.camera.return_to(destination),
                    allow_stationary=True,
                    target={axis: destination[axis] for axis in ("pan", "tilt")}
                    if destination.get("kind") == "absolute"
                    else None,
                    persist_recovery_return_intent=True,
                    attempt_seconds=self._return_attempt_seconds(cursor),
                )
            except PanoramaCaptureError as error:
                if (
                    error.code not in {"stability_timeout", "motion_not_observed"}
                    or self._stop_failed
                ):
                    raise
                await self._confirm_stop()
                if self.physical_state != "stopped":
                    raise PanoramaCaptureError("stop_unconfirmed") from None
        # The saved return is observed directly. Historical pulse durations from
        # reference discovery are never replayed as positional evidence.
        comparison = await asyncio.to_thread(
            _anchor_match, self._private_image(reference["path"]), self.last_frame["image"]
        )
        if not comparison.get("verified") or comparison.get("overlap", 0) < 0.85:
            raise PanoramaCaptureError("relocalization_required")
        cursor["anchor"] = reference_anchor
        if not await self._current_anchor(cursor, row=0):
            raise PanoramaCaptureError("relocalization_required")
        recovery["state"] = "confirmed"
        cursor["relocalization_failed"] = False
        cursor.update(
            row=0,
            direction=cursor.pop("after_reference_direction"),
            stage=cursor.pop("after_reference_stage"),
        )
        cursor.pop("seek", None)
        cursor.pop("vertical_seek", None)
        if "transition" in cursor:
            cursor["transition"]["state"] = "confirmed"
        await self._persist()

    async def _continuous_scan(self) -> None:
        self._check()
        if not any(
            self.capabilities.get(key)
            for key in ("continuous_supported", "velocity_supported", "relative_supported")
        ):
            raise PanoramaCaptureError("camera_movement_unsupported")
        if self.capabilities.get("axes", {}).get("pan") is False:
            raise PanoramaCaptureError("horizontal_movement_unavailable")
        cursor = self.checkpoint.get("continuous_cursor")
        if cursor is None:
            if self.checkpoint.get("mode") == "continuous" and self.captures:
                raise PanoramaCaptureError("continuous_resume_unavailable")
            cursor = {
                "version": CONTINUOUS_CURSOR_VERSION,
                "stage": "reference",
                "row": 0,
                "direction": -1,
                "branch": -1,
                "bands": {"0": {"complete": False, "edges": {}, "origin": "center"}},
                "finished_branches": [],
                "horizontal_duration": 0.3,
                "recovery_attempts": {},
            }
            self.checkpoint["continuous_cursor"] = cursor
        elif cursor.get("version") != CONTINUOUS_CURSOR_VERSION:
            raise PanoramaCaptureError("continuous_resume_unavailable")
        self.checkpoint["mode"] = "continuous"
        self.coverage["bands"] = cursor["bands"]
        # The cursor is the durable handoff from absolute fallback to continuous
        # acquisition. Persist it before reference discovery can move the camera.
        await self._persist()
        while cursor["stage"] != "done":
            self._check()
            stage = cursor["stage"]
            if stage == "reference":
                await self._find_reference(cursor)
                await self._save_reference_destination(cursor)
                cursor["stage"] = "pan"
                await self._persist()
                continue
            if stage == "return_reference":
                await self._return_to_reference_band(cursor)
                continue
            if stage == "pan":
                if cursor.get("visual_band_return") is not None:
                    try:
                        await self._return_to_band_origin(cursor)
                    except PanoramaCaptureError as error:
                        if error.code not in {
                            "band_origin_route_unavailable",
                            "band_origin_return_unverified",
                        }:
                            raise
                        self.issues.append(
                            {
                                "code": error.code,
                                "row": cursor.get("row"),
                            }
                        )
                        cursor["relocalization_failed"] = True
                        await self._persist()
                        break
                    continue
                row, direction = cursor["row"], cursor["direction"]
                band = cursor["bands"][str(row)]
                try:
                    outcome, duration = await self._seek(
                        "pan",
                        direction,
                        row=row,
                        duration=cursor["horizontal_duration"],
                    )
                    if outcome not in {"limit", "loop"}:
                        raise PanoramaCaptureError("pan_row_limit_unconfirmed")
                except PanoramaCaptureError as error:
                    if await self._recover(cursor, error, axis="pan"):
                        continue
                    break
                cursor["horizontal_duration"] = duration
                previous_edge = band["edges"].get(str(direction))
                band["edges"][str(direction)] = outcome
                if previous_edge == "unconfirmed" and outcome in {"limit", "loop"}:
                    self.issues = [
                        issue
                        for issue in self.issues
                        if not (
                            issue.get("code") == "horizontal_coverage_partial"
                            and issue.get("row") == row
                            and issue.get("direction") == direction
                        )
                    ]
                band["complete"] = outcome == "loop" or all(
                    band["edges"].get(str(side)) == "limit" for side in (-1, 1)
                )
                cursor.pop("seek", None)
                if outcome != "loop" and str(-direction) not in band["edges"]:
                    cursor["direction"] = -direction
                elif row == 0 and not band["complete"]:
                    # A partial reference band cannot authorize vertical
                    # exploration. Keep its unresolved side as the resumable
                    # destination and preserve every accepted photograph.
                    cursor["direction"] = next(
                        side
                        for side in (-1, 1)
                        if band["edges"].get(str(side)) != "limit"
                    )
                    if await self._recover(cursor, None, axis="pan"):
                        await self._persist()
                        continue
                    cursor["horizontal_coverage_blocked"] = True
                    band["end_path"] = cursor.get("anchor", {}).get("path")
                    await self._persist()
                    break
                else:
                    cursor.pop("horizontal_coverage_blocked", None)
                    band["end_path"] = cursor.get("anchor", {}).get("path")
                    fixed_tilt = self.capabilities.get("axes", {}).get("tilt") is False
                    has_band_origin = row == 0 or (
                        isinstance(band.get("origin_capture_id"), str)
                        and isinstance(band.get("start_path"), str)
                    )
                    if fixed_tilt or not has_band_origin:
                        # Version-four checkpoints created before visual band
                        # returns did not bind later rows to an origin capture.
                        # Preserve their resumability without inventing a route;
                        # every newly created band records this binding above.
                        cursor.update(stage="step", direction=-direction)
                    else:
                        try:
                            await self._prepare_band_origin_return(
                                cursor,
                                row=row,
                                after_direction=-direction,
                            )
                        except PanoramaCaptureError as error:
                            if error.code != "band_origin_route_unavailable":
                                raise
                            self.issues.append({"code": error.code, "row": row})
                            cursor["relocalization_failed"] = True
                            await self._persist()
                            break
                await self._persist()
                continue
            if stage != "step":
                raise PanoramaCaptureError("continuous_resume_unavailable")
            fixed_tilt = self.capabilities.get("axes", {}).get("tilt") is False
            if "branch_limit" not in cursor:
                cursor["branch_limit"] = len(self.captures) + max(
                    1, (MAX_CAPTURES - len(self.captures)) // 2
                )
            exhausted = abs(cursor["row"]) >= MAX_ROWS // 2 or (
                cursor["branch"] == -1
                and (
                    len(self.captures) >= cursor["branch_limit"]
                    or self._active_elapsed() >= MAX_JOB_SECONDS * 0.55
                )
            )
            if not fixed_tilt and not exhausted:
                try:
                    if await self._next_band(cursor):
                        continue
                except PanoramaCaptureError as error:
                    if await self._recover(cursor, error, axis="tilt"):
                        continue
                    break
            if exhausted:
                self.issues.append(
                    {
                        "code": "vertical_branch_budget_exhausted",
                        "branch": cursor["branch"],
                    }
                )
            if fixed_tilt or not await self._recover(cursor, None, axis="tilt"):
                cursor["stage"] = "done"
                await self._persist()
        self.complete = (
            all(band.get("complete") for band in cursor["bands"].values())
            and not self.issues
            and (
                self.capabilities.get("axes", {}).get("tilt") is False
                or all(
                    self.boundaries.get(key, {}).get("confirmed")
                    for key in ("tilt_min", "tilt_max")
                )
            )
        )

    def _private_image(self, path: str) -> np.ndarray:
        candidate = Path(path).resolve()
        if not candidate.is_relative_to(self.directory) or not candidate.is_file():
            raise PanoramaCaptureError("invalid_checkpoint_file")
        image = cv2.imread(str(candidate))
        if image is None:
            raise PanoramaCaptureError("invalid_checkpoint_image")
        return image

    async def _relocalize(self, current: dict) -> None:
        cursor = self.checkpoint.get("continuous_cursor") or {}
        self.last_frame, self.physical_state = current, "stopped"
        try:
            try:
                await self._relocalize_observed(current)
            except PanoramaCaptureError as error:
                if (
                    error.code != "relocalization_required"
                    or cursor.get("version") != CONTINUOUS_CURSOR_VERSION
                    or self._reference_destination(cursor) is None
                    or cursor.get("recovery", {}).get("state") == "pending"
                ):
                    raise
                if cursor.get("stage") != "return_reference":
                    if not await self._recover(
                        cursor, error, axis="tilt" if cursor.get("stage") == "step" else "pan"
                    ) or cursor.get("stage") != "return_reference":
                        raise
                await self._return_to_reference_band(cursor)
                await self._relocalize_observed(self.last_frame)
            cursor["relocalization_failed"] = False
        except PanoramaCaptureError:
            cursor["relocalization_failed"] = True
            raise

    async def _relocalize_observed(self, current: dict) -> None:
        if len(self.captures) < 2:
            raise PanoramaCaptureError("relocalization_required")
        cursor = self.checkpoint.get("continuous_cursor") or {}
        resume_at_reference = False
        reference_capture = None
        pending_seek_intent = None
        if cursor:
            if cursor.get("version") != CONTINUOUS_CURSOR_VERSION:
                raise PanoramaCaptureError("continuous_resume_unavailable")
            if (
                _finite(self.checkpoint.get("active_seconds")) is None
                or self.checkpoint["active_seconds"] < 0
            ):
                raise PanoramaCaptureError("continuous_resume_unavailable")
            anchor_row = cursor.get("anchor", {}).get("row")
            if cursor.get("stage") == "pan" and anchor_row != cursor.get("row"):
                raise PanoramaCaptureError("relocalization_required")
            try:
                pending_seek_intent = _pending_seek_intent(cursor)
            except ValueError:
                raise PanoramaCaptureError("continuous_resume_unavailable") from None
            image = self._anchor_image(cursor, row=anchor_row)
            pending = await asyncio.to_thread(_match, current["image"], image)
            anchor_matches = bool(
                pending.get("verified") and pending.get("overlap", 0) >= 0.85
                and pending.get("displacement", math.inf) <= 15
            )
            reference_capture = next(
                (
                    capture
                    for capture in self.captures
                    if capture.get("path") == cursor.get("reference_path")
                    and capture.get("row_index") == 0
                    and capture.get("quality", {}).get("stable") is True
                ),
                None,
            )
            if not anchor_matches or cursor.get("stage") == "return_reference":
                if reference_capture is not None:
                    reference = await asyncio.to_thread(
                        _match, current["image"], self._private_image(reference_capture["path"])
                    )
                    resume_at_reference = bool(
                        reference.get("verified") and reference.get("overlap", 0) >= 0.85
                        and reference.get("displacement", math.inf) <= 15
                    )
                if not resume_at_reference and pending_seek_intent is None:
                    raise PanoramaCaptureError("relocalization_required")
        recent = list(range(max(0, len(self.captures) - 6), len(self.captures)))
        uniform = np.linspace(
            0, len(self.captures) - 1, min(12, len(self.captures)), dtype=int
        ).tolist()
        # Returning near the starting view must not lose its evidence merely
        # because a growing uniform sample skips the first pilot returns.
        anchors = list(range(min(4, len(self.captures))))
        pilots = [
            index
            for index, capture in enumerate(self.captures[:12])
            if capture.get("role") in {"pilot_pan", "pilot_tilt", "pilot_return"}
        ]
        indices = list(dict.fromkeys(anchors + pilots + recent[::-1] + uniform[::-1]))
        links = []
        for index in indices:
            if (
                pending_seek_intent is not None
                and self.captures[index].get("row_index") != pending_seek_intent["row"]
            ):
                continue
            image = self._private_image(self.captures[index]["path"])
            match = await asyncio.to_thread(_match, current["image"], image)
            if match.get("verified") and match["inliers"] >= 40:
                # Retain the matching resolution, not every decoded full frame.
                links.append((index, _gray(image), match))
            if len(links) >= 2:
                first, second = links[-2:]
                between = await asyncio.to_thread(_match, first[1], second[1])
                if between.get("verified") and between.get("displacement", 0) >= 10:
                    expected = np.asarray(second[2]["homography"])
                    actual = np.asarray(between["homography"]) @ np.asarray(first[2]["homography"])
                    grid = np.float32([[[200, 150], [700, 150], [700, 400], [200, 400]]])
                    residual = np.linalg.norm(
                        cv2.perspectiveTransform(grid, expected)
                        - cv2.perspectiveTransform(grid, actual),
                        axis=2,
                    )
                    if float(np.max(residual)) <= 5:
                        # An unconstrained homography also matches optical zoom.
                        # Resume only near a recorded view with consistent local
                        # field of view; arbitrary relocalization stays unsupported.
                        for capture_index, _, link in links:
                            matrix = np.asarray(link["homography"])
                            matrix /= matrix[2, 2]
                            center = np.float32([[[480, 270], [500, 270], [480, 290]]])
                            warped = cv2.perspectiveTransform(center, matrix)[0]
                            jacobian = np.column_stack(
                                ((warped[1] - warped[0]) / 20, (warped[2] - warped[0]) / 20)
                            )
                            scales = np.linalg.svd(jacobian, compute_uv=False)
                            if link["displacement"] < 50 and np.all(
                                (scales >= 0.99) & (scales <= 1.01)
                            ):
                                if cursor:
                                    self.last_frame = current
                                    self.physical_state = "stopped"
                                    if resume_at_reference:
                                        if cursor["stage"] != "return_reference":
                                            if not await self._recover(
                                                cursor,
                                                PanoramaCaptureError("relocalization_required"),
                                                axis="tilt" if cursor["stage"] == "step" else "pan",
                                            ):
                                                raise PanoramaCaptureError(
                                                    "relocalization_required"
                                                )
                                        cursor["anchor"] = {
                                            "capture_id": reference_capture["id"],
                                            "path": reference_capture["path"],
                                            "row": 0,
                                        }
                                        cursor["recovery"]["state"] = "confirmed"
                                    transition = cursor.get("transition")
                                    if isinstance(transition, dict):
                                        if pending_seek_intent is None:
                                            if (
                                                transition.get("state") == "pending"
                                                and resume_at_reference
                                                and cursor.get("recovery", {}).get("state")
                                                == "confirmed"
                                            ):
                                                transition["state"] = "confirmed"
                                            elif transition.get("state") in {
                                                "accepted",
                                                "confirmed",
                                                "no_effect_verified",
                                                "not_issued",
                                            }:
                                                transition["state"] = "confirmed"
                                            else:
                                                cursor["relocalization_failed"] = True
                                                await self._persist()
                                                raise PanoramaCaptureError(
                                                    "continuous_resume_unavailable"
                                                )
                                        else:
                                            seek = cursor[pending_seek_intent["state_key"]]
                                            recovery_level = self.checkpoint.get(
                                                "seek_recovery_level", {}
                                            ).get(pending_seek_intent["axis"], 0)
                                            command_failures = seek.get("command_failures", 0)
                                            duration = pending_seek_intent["duration"]
                                            if (
                                                type(recovery_level) is not int
                                                or not 0 <= recovery_level < 2
                                                or type(command_failures) is not int
                                                or not 0
                                                <= command_failures
                                                < MAX_UNCONFIRMED_RETRIES_PER_SEEK
                                                or duration <= MINIMUM_SEEK_PULSE_SECONDS
                                            ):
                                                transition.update(
                                                    state="recovery_exhausted",
                                                    outcome="unconfirmed_retry_budget_exhausted",
                                                )
                                                cursor["relocalization_failed"] = True
                                                await self._persist()
                                                raise PanoramaCaptureError(
                                                    "continuous_resume_unavailable"
                                                )
                                            if not anchor_matches:
                                                # A restart can split command receipt
                                                # persistence from its physical effect.
                                                # The coherent local graph proves the
                                                # stopped current view is still inside
                                                # the accepted row. Continue from that
                                                # view without replaying or claiming the
                                                # uncertain command as a photograph.
                                                transition.update(
                                                    state="relocalized",
                                                    outcome="stopped_graph_relocalized",
                                                    recovery={
                                                        "method": "verified_local_capture_graph",
                                                        "nearest_capture_id": self.captures[
                                                            capture_index
                                                        ]["id"],
                                                        "nearest_displacement": link[
                                                            "displacement"
                                                        ],
                                                        "nearest_overlap": link.get("overlap"),
                                                    },
                                                )
                                                seek["origin_uncertain_since_anchor"] = True
                                                self.checkpoint.setdefault(
                                                    "relocalizations", []
                                                ).append(
                                                    {
                                                        "method": "verified_local_capture_graph",
                                                        "axis": pending_seek_intent["axis"],
                                                        "direction": pending_seek_intent[
                                                            "direction"
                                                        ],
                                                        "row": pending_seek_intent["row"],
                                                        "nearest_capture_id": self.captures[
                                                            capture_index
                                                        ]["id"],
                                                    }
                                                )
                                                self.checkpoint["relocalizations"] = (
                                                    self.checkpoint["relocalizations"][-8:]
                                                )
                                            # At the exact old anchor, a fresh causal
                                            # comparison separately proves that the
                                            # uncertain pulse had no visible effect.
                                            elif not await self._pending_baseline_unchanged(
                                                pending_seek_intent
                                            ):
                                                transition.update(
                                                    state="effect_or_state_ambiguous",
                                                    outcome="recovery_required",
                                                )
                                                cursor["relocalization_failed"] = True
                                                await self._persist()
                                                raise PanoramaCaptureError(
                                                    "relocalization_required"
                                                )
                                            else:
                                                last_verified = _finite(
                                                    seek.get("last_verified_duration")
                                                )
                                                reduced = max(
                                                    MINIMUM_SEEK_PULSE_SECONDS,
                                                    min(
                                                        duration / 2,
                                                        last_verified or duration / 2,
                                                    ),
                                                )
                                                seek.update(
                                                    duration=reduced,
                                                    command_failures=command_failures + 1,
                                                )
                                                self.checkpoint.setdefault(
                                                    "seek_recovery_level", {}
                                                )[pending_seek_intent["axis"]] = recovery_level + 1
                                                transition.update(
                                                    state="no_effect_verified",
                                                    outcome="stopped_baseline_unchanged",
                                                    recovery={
                                                        "method": "causal_baseline_verified",
                                                        "previous_duration": duration,
                                                        "next_duration": reduced,
                                                    },
                                                )
                                    await self._persist()
                                return
        raise PanoramaCaptureError("relocalization_required")

    async def _recover_pilot_response(self, reference: np.ndarray) -> None:
        """Recover coarse response from this job's own persisted photographs."""
        if not self.saved_return or self.saved_return.get("kind") != "absolute":
            return
        for capture in self.captures[:12]:
            role = capture.get("role")
            if role not in {"pilot_pan", "pilot_tilt"}:
                continue
            axis = "pan" if role == "pilot_pan" else "tilt"
            if _finite(self.checkpoint.get("pilot_response", {}).get(axis)) is not None:
                continue
            matching = await asyncio.to_thread(
                _match, reference, self._private_image(capture["path"])
            )
            self._remember_axis_response(
                axis, matching, self.saved_return, capture.get("pose", {}), kind="persisted_pilot"
            )

    async def _validate_return(self, reference: np.ndarray, frame: dict) -> dict:
        comparison = dict(await asyncio.to_thread(_match, reference, frame["image"]))
        for key in ("overlap", "displacement", "shift_x", "shift_y"):
            if key in comparison and _finite(comparison[key]) is None:
                comparison[key] = None
                comparison["verified"] = False
                comparison.setdefault("code", "visual_response_unavailable")
        _write_json(self.directory / "return-validation.json", comparison)
        overlap = _finite(comparison.get("overlap"))
        displacement = _finite(comparison.get("displacement"))
        if (
            comparison.get("verified") is True
            and overlap is not None
            and overlap >= 0.85
            and displacement is not None
            and displacement <= 3
        ):
            await asyncio.to_thread(
                _write_image, self.directory / "return-final.jpg", frame["image"]
            )
            self.physical_state = "restored"
            epoch_value = self.checkpoint.get("return_epoch", "final")
            if isinstance(epoch_value, str) and epoch_value:
                self.checkpoint["restored_return_epoch"] = epoch_value
        return comparison

    async def _restore(self) -> None:
        cursor = self.checkpoint.get("continuous_cursor")
        if cursor is not None:
            cursor["resume_anchor_verified"] = False
        if not self.saved_return or not self.checkpoint.get("initial_path"):
            self.issues.append({"code": "return_reference_unavailable"})
            if self.acquired and not self._stop_failed and self.physical_state == "unknown":
                await self._confirm_stop()
            return
        self.active_finished = self.active_finished or time.monotonic()
        self.returning = True
        self.physical_state = "returning"
        await self._emit("returning")
        reference = self._private_image(self.checkpoint["initial_path"])
        return_issue = None
        try:
            await self._recover_pilot_response(reference)
            continuous_finish_available = self.capabilities.get("velocity_supported") is True
            relative_finish_available = self.capabilities.get("relative_supported") is True
            initial_comparison = None
            if self.last_frame is not None:
                initial_comparison = await asyncio.to_thread(
                    _match, reference, self.last_frame["image"]
                )
                initial_overlap = _finite(initial_comparison.get("overlap"))
                initial_displacement = _finite(initial_comparison.get("displacement"))
                if (
                    initial_comparison.get("verified") is True
                    and initial_overlap is not None
                    and initial_overlap >= 0.85
                    and initial_displacement is not None
                    and initial_displacement <= 3
                ):
                    await self._confirm_stop()
                    if self.physical_state == "stopped":
                        await self._validate_return(reference, self.last_frame)
                    if self.physical_state == "restored":
                        return
            target = (
                {axis: self.saved_return[axis] for axis in ("pan", "tilt")}
                if self.saved_return.get("kind") == "absolute"
                else None
            )
            # Once the strict 3 px gate above has failed, recall the saved
            # destination at most once for this deliberate return epoch. A
            # persisted planned intent is deliberately ambiguous after an
            # interruption: it may have reached the device, so a retry observes
            # the stopped view and never resends the same epoch.
            epoch_value = self.checkpoint.get("return_epoch", "final")
            return_epoch = (
                epoch_value
                if isinstance(epoch_value, str) and epoch_value
                else "final"
            )
            recalls_value = self.checkpoint.get("return_coarse_recalls")
            if recalls_value is None:
                coarse_recalls: dict[str, Any] = {}
                legacy_coarse = self.checkpoint.get("return_coarse_recall")
                if "return_coarse_recall" in self.checkpoint:
                    legacy_epoch = (
                        legacy_coarse.get("epoch", "final")
                        if isinstance(legacy_coarse, dict)
                        else "final"
                    )
                    if not isinstance(legacy_epoch, str) or not legacy_epoch:
                        legacy_epoch = "final"
                    coarse_recalls[legacy_epoch] = (
                        legacy_coarse
                        if isinstance(legacy_coarse, dict)
                        else {
                            "epoch": legacy_epoch,
                            "state": "invalid_persisted_intent",
                        }
                    )
                self.checkpoint["return_coarse_recalls"] = coarse_recalls
            elif isinstance(recalls_value, dict):
                coarse_recalls = recalls_value
            else:
                # A malformed persisted ledger cannot prove which command may
                # already have reached the device. Fail closed for this epoch.
                coarse_recalls = {
                    return_epoch: {
                        "epoch": return_epoch,
                        "state": "invalid_persisted_ledger",
                    }
                }
                self.checkpoint["return_coarse_recalls"] = coarse_recalls
            coarse_value = coarse_recalls.get(return_epoch)
            coarse_previously_planned = return_epoch in coarse_recalls
            previous_strategy = self.checkpoint.get("return_correction_strategy")
            if (
                not coarse_previously_planned
                and return_epoch == "final"
                and isinstance(previous_strategy, dict)
                and previous_strategy.get("coarse") == "saved_destination"
            ):
                coarse_value = {
                    "epoch": return_epoch,
                    "state": "legacy_planned",
                    "reason": "strategy_precedes_coarse_ledger",
                }
                coarse_recalls[return_epoch] = coarse_value
                self.checkpoint["return_coarse_recall"] = coarse_value
                coarse_previously_planned = True
                await self._persist()
            if not coarse_previously_planned:
                coarse = {
                    "epoch": return_epoch,
                    "state": "planned",
                    "kind": self.saved_return.get("kind"),
                    "before_match_verified": bool(
                        isinstance(initial_comparison, dict)
                        and initial_comparison.get("verified") is True
                    ),
                    "before_overlap": (
                        _finite(initial_comparison.get("overlap"))
                        if isinstance(initial_comparison, dict)
                        else None
                    ),
                    "before_pixels": (
                        _finite(initial_comparison.get("displacement"))
                        if isinstance(initial_comparison, dict)
                        else None
                    ),
                    "before_frame_sequence": (
                        self.last_frame.get("sequence") if self.last_frame is not None else None
                    ),
                    "before_frame_generation": (
                        self.last_frame.get("generation") if self.last_frame is not None else None
                    ),
                }
                coarse_recalls[return_epoch] = coarse
                self.checkpoint["return_coarse_recall"] = coarse
                await self._persist()
                try:
                    result = await self._move(
                        lambda: self.camera.return_to(self.saved_return),
                        target=target,
                        allow_stationary=True,
                        attempt_seconds=self._return_attempt_seconds(),
                    )
                except PanoramaCaptureError as error:
                    coarse.update(state="observation_failed", reason=error.code)
                    self.checkpoint["return_coarse_outcome"] = error.code
                    await self._persist()
                    if error.code not in {"motion_not_observed", "stability_timeout"}:
                        raise
                    await self._confirm_stop()
                    if self.physical_state != "stopped" or self.last_frame is None:
                        raise
                    finish_comparison = await self._validate_return(reference, self.last_frame)
                    if self.physical_state == "restored":
                        return
                    finish_overlap = _finite(finish_comparison.get("overlap"))
                    if (
                        finish_comparison.get("verified") is not True
                        or finish_overlap is None
                        or finish_overlap < 0.85
                    ):
                        raise
                    result = {"frame": self.last_frame, "pose": self.last_pose}
                else:
                    finish_comparison = await asyncio.to_thread(
                        _match, reference, result["frame"]["image"]
                    )
                    coarse.update(
                        state="observed",
                        after_match_verified=finish_comparison.get("verified") is True,
                        after_overlap=_finite(finish_comparison.get("overlap")),
                        after_pixels=_finite(finish_comparison.get("displacement")),
                        after_shift=[
                            _finite(finish_comparison.get("shift_x")),
                            _finite(finish_comparison.get("shift_y")),
                        ],
                    )
                    self.checkpoint["return_coarse_outcome"] = "observed"
                    await self._persist()
            else:
                if not isinstance(coarse_value, dict):
                    coarse_value = {
                        "epoch": return_epoch,
                        "state": "invalid_persisted_intent",
                    }
                    coarse_recalls[return_epoch] = coarse_value
                    await self._persist()
                self.checkpoint["return_coarse_recall"] = coarse_value
                await self._confirm_stop()
                if self.physical_state != "stopped" or self.last_frame is None:
                    raise PanoramaCaptureError("stop_unconfirmed")
                finish_comparison = await self._validate_return(reference, self.last_frame)
                if self.physical_state == "restored":
                    return
                finish_overlap = _finite(finish_comparison.get("overlap"))
                if (
                    finish_comparison.get("verified") is not True
                    or finish_overlap is None
                    or finish_overlap < 0.85
                ):
                    raise PanoramaCaptureError("return_coarse_observation_unconfirmed")
                result = {"frame": self.last_frame, "pose": self.last_pose}
            absolute_finish_axes = _pilot_axes_authorized_for_absolute_finish(
                self.checkpoint
            )
            absolute_finish_calibrated = bool(absolute_finish_axes)
            finish_overlap = _finite(finish_comparison.get("overlap"))
            finish_shift = {
                "pan": _finite(finish_comparison.get("shift_x")),
                "tilt": _finite(finish_comparison.get("shift_y")),
            }
            visual_axes = [
                axis
                for axis in ("pan", "tilt")
                if self.capabilities.get("axes", {}).get(axis) is True
                and finish_shift[axis] is not None
                and abs(finish_shift[axis]) >= CORRECTION_PRECONDITION_PIXELS
            ]
            visual_error_controllable = bool(
                finish_comparison.get("verified") is True
                and finish_overlap is not None
                and finish_overlap >= 0.85
                and visual_axes
            )
            safe_continuous_finish = (
                continuous_finish_available and visual_error_controllable
            )
            visual_finish_supported = visual_error_controllable and (
                continuous_finish_available or relative_finish_available
            )
            visual_return = visual_finish_supported and (
                safe_continuous_finish or target is None or not absolute_finish_calibrated
            )
            self.checkpoint["return_correction_strategy"] = {
                "coarse": "saved_destination",
                "finish": (
                    "visual_continuous"
                    if visual_return and continuous_finish_available
                    else "visual_relative"
                    if visual_return
                    else "absolute_calibrated"
                    if target is not None and absolute_finish_calibrated
                    else "unavailable"
                ),
                "pilot_cycles_closed": absolute_finish_calibrated,
                "absolute_axes": sorted(absolute_finish_axes),
                "visual_axes": visual_axes,
                "return_epoch": return_epoch,
                "shared_command_budget": MAXIMUM_RETURN_CORRECTIONS,
                "commands_already_planned": len(_return_correction_commands(self.checkpoint)),
            }
            await self._persist()
            if visual_return:
                from .panorama_navigation import correct_reference

                result = await correct_reference(self, reference, result)
            absolute_trace_value = self.checkpoint.get("absolute_return_corrections", [])
            absolute_trace = absolute_trace_value if isinstance(absolute_trace_value, list) else []
            self.checkpoint["absolute_return_corrections"] = absolute_trace
            probed: set[str] = {
                str(entry.get("axis"))
                for entry in absolute_trace
                if isinstance(entry, dict) and entry.get("kind") == "probe" and entry.get("axis")
            }
            previous_error = next(
                (
                    value
                    for value in reversed(
                        [_finite(entry.get("before_pixels")) for entry in absolute_trace if isinstance(entry, dict)]
                    )
                    if value is not None
                ),
                None,
            )
            remaining_absolute_corrections = max(
                0,
                MAXIMUM_RETURN_CORRECTIONS
                - len(_return_correction_commands(self.checkpoint)),
            )
            absolute_terminal_state = (
                self.checkpoint.get("absolute_return_correction_state", "stopped")
                if not remaining_absolute_corrections
                else "stopped"
            )
            for correction in range(remaining_absolute_corrections + 1):
                comparison = await self._validate_return(reference, result["frame"])
                if self.physical_state == "restored":
                    self.checkpoint["absolute_return_correction_state"] = "complete"
                    await self._persist()
                    return
                overlap = _finite(comparison.get("overlap"))
                displacement = _finite(comparison.get("displacement"))
                shift_x = _finite(comparison.get("shift_x"))
                shift_y = _finite(comparison.get("shift_y"))
                if (
                    comparison.get("verified") is not True
                    or overlap is None
                    or overlap < 0.85
                    or displacement is None
                    or shift_x is None
                    or shift_y is None
                ):
                    absolute_terminal_state = "rejected"
                    break
                if correction == remaining_absolute_corrections:
                    absolute_terminal_state = "budget_exhausted"
                    break
                # Four bounded adjustments maximum; partial primary-axis gains
                # do not require an unrelated axis to be known. Cross terms are
                # learned only from short, observed single-axis probes.
                response = self.checkpoint.get("pilot_response", {})
                if not target or visual_return or not absolute_finish_calibrated:
                    break
                limits = self.capabilities.get("limits", {})
                current_pose = result.get("pose", {})
                if any(
                    _finite(current_pose.get(axis)) is None or not limits.get(axis)
                    for axis in ("pan", "tilt")
                ):
                    break
                target = {axis: current_pose[axis] for axis in ("pan", "tilt")}
                error_vector = np.array([shift_x, shift_y])
                axes = [
                    axis
                    for axis in ("pan", "tilt")
                    if axis in absolute_finish_axes
                    and _finite(response.get(axis)) is not None
                ]
                local = self.checkpoint.get("return_jacobian", {})
                columns = [
                    local.get(axis, [response[axis], 0] if axis == "pan" else [0, response[axis]])
                    for axis in axes
                ]
                jacobian = np.column_stack(columns) if columns else np.empty((2, 0))
                correction_delta = np.linalg.pinv(jacobian) @ error_vector
                residual = error_vector - jacobian @ correction_delta
                missing = [
                    axis
                    for axis in absolute_finish_axes
                    if axis not in axes and axis not in probed
                ]
                worsened = (
                    previous_error is not None and comparison["displacement"] > previous_error * 1.1
                )
                probe_axis = None
                if missing and float(np.linalg.norm(residual)) > 2:
                    probe_axis = max(
                        missing, key=lambda axis: abs(error_vector[0 if axis == "pan" else 1])
                    )
                elif worsened:
                    probe_axis = next(
                        (axis for axis in axes if axis not in probed and axis not in local), None
                    )
                    if probe_axis is None:
                        break
                if probe_axis is not None:
                    probed.add(probe_axis)
                    amount = min(
                        0.01, (limits[probe_axis]["max"] - limits[probe_axis]["min"]) * 0.005
                    )
                    if target[probe_axis] + amount > limits[probe_axis]["max"]:
                        amount *= -1
                    probe_target = {**target, probe_axis: target[probe_axis] + amount}
                    entry = {
                        "kind": "probe",
                        "axis": probe_axis,
                        "target": dict(probe_target),
                        "before_pixels": displacement,
                        "overlap": overlap,
                        "before_shift": [shift_x, shift_y],
                        "state": "planned",
                    }
                    _append_return_correction_command(
                        self.checkpoint,
                        absolute_trace,
                        entry,
                        modality="absolute_probe",
                    )
                    self.checkpoint["absolute_return_correction_state"] = "active"
                    await self._persist()
                    try:
                        probe = await self._absolute(
                            probe_target,
                            allow_stationary=True,
                            expected_frame=result["frame"],
                        )
                    except PanoramaCaptureError as error:
                        entry.update(state="failed", reason=error.code)
                        self.checkpoint["absolute_return_correction_state"] = "failed"
                        await self._persist()
                        raise
                    matching = await asyncio.to_thread(
                        _match, result["frame"]["image"], probe["frame"]["image"]
                    )
                    response_qualified = self._remember_axis_response(
                        probe_axis, matching, current_pose, probe["pose"], kind="return_probe"
                    )
                    entry.update(
                        state="observed",
                        response_qualified=response_qualified,
                        observed_overlap=_finite(matching.get("overlap")),
                        observed_displacement=_finite(matching.get("displacement")),
                    )
                    after = await asyncio.to_thread(
                        _match, reference, probe["frame"]["image"]
                    )
                    entry.update(
                        after_match_verified=after.get("verified") is True,
                        after_code=(after.get("code") if isinstance(after.get("code"), str) else None),
                        after_overlap=_finite(after.get("overlap")),
                        after_pixels=_finite(after.get("displacement")),
                        after_shift=[
                            _finite(after.get("shift_x")),
                            _finite(after.get("shift_y")),
                        ],
                    )
                    await self._persist()
                    if not response_qualified:
                        absolute_terminal_state = "rejected"
                        result = probe
                        break
                    result = probe
                    previous_error = None
                    continue
                if not axes or (len(axes) == 2 and np.linalg.cond(jacobian) > 50):
                    break
                target_changed = False
                for axis, adjustment in zip(axes, correction_delta, strict=True):
                    corrected = float(
                        np.clip(
                            target[axis] - np.clip(adjustment, -0.01, 0.01),
                            limits[axis]["min"],
                            limits[axis]["max"],
                        )
                    )
                    target_changed = target_changed or not math.isclose(
                        corrected, target[axis], abs_tol=1e-8
                    )
                    target[axis] = corrected
                if not target_changed:
                    break
                previous_error = displacement
                entry = {
                    "kind": "correction",
                    "target": dict(target),
                    "before_pixels": displacement,
                    "overlap": overlap,
                    "before_shift": [shift_x, shift_y],
                    "state": "planned",
                }
                _append_return_correction_command(
                    self.checkpoint,
                    absolute_trace,
                    entry,
                    modality="absolute_correction",
                )
                self.checkpoint["absolute_return_correction_state"] = "active"
                await self._persist()
                try:
                    result = await self._absolute(
                        target,
                        allow_stationary=True,
                        expected_frame=result["frame"],
                    )
                except PanoramaCaptureError as error:
                    entry.update(state="failed", reason=error.code)
                    self.checkpoint["absolute_return_correction_state"] = "failed"
                    await self._persist()
                    raise
                after = await asyncio.to_thread(
                    _match, reference, result["frame"]["image"]
                )
                entry.update(
                    state="observed",
                    after_match_verified=after.get("verified") is True,
                    after_code=(after.get("code") if isinstance(after.get("code"), str) else None),
                    after_overlap=_finite(after.get("overlap")),
                    after_pixels=_finite(after.get("displacement")),
                    after_shift=[
                        _finite(after.get("shift_x")),
                        _finite(after.get("shift_y")),
                    ],
                )
                await self._persist()
            self.checkpoint["absolute_return_correction_state"] = absolute_terminal_state
            await self._persist()
            return_issue = {"code": "return_framing_unconfirmed"}
        except _Stopped:
            raise
        except PanoramaCaptureError as error:
            if error.code == "control_lost":
                self.physical_state = "ownership_lost"
            return_issue = {"code": "return_framing_unconfirmed", "reason": error.code}
        finally:
            self.returning = False
            if (
                self.acquired
                and not self.cancelled()
                and not self._stop_failed
                and self.physical_state in {"unknown", "returning"}
            ):
                # A failed microcorrection may still have accepted Stop. Observe
                # that stopped view without another return command; stopping
                # and restoring the original framing remain separate outcomes.
                await self._confirm_stop()
                if self.physical_state == "stopped" and self.last_frame is not None:
                    # The travel may be untrackable even when the preset arrives.
                    # Only a fresh stopped window matching the saved reference
                    # can establish restoration; command acceptance cannot.
                    await self._validate_return(reference, self.last_frame)
            if return_issue is not None and self.physical_state != "restored":
                self.issues.append(return_issue)

    async def _verify_control(self) -> None:
        """Use the production movement/return path for twelve bounded checks."""
        if not any(
            self.capabilities.get(key)
            for key in ("velocity_supported", "relative_supported")
        ):
            raise PanoramaCaptureError("camera_movement_unsupported")
        value = self.checkpoint.get("control_verification")
        if value is None:
            verification = {
                "status": "running",
                "checks": [],
                "analysis_width": ANALYSIS_WIDTH,
                "commands_attempted": 0,
                "command_budget": MAX_CONTINUOUS_STEPS,
            }
            self.checkpoint["control_verification"] = verification
            await self._persist()
        elif not isinstance(value, dict) or not isinstance(value.get("checks"), list):
            raise PanoramaCaptureError("control_verification_resume_unavailable")
        else:
            verification = value
            verification.setdefault("analysis_width", ANALYSIS_WIDTH)
            verification.setdefault("command_budget", MAX_CONTINUOUS_STEPS)
            attempted = verification.get("commands_attempted")
            if type(attempted) is not int or attempted < 0:
                verification["status"] = "resume_unconfirmed"
                verification["resume_error"] = "command_audit_unavailable"
                await self._persist()
                return
        original = self._private_image(self.checkpoint["initial_path"])
        plan = [
            (repetition, axis, direction)
            for repetition in range(3)
            for axis, direction in (("pan", 1), ("pan", -1), ("tilt", 1), ("tilt", -1))
            if self.capabilities.get("axes", {}).get(axis) is True
        ]
        checks = verification["checks"]
        if len(checks) > len(plan):
            verification["status"] = "resume_unconfirmed"
            verification["resume_error"] = "check_audit_inconsistent"
            await self._persist()
            return
        for index, (repetition, axis, direction) in enumerate(plan):
            if index < len(checks):
                existing = checks[index]
                if (
                    not isinstance(existing, dict)
                    or existing.get("repetition") != repetition + 1
                    or existing.get("axis") != axis
                    or existing.get("direction") != direction
                    or existing.get("status") != "verified"
                ):
                    verification["status"] = "resume_unconfirmed"
                    verification["resume_error"] = "pending_check_not_replayed"
                    await self._persist()
                    return
                continue
            self._check()
            # Reserve one probe, one coarse return and four fine corrections.
            if verification["commands_attempted"] + 6 > verification["command_budget"]:
                verification["status"] = "budget_exhausted"
                await self._persist()
                return
            check = {
                "repetition": repetition + 1,
                "axis": axis,
                "direction": direction,
                "duration_seconds": 0.12,
                "status": "reference_pending",
            }
            checks.append(check)
            await self._persist()
            self.last_frame = await self._reference_window()
            reference_frame = self.last_frame
            before = await asyncio.to_thread(_match, original, reference_frame["image"])
            reference_comparison, reference_error = _control_reference_evidence(
                before, reference_frame
            )
            check["reference_comparison"] = reference_comparison
            if reference_error is not None:
                check.update(status="reference_unconfirmed", error=reference_error)
                verification["status"] = "reference_unconfirmed"
                # The fresh window proves that the camera is stationary, but
                # supersedes the earlier claim that its framing is restored.
                self.physical_state = "stopped"
                await self._persist()
                return
            return_epoch = (
                f"control:{index + 1}:{repetition + 1}:{axis}:{direction}"
            )
            check.update(status="pending", return_epoch=return_epoch)
            # The epoch is persisted before dispatch. If dispatch or later
            # observation is interrupted, this exact check can only observe its
            # already-planned return; a restart never manufactures a new epoch.
            self.checkpoint["return_epoch"] = return_epoch
            await self._persist()
            await self._emit("verifying_control")
            try:
                moved = await self._pulse(
                    axis, direction, 0.12, expected_frame=reference_frame
                )
                movement = moved.get("match", {})
                check["movement"] = movement
                if moved.get("stationary") or not movement.get("verified"):
                    raise PanoramaCaptureError("visual_response_unavailable")
                try:
                    overlap = float(movement["overlap"])
                    displacement = float(movement["displacement"])
                    observed_shift = [
                        float(movement["shift_x"]),
                        float(movement["shift_y"]),
                    ]
                except (KeyError, TypeError, ValueError):
                    check["return_outbound_seed"] = "unavailable"
                else:
                    if (
                        math.isfinite(overlap)
                        and math.isfinite(displacement)
                        and all(math.isfinite(value) for value in observed_shift)
                        and overlap >= 0.85
                        and displacement >= 2.0
                    ):
                        seed = {
                            "return_epoch": return_epoch,
                            "axis": axis,
                            "direction": direction,
                            "duration_seconds": check["duration_seconds"],
                            "overlap": overlap,
                            "displacement": displacement,
                            "observed_shift": observed_shift,
                            "source": "control_verification_outbound",
                        }
                        self.checkpoint["return_outbound_seed"] = seed
                        check["return_outbound_seed"] = "stored"
                    else:
                        check["return_outbound_seed"] = "unavailable"
                check["status"] = "movement_observed"
            except PanoramaCaptureError as error:
                check.update(status="movement_unconfirmed", error=error.code)
                self.issues.append({"code": error.code})
                # One production return attempt; do not restart its budget in
                # a second diagnostic controller or a fallback strategy.
                await self._restore()
                verification["status"] = "failed"
                await self._persist()
                return
            await self._restore()
            check["return_confirmed"] = self.physical_state == "restored"
            check["return_comparison"] = await asyncio.to_thread(
                _match, original, self.last_frame["image"]
            ) if self.last_frame is not None else {"verified": False}
            if self.physical_state != "restored":
                verification["status"] = "return_unconfirmed"
                await self._persist()
                return
            check["status"] = "verified"
            # Include previous checks and returns in the same active budget.
            self.active_finished = None
            await self._persist()
        verification["status"] = "verified" if verification["checks"] else "unavailable"
        # Every diagnostic movement above has a confirmed return. The eventual
        # scan is a distinct outbound flow and therefore owns the final epoch.
        self.checkpoint["return_epoch"] = "final"
        await self._persist()

    async def _acquire_panorama(self, limits: dict | None) -> None:
        if self.region_policy is not None:
            await acquire_region(self)
            return
        fallback = self.checkpoint.get("absolute_grid_fallback")
        fallback_resume = bool(
            isinstance(fallback, dict)
            and fallback.get("state") in {"selected", "active", "complete"}
        )
        continuous_resume = (
            isinstance(self.checkpoint.get("continuous_cursor"), dict)
            or self.checkpoint.get("mode") == "continuous_fallback_pending"
            or fallback_resume
        )
        if continuous_resume:
            selected_mode = fallback.get("continuous_mode") if fallback_resume else None
            selected_mode_supported = bool(
                selected_mode == "velocity"
                and self.capabilities.get("velocity_supported") is True
                or selected_mode == "relative"
                and self.capabilities.get("relative_supported") is True
            )
            if fallback_resume and not (
                selected_mode_supported
                and self.capabilities.get("axes", {}).get("pan") is True
            ):
                fallback["state"] = "unavailable"
                await self._persist()
                raise PanoramaCaptureError("pilot_cycle_unconfirmed")
            if isinstance(fallback, dict):
                fallback["state"] = "active"
            if self.checkpoint.get("continuous_cursor") is None:
                self.checkpoint["mode"] = "continuous_fallback_pending"
            await self._persist()
            await self._continuous_scan()
            if isinstance(fallback, dict):
                fallback["state"] = "complete"
                await self._persist()
            return
        if limits is None:
            await self._continuous_scan()
            return
        try:
            await self._absolute_scan(limits)
        except PanoramaCaptureError as error:
            if error.code != "pilot_cycle_unconfirmed":
                raise
            fallback = {
                "state": "selected",
                "reason": error.code,
                "from": "absolute",
                "to": "continuous",
                "continuous_mode": (
                    "velocity"
                    if self.capabilities.get("velocity_supported") is True
                    else "relative"
                    if self.capabilities.get("relative_supported") is True
                    else None
                ),
                "absolute_plan_published": False,
            }
            self.checkpoint["absolute_grid_fallback"] = fallback
            if not (
                fallback["continuous_mode"] in {"velocity", "relative"}
                and self.capabilities.get("axes", {}).get("pan") is True
            ):
                fallback["state"] = "unavailable"
                await self._persist()
                raise
            for key in (
                "plan",
                "next_index",
                "pilot_steps",
                "grid_geometry",
                "absolute_grid",
            ):
                self.checkpoint.pop(key, None)
            self.checkpoint["mode"] = "continuous_fallback_pending"
            await self._persist()
            fallback["state"] = "active"
            await self._persist()
            await self._continuous_scan()
            fallback["state"] = "complete"
            await self._persist()

    async def run(self, *, return_only: bool, verify_control: bool = False) -> dict:
        self.capabilities = await self.camera.discover()
        if self.region_policy is not None and not return_only:
            if any(self.capabilities.get("axes", {}).get(axis) is not True for axis in ("pan", "tilt")):
                raise PanoramaCaptureError("region_axes_unavailable")
            if not any(self.capabilities.get(key) is True for key in ("velocity_supported", "relative_supported")):
                raise PanoramaCaptureError("region_motion_unavailable")
        recorded_identity = self.checkpoint.get("source_identity")
        current_identity = self.capabilities.get("source_identity", {})
        if return_only and isinstance(recorded_identity, dict):
            # Old checkpoints predate these additive discovery fields. Keep
            # their original-return contract; never migrate their scan cursor.
            recorded_identity = {
                **{key: current_identity.get(key) for key in ("configuration_token", "node_token")
                   if key not in recorded_identity and key in current_identity},
                **recorded_identity,
            }
        if self.checkpoint and recorded_identity != current_identity:
            raise PanoramaCaptureError("checkpoint_source_changed")
        if self.region_policy is not None:
            self.checkpoint["acquisition_policy"] = dict(self.region_policy)
            if self._resuming and not return_only:
                error = region_resume_error(self.checkpoint)
                if error is not None:
                    raise PanoramaCaptureError(error)
        if (
            self._resuming
            and not return_only
            and "plan" in self.checkpoint
        ):
            _validate_absolute_grid_checkpoint(
                self.checkpoint, capabilities=self.capabilities
            )
        if (
            self._resuming
            and not return_only
            and self.checkpoint.get("mode") == "absolute"
            and "plan" not in self.checkpoint
            and self.checkpoint.get("pilot_attempts")
        ):
            _validate_absolute_pilot_limits(
                self.checkpoint, limits=self.capabilities.get("limits")
            )
        if return_only and not self.saved_return:
            raise PanoramaCaptureError("return_reference_unavailable")
        if self.cancelled():
            return self.result()
        await self.camera.acquire()
        self.acquired = True
        interrupted = False
        return_started = return_only
        separate_region_outcomes = self.region_policy is not None and self.region_policy["version"] in {2, 3, 4}
        try:
            cursor = self.checkpoint.get("continuous_cursor")
            if cursor is not None:
                cursor["resume_anchor_verified"] = False
            if not await self._stop():
                raise PanoramaCaptureError("stop_unconfirmed")
            try:
                self.last_frame = await self._reference_window()
            except PanoramaCaptureError as error:
                if (
                    self._resuming
                    or return_only
                    or error.code != "stop_observation_unconfirmed"
                    or self.last_frame is None
                    or time.monotonic() - self.last_frame["received_monotonic"] > 1
                    or _texture_support(self.last_frame["image"])["distributed"]
                ):
                    raise
                # Keep an honest, unqualified original reference. A bounded
                # preflight may leave a textureless initial view; only later
                # visually confirmed movements can produce accepted photos.
                self.last_frame["return_reference_evidence"] = {"status": "untextured_unverified"}
            self.physical_state = "stopped"
            self.last_pose = await self._observational_readback()
            original_zoom = _finite(self.checkpoint.get("initial_zoom"))
            observed_zoom = _finite(self.last_pose.get("zoom"))
            if (
                original_zoom is not None
                and observed_zoom is not None
                and abs(original_zoom - observed_zoom) > 0.001
            ):
                raise PanoramaCaptureError("optical_state_changed")
            if self._resuming and not return_only and not verify_control:
                if self.region_policy is None:
                    await self._relocalize(self.last_frame)
                else:
                    anchor = self.captures[-1]
                    matching = await asyncio.to_thread(_match, self._private_image(anchor["path"]), self.last_frame["image"])
                    if (matching.get("verified") is not True or matching.get("overlap", 0) < 0.85
                            or matching.get("displacement", math.inf) > 15):
                        raise PanoramaCaptureError("region_resume_unavailable")
                    cursor["resume_anchor_verified"] = True
            if not self.saved_return and not self.checkpoint.get("initial_path"):
                initial_path = self.directory / "initial-reference.png"
                await asyncio.to_thread(_write_image, initial_path, self.last_frame["image"])
                self.checkpoint["initial_path"] = str(initial_path)
                self.checkpoint["initial_zoom"] = observed_zoom
                self.checkpoint["initial_reference_evidence"] = self.last_frame.get(
                    "return_reference_evidence"
                )
                await self._persist()
                if not await self._save_original_destination():
                    # A return reference is a movement precondition. A stopped
                    # camera without an absolute destination or owned preset
                    # must never be probed, scanned, or used for verification.
                    raise PanoramaCaptureError("return_reference_unavailable")
            if return_only:
                await self._restore()
            elif verify_control:
                await self._verify_control()
            else:
                limits = self._limits()
                await self._acquire_panorama(limits)
                return_started = True
                await self._restore()
        except (_Stopped, asyncio.CancelledError):
            interrupted = True
            self.issues.append({"code": "capture_interrupted"})
            await self._confirm_stop()
            cursor = self.checkpoint.get("continuous_cursor")
            if cursor is not None and self.physical_state == "stopped":
                try:
                    cursor["resume_anchor_verified"] = await self._current_anchor(
                        cursor, row=cursor.get("anchor", {}).get("row")
                    )
                except (PanoramaCaptureError, _Stopped):
                    cursor["resume_anchor_verified"] = False
        except PanoramaCaptureError as error:
            self.complete = False
            self.issues.append({"code": error.code})
            if error.code == "control_lost":
                self.physical_state = "ownership_lost"
            elif not self.cancelled() and not self._stop_failed:
                # An uncertain past effect still forbids a recall. Qualify the
                # present stop independently instead of leaving an accepted
                # Stop as the final physical evidence without observing it.
                await self._confirm_stop()
                try:
                    uncertain_regional_command = (
                        self.region_policy is not None and self.region_policy["version"] == 4
                        and (any(command.get("state") != "observed" for command in self.checkpoint.get("region_commands", []))
                             # Only normal exhaustion has a known stopped
                             # acquisition endpoint. Unknown observation or
                             # preparation failures do not authorize a recall.
                             or error.code not in {"region_budget_exhausted", "scan_budget_exhausted"})
                    )
                    if (
                        self.physical_state == "stopped"
                        and not (separate_region_outcomes and return_started)
                        and not uncertain_regional_command
                    ):
                        await self._restore()
                except (_Stopped, asyncio.CancelledError):
                    interrupted = True
                    await self._confirm_stop()
        except Exception as error:
            # Storage, vision and callback failures must not leave motion or a
            # renewal task alive. Do not serialize an exception containing URLs.
            self.complete = False
            stage = "return" if separate_region_outcomes and return_started else "acquisition"
            self.issues.append({"code": f"{stage}_failed"})
            self._record_internal_failure(error, stage=stage)
            await self._confirm_stop()
        finally:
            # Preserve a temporary return preset for deliberate return after Stop.
            # Normal completion attempts removal; failed cleanup remains auditable.
            try:
                if (
                    not interrupted and self.physical_state in {"stopped", "restored"}
                    and not self._has_pending_capture()
                ):
                    cursor = self.checkpoint.get("continuous_cursor") or {}
                    destinations = [
                        cursor.get("reference_destination"),
                        self.saved_return if self.physical_state == "restored" else None,
                        *self.checkpoint.get("pending_returns", []),
                    ]
                    pending = []
                    removed = set()
                    for destination in destinations:
                        if not isinstance(destination, dict):
                            continue
                        token = destination.get("preset_token")
                        if token and token in removed:
                            continue
                        try:
                            await self.camera.remove_return(destination)
                            if token:
                                removed.add(token)
                        except PanoramaCaptureError as error:
                            pending.append(destination)
                            self.issues.append({"code": error.code})
                    self.checkpoint["pending_returns"] = pending
                    if self.saved_return and self.saved_return.get("preset_token") in removed:
                        self.saved_return = None
                    if cursor.get("reference_destination", {}).get("preset_token") in removed:
                        cursor.pop("reference_destination", None)
                try:
                    await self._persist()
                except Exception as error:
                    self._persistence_failed = True
                    self.complete = False
                    self.issues.append({"code": "checkpoint_write_failed"})
                    self._record_internal_failure(error, stage="final_checkpoint")
            finally:
                await self.camera.close()
        return self.result()

    def _has_pending_capture(self) -> bool:
        if self.region_policy is not None:
            return region_resume_error(self.checkpoint) is None
        if (
            self.complete or not 2 <= len(self.captures) < MAX_CAPTURES
            or self._active_elapsed() >= MAX_JOB_SECONDS
        ):
            return False
        cursor = self.checkpoint.get("continuous_cursor")
        if self.checkpoint.get("mode") != "continuous":
            return bool(self.checkpoint.get("plan"))
        return bool(
            isinstance(cursor, dict) and cursor.get("version") == CONTINUOUS_CURSOR_VERSION
            and cursor.get("stage") in {"pan", "step", "return_reference"}
            and not (cursor.get("stage") == "step" and {-1, 1}.issubset(cursor.get("finished_branches", [])))
            and cursor.get("relocalization_failed") is not True
            and self._reference_destination(cursor) is not None
        )

    def result(self) -> dict:
        coverage = dict(self.coverage)
        if "bands" in coverage:
            coverage["bands"] = {
                str(row): {
                    "complete": bool(band.get("complete")),
                    "edges": dict(band.get("edges", {})),
                }
                for row, band in coverage["bands"].items()
            }
        return {
            "captures": self.captures,
            "complete": self.complete,
            "coverage": {
                **coverage,
                "progress": self._coverage_progress(),
                "kind": (
                    "initial_region" if self.region_policy is not None and self.region_policy["version"] in {2, 3, 4}
                    else "confirmed_reachable_domain" if self.complete else "attempted_reachable_domain"
                ),
            },
            "physical_state": self.physical_state,
            "issues": self.issues,
            "checkpoint": self.checkpoint or None,
        }


async def run_panorama_scan(
    camera: Any,
    output_dir: Path,
    *,
    progress: Callable[[dict], Awaitable[Any]],
    cancelled: Callable[[], bool],
    checkpoint: dict | None = None,
    return_only: bool = False,
    verify_control: bool = False,
    acquisition_policy: dict | None = None,
) -> dict:
    """Acquire a source panorama or deliberately restore an interrupted framing."""
    scan = _Scan(camera, output_dir, progress, cancelled, checkpoint, acquisition_policy)
    return await scan.run(return_only=return_only, verify_control=verify_control)
