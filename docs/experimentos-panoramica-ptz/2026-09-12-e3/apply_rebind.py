"""Apply the reviewed Garagem source rebind only with an explicit flag.

Default mode is dry-run. Apply mode builds exact ONVIF-bound sources from the
current active settings, performs one API patch, reads it back, and restores the
previous devices list if the readback does not match the requested binding.
No URI, credential, or full settings payload is written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from toposync_ext_cameras.onvif.client import OnvifClient
from toposync_ext_cameras.settings import CamerasExtensionSettings


DIRECTORY = Path(__file__).resolve().parent
EXTENSION_ID = "com.toposync.cameras"
CAMERA_ID = "camera_3_177980"
TARGETS = {"profile_1": "profile_1", "profile_2": "profile_2"}


def _api_json(base_url: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    content = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=content, method=method)
    if content is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Toposync API returned HTTP {error.code}") from error


def _local_extension() -> dict[str, Any]:
    payload = json.loads(Path(".toposync-data/config.json").read_text())
    return copy.deepcopy(payload["settings"]["extensions"][EXTENSION_ID])


async def _propose(extension: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = CamerasExtensionSettings.model_validate(extension).model_dump(mode="json")
    camera = next(device for device in normalized["devices"] if device.get("id") == CAMERA_ID)
    onvif = camera["onvif"]
    client = OnvifClient(
        xaddr=str(onvif.get("xaddr") or ""),
        username=str(onvif.get("username") or ""),
        password=str(onvif.get("password") or ""),
        timeout_s=5.0,
    )
    profiles = {profile.token: profile for profile in await client.get_profiles(str(onvif.get("media_xaddr") or ""))}
    summary: dict[str, Any] = {}
    for source in camera["sources"]:
        source_id = str(source.get("id") or "")
        if source_id not in TARGETS:
            continue
        token = TARGETS[source_id]
        profile = profiles.get(token)
        if profile is None:
            raise RuntimeError(f"ONVIF profile '{token}' is no longer available")
        uri = await client.get_stream_uri(str(onvif.get("media_xaddr") or ""), profile_token=token)
        if not uri:
            raise RuntimeError(f"ONVIF profile '{token}' returned no URI")
        video = source.get("video") if isinstance(source.get("video"), dict) else {}
        if int(profile.width or 0) != int(video.get("width") or 0) or int(profile.height or 0) != int(video.get("height") or 0):
            raise RuntimeError(f"ONVIF profile '{token}' dimensions differ from configured source '{source_id}'")
        source["origin"] = {
            "type": "onvif_profile",
            "rtsp_url": uri,
            "stream_username": "",
            "stream_password": "",
            "profile_token": token,
            "profile_name": profile.name,
            "has_ptz": bool(profile.has_ptz),
            "metadata": {},
        }
        summary[source_id] = {
            "profile_token": token,
            "profile_name": profile.name,
            "dimensions": [profile.width, profile.height],
            "fps": profile.fps,
            "canonical_uri_received": True,
        }
    proposed = CamerasExtensionSettings.model_validate(normalized).model_dump(mode="json")
    return proposed, summary


def _camera_summary(extension: dict[str, Any]) -> dict[str, Any]:
    camera = next(device for device in extension["devices"] if device.get("id") == CAMERA_ID)
    return {
        str(source.get("id") or ""): {
            "origin_type": str((source.get("origin") or {}).get("type") or ""),
            "profile_token": str((source.get("origin") or {}).get("profile_token") or ""),
            "uri_present": bool((source.get("origin") or {}).get("rtsp_url")),
            "stream_credentials_present": bool(
                (source.get("origin") or {}).get("stream_username")
                or (source.get("origin") or {}).get("stream_password")
            ),
        }
        for source in camera["sources"]
        if str(source.get("id") or "") in TARGETS
    }


def _identity(extension: dict[str, Any]) -> str:
    serializable = json.dumps(_camera_summary(extension), sort_keys=True).encode("utf-8")
    return hashlib.sha256(serializable).hexdigest()


def _read_active_extension(base_url: str) -> dict[str, Any]:
    settings = _api_json(base_url, "/api/settings")
    extensions = settings.get("extensions") if isinstance(settings, dict) else None
    if not isinstance(extensions, dict) or not isinstance(extensions.get(EXTENSION_ID), dict):
        raise RuntimeError("Active Toposync camera extension settings are unavailable")
    return copy.deepcopy(extensions[EXTENSION_ID])


def _apply(base_url: str, *, before: dict[str, Any], proposed: dict[str, Any]) -> tuple[bool, bool]:
    _api_json(
        base_url,
        f"/api/settings/extensions/{EXTENSION_ID}",
        method="PATCH",
        body={"devices": proposed["devices"]},
    )
    readback = _read_active_extension(base_url)
    expected = _camera_summary(proposed)
    if _camera_summary(readback) == expected:
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
    before = _read_active_extension(arguments.base_url) if arguments.apply else _local_extension()
    proposed, sources = asyncio.run(_propose(before))
    applied = False
    rolled_back = False
    if arguments.apply:
        applied, rolled_back = _apply(arguments.base_url, before=before, proposed=proposed)
    report = {
        "operation": "garagem_onvif_source_rebind",
        "mode": "apply" if arguments.apply else "dry_run",
        "ptz_commands_issued": 0,
        "source_identity_before": _identity(before),
        "source_identity_proposed": _identity(proposed),
        "sources": sources,
        "applied": applied,
        "rolled_back_after_failed_readback": rolled_back,
        "uris_or_credentials_persisted": False,
    }
    suffix = "apply" if arguments.apply else "dry-run"
    (DIRECTORY / f"report-rebind-{suffix}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
