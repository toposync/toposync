"""E7A: one bounded AbsoluteMove return experiment through Toposync only."""

from __future__ import annotations

import argparse
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

from bounded_motion import CAMERA_ID, SOURCE_ID, _may_have_reached_camera, _request_json, _safe_frame_summary, _visual_match  # noqa: E402
from horizon_return import NATIVE_PULSE_OBSERVATION_GRACE_SECONDS, _quiet_window, _without_image  # noqa: E402


OUTBOUND_SPEED = 0.1
OUTBOUND_DURATION_SECONDS = 0.35
RETURN_OVERLAP_MINIMUM = 0.85
RETURN_DISPLACEMENT_MAXIMUM = 3.0


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _without_body(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "body"}


def _absolute_target(value: dict[str, Any]) -> dict[str, float] | None:
    body = value.get("body") if isinstance(value, dict) else None
    status = body.get("status") if isinstance(body, dict) else None
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E7A1")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-absolute-return.json")
    arguments = parser.parse_args()

    key = uuid.uuid4().hex
    outbound_may_have_started = False
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "one_continuous_outbound_then_one_absolute_return",
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
            "ptz_commands_maximum": 2,
            "strict_return": {
                "overlap_minimum": RETURN_OVERLAP_MINIMUM,
                "displacement_maximum_pixels": RETURN_DISPLACEMENT_MAXIMUM,
            },
        },
    }
    try:
        status = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/status?source_id={SOURCE_ID}",
        )
        target = _absolute_target(status)
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
        report["outbound_visual_response"] = _visual_match(
            baseline, moved if isinstance(moved, dict) else None
        )
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
        returned_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["post_absolute_return_window"] = _without_image(returned_window)
        returned = returned_window.get("representative")
        match = _visual_match(baseline, returned if isinstance(returned, dict) else None)
        report["absolute_return_visual"] = match
        if returned_window.get("quiet") and _strict_return(match):
            report["outcome"] = "absolute_return_verified"
        else:
            report["outcome"] = "absolute_return_unverified"
    finally:
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
