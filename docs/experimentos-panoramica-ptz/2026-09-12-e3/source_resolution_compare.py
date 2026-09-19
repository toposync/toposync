"""Compare sanitized configured source routing from disk and the running Toposync API."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
SOURCE_ID = "profile_1"
EXTENSION_ID = "com.toposync.cameras"


def _summary(extension: Any) -> dict[str, Any]:
    devices = extension.get("devices") if isinstance(extension, dict) else []
    camera = next(
        (item for item in devices if isinstance(item, dict) and item.get("id") == CAMERA_ID),
        None,
    )
    sources = camera.get("sources") if isinstance(camera, dict) else []
    source = next(
        (item for item in sources if isinstance(item, dict) and item.get("id") == SOURCE_ID),
        None,
    )
    origin = source.get("origin") if isinstance(source, dict) and isinstance(source.get("origin"), dict) else {}
    ingest = source.get("ingest") if isinstance(source, dict) and isinstance(source.get("ingest"), dict) else {}
    return {
        "found": bool(source),
        "origin_type": str(origin.get("type") or ""),
        "profile_token": str(origin.get("profile_token") or ""),
        "uri_present": bool(origin.get("rtsp_url")),
        "ingest_mode": str(ingest.get("mode") or ""),
        "ingest_host_server_id": str(ingest.get("host_server_id") or ""),
    }


def _api_extension(base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/settings", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            extensions = payload.get("extensions") if isinstance(payload, dict) else {}
            return {"ok": True, "summary": _summary(extensions.get(EXTENSION_ID) if isinstance(extensions, dict) else {})}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status_code": int(error.code), "error": "settings_request_failed"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    arguments = parser.parse_args()
    local = json.loads(Path(".toposync-data/config.json").read_text())
    local_extension = local.get("settings", {}).get("extensions", {}).get(EXTENSION_ID, {})
    api = _api_extension(arguments.base_url)
    local_summary = _summary(local_extension)
    report = {
        "experiment_id": "E3",
        "attempt": "configured_source_resolution_comparison",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "local_config": local_summary,
        "running_api": api,
        "same_sanitized_routing": bool(api.get("ok") and api.get("summary") == local_summary),
    }
    (DIRECTORY / "report-source-resolution-compare.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "local_config": local_summary,
        "running_api": api,
        "same_sanitized_routing": report["same_sanitized_routing"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
