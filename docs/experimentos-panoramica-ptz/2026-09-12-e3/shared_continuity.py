"""E3 shared-decoder continuity sample through the Toposync snapshot API only."""

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
ALLOWED_HEADERS = {
    "x-toposync-snapshot-backend",
    "x-toposync-snapshot-capture-evidence",
    "x-toposync-snapshot-frame-generation",
    "x-toposync-snapshot-frame-sequence",
    "x-toposync-snapshot-freshness",
    "x-toposync-snapshot-transport",
}


def _snapshot(base_url: str) -> dict[str, object]:
    query = urllib.parse.urlencode(
        {"source_id": SOURCE_ID, "fresh": "true", "freshness": "decoder"}
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/snapshot?{query}", method="GET"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            response.read()
            headers = {
                name.lower(): value
                for name, value in response.headers.items()
                if name.lower() in ALLOWED_HEADERS
            }
            return {
                "ok": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "sequence": int(headers.get("x-toposync-snapshot-frame-sequence") or 0),
                "generation": int(headers.get("x-toposync-snapshot-frame-generation") or 0),
                "headers": headers,
            }
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "error": "snapshot_request_failed"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--frames", type=int, default=10)
    arguments = parser.parse_args()
    if not 6 <= arguments.frames <= 16:
        raise SystemExit("--frames must be between 6 and 16")

    warm = _snapshot(arguments.base_url)
    samples = []
    completed_at = []
    for _ in range(arguments.frames):
        sample = _snapshot(arguments.base_url)
        samples.append(sample)
        completed_at.append(time.monotonic())
        time.sleep(0.12)

    intervals_ms = [round((later - earlier) * 1000, 1) for earlier, later in zip(completed_at, completed_at[1:])]
    sequences = [sample["sequence"] for sample in samples if sample.get("ok") and isinstance(sample.get("sequence"), int)]
    sequence_steps = [later - earlier for earlier, later in zip(sequences, sequences[1:])]
    report = {
        "experiment_id": "E3",
        "attempt": "shared_decoder_continuity_after_rebind",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "source_id": SOURCE_ID},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "cold_or_reacquire_sample": warm,
        "warm_samples": samples,
        "completion_intervals_ms": intervals_ms,
        "sequence_steps": sequence_steps,
        "summary": {
            "all_warm_samples_obtained": all(sample.get("ok") for sample in samples),
            "distinct_sequence_steps": sum(1 for step in sequence_steps if step > 0),
            "sample_count": len(samples),
            "largest_completion_interval_ms": max(intervals_ms, default=None),
            "largest_sequence_step": max(sequence_steps, default=None),
        },
        "evidence_limit": "Decoder sequence progression proves fresh decoded frames in this process; it does not prove physical exposure time or compare the cost of a second direct RTSP connection.",
    }
    (DIRECTORY / "report-shared-continuity.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
