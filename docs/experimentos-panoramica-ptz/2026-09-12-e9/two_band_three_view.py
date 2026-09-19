"""E9 physical proof: four views in two tilt bands with bounded anchor returns."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))
E7_DIRECTORY = DIRECTORY.parent / "2026-09-12-e7"
if str(E7_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E7_DIRECTORY))

from bounded_motion import CAMERA_ID, SOURCE_ID, _may_have_reached_camera, _request_json, _visual_match  # noqa: E402
from closed_loop_recovery import _RecoveryScanner, _outbound_seed  # noqa: E402
from horizon_return import NATIVE_PULSE_OBSERVATION_GRACE_SECONDS, _quiet_window, _without_image  # noqa: E402
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError  # noqa: E402
from toposync_ext_cameras.panorama_navigation import correct_reference  # noqa: E402


PULSE_SPEED = 0.1
PULSE_DURATION_SECONDS = 0.35
RETURN_OVERLAP_MINIMUM = 0.85
RETURN_DISPLACEMENT_MAXIMUM = 3.0
CONNECTION_OVERLAP_MINIMUM = 0.25
CONNECTION_DISPLACEMENT_MINIMUM = 2.0
FINE_ENTRY_MAXIMUM_PIXELS = 35.0


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _without_body(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "body"}


def _absolute_target(value: dict[str, Any]) -> dict[str, float] | None:
    status = value.get("body", {}).get("status") if isinstance(value, dict) else None
    try:
        pan, tilt = float(status["pan"]), float(status["tilt"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and -1.0 <= item <= 1.0 for item in (pan, tilt)):
        return None
    return {"pan": pan, "tilt": tilt}


def _strict_return(match: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(match, dict)
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= RETURN_OVERLAP_MINIMUM
        and float(match.get("displacement") or float("inf")) <= RETURN_DISPLACEMENT_MAXIMUM
    )


def _coverage_connection(match: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(match, dict)
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= CONNECTION_OVERLAP_MINIMUM
        and float(match.get("displacement") or 0.0) >= CONNECTION_DISPLACEMENT_MINIMUM
    )


def _fine_entry(match: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(match, dict)
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= RETURN_OVERLAP_MINIMUM
        and float(match.get("displacement") or float("inf")) <= FINE_ENTRY_MAXIMUM_PIXELS
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E9P1")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-two-band-three-view.json")
    parser.add_argument("--final-fine-command-budget", type=int, choices=(0, 2), default=0)
    arguments = parser.parse_args()

    key = uuid.uuid4().hex
    anchor_dirty = False
    anchor_returns = 0
    baseline: dict[str, Any] | None = None
    target: dict[str, float] | None = None
    scanner: _RecoveryScanner | None = None
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "four_view_two_tilt_band_coverage_with_two_absolute_anchor_returns",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "images_persisted": False,
        "ptz_commands_issued": 0,
        "limits": {
            "continuous_pulse": {"speed": PULSE_SPEED, "duration_seconds": PULSE_DURATION_SECONDS},
            "continuous_commands_maximum": 3,
            "absolute_anchor_returns_maximum": 2,
            "final_visual_corrections_maximum": arguments.final_fine_command_budget,
            "ptz_commands_maximum": 5 + arguments.final_fine_command_budget,
            "strict_return_displacement_maximum_pixels": RETURN_DISPLACEMENT_MAXIMUM,
            "connection_overlap_minimum": CONNECTION_OVERLAP_MINIMUM,
            "connection_displacement_minimum_pixels": CONNECTION_DISPLACEMENT_MINIMUM,
        },
        "views": {},
        "connections": {},
    }

    def capture_view(name: str) -> dict[str, Any] | None:
        window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["views"][name] = _without_image(window)
        frame = window.get("representative")
        return frame if window.get("quiet") and isinstance(frame, dict) else None

    def move(axis: str, direction: int, name: str) -> dict[str, Any] | None:
        nonlocal anchor_dirty
        body = {
            "source_id": SOURCE_ID,
            "pan": direction * PULSE_SPEED if axis == "pan" else 0.0,
            "tilt": direction * PULSE_SPEED if axis == "tilt" else 0.0,
            "zoom": 0.0,
            "timeout_s": PULSE_DURATION_SECONDS,
        }
        command = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body=body,
            idempotency_key=f"{arguments.experiment_id.lower()}-{name}-{key}",
        )
        report[f"{name}_command"] = _without_body(command)
        report["ptz_commands_issued"] += 1
        if not _may_have_reached_camera(command):
            return None
        anchor_dirty = True
        time.sleep(PULSE_DURATION_SECONDS + NATIVE_PULSE_OBSERVATION_GRACE_SECONDS)
        return capture_view(name)

    def return_to_anchor(name: str) -> tuple[dict[str, Any] | None, bool]:
        nonlocal anchor_dirty, anchor_returns
        if baseline is None or target is None or anchor_returns >= 2:
            return None, False
        anchor_returns += 1
        command = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/absolute-move",
            method="POST",
            body={"source_id": SOURCE_ID, **target},
            idempotency_key=f"{arguments.experiment_id.lower()}-{name}-{key}",
        )
        report[f"{name}_command"] = _without_body(command)
        report["ptz_commands_issued"] += 1
        # A dispatched absolute move is never retried in this experiment.
        anchor_dirty = False
        if not command.get("ok"):
            return None, False
        frame = capture_view(name)
        match = _visual_match(baseline, frame)
        report["connections"][f"baseline_to_{name}"] = match
        return frame, bool(frame is not None and _strict_return(match))

    try:
        position = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/status?source_id={SOURCE_ID}",
        )
        target = _absolute_target(position)
        report["initial_position"] = target
        if target is None:
            report["outcome"] = "absolute_position_unavailable"
            return
        baseline = capture_view("baseline")
        if baseline is None:
            report["outcome"] = "quiet_baseline_unavailable"
            return

        pan_view = move("pan", 1, "base_pan")
        if pan_view is None:
            report["outcome"] = "base_pan_view_unavailable"
            return
        report["connections"]["baseline_to_base_pan"] = _visual_match(baseline, pan_view)
        _, first_return_ok = return_to_anchor("after_base_pan_return")
        if not first_return_ok:
            report["outcome"] = "first_anchor_return_unverified"
            return

        tilt_view = move("tilt", 1, "tilt")
        if tilt_view is None:
            report["outcome"] = "tilt_view_unavailable"
            return
        report["connections"]["baseline_to_tilt"] = _visual_match(baseline, tilt_view)
        tilt_pan_view = move("pan", 1, "tilt_pan")
        if tilt_pan_view is None:
            report["outcome"] = "tilt_pan_view_unavailable"
            return
        report["connections"]["tilt_to_tilt_pan"] = _visual_match(tilt_view, tilt_pan_view)
        final_frame, final_return_ok = return_to_anchor("after_tilt_pan_return")
        final_match = report["connections"].get("baseline_to_after_tilt_pan_return")
        if (
            not final_return_ok
            and arguments.final_fine_command_budget
            and final_frame is not None
            and _fine_entry(final_match)
        ):
            scanner = _RecoveryScanner(
                arguments.base_url,
                key,
                report,
                arguments.output.with_name("two-band-three-view-return-ledger.json"),
                return_epoch="e9p-final-return",
                maximum_commands=arguments.final_fine_command_budget,
            )
            report["fine_correction_preconditions"] = []
            report["fine_correction_commands"] = []
            report["fine_correction_observations"] = []
            tilt_seed = _outbound_seed(
                report["connections"].get("baseline_to_tilt"),
                duration_seconds=PULSE_DURATION_SECONDS,
                return_epoch="e9p-final-return",
                axis="tilt",
                direction=1,
            )
            if tilt_seed is None:
                report["return_outbound_seed"] = "unavailable"
            else:
                scanner.checkpoint["return_outbound_seed"] = tilt_seed
                report["return_outbound_seed"] = {
                    name: value for name, value in tilt_seed.items() if name != "source"
                }
                commands_before_fine = report["ptz_commands_issued"]
                try:
                    asyncio.run(
                        correct_reference(scanner, baseline["image"], {"frame": final_frame})
                    )
                except PanoramaCaptureError as error:
                    report["fine_correction_error"] = error.code
                finally:
                    report["ptz_commands_issued"] = commands_before_fine + scanner.commands
                    report["fine_correction_ledger"] = scanner.checkpoint
                fine_frame = capture_view("after_final_visual_correction")
                final_match = _visual_match(baseline, fine_frame)
                report["connections"]["baseline_to_after_final_visual_correction"] = final_match
                final_return_ok = bool(fine_frame is not None and _strict_return(final_match))
        if not final_return_ok:
            report["outcome"] = "final_anchor_return_unverified"
            return

        required_connections = (
            "baseline_to_base_pan",
            "baseline_to_tilt",
            "tilt_to_tilt_pan",
        )
        if all(_coverage_connection(report["connections"].get(name)) for name in required_connections):
            report["outcome"] = "two_band_three_view_coverage_verified"
        else:
            report["outcome"] = "two_band_route_visual_connection_unverified"
    except Exception as error:
        report["outcome"] = "experiment_exception"
        report["error_type"] = type(error).__name__
    finally:
        if anchor_dirty:
            _, recovered = return_to_anchor("safety_anchor_return")
            report["safety_anchor_return_verified"] = recovered
        report["anchor_return_commands_issued"] = anchor_returns
        _atomic(arguments.output, report)
        print(
            json.dumps(
                {
                    "outcome": report.get("outcome"),
                    "ptz_commands_issued": report["ptz_commands_issued"],
                    "anchor_return_commands_issued": anchor_returns,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
