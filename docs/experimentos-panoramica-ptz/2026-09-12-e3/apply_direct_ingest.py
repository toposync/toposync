"""Atomically align Garagem ONVIF profile sources with direct local capture.

The script defaults to dry-run. Apply mode patches only the ingest mode of the
two already-bound ONVIF video sources, reads it back, and restores the previous
devices list if the sanitized readback is not exact.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from toposync_ext_cameras.settings import CamerasExtensionSettings


DIRECTORY = Path(__file__).resolve().parent
EXTENSION_ID = "com.toposync.cameras"
CAMERA_ID = "camera_3_177980"
SOURCE_IDS = {"profile_1", "profile_2"}


def _api_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=content, method=method)
    if content is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Toposync API returned HTTP {error.code}") from error


def _read_active_extension(base_url: str) -> dict[str, Any]:
    settings = _api_json(base_url, "/api/settings")
    extensions = settings.get("extensions") if isinstance(settings, dict) else None
    extension = extensions.get(EXTENSION_ID) if isinstance(extensions, dict) else None
    if not isinstance(extension, dict):
        raise RuntimeError("Active Toposync camera extension settings are unavailable")
    return copy.deepcopy(extension)


def _summary(extension: dict[str, Any]) -> dict[str, dict[str, Any]]:
    camera = next(
        (device for device in extension.get("devices", []) if device.get("id") == CAMERA_ID),
        None,
    )
    if not isinstance(camera, dict):
        raise RuntimeError("Configured Garagem camera was not found")
    result: dict[str, dict[str, Any]] = {}
    for source in camera.get("sources", []):
        if not isinstance(source, dict) or str(source.get("id") or "") not in SOURCE_IDS:
            continue
        origin = source.get("origin") if isinstance(source.get("origin"), dict) else {}
        ingest = source.get("ingest") if isinstance(source.get("ingest"), dict) else {}
        result[str(source["id"])] = {
            "origin_type": str(origin.get("type") or ""),
            "profile_token": str(origin.get("profile_token") or ""),
            "uri_present": bool(origin.get("rtsp_url")),
            "ingest_mode": str(ingest.get("mode") or ""),
        }
    if set(result) != SOURCE_IDS:
        raise RuntimeError("Configured Garagem main/sub source pair is incomplete")
    return result


def _identity(extension: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_summary(extension), sort_keys=True).encode("utf-8")).hexdigest()


def _propose(before: dict[str, Any]) -> dict[str, Any]:
    normalized = CamerasExtensionSettings.model_validate(before).model_dump(mode="json")
    camera = next(device for device in normalized["devices"] if device.get("id") == CAMERA_ID)
    for source in camera["sources"]:
        if str(source.get("id") or "") not in SOURCE_IDS:
            continue
        ingest = source.get("ingest") if isinstance(source.get("ingest"), dict) else {}
        source["ingest"] = {**ingest, "mode": "direct"}
    return CamerasExtensionSettings.model_validate(normalized).model_dump(mode="json")


def _apply(base_url: str, *, before: dict[str, Any], proposed: dict[str, Any]) -> tuple[bool, bool]:
    _api_json(
        base_url,
        f"/api/settings/extensions/{EXTENSION_ID}",
        method="PATCH",
        body={"devices": proposed["devices"]},
    )
    if _summary(_read_active_extension(base_url)) == _summary(proposed):
        return True, False
    _api_json(
        base_url,
        f"/api/settings/extensions/{EXTENSION_ID}",
        method="PATCH",
        body={"devices": before["devices"]},
    )
    return False, True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    before = _read_active_extension(arguments.base_url)
    proposed = _propose(before)
    applied = rolled_back = False
    if arguments.apply:
        applied, rolled_back = _apply(arguments.base_url, before=before, proposed=proposed)
    report = {
        "experiment_id": "E3",
        "operation": "garagem_direct_ingest_alignment",
        "mode": "apply" if arguments.apply else "dry_run",
        "ptz_commands_issued": 0,
        "before_identity": _identity(before),
        "proposed_identity": _identity(proposed),
        "before": _summary(before),
        "proposed": _summary(proposed),
        "applied": applied,
        "rolled_back_after_failed_readback": rolled_back,
        "uris_or_credentials_persisted": False,
    }
    suffix = "apply" if arguments.apply else "dry-run"
    (DIRECTORY / f"report-direct-ingest-{suffix}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
