"""E6 repeatable-return trial with an explicit 12-second visual horizon.

This is a distinct, one-pulse experiment. It starts only after a quiet visual
baseline is observed, returns once through a newly-created temporary preset,
and retains that preset whenever the exact visual return gate is not met.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from bounded_motion import CAMERA_ID, SOURCE_ID, _request_json, _safe_frame_summary, _snapshot, _visual_match
from toposync_ext_cameras.processing.visual_anchor import (
    create_visual_anchor,
    visual_anchor_summary,
)


DIRECTORY = Path(__file__).resolve().parent
QUIET_PAIR_LIMIT = 0.15
QUIET_FRAME_COUNT = 5
RETURN_LIMIT = 15.0
# The PTZ controller gives a finite ContinuousMove a lease/fence-bound
# watchdog.  An ONVIF device timeout adds an independent bound when the device
# publishes one. The experiment must leave that native pulse intact. This
# margin starts *after* the HTTP acknowledgement, so it cannot cut the pulse
# short even when the acknowledgement was fast.
NATIVE_PULSE_OBSERVATION_GRACE_SECONDS = 0.35


def _quiet_window(base_url: str, *, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    frames: list[dict[str, Any]] = []
    matches: list[dict[str, Any] | None] = []
    while time.monotonic() - started < timeout_seconds:
        frame = _snapshot(base_url)
        frame["observation_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        if not frame.get("ok"):
            frames = []
            matches = []
            time.sleep(0.12)
            continue
        if frames:
            match = _visual_match(frames[-1], frame)
            matches.append(match)
            quiet_pair = bool(
                match
                and match.get("verified") is True
                and isinstance(match.get("displacement"), (int, float))
                and float(match["displacement"]) <= QUIET_PAIR_LIMIT
            )
            if not quiet_pair:
                frames = [frame]
                matches = []
            else:
                frames.append(frame)
        else:
            frames = [frame]
        if len(frames) >= QUIET_FRAME_COUNT:
            return {
                "quiet": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "frames": frames,
                "consecutive_matches": matches,
                "representative": frames[-1],
            }
        time.sleep(0.12)
    return {
        "quiet": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "frames": frames,
        "consecutive_matches": matches,
        "representative": frames[-1] if frames else None,
    }


def _without_image(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "quiet": window["quiet"],
        "elapsed_ms": window["elapsed_ms"],
        "frames": [_safe_frame_summary(frame) for frame in window["frames"]],
        "consecutive_matches": window["consecutive_matches"],
    }


def _may_have_reached_camera(result: dict[str, Any]) -> bool:
    status_code = result.get("status_code")
    return not isinstance(status_code, int) or status_code >= 500 or bool(result.get("ok"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E6")
    parser.add_argument("--attempt", default="one_pulse_horizon_12_seconds")
    parser.add_argument("--axis", choices=("pan", "tilt"), default="pan")
    parser.add_argument("--direction", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--pan", type=float, default=0.12)
    parser.add_argument("--duration", type=float, default=0.35)
    parser.add_argument("--minimum-displacement", type=float, default=None)
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-horizon-return.json")
    arguments = parser.parse_args()
    if not 0.0 < arguments.pan <= 0.5:
        raise SystemExit("--pan must be in (0, 0.5]")
    if not 0.0 < arguments.duration <= 1.0:
        raise SystemExit("--duration must be in (0, 1.0]")
    if (
        arguments.minimum_displacement is not None
        and arguments.minimum_displacement <= 0.0
    ):
        raise SystemExit("--minimum-displacement must be positive when provided")
    key = uuid.uuid4().hex
    preset_token = ""
    movement_may_have_started = False
    stop_attempted = False
    return_attempted = False
    native_pulse_window_elapsed = False
    baseline_anchor: dict[str, Any] | None = None
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": arguments.attempt,
        "camera": {"id": CAMERA_ID, "source_id": SOURCE_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "visual_descriptor_persisted": False,
        "limits": {
            "continuous_axis": arguments.axis,
            "continuous_direction": arguments.direction,
            "continuous_axis_velocity": arguments.pan,
            "continuous_duration_seconds": arguments.duration,
            "maximum_motor_commands": 3,
            "native_pulse_observation_grace_seconds": NATIVE_PULSE_OBSERVATION_GRACE_SECONDS,
            "quiet_window_frame_count": QUIET_FRAME_COUNT,
            "quiet_pair_limit_analysis_pixels": QUIET_PAIR_LIMIT,
            "settle_timeout_seconds": 12.0,
            "return_limit_analysis_pixels": RETURN_LIMIT,
        },
        "temporary_preset": {"created": False, "removed": False, "preserved_for_manual_recovery": False},
    }
    try:
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["baseline_window"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window["quiet"] or not isinstance(baseline, dict):
            report["outcome"] = "quiet_baseline_unavailable"
            return
        baseline_anchor = create_visual_anchor(baseline["image"])
        report["baseline_anchor_summary"] = visual_anchor_summary(baseline_anchor)
        if baseline_anchor.get("created") is not True:
            report["outcome"] = "baseline_visual_anchor_unavailable"
            return
        preset = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets",
            method="POST",
            body={"source_id": SOURCE_ID, "name": f"TopoSync {arguments.experiment_id} {key[:8]}", "idempotency_key": f"e6h-preset-{key}"},
            idempotency_key=f"e6h-preset-{key}",
        )
        report["temporary_preset"]["create_response"] = {name: value for name, value in preset.items() if name != "body"}
        if not preset.get("ok") or not isinstance(preset.get("body"), dict):
            report["outcome"] = "temporary_preset_unavailable"
            return
        preset_token = str(preset["body"].get("token") or "")
        report["temporary_preset"]["created"] = bool(preset_token)
        if not preset_token:
            report["outcome"] = "temporary_preset_missing_token"
            return
        velocity = {
            "pan": arguments.direction * arguments.pan if arguments.axis == "pan" else 0.0,
            "tilt": arguments.direction * arguments.pan if arguments.axis == "tilt" else 0.0,
            "zoom": 0.0,
        }
        move = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={"source_id": SOURCE_ID, **velocity, "timeout_s": arguments.duration},
            idempotency_key=f"e6h-move-{key}",
        )
        report["move"] = {name: value for name, value in move.items() if name != "body"}
        report["ptz_commands_issued"] += 1
        movement_may_have_started = _may_have_reached_camera(move)
        if not movement_may_have_started:
            report["outcome"] = "move_rejected_before_dispatch"
            return
        # Do not send a competing Stop here.  It would cancel the controller's
        # finite-pulse watchdog immediately and turn a one-second request into
        # a tens-of-milliseconds request. The controller watchdog remains
        # armed if this client exits during the wait; supported ONVIF devices
        # also receive a device-level Timeout.
        native_wait_seconds = arguments.duration + NATIVE_PULSE_OBSERVATION_GRACE_SECONDS
        wait_started = time.monotonic()
        time.sleep(native_wait_seconds)
        native_pulse_window_elapsed = True
        report["native_pulse_window"] = {
            "script_issued_stop_before_deadline": False,
            "waited_after_move_acknowledgement_seconds": round(
                time.monotonic() - wait_started, 4
            ),
            "minimum_wait_seconds": native_wait_seconds,
            "safety_contract": "controller_watchdog_and_onvif_timeout",
        }
        moved_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_pulse_window"] = _without_image(moved_window)
        moved = moved_window.get("representative")
        report["baseline_to_post_pulse"] = _visual_match(
            baseline, moved if isinstance(moved, dict) else None
        )
        displacement = report["baseline_to_post_pulse"]
        response_observed = bool(
            displacement
            and displacement.get("verified") is True
            and isinstance(displacement.get("displacement"), (int, float))
            and (
                arguments.minimum_displacement is None
                or float(displacement["displacement"]) >= arguments.minimum_displacement
            )
        )
        report["actuator_response"] = {
            "minimum_displacement_analysis_pixels": arguments.minimum_displacement,
            "observed": response_observed,
        }
        return_attempted = True
        returned = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
            method="POST",
            body={"source_id": SOURCE_ID, "preset_token": preset_token},
            idempotency_key=f"e6h-return-{key}",
        )
        report["return_command"] = {name: value for name, value in returned.items() if name != "body"}
        report["ptz_commands_issued"] += 1
        if not returned.get("ok"):
            report["outcome"] = "return_command_not_confirmed"
            return
        returned_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_return_window"] = _without_image(returned_window)
        returned_frame = returned_window.get("representative")
        visual_return = _visual_match(baseline, returned_frame if isinstance(returned_frame, dict) else None)
        report["visual_return"] = visual_return
        accepted = bool(
            returned_window["quiet"]
            and visual_return
            and visual_return.get("verified") is True
            and float(visual_return.get("overlap") or 0.0) >= 0.85
            and float(visual_return.get("displacement") or float("inf")) <= RETURN_LIMIT
        )
        if accepted:
            remove = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = remove
            report["temporary_preset"]["removed"] = bool(remove.get("ok"))
            if not remove.get("ok"):
                report["outcome"] = "completed_return_preset_cleanup_failed"
            elif arguments.minimum_displacement is not None and not response_observed:
                report["outcome"] = "finite_pulse_completed_response_unobserved"
            else:
                report["outcome"] = "completed_with_observable_response_and_visual_return"
        else:
            report["temporary_preset"]["preserved_for_manual_recovery"] = True
            report["outcome"] = "visual_return_unverified_preset_preserved"
    finally:
        if (
            preset_token
            and movement_may_have_started
            and not stop_attempted
            and not return_attempted
        ):
            stop = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/stop",
                method="POST",
                body={"source_id": SOURCE_ID, "pan_tilt": True, "zoom": False},
                idempotency_key=f"e6h-stop-{key}",
            )
            report["fallback_stop"] = {name: value for name, value in stop.items() if name != "body"}
            report["ptz_commands_issued"] += 1
            stop_attempted = True
            report["fallback_stop_reason"] = (
                "interrupted_before_return_after_native_pulse_window"
                if native_pulse_window_elapsed
                else "interrupted_before_native_pulse_window"
            )
        if preset_token and movement_may_have_started and not return_attempted:
            returned = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
                method="POST",
                body={"source_id": SOURCE_ID, "preset_token": preset_token},
                idempotency_key=f"e6h-return-{key}",
            )
            report["fallback_return_command"] = {name: value for name, value in returned.items() if name != "body"}
            report["ptz_commands_issued"] += 1
        if preset_token and not report["temporary_preset"]["removed"] and not report["temporary_preset"]["preserved_for_manual_recovery"]:
            report["temporary_preset"]["preserved_for_manual_recovery"] = True
        if (
            preset_token
            and movement_may_have_started
            and not report["temporary_preset"]["removed"]
            and isinstance(baseline_anchor, dict)
            and baseline_anchor.get("created") is True
        ):
            # Recovery after a process boundary needs correspondence evidence,
            # but retaining the source raster would violate this experiment's
            # no-image policy. The bounded descriptor is used only to reject
            # an unverified return; it cannot make a return positive by itself.
            report["recovery_visual_anchor"] = baseline_anchor
            report["visual_descriptor_persisted"] = True
        arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps({
            "outcome": report.get("outcome"),
            "ptz_commands_issued": report["ptz_commands_issued"],
            "temporary_preset": report["temporary_preset"],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
