"""E7A2: bounded absolute approach followed by production visual correction."""

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
if str(DIRECTORY) not in sys.path:
    sys.path.insert(0, str(DIRECTORY))

from bounded_motion import CAMERA_ID, SOURCE_ID, _may_have_reached_camera, _request_json, _visual_match  # noqa: E402
from closed_loop_recovery import _RecoveryScanner, _outbound_seed  # noqa: E402
from horizon_return import NATIVE_PULSE_OBSERVATION_GRACE_SECONDS, _quiet_window, _without_image  # noqa: E402
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError  # noqa: E402
from toposync_ext_cameras.panorama_navigation import correct_reference  # noqa: E402


OUTBOUND_SPEED = 0.1
OUTBOUND_DURATION_SECONDS = 0.35
RETURN_OVERLAP_MINIMUM = 0.85
RETURN_DISPLACEMENT_MAXIMUM = 3.0
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
    parser.add_argument("--experiment-id", default="E7A2")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-absolute-visual-return.json")
    arguments = parser.parse_args()

    key = uuid.uuid4().hex
    outbound_may_have_started = False
    scanner: _RecoveryScanner | None = None
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "one_continuous_outbound_one_absolute_approach_then_bounded_visual_return",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "images_persisted": False,
        "ptz_commands_issued": 0,
        "limits": {
            "outbound": {
                "axis": "pan",
                "direction": 1,
                "speed": OUTBOUND_SPEED,
                "duration_seconds": OUTBOUND_DURATION_SECONDS,
            },
            "absolute_return_commands_maximum": 1,
            "fine_corrections_maximum": 4,
            "ptz_commands_maximum": 6,
            "fine_entry_maximum_pixels": FINE_ENTRY_MAXIMUM_PIXELS,
            "strict_return_maximum_pixels": RETURN_DISPLACEMENT_MAXIMUM,
        },
    }
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
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["baseline_window"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window.get("quiet") or not isinstance(baseline, dict):
            report["outcome"] = "quiet_baseline_unavailable"
            return
        outbound = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={
                "source_id": SOURCE_ID,
                "pan": OUTBOUND_SPEED,
                "tilt": 0.0,
                "zoom": 0.0,
                "timeout_s": OUTBOUND_DURATION_SECONDS,
            },
            idempotency_key=f"{arguments.experiment_id.lower()}-outbound-{key}",
        )
        report["outbound_command"] = _without_body(outbound)
        report["ptz_commands_issued"] += 1
        outbound_may_have_started = _may_have_reached_camera(outbound)
        if not outbound_may_have_started:
            report["outcome"] = "outbound_rejected_before_dispatch"
            return
        time.sleep(OUTBOUND_DURATION_SECONDS + NATIVE_PULSE_OBSERVATION_GRACE_SECONDS)
        outbound_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_outbound_window"] = _without_image(outbound_window)
        moved = outbound_window.get("representative")
        outbound_match = _visual_match(baseline, moved if isinstance(moved, dict) else None)
        report["outbound_visual_response"] = outbound_match
        absolute = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/absolute-move",
            method="POST",
            body={"source_id": SOURCE_ID, **target},
            idempotency_key=f"{arguments.experiment_id.lower()}-absolute-return-{key}",
        )
        report["absolute_return_command"] = _without_body(absolute)
        report["ptz_commands_issued"] += 1
        if not absolute.get("ok"):
            report["outcome"] = "absolute_return_command_unconfirmed"
            return
        coarse_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_absolute_return_window"] = _without_image(coarse_window)
        coarse_frame = coarse_window.get("representative")
        coarse_match = _visual_match(baseline, coarse_frame if isinstance(coarse_frame, dict) else None)
        report["absolute_return_visual"] = coarse_match
        if coarse_window.get("quiet") and _strict_return(coarse_match):
            report["outcome"] = "absolute_return_verified_without_fine_correction"
            return
        if not coarse_window.get("quiet") or not isinstance(coarse_frame, dict) or not _fine_entry(coarse_match):
            report["outcome"] = "absolute_return_outside_visual_correction_envelope"
            return
        scanner = _RecoveryScanner(
            arguments.base_url,
            key,
            report,
            arguments.output.with_name("absolute-visual-return-ledger.json"),
            return_epoch="e7a2",
        )
        outbound_seed = _outbound_seed(
            outbound_match,
            duration_seconds=OUTBOUND_DURATION_SECONDS,
            return_epoch="e7a2",
        )
        if outbound_seed is not None:
            scanner.checkpoint["return_outbound_seed"] = outbound_seed
            report["return_outbound_seed"] = {
                name: value for name, value in outbound_seed.items() if name != "source"
            }
        else:
            report["return_outbound_seed"] = "unavailable"
            report["outcome"] = "outbound_seed_unavailable"
            return
        result = asyncio.run(correct_reference(scanner, baseline["image"], {"frame": coarse_frame}))
        report["fine_correction_ledger"] = scanner.checkpoint
        report["ptz_commands_issued"] += scanner.commands
        final_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_fine_return_window"] = _without_image(final_window)
        final_frame = final_window.get("representative")
        final_match = _visual_match(baseline, final_frame if isinstance(final_frame, dict) else None)
        report["final_visual_return"] = final_match
        if final_window.get("quiet") and _strict_return(final_match):
            report["outcome"] = "absolute_visual_return_verified"
        elif result.get("frame") is not None:
            report["outcome"] = "absolute_visual_return_unverified"
        else:
            report["outcome"] = "absolute_visual_return_unavailable"
    except PanoramaCaptureError as error:
        report["outcome"] = "absolute_visual_return_error"
        report["error"] = error.code
    finally:
        if scanner is not None:
            report["ptz_commands_issued"] = max(
                report["ptz_commands_issued"], 2 + scanner.commands
            )
        if outbound_may_have_started and report["ptz_commands_issued"] < 2:
            report["safety_note"] = "No unplanned fallback command was sent after the bounded return path became unavailable."
        _atomic(arguments.output, report)
        print(
            json.dumps(
                {
                    "outcome": report.get("outcome"),
                    "ptz_commands_issued": report["ptz_commands_issued"],
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
