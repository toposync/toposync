"""E7R3: one visually verified recall of the preset preserved by E7R2."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
E6_DIRECTORY = DIRECTORY.parent / "2026-09-12-e6"
if str(E6_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(E6_DIRECTORY))

from bounded_motion import CAMERA_ID, SOURCE_ID, _request_json  # noqa: E402
from horizon_return import _quiet_window, _without_image  # noqa: E402
from toposync_ext_cameras.processing.visual_anchor import match_visual_anchor  # noqa: E402


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _exact(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("verified") is True
        and float(match.get("overlap") or 0.0) >= 0.85
        and float(match.get("displacement") or float("inf")) <= 3.0
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--experiment-id", default="E7R3")
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-preserved-preset-recovery.json")
    parser.add_argument("--destination", type=Path, default=DIRECTORY / "preserved-preset-destination.json")
    parser.add_argument("--reference-report", type=Path, default=DIRECTORY / "report-direct-closed-loop-recovery.json")
    arguments = parser.parse_args()

    destination = json.loads(arguments.destination.read_text())
    reference = json.loads(arguments.reference_report.read_text()).get("recovery_visual_anchor")
    preset = destination.get("preset") if isinstance(destination, dict) else None
    token = preset.get("token") if isinstance(preset, dict) else None
    report: dict[str, Any] = {
        "experiment_id": arguments.experiment_id,
        "attempt": "single_preserved_preset_visual_recovery",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "temporary_preset": {"was_preserved": True, "preserved": True, "removed": False},
    }
    if not isinstance(reference, dict) or not isinstance(token, str) or not token:
        report["outcome"] = "private_recovery_reference_unavailable"
        _atomic(arguments.output, report)
        print(json.dumps({"outcome": report["outcome"], "ptz_commands_issued": 0}, sort_keys=True))
        return
    before = _quiet_window(arguments.base_url, timeout_seconds=12.0)
    report["before_window"] = _without_image(before)
    current = before.get("representative")
    if not before.get("quiet") or not isinstance(current, dict):
        report["outcome"] = "current_view_not_quiet"
        _atomic(arguments.output, report)
        print(json.dumps({"outcome": report["outcome"], "ptz_commands_issued": 0}, sort_keys=True))
        return
    report["before_reference_match"] = match_visual_anchor(reference, current["image"])
    recalled = _request_json(
        arguments.base_url,
        f"/api/cameras/cameras/{CAMERA_ID}/ptz/goto-preset",
        method="POST",
        body={"source_id": SOURCE_ID, "preset_token": token},
        idempotency_key=f"{arguments.experiment_id.lower()}-single-preserved-return",
    )
    report["return_command"] = {key: value for key, value in recalled.items() if key != "body"}
    report["ptz_commands_issued"] = 1
    if not recalled.get("ok"):
        report["outcome"] = "return_command_not_confirmed"
    else:
        after = _quiet_window(arguments.base_url, timeout_seconds=12.0)
        report["after_window"] = _without_image(after)
        frame = after.get("representative")
        match = match_visual_anchor(reference, frame["image"]) if after.get("quiet") and isinstance(frame, dict) else None
        report["visual_return"] = match
        if after.get("quiet") and _exact(match):
            removed = _request_json(
                arguments.base_url,
                f"/api/cameras/cameras/{CAMERA_ID}/ptz/presets/{urllib.parse.quote(token, safe='')}?source_id={SOURCE_ID}",
                method="DELETE",
            )
            report["temporary_preset"]["remove_response"] = {key: value for key, value in removed.items() if key != "body"}
            report["temporary_preset"]["removed"] = bool(removed.get("ok"))
            if removed.get("ok"):
                arguments.destination.unlink(missing_ok=True)
                report["temporary_preset"]["preserved"] = False
                report["outcome"] = "preserved_preset_return_verified"
            else:
                report["outcome"] = "return_verified_preset_cleanup_failed"
        else:
            report["outcome"] = "preserved_preset_return_unverified"
    _atomic(arguments.output, report)
    print(json.dumps({"outcome": report["outcome"], "ptz_commands_issued": report["ptz_commands_issued"], "temporary_preset": report["temporary_preset"]}, sort_keys=True))


if __name__ == "__main__":
    main()
