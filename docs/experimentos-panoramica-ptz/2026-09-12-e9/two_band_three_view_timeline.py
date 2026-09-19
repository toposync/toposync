"""E9P3: two-band route whose next step waits for observed visual arrival.

The prior E9 attempts treated five consecutive quiet snapshots as a terminal
view.  E6T showed that, on this camera, those snapshots can precede delayed
tilt video motion.  This probe keeps the same small route and command budget,
but gates each next PTZ command on an observed visual departure followed by a
quiet terminal window.  It retains no camera raster outside process memory.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))

from bounded_motion import CAMERA_ID, SOURCE_ID, _request_json, _safe_frame_summary, _snapshot, _visual_match  # noqa: E402
from horizon_return import _quiet_window, _without_image  # noqa: E402
from timed_tilt_probe import (  # noqa: E402
    PULSE_DURATION_SECONDS,
    PULSE_SPEED,
    RETURN_DISPLACEMENT_MAXIMUM,
    RETURN_OVERLAP_MINIMUM,
    _absolute_target,
    _action_summary,
    _control_status,
    _match_summary,
    _start_action,
)


TIMELINE_TIMEOUT_SECONDS = 12.0
POLL_INTERVAL_SECONDS = 0.075
QUIET_PAIR_MAXIMUM = 0.15
QUIET_FRAME_COUNT = 5
CONNECTION_DISPLACEMENT_MINIMUM = 2.0
CONNECTION_OVERLAP_MINIMUM = 0.85


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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


def _coverage_edge(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and isinstance(match.get("displacement"), (int, float))
        and float(match["displacement"]) >= CONNECTION_DISPLACEMENT_MINIMUM
        and float(match.get("overlap") or 0.0) >= CONNECTION_OVERLAP_MINIMUM
    )


def _strict_return(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and isinstance(match.get("displacement"), (int, float))
        and float(match["displacement"]) <= RETURN_DISPLACEMENT_MAXIMUM
        and float(match.get("overlap") or 0.0) >= RETURN_OVERLAP_MINIMUM
    )


def _fresh_after(frame: dict[str, Any], prior: dict[str, Any]) -> bool:
    return bool(
        frame.get("ok")
        and isinstance(frame.get("sequence"), int)
        and isinstance(prior.get("sequence"), int)
        and int(frame["sequence"]) > int(prior["sequence"])
        and int(frame.get("generation") or 0) == int(prior.get("generation") or 0)
    )


def _timeline(
    base_url: str,
    *,
    reference: dict[str, Any],
    prior: dict[str, Any] | None,
    arrival: Callable[[dict[str, Any] | None], bool],
    require_departure: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Wait for an observed terminal view; only scalar evidence is retained."""
    started = time.monotonic()
    previous: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = []
    departure_seen = not require_departure
    quiet_count = 0
    terminal: dict[str, Any] | None = None
    first_departure_ms: float | None = None
    first_arrival_ms: float | None = None
    while time.monotonic() - started < TIMELINE_TIMEOUT_SECONDS:
        observed_at = time.monotonic()
        frame = _snapshot(base_url)
        to_reference = _visual_match(reference, frame)
        to_previous = _visual_match(previous, frame) if previous is not None else None
        control = _control_status(base_url)
        fresh = prior is None or _fresh_after(frame, prior)
        sample = {
            "offset_ms": round((observed_at - started) * 1000, 1),
            "frame": _safe_frame_summary(frame),
            "fresh_after_prior": fresh,
            "to_reference": _match_summary(to_reference),
            "to_previous": _match_summary(to_previous),
            "controller": _control_summary(control),
        }
        samples.append(sample)
        if not departure_seen and _coverage_edge(to_reference):
            departure_seen = True
            first_departure_ms = sample["offset_ms"]
            quiet_count = 0
        quiet_pair = bool(
            to_previous
            and to_previous.get("verified") is True
            and isinstance(to_previous.get("displacement"), (int, float))
            and float(to_previous["displacement"]) <= QUIET_PAIR_MAXIMUM
        )
        if departure_seen and fresh and arrival(to_reference) and quiet_pair:
            quiet_count += 1
        else:
            quiet_count = 0
        if departure_seen and fresh and arrival(to_reference) and quiet_count >= QUIET_FRAME_COUNT - 1:
            terminal = frame
            first_arrival_ms = sample["offset_ms"]
            break
        if frame.get("ok") and frame.get("image") is not None:
            previous = frame
        time.sleep(POLL_INTERVAL_SECONDS)

    stopping = [item for item in samples if item["controller"].get("state") == "stopping"]
    return (
        {
            "duration_seconds": round(max(0.0, time.monotonic() - started), 4),
            "sample_count": len(samples),
            "samples": samples,
            "departure_seen": departure_seen,
            "first_departure_offset_ms": first_departure_ms,
            "first_terminal_arrival_offset_ms": first_arrival_ms,
            "first_controller_stopping_offset_ms": stopping[0]["offset_ms"] if stopping else None,
            "terminal_observed": terminal is not None,
        },
        terminal,
    )


def _dispatch(
    base_url: str,
    *,
    path: str,
    body: dict[str, Any],
    idempotency_key: str,
) -> tuple[threading.Thread, dict[str, Any], float]:
    sent = time.monotonic()
    thread, result = _start_action(
        base_url,
        path=path,
        body=body,
        idempotency_key=idempotency_key,
        sent_monotonic=sent,
    )
    return thread, result, sent


def _maybe_reached_camera(result: dict[str, Any]) -> bool:
    status = result.get("status_code")
    return bool(result.get("ok") or not isinstance(status, int) or status >= 500)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E9P3")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-e9p3-timeline-route.json")
    arguments = parser.parse_args()
    key = uuid.uuid4().hex
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "two_band_three_view_visual_arrival_gated",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "images_persisted": False,
        "ptz_commands_issued": 0,
        "anchor_returns_issued": 0,
        "limits": {
            "continuous_moves_maximum": 3,
            "absolute_anchor_returns_maximum": 2,
            "ptz_commands_maximum": 5,
            "visual_corrections_maximum": 0,
            "presets_created": 0,
            "pulse": {"speed": PULSE_SPEED, "duration_seconds": PULSE_DURATION_SECONDS},
            "per_arrival_timeline_timeout_seconds": TIMELINE_TIMEOUT_SECONDS,
        },
        "views": {},
        "connections": {},
    }
    active_threads: list[threading.Thread] = []
    anchor_dirty = False
    target: dict[str, float | None] | None = None

    def dispatch_motion(axis: str, view_name: str, reference: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal anchor_dirty
        thread, result, _sent = _dispatch(
            arguments.base_url,
            path=f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            body={
                "source_id": SOURCE_ID,
                "pan": PULSE_SPEED if axis == "pan" else 0.0,
                "tilt": PULSE_SPEED if axis == "tilt" else 0.0,
                "zoom": 0.0,
                "timeout_s": PULSE_DURATION_SECONDS,
            },
            idempotency_key=f"{arguments.experiment_id.lower()}-{view_name}-{key}",
        )
        active_threads.append(thread)
        timeline, terminal = _timeline(
            arguments.base_url,
            reference=reference,
            prior=reference,
            arrival=_coverage_edge,
            require_departure=True,
        )
        thread.join(timeout=8.0)
        report[f"{view_name}_command"] = _action_summary(result)
        report["ptz_commands_issued"] += 1
        report["views"][view_name] = timeline
        if thread.is_alive() or not _maybe_reached_camera(result):
            return None
        anchor_dirty = True
        return terminal

    def return_to_anchor(name: str, prior: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        nonlocal anchor_dirty
        if target is None or report["anchor_returns_issued"] >= 2:
            return None, False
        thread, result, _sent = _dispatch(
            arguments.base_url,
            path=f"/api/cameras/cameras/{CAMERA_ID}/ptz/absolute-move",
            body={"source_id": SOURCE_ID, **target},
            idempotency_key=f"{arguments.experiment_id.lower()}-{name}-{key}",
        )
        active_threads.append(thread)
        report["anchor_returns_issued"] += 1
        timeline, terminal = _timeline(
            arguments.base_url,
            reference=baseline,
            prior=prior,
            arrival=_strict_return,
            require_departure=False,
        )
        thread.join(timeout=8.0)
        report[f"{name}_command"] = _action_summary(result)
        report["ptz_commands_issued"] += 1
        report["views"][name] = timeline
        match = _visual_match(baseline, terminal) if isinstance(terminal, dict) else None
        report["connections"][f"baseline_to_{name}"] = _match_summary(match)
        anchor_dirty = False
        return terminal, bool(not thread.is_alive() and bool(result.get("ok")) and _strict_return(match))

    try:
        initial = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/status?source_id={SOURCE_ID}",
        )
        target = _absolute_target(initial)
        report["initial_position"] = target
        if target is None:
            report["outcome"] = "absolute_anchor_unavailable"
            return
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["views"]["baseline"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window.get("quiet") or not isinstance(baseline, dict):
            report["outcome"] = "quiet_baseline_unavailable"
            return

        pan_view = dispatch_motion("pan", "base_pan", baseline)
        if pan_view is None:
            report["outcome"] = "base_pan_terminal_not_observed"
            return
        report["connections"]["baseline_to_base_pan"] = _match_summary(_visual_match(baseline, pan_view))
        _, returned = return_to_anchor("after_base_pan_return", pan_view)
        if not returned:
            report["outcome"] = "first_anchor_return_unverified"
            return

        tilt_view = dispatch_motion("tilt", "tilt", baseline)
        if tilt_view is None:
            report["outcome"] = "tilt_terminal_not_observed"
            return
        report["connections"]["baseline_to_tilt"] = _match_summary(_visual_match(baseline, tilt_view))
        tilt_pan_view = dispatch_motion("pan", "tilt_pan", tilt_view)
        if tilt_pan_view is None:
            report["outcome"] = "tilt_pan_terminal_not_observed"
            return
        report["connections"]["tilt_to_tilt_pan"] = _match_summary(_visual_match(tilt_view, tilt_pan_view))
        _, returned = return_to_anchor("after_tilt_pan_return", tilt_pan_view)
        if not returned:
            report["outcome"] = "final_anchor_return_unverified"
            return

        required = ("baseline_to_base_pan", "baseline_to_tilt", "tilt_to_tilt_pan")
        if all(_coverage_edge(report["connections"].get(name)) for name in required):
            report["outcome"] = "two_band_three_view_coverage_verified"
        else:
            report["outcome"] = "two_band_route_visual_connection_unverified"
    except Exception as error:  # noqa: BLE001
        report["outcome"] = "experiment_exception"
        report["error_type"] = type(error).__name__
    finally:
        for thread in active_threads:
            if thread.is_alive():
                thread.join(timeout=8.0)
        if anchor_dirty and target is not None and report["anchor_returns_issued"] < 2:
            safety_thread, safety_result, _sent = _dispatch(
                arguments.base_url,
                path=f"/api/cameras/cameras/{CAMERA_ID}/ptz/absolute-move",
                body={"source_id": SOURCE_ID, **target},
                idempotency_key=f"{arguments.experiment_id.lower()}-safety-return-{key}",
            )
            active_threads.append(safety_thread)
            safety_thread.join(timeout=8.0)
            report["safety_anchor_return_command"] = _action_summary(safety_result)
            report["anchor_returns_issued"] += 1
            report["ptz_commands_issued"] += 1
            report["safety_anchor_return_dispatched"] = True
        _atomic(arguments.output, report)
        print(
            json.dumps(
                {
                    "outcome": report.get("outcome"),
                    "ptz_commands_issued": report.get("ptz_commands_issued"),
                    "anchor_returns_issued": report.get("anchor_returns_issued"),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
