"""E2R: compare the configured streams in two quiet pan views and return once."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
E7_DIRECTORY = DIRECTORY.parent / "2026-09-12-e7"
for location in (E6_DIRECTORY, E7_DIRECTORY, DIRECTORY):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from bounded_motion import (  # noqa: E402
    CAMERA_ID,
    SOURCE_ID,
    _may_have_reached_camera,
    _request_json,
)
from horizon_return import _quiet_window, _without_image  # noqa: E402
from run import SOURCE_IDS, _fetch_snapshot, _match_summary, _without_image as _without_snapshot_image  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import (  # noqa: E402
    create_visual_anchor,
    match_visual_anchor,
)


OUTBOUND_SPEED = 0.1
OUTBOUND_DURATION_SECONDS = 0.35


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _response_summary(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "body"}


def _exact(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= 0.85
        and float(match.get("displacement") or float("inf")) <= 3.0
    )


def _sample_after_warmup(base_url: str, source_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    initial = _fetch_snapshot(base_url, source_id, freshness_mode="decoder")
    if initial.get("obtained"):
        return initial, {"attempts": 1, "warmup_required": False}
    if initial.get("status") == 503 and "Fresh camera frame is temporarily unavailable" in str(initial.get("error") or ""):
        time.sleep(1.0)
        confirmed = _fetch_snapshot(base_url, source_id, freshness_mode="decoder")
        return confirmed, {
            "attempts": 2,
            "warmup_required": True,
            "initial_status": 503,
            "confirmation_obtained": bool(confirmed.get("obtained")),
        }
    return initial, {
        "attempts": 1,
        "warmup_required": False,
        "initial_status": initial.get("status"),
    }


def _pair(base_url: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    sampled = {source_id: _sample_after_warmup(base_url, source_id) for source_id in SOURCE_IDS}
    samples = {source_id: item[0] for source_id, item in sampled.items()}
    main, sub = samples["profile_1"], samples["profile_2"]
    match = _match_summary(main["image"], sub["image"]) if main.get("obtained") and sub.get("obtained") else None
    return {
        source_id: {
            **_without_snapshot_image(sample),
            "warmup": sampled[source_id][1],
        }
        for source_id, sample in samples.items()
    }, match


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
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-two-direction-stream-equivalence.json")
    parser.add_argument(
        "--private-destination",
        type=Path,
        default=DIRECTORY / "two-direction-preserved-return-destination.json",
    )
    arguments = parser.parse_args()
    key = uuid.uuid4().hex
    preset_token = ""
    outbound_started = False
    report: dict[str, Any] = {
        "experiment_id": "E2R",
        "attempt": "two_quiet_pan_views_with_one_visual_preset_return",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_ids": list(SOURCE_IDS)},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "limits": {
            "outbound": {"axis": "pan", "direction": 1, "velocity": OUTBOUND_SPEED, "duration_seconds": OUTBOUND_DURATION_SECONDS},
            "maximum_motor_actions": 2,
            "return": "one GotoPreset only after a quiet second view",
        },
        "temporary_preset": {"created": False, "preserved": False, "removed": False},
    }
    anchor: dict[str, Any] | None = None
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
        baseline_samples, baseline_match = _pair(arguments.base_url)
        report["baseline_samples"] = baseline_samples
        report["baseline_correspondence_normalized_640x360"] = baseline_match
        if baseline_match is None or baseline_match.get("verified") is not True:
            report["outcome"] = "baseline_stream_correspondence_unavailable"
            return
        preset = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets",
            method="POST",
            body={"source_id": SOURCE_ID, "name": f"TopoSync E2R {key[:8]}", "idempotency_key": f"e2r-preset-{key}"},
            idempotency_key=f"e2r-preset-{key}",
        )
        report["temporary_preset"]["create_response"] = _response_summary(preset)
        body = preset.get("body") if isinstance(preset.get("body"), dict) else {}
        token = body.get("token")
        preset_token = token if isinstance(token, str) else ""
        report["temporary_preset"]["created"] = bool(preset_token)
        if not preset.get("ok") or not preset_token:
            report["outcome"] = "temporary_preset_unavailable"
            return
        outbound = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/move",
            method="POST",
            body={"source_id": SOURCE_ID, "pan": OUTBOUND_SPEED, "tilt": 0.0, "zoom": 0.0, "timeout_s": OUTBOUND_DURATION_SECONDS},
            idempotency_key=f"e2r-outbound-{key}",
        )
        report["outbound_command"] = _response_summary(outbound)
        report["ptz_commands_issued"] += 1
        outbound_started = _may_have_reached_camera(outbound)
        if not outbound_started:
            report["outcome"] = "outbound_rejected_before_dispatch"
            return
        time.sleep(OUTBOUND_DURATION_SECONDS + 1.4)
        second_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["second_view_window"] = _without_image(second_window)
        if not second_window.get("quiet"):
            report["outcome"] = "second_view_not_quiet_preset_preserved"
            return
        second_samples, second_match = _pair(arguments.base_url)
        report["second_view_samples"] = second_samples
        report["second_view_correspondence_normalized_640x360"] = second_match
        recalled = _request_json(
            arguments.base_url,
            f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
            method="POST",
            body={"source_id": SOURCE_ID, "preset_token": preset_token},
            idempotency_key=f"e2r-return-{key}",
        )
        report["return_command"] = _response_summary(recalled)
        report["ptz_commands_issued"] += 1
        if not recalled.get("ok"):
            report["outcome"] = "return_command_not_confirmed_preset_preserved"
            return
        returned_window = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["returned_window"] = _without_image(returned_window)
        returned = returned_window.get("representative")
        visual_return = match_visual_anchor(anchor, returned["image"]) if returned_window.get("quiet") and isinstance(returned, dict) else None
        report["visual_return"] = visual_return
        both_views_verified = bool(
            baseline_match and baseline_match.get("verified") is True and second_match and second_match.get("verified") is True
        )
        if _exact(visual_return):
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _response_summary(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
            report["temporary_preset"]["preserved"] = not bool(removed.get("ok"))
            report["outcome"] = "two_views_stream_compatible_return_verified" if both_views_verified and removed.get("ok") else "return_verified_stream_evidence_incomplete"
        else:
            report["outcome"] = "return_unverified_preset_preserved"
    finally:
        if preset_token and not outbound_started:
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(preset_token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = _response_summary(removed)
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
        elif preset_token and not report["temporary_preset"]["removed"]:
            report["temporary_preset"]["preserved"] = True
            if anchor and anchor.get("created") is True:
                _private_destination(arguments.private_destination, preset_token, anchor)
                report["temporary_preset"]["private_destination_persisted"] = True
        _atomic(arguments.output, report)
        print(json.dumps({key: report.get(key) for key in ("outcome", "ptz_commands_issued", "temporary_preset")}, sort_keys=True))


if __name__ == "__main__":
    main()
