"""Prepare, but do not apply, the minimal Garagem source rebind.

It exercises the production source schemas and credential resolution with live
ONVIF media metadata. Canonical URIs and credentials stay in memory and never
enter the report.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from toposync_ext_cameras.onvif.client import OnvifClient
from toposync_ext_cameras.settings import CameraSourceOrigin, get_camera_source_credentials
from toposync_ext_streaming.streaming.camera_ingest import camera_source_credentials


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
TARGETS = {"profile_1": "profile_1", "profile_2": "profile_2"}


async def _proposed_sources(camera: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    onvif = camera["onvif"]
    client = OnvifClient(
        xaddr=str(onvif.get("xaddr") or ""),
        username=str(onvif.get("username") or ""),
        password=str(onvif.get("password") or ""),
        timeout_s=5.0,
    )
    profiles = await client.get_profiles(str(onvif.get("media_xaddr") or ""))
    by_token = {profile.token: profile for profile in profiles}
    result: dict[str, dict[str, Any]] = {}
    checks: dict[str, Any] = {}
    for source_id, token in TARGETS.items():
        source = next(item for item in camera["sources"] if item.get("id") == source_id)
        profile = by_token[token]
        uri = await client.get_stream_uri(str(onvif.get("media_xaddr") or ""), profile_token=token)
        proposed = copy.deepcopy(source)
        proposed["origin"] = {
            "type": "onvif_profile",
            "rtsp_url": uri,
            "stream_username": "",
            "stream_password": "",
            "profile_token": token,
            "profile_name": profile.name,
            "has_ptz": bool(profile.has_ptz),
            "metadata": {},
        }
        normalized_origin = CameraSourceOrigin.model_validate(proposed["origin"]).model_dump(mode="json")
        proposed["origin"] = normalized_origin
        camera_credentials = get_camera_source_credentials(camera, proposed)
        ingest_credentials = camera_source_credentials(camera, proposed)
        configured_video = source.get("video") if isinstance(source.get("video"), dict) else {}
        checks[source_id] = {
            "profile_token": token,
            "profile_name": profile.name,
            "profile_video": {"width": profile.width, "height": profile.height, "fps": profile.fps, "encoding": profile.encoding},
            "configured_video": {
                "width": configured_video.get("width"),
                "height": configured_video.get("height"),
                "fps": configured_video.get("fps"),
                "codec": configured_video.get("codec"),
            },
            "dimensions_match": profile.width == configured_video.get("width") and profile.height == configured_video.get("height"),
            "canonical_uri_available": bool(uri),
            "camera_fallback_credentials_available": bool(camera_credentials[0] and camera_credentials[1]),
            "ingest_fallback_credentials_available": bool(ingest_credentials[0] and ingest_credentials[1]),
            "schema_valid": True,
        }
        result[source_id] = proposed
    return result, checks


def main() -> None:
    settings = json.loads(Path(".toposync-data/config.json").read_text())
    extension = settings["settings"]["extensions"]["com.toposync.cameras"]
    camera = next(device for device in extension["devices"] if device.get("id") == CAMERA_ID)
    proposed_sources, checks = asyncio.run(_proposed_sources(camera))
    report = {
        "camera": {"id": CAMERA_ID, "label": "Garagem"},
        "operation": "proposed_source_rebind_not_applied",
        "ptz_commands_issued": 0,
        "sources": checks,
        "mutation_scope": {
            "changed_source_ids": sorted(proposed_sources),
            "preserved_source_ids": ["profile_3"],
            "preserved_fields": ["source id", "role", "video metadata", "ingest settings", "camera control settings"],
            "origin_replacement": "onvif_profile with exact token, canonical URI and credential fallback",
        },
        "credentials_or_uris_persisted": False,
        "ready_to_apply": all(
            item["dimensions_match"]
            and item["canonical_uri_available"]
            and item["camera_fallback_credentials_available"]
            and item["ingest_fallback_credentials_available"]
            and item["schema_valid"]
            for item in checks.values()
        ),
    }
    (DIRECTORY / "report-rebind-preflight.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({"ready_to_apply": report["ready_to_apply"], "sources": checks}, sort_keys=True))


if __name__ == "__main__":
    main()
