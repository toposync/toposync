"""Read-only post-E6 PTZ status sample through the Toposync API."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
SOURCE_ID = "profile_1"


def _sample(base_url: str) -> dict[str, object]:
    query = urllib.parse.urlencode({"source_id": SOURCE_ID})
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/ptz/status?{query}", method="GET"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            body = json.loads(response.read().decode("utf-8"))
            raw = body.get("status") if isinstance(body, dict) else {}
            raw = raw if isinstance(raw, dict) else {}
            return {
                "ok": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "status": {
                    key: raw.get(key)
                    for key in ("pan", "tilt", "zoom", "move_status", "error", "pan_tilt_space", "zoom_space")
                },
            }
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "error": "ptz_status_request_failed"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    arguments = parser.parse_args()
    samples = []
    for _ in range(3):
        samples.append(_sample(arguments.base_url))
        time.sleep(0.25)
    position_available = all(
        sample.get("ok")
        and isinstance(sample.get("status"), dict)
        and sample["status"].get("pan") is not None
        and sample["status"].get("tilt") is not None
        for sample in samples
    )
    report = {
        "experiment_id": "E6",
        "attempt": "post_return_ptz_status_observation",
        "ptz_commands_issued": 0,
        "samples": samples,
        "reported_position_available": position_available,
        "usable_as_exact_return_evidence": False,
        "interpretation": "A readable PTZ position is auxiliary until repeatable correspondence to the camera image is independently demonstrated. This run did not establish that correspondence.",
    }
    (DIRECTORY / "report-post-return-status.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "reported_position_available": position_available,
        "usable_as_exact_return_evidence": False,
        "samples": samples,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
