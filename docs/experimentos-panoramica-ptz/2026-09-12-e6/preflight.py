"""E6 preflight: verify status and video conditions before any motor command."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
E2_REPORT = DIRECTORY.parent / "2026-09-12-e2" / "report.json"
CAMERA_ID = "camera_3_177980"
SOURCE_ID = "profile_1"


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}", timeout=15.0) as response:
            return {"ok": True, "status_code": int(response.status), "body": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "error": "Toposync PTZ status request failed"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    arguments = parser.parse_args()
    status_response = _get_json(
        arguments.base_url,
        f"/api/cameras/cameras/{CAMERA_ID}/ptz/status?source_id={SOURCE_ID}",
    )
    raw_status = status_response.get("body", {}).get("status", {}) if status_response.get("ok") else {}
    e2 = json.loads(E2_REPORT.read_text())
    video_ready = bool(e2.get("acceptance", {}).get("both_samples_obtained"))
    status_summary = {
        key: raw_status.get(key)
        for key in ("pan", "tilt", "zoom", "move_status", "error", "pan_tilt_space", "zoom_space")
    }
    recoverable = bool(
        status_response.get("ok")
        and status_summary.get("error") in (None, "")
        and status_summary.get("move_status") not in {"MOVING", "UNKNOWN", "ERROR"}
        and status_summary.get("pan") is not None
        and status_summary.get("tilt") is not None
    )
    report = {
        "experiment_id": "E6",
        "attempt": "preflight_after_source_rebind",
        "camera": {"id": CAMERA_ID, "source_id": SOURCE_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "toposync_video_ready": video_ready,
        "ptz_status": status_summary,
        "recoverable_pre_move_state": recoverable,
        "decision": (
            "ready_for_one_bounded_motion" if video_ready and recoverable
            else "do_not_send_ptz_command"
        ),
    }
    (DIRECTORY / "report-preflight.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
