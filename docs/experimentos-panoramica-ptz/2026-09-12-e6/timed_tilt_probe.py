"""E6T: one bounded tilt pulse with a visual and controller-state timeline.

This diagnostic intentionally begins visual sampling when the command is sent,
not after a fixed grace period.  It records only scalar correspondence and
decoder-sequence evidence; rasters, stream locations and credentials never
leave process memory.

It is not a general capture workflow.  The explicit two-command budget is:
one finite ContinuousMove and, after observation, one AbsoluteMove to the
pre-read anchor.  It never retries either command, creates no preset and never
uses visual correction.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from bounded_motion import (  # noqa: E402
    CAMERA_ID,
    SOURCE_ID,
    _request_json,
    _safe_frame_summary,
    _snapshot,
    _visual_match,
)
from horizon_return import _quiet_window, _without_image  # noqa: E402


DIRECTORY = Path(__file__).resolve().parent
PULSE_SPEED = 0.1
PULSE_DURATION_SECONDS = 0.35
TIMELINE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.075
RETURN_OVERLAP_MINIMUM = 0.85
RETURN_DISPLACEMENT_MAXIMUM = 3.0
VISUAL_MOTION_MINIMUM = 2.0
QUIET_PAIR_MAXIMUM = 0.15


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _without_body(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"body", "_ack_offset_ms"}
    }


def _action_summary(result: dict[str, Any]) -> dict[str, Any]:
    body = result.get("body")
    telemetry = body.get("telemetry") if isinstance(body, dict) else None
    if not isinstance(telemetry, dict):
        telemetry = None
    return {
        **_without_body(result),
        "ack_offset_ms": result.get("_ack_offset_ms"),
        "telemetry": (
            {
                key: telemetry.get(key)
                for key in (
                    "command_kind",
                    "command_elapsed_seconds",
                    "transport_elapsed_seconds",
                    "pulse_remaining_seconds",
                    "device_timeout_seconds",
                    "motion_epoch",
                    "stale_after_execution",
                )
                if key in telemetry
            }
            if telemetry is not None
            else None
        ),
    }


def _absolute_target(status_result: dict[str, Any]) -> dict[str, float | None] | None:
    body = status_result.get("body")
    status = body.get("status") if isinstance(body, dict) else None
    if not isinstance(status, dict):
        return None
    target: dict[str, float | None] = {}
    for axis in ("pan", "tilt", "zoom"):
        value = status.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            target[axis] = None
        else:
            target[axis] = float(value)
    return target if target.get("pan") is not None or target.get("tilt") is not None else None


def _control_status(base_url: str) -> dict[str, Any]:
    return _request_json(
        base_url,
        f"/api/cameras/cameras/{CAMERA_ID}/ptz/control-status?source_id={SOURCE_ID}",
    )


def _strict_return(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= RETURN_OVERLAP_MINIMUM
        and isinstance(match.get("displacement"), (int, float))
        and float(match["displacement"]) <= RETURN_DISPLACEMENT_MAXIMUM
    )


def _match_summary(match: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(match, dict):
        return None
    return {
        key: match.get(key)
        for key in (
            "verified",
            "accepted_as_visual_return",
            "code",
            "displacement",
            "overlap",
            "shift_x",
            "shift_y",
            "inliers",
        )
    }


def _control_summary(result: dict[str, Any]) -> dict[str, Any]:
    body = result.get("body")
    control = body.get("control") if isinstance(body, dict) else None
    if not isinstance(control, dict):
        return {"ok": bool(result.get("ok")), "available": False}
    return {
        "ok": bool(result.get("ok")),
        "available": True,
        "state": str(control.get("state") or ""),
        "move_status": str(control.get("move_status") or ""),
        "motion_state": str(control.get("motion_state") or ""),
        "motion_epoch": control.get("motion_epoch"),
        "last_command_kind": str(control.get("last_command_kind") or ""),
        "geometry_safe": bool(control.get("geometry_safe")),
    }


def _visual_timeline(
    base_url: str,
    *,
    baseline: dict[str, Any],
    duration_seconds: float,
    action_thread: threading.Thread | None = None,
    action_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe fresh decoder frames without storing them after comparison."""
    started = time.monotonic()
    previous: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = []
    while time.monotonic() - started < duration_seconds:
        observed_at = time.monotonic()
        frame = _snapshot(base_url)
        control = _control_status(base_url)
        to_baseline = _visual_match(baseline, frame)
        to_previous = _visual_match(previous, frame) if previous is not None else None
        samples.append(
            {
                "offset_ms": round((observed_at - started) * 1000, 1),
                "frame": _safe_frame_summary(frame),
                "to_baseline": _match_summary(to_baseline),
                "to_previous": _match_summary(to_previous),
                "controller": _control_summary(control),
            }
        )
        if frame.get("ok") and frame.get("image") is not None:
            previous = frame
        time.sleep(POLL_INTERVAL_SECONDS)

    visual_samples = [
        item for item in samples if isinstance(item.get("to_baseline"), dict)
    ]
    visual_motion = [
        item
        for item in visual_samples
        if item["to_baseline"].get("verified") is True
        and isinstance(item["to_baseline"].get("displacement"), (int, float))
        and float(item["to_baseline"]["displacement"]) >= VISUAL_MOTION_MINIMUM
        and float(item["to_baseline"].get("overlap") or 0.0) >= RETURN_OVERLAP_MINIMUM
    ]
    quiet_after_motion = [
        item
        for item in samples
        if isinstance(item.get("to_previous"), dict)
        and item["to_previous"].get("verified") is True
        and isinstance(item["to_previous"].get("displacement"), (int, float))
        and float(item["to_previous"]["displacement"]) <= QUIET_PAIR_MAXIMUM
    ]
    controller_stopping = [
        item for item in samples if item["controller"].get("state") == "stopping"
    ]
    return {
        "duration_seconds": round(max(0.0, time.monotonic() - started), 4),
        "sample_count": len(samples),
        "samples": samples,
        "first_visual_motion_offset_ms": visual_motion[0]["offset_ms"] if visual_motion else None,
        "last_visual_motion_offset_ms": visual_motion[-1]["offset_ms"] if visual_motion else None,
        "first_quiet_pair_offset_ms": quiet_after_motion[0]["offset_ms"] if quiet_after_motion else None,
        "first_controller_stopping_offset_ms": (
            controller_stopping[0]["offset_ms"] if controller_stopping else None
        ),
        "action_thread_finished": action_thread is None or not action_thread.is_alive(),
        "action_result_available": isinstance(action_result, dict) and bool(action_result),
    }


def _start_action(
    base_url: str,
    *,
    path: str,
    body: dict[str, Any],
    idempotency_key: str,
    sent_monotonic: float,
) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result.update(
                _request_json(
                    base_url,
                    path,
                    method="POST",
                    body=body,
                    idempotency_key=idempotency_key,
                )
            )
        finally:
            result["_ack_offset_ms"] = round(
                max(0.0, time.monotonic() - sent_monotonic) * 1000, 1
            )

    thread = threading.Thread(target=run, name="e6t-ptz-action", daemon=False)
    thread.start()
    return thread, result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E6T")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-timed-tilt-probe.json")
    arguments = parser.parse_args()
    key = uuid.uuid4().hex
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "one_tilt_pulse_with_sent_to_visual_timeline",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "images_persisted": False,
        "ptz_commands_issued": 0,
        "limits": {
            "continuous_moves_maximum": 1,
            "absolute_anchor_returns_maximum": 1,
            "visual_corrections_maximum": 0,
            "presets_created": 0,
            "pulse": {"axis": "tilt", "direction": 1, "speed": PULSE_SPEED, "duration_seconds": PULSE_DURATION_SECONDS},
            "timeline_seconds": TIMELINE_SECONDS,
        },
    }
    action_threads: list[threading.Thread] = []
    try:
        initial_status = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/status?source_id={SOURCE_ID}",
        )
        target = _absolute_target(initial_status)
        report["initial_position"] = target
        report["initial_status_observation"] = {
            "ok": bool(initial_status.get("ok")),
            "status_code": initial_status.get("status_code"),
            "reported_move_status": (
                initial_status.get("body", {}).get("status", {}).get("move_status")
                if isinstance(initial_status.get("body"), dict)
                else None
            ),
        }
        if target is None:
            report["outcome"] = "absolute_anchor_unavailable_before_motion"
            return
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["baseline_window"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window.get("quiet") or not isinstance(baseline, dict):
            report["outcome"] = "quiet_baseline_unavailable"
            return

        command_sent_monotonic = time.monotonic()
        outgoing_thread, outgoing = _start_action(
            arguments.base_url,
            path=f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            body={
                "source_id": SOURCE_ID,
                "pan": 0.0,
                "tilt": PULSE_SPEED,
                "zoom": 0.0,
                "timeout_s": PULSE_DURATION_SECONDS,
            },
            idempotency_key=f"e6t-tilt-{key}",
            sent_monotonic=command_sent_monotonic,
        )
        action_threads.append(outgoing_thread)
        report["outgoing_command_sent"] = True
        report["outgoing_timeline"] = _visual_timeline(
            arguments.base_url,
            baseline=baseline,
            duration_seconds=TIMELINE_SECONDS,
            action_thread=outgoing_thread,
            action_result=outgoing,
        )
        outgoing_thread.join(timeout=8.0)
        report["outgoing_command"] = _action_summary(outgoing)
        report["outgoing_command_ack_offset_ms"] = outgoing.get("_ack_offset_ms")
        report["ptz_commands_issued"] += 1
        if outgoing_thread.is_alive():
            report["outcome"] = "outgoing_command_unconfirmed"
            return

        outgoing_status = outgoing.get("status_code")
        outgoing_may_have_reached_camera = bool(
            outgoing.get("ok")
            or not isinstance(outgoing_status, int)
            or outgoing_status >= 500
        )
        report["outgoing_may_have_reached_camera"] = outgoing_may_have_reached_camera
        if not outgoing_may_have_reached_camera:
            report["outcome"] = "outgoing_rejected_before_dispatch"
            return

        return_sent_monotonic = time.monotonic()
        return_thread, returned = _start_action(
            arguments.base_url,
            path=f"/api/cameras/cameras/{CAMERA_ID}/ptz/absolute-move",
            body={"source_id": SOURCE_ID, **target},
            idempotency_key=f"e6t-return-{key}",
            sent_monotonic=return_sent_monotonic,
        )
        action_threads.append(return_thread)
        report["return_command_sent"] = True
        report["return_timeline"] = _visual_timeline(
            arguments.base_url,
            baseline=baseline,
            duration_seconds=TIMELINE_SECONDS,
            action_thread=return_thread,
            action_result=returned,
        )
        return_thread.join(timeout=8.0)
        report["return_command"] = _action_summary(returned)
        report["return_command_ack_offset_ms"] = returned.get("_ack_offset_ms")
        report["ptz_commands_issued"] += 1
        final_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["final_window"] = _without_image(final_window)
        final_frame = final_window.get("representative")
        final_match = _visual_match(baseline, final_frame if isinstance(final_frame, dict) else None)
        report["baseline_to_final"] = _match_summary(final_match)
        if return_thread.is_alive() or not returned.get("ok"):
            report["outcome"] = "return_command_unconfirmed"
        elif not final_window.get("quiet") or not _strict_return(final_match):
            report["outcome"] = "final_anchor_return_unverified"
        elif report["outgoing_timeline"].get("first_visual_motion_offset_ms") is None:
            report["outcome"] = "tilt_response_not_observed_return_verified"
        else:
            report["outcome"] = "timed_tilt_response_and_visual_return_verified"
    except Exception as error:  # noqa: BLE001
        report["outcome"] = "experiment_exception"
        report["error_type"] = type(error).__name__
    finally:
        for thread in action_threads:
            if thread.is_alive():
                thread.join(timeout=8.0)
        _atomic(arguments.output, report)
        print(
            json.dumps(
                {
                    "outcome": report.get("outcome"),
                    "ptz_commands_issued": report.get("ptz_commands_issued"),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
