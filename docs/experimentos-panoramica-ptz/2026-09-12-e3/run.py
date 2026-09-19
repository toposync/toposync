"""E3: compare Toposync acquisition paths without commanding the camera."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
E2_DIRECTORY = SCRIPT_DIRECTORY.parent / "2026-09-12-e2"
CAMERA_ID = "camera_3_177980"
SOURCE_IDS = ("profile_1", "profile_2")


def _safe(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?:https?|rtsp)://[^\s\"']+", "[endpoint]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", text)
    return text[:320]


def _request_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method=method)
    if encoded is not None:
        request.add_header("Content-Type", "application/json")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            raw = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "status_code": int(response.status), "elapsed_ms": round((time.monotonic()-started)*1000, 1), "body": raw}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "elapsed_ms": round((time.monotonic()-started)*1000, 1), "error": _safe(error.read().decode("utf-8", errors="replace"))}
    except Exception as error:
        return {"ok": False, "elapsed_ms": round((time.monotonic()-started)*1000, 1), "error": _safe(error)}


def _health_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or not isinstance(result.get("body"), dict):
        return {key: value for key, value in result.items() if key != "body"}
    sources = result["body"].get("sources")
    selected = []
    for item in sources if isinstance(sources, list) else []:
        if not isinstance(item, dict) or item.get("camera_id") != CAMERA_ID:
            continue
        selected.append({
            key: item.get(key)
            for key in (
                "camera_source_id", "backend", "configured_backend", "opened", "status",
                "capture_fps", "target_fps", "frames_captured", "restarts_total",
                "decode_failures", "rtsp_transport", "used_ingest", "ingest_mode",
                "ingest_warnings", "ingest_blocking_errors", "recommended_action",
            )
        })
    return {"ok": True, "status_code": result["status_code"], "sources": selected}


def _probe_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or not isinstance(result.get("body"), dict):
        return {key: value for key, value in result.items() if key != "body"}
    body = result["body"]
    return {
        "ok": True,
        "status_code": result["status_code"],
        "elapsed_ms": result["elapsed_ms"],
        "probe_status": body.get("status"),
        "probe_latency_ms": body.get("latency_ms"),
        "backend": body.get("backend"),
        "source": body.get("source"),
        "transports_tested": body.get("transports_tested"),
        "error": _safe(body.get("error")) if body.get("error") else None,
    }


def _e2_evidence() -> dict[str, Any]:
    decoder = json.loads((E2_DIRECTORY / "report-decoder-fresh.json").read_text())
    fallback = json.loads((E2_DIRECTORY / "report.json").read_text())
    return {
        "shared_decoder_fresh": {
            source_id: {key: decoder["samples"][source_id].get(key) for key in ("status", "error", "elapsed_ms")}
            for source_id in SOURCE_IDS
        },
        "centralized_ingest_snapshot_fallback": {
            source_id: {key: fallback["samples"][source_id].get(key) for key in ("status", "error", "elapsed_ms")}
            for source_id in SOURCE_IDS
        },
    }


def _conclusion(probes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    succeeded = [source_id for source_id, result in probes.items() if result.get("probe_status") == "ok"]
    if succeeded:
        return {
            "classification": "ingest_specific_failure",
            "reason": "At least one direct configured-origin probe succeeded while both shared and centralized-ingest snapshot attempts from E2 failed.",
            "directly_reachable_sources": succeeded,
        }
    return {
        "classification": "source_path_inconclusive",
        "reason": "No direct configured-origin probe succeeded; E2 already recorded shared and centralized-ingest failures.",
        "directly_reachable_sources": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    base_url = arguments.base_url.rstrip("/")
    health_before = _health_summary(_request_json(f"{base_url}/api/cameras/runtime/source-health"))
    probes = {
        source_id: _probe_summary(
            _request_json(
                f"{base_url}/api/cameras/cameras/{CAMERA_ID}/rtsp/probe",
                method="POST",
                body={"source_id": source_id, "timeout_ms": 8000},
            )
        )
        for source_id in SOURCE_IDS
    }
    health_after = _health_summary(_request_json(f"{base_url}/api/cameras/runtime/source-health"))
    report = {
        "experiment_id": "E3",
        "camera": {"id": CAMERA_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "reused_e2": _e2_evidence(),
        "health_before": health_before,
        "direct_configured_origin_probes": probes,
        "health_after": health_after,
        "conclusion": _conclusion(probes),
        "images_persisted": False,
    }
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"conclusion": report["conclusion"], "probes": probes}, sort_keys=True))


if __name__ == "__main__":
    main()
