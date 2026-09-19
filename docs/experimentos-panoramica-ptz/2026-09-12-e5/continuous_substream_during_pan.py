"""E5R: keep the Toposync low-resolution capture lease active through one pan."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DIRECTORY = Path(__file__).resolve().parent
E2_DIRECTORY = DIRECTORY.parent / "2026-09-12-e2"
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
for location in (E2_DIRECTORY, E6_DIRECTORY):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from bounded_motion import CAMERA_ID, SOURCE_ID, _may_have_reached_camera, _request_json  # noqa: E402
from horizon_return import _quiet_window, _without_image  # noqa: E402
from run import _fetch_snapshot  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import create_visual_anchor, match_visual_anchor  # noqa: E402


OUTBOUND_SPEED = 0.1
OUTBOUND_DURATION_SECONDS = 0.35
OBSERVATION_DURATION_SECONDS = 8.0


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _without_body(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "body"}


def _exact(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= 0.85
        and float(match.get("displacement") or float("inf")) <= 3.0
    )


def _observe(base_url: str, result: dict[str, Any]) -> None:
    body = json.dumps(
        {"duration_s": OBSERVATION_DURATION_SECONDS, "sample_interval_s": 0.05}
    ).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/sources/profile_2/capture-observation",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25.0) as response:
            result["body"] = json.load(response)
            result["status"] = int(response.status)
    except Exception as error:  # The aggregate response stays the experiment's evidence.
        result["error"] = type(error).__name__


def _stream_evidence(observation: dict[str, Any] | None, *, move_started_at: float, move_quiet_at: float, return_started_at: float) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {"status": "observation_unavailable"}
    samples = observation.get("samples") if isinstance(observation.get("samples"), list) else []
    published = [float(item.get("published_at") or 0.0) for item in samples if isinstance(item, dict)]
    before = sum(1 for stamp in published if 0.0 < stamp < move_started_at)
    during = sum(1 for stamp in published if move_started_at <= stamp <= move_quiet_at)
    returning = sum(1 for stamp in published if return_started_at <= stamp)
    maximum_gap = observation.get("maximum_observed_gap_seconds")
    continuous = (
        before >= 1
        and during >= 2
        and returning >= 1
        and isinstance(maximum_gap, int | float)
        and float(maximum_gap) <= 1.0
        and observation.get("released_while_observing") is False
    )
    return {
        "status": "continuous_through_pan_and_return" if continuous else "continuity_not_confirmed",
        "frames_before_move": before,
        "frames_during_move_and_settle": during,
        "frames_during_return": returning,
        "distinct_frame_count": observation.get("distinct_frame_count"),
        "maximum_observed_gap_seconds": maximum_gap,
        "released_while_observing": observation.get("released_while_observing"),
        "images_persisted": observation.get("images_persisted"),
    }


def _private_destination(path: Path, token: str, anchor: dict[str, Any]) -> None:
    _atomic(
        path,
        {
            "camera_id": CAMERA_ID,
            "source_id": SOURCE_ID,
            "preset": {"token": token},
            "reference_visual_anchor": anchor,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-continuous-substream-during-pan.json")
    parser.add_argument(
        "--private-destination",
        type=Path,
        default=DIRECTORY / "continuous-substream-preserved-return-destination.json",
    )
    arguments = parser.parse_args()
    key = uuid.uuid4().hex
    preset_token = ""
    outbound_started = False
    observer_result: dict[str, Any] = {}
    observer: threading.Thread | None = None
    anchor: dict[str, Any] | None = None
    report: dict[str, Any] = {
        "experiment_id": "E5R",
        "attempt": "continuous_native_substream_through_one_bounded_pan",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID, "observed_source_id": "profile_2"},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "limits": {
            "outbound": {"axis": "pan", "direction": 1, "velocity": OUTBOUND_SPEED, "duration_seconds": OUTBOUND_DURATION_SECONDS},
            "observer_duration_seconds": OBSERVATION_DURATION_SECONDS,
            "maximum_motor_actions": 2,
        },
        "temporary_preset": {"created": False, "preserved": False, "removed": False},
    }
    move_started_at = move_quiet_at = return_started_at = 0.0
    try:
        baseline_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["baseline_window"] = _without_image(baseline_window)
        baseline = baseline_window.get("representative")
        if not baseline_window.get("quiet") or not isinstance(baseline, dict):
            report["outcome"] = "baseline_not_quiet"
            return
        anchor = create_visual_anchor(baseline["image"])
        if anchor.get("created") is not True:
            report["outcome"] = "baseline_visual_anchor_unavailable"
            return
        warmup = _fetch_snapshot(arguments.base_url, "profile_2", freshness_mode="decoder")
        report["substream_preflight"] = {key: value for key, value in warmup.items() if key != "image"}
        if not warmup.get("obtained"):
            report["outcome"] = "substream_preflight_unavailable"
            return
        preset = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets",
            method="POST",
            body={"source_id": SOURCE_ID, "name": f"TopoSync E5R {key[:8]}", "idempotency_key": f"e5r-preset-{key}"},
            idempotency_key=f"e5r-preset-{key}",
        )
        report["temporary_preset"]["create_response"] = _without_body(preset)
        body = preset.get("body") if isinstance(preset.get("body"), dict) else {}
        token = body.get("token")
        preset_token = token if isinstance(token, str) else ""
        report["temporary_preset"]["created"] = bool(preset_token)
        if not preset.get("ok") or not preset_token:
            report["outcome"] = "temporary_preset_unavailable"
            return
        observer = threading.Thread(target=_observe, args=(arguments.base_url, observer_result), daemon=False)
        observer.start()
        time.sleep(0.75)
        move_started_at = time.time()
        outbound = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={"source_id": SOURCE_ID, "pan": OUTBOUND_SPEED, "tilt": 0.0, "zoom": 0.0, "timeout_s": OUTBOUND_DURATION_SECONDS},
            idempotency_key=f"e5r-outbound-{key}",
        )
        report["outbound_command"] = _without_body(outbound)
        report["ptz_commands_issued"] += 1
        outbound_started = _may_have_reached_camera(outbound)
        if not outbound_started:
            report["outcome"] = "outbound_rejected_before_dispatch"
            return
        time.sleep(OUTBOUND_DURATION_SECONDS + 1.4)
        moved_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        move_quiet_at = time.time()
        report["post_outbound_window"] = _without_image(moved_window)
        if not moved_window.get("quiet"):
            report["outcome"] = "post_outbound_not_quiet_preset_preserved"
            return
        return_started_at = time.time()
        recalled = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
            method="POST",
            body={"source_id": SOURCE_ID, "preset_token": preset_token},
            idempotency_key=f"e5r-return-{key}",
        )
        report["return_command"] = _without_body(recalled)
        report["ptz_commands_issued"] += 1
        if not recalled.get("ok"):
            report["outcome"] = "return_command_not_confirmed_preset_preserved"
            return
        returned_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["returned_window"] = _without_image(returned_window)
        returned = returned_window.get("representative")
        visual_return = match_visual_anchor(anchor, returned["image"]) if returned_window.get("quiet") and isinstance(returned, dict) else None
        report["visual_return"] = visual_return
        if _exact(visual_return):
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _without_body(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
            report["temporary_preset"]["preserved"] = not bool(removed.get("ok"))
            report["outcome"] = "return_verified_pending_stream_evidence" if removed.get("ok") else "return_verified_preset_cleanup_failed"
        else:
            report["outcome"] = "return_unverified_preset_preserved"
    finally:
        if observer is not None:
            observer.join(timeout=15.0)
            observation = observer_result.get("body") if isinstance(observer_result.get("body"), dict) else None
            report["substream_observation"] = _stream_evidence(
                observation,
                move_started_at=move_started_at,
                move_quiet_at=move_quiet_at,
                return_started_at=return_started_at,
            )
            report["substream_observation_status"] = observer_result.get("status")
            if observer.is_alive():
                report["substream_observation_error"] = "observer_timeout"
            elif "error" in observer_result:
                report["substream_observation_error"] = observer_result["error"]
        if preset_token and not outbound_started:
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _without_body(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
        elif preset_token and not report["temporary_preset"]["removed"]:
            report["temporary_preset"]["preserved"] = True
            if anchor and anchor.get("created") is True:
                _private_destination(arguments.private_destination, preset_token, anchor)
                report["temporary_preset"]["private_destination_persisted"] = True
        if report.get("outcome") == "return_verified_pending_stream_evidence":
            evidence = report.get("substream_observation")
            report["outcome"] = (
                "continuous_native_substream_through_pan_verified"
                if isinstance(evidence, dict) and evidence.get("status") == "continuous_through_pan_and_return"
                else "return_verified_substream_continuity_inconclusive"
            )
        _atomic(arguments.output, report)
        print(json.dumps({key: report.get(key) for key in ("outcome", "ptz_commands_issued", "substream_observation", "temporary_preset")}, sort_keys=True))


if __name__ == "__main__":
    main()
