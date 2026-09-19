"""Bounded E2 acquisition experiment using the running Toposync API.

The script deliberately issues no PTZ request. It retains neither camera image
nor endpoint/credential material: its report contains source dimensions,
non-temporal response evidence, hashes, and aggregate visual correspondence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from toposync_ext_cameras.onvif.client import OnvifClient, OnvifError
from toposync_ext_cameras.panorama_scan import _match


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
SOURCE_IDS = ("profile_1", "profile_2")
ALLOWED_HEADERS = {
    "content-type",
    "x-toposync-snapshot-backend",
    "x-toposync-snapshot-source",
    "x-toposync-snapshot-transport",
    "x-toposync-snapshot-mode",
    "x-toposync-snapshot-capture-evidence",
    "x-toposync-snapshot-freshness",
    "x-toposync-snapshot-frame-generation",
    "x-toposync-snapshot-frame-sequence",
}


def _safe_message(error: BaseException | str) -> str:
    message = str(error)
    message = re.sub(r"(?:https?|rtsp)://[^\s]+", "[endpoint]", message)
    message = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", message)
    message = re.sub(r"(?i)(password|username|token)=([^\s&]+)", r"\1=[redacted]", message)
    return message[:300]


def _read_camera() -> dict[str, Any]:
    settings = json.loads(Path(".toposync-data/config.json").read_text())
    devices = settings["settings"]["extensions"]["com.toposync.cameras"]["devices"]
    for device in devices:
        if isinstance(device, dict) and device.get("id") == CAMERA_ID:
            return device
    raise RuntimeError("Configured Garagem camera was not found")


def _source_summary(camera: dict[str, Any]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for source in camera.get("sources", []):
        if not isinstance(source, dict) or source.get("id") not in SOURCE_IDS:
            continue
        origin = source.get("origin") if isinstance(source.get("origin"), dict) else {}
        video = source.get("video") if isinstance(source.get("video"), dict) else {}
        summaries[str(source["id"])] = {
            "id": str(source["id"]),
            "name": str(source.get("name") or ""),
            "role": str(source.get("role") or ""),
            "enabled": bool(source.get("enabled", True)),
            "origin_type": str(origin.get("type") or ""),
            "origin_profile_name": str(origin.get("profile_name") or ""),
            "origin_profile_token": str(origin.get("profile_token") or ""),
            "origin_has_ptz": bool(origin.get("has_ptz", False)),
            "video": {
                "codec": str(video.get("codec") or ""),
                "width": video.get("width"),
                "height": video.get("height"),
                "fps": video.get("fps"),
            },
        }
    if set(summaries) != set(SOURCE_IDS):
        raise RuntimeError("Configured main/sub source pair is incomplete")
    return summaries


async def _onvif_profiles(camera: dict[str, Any]) -> dict[str, Any]:
    onvif = camera.get("onvif") if isinstance(camera.get("onvif"), dict) else {}
    client = OnvifClient(
        xaddr=str(onvif.get("xaddr") or ""),
        username=str(onvif.get("username") or ""),
        password=str(onvif.get("password") or ""),
        timeout_s=5.0,
    )
    try:
        profiles = await client.get_profiles(str(onvif.get("media_xaddr") or ""))
    except OnvifError as error:
        return {"completed": False, "error": _safe_message(error), "profiles": []}
    return {
        "completed": True,
        "profiles": [
            {
                "token": profile.token,
                "name": profile.name,
                "encoding": profile.encoding,
                "width": profile.width,
                "height": profile.height,
                "fps": profile.fps,
                "has_ptz": profile.has_ptz,
                "has_ptz_configuration": bool(profile.ptz_configuration_token),
            }
            for profile in profiles
        ],
    }


def _fetch_snapshot(base_url: str, source_id: str, *, freshness_mode: str) -> dict[str, Any]:
    query = "fresh=true&freshness=decoder" if freshness_mode == "decoder" else "fresh=false"
    endpoint = (
        f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/snapshot"
        f"?source_id={source_id}&{query}"
    )
    started = time.monotonic()
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=18.0) as response:
            blob = response.read()
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in ALLOWED_HEADERS
            }
            status = int(response.status)
    except urllib.error.HTTPError as error:
        return {
            "obtained": False,
            "status": int(error.code),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error": _safe_message(error.read().decode("utf-8", errors="replace")),
        }
    except Exception as error:  # bounded experiment needs failure evidence, not a retry loop
        return {
            "obtained": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error": _safe_message(error),
        }
    image = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return {
            "obtained": False,
            "status": status,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error": "Toposync snapshot response was not a decodable image",
        }
    return {
        "obtained": True,
        "status": status,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "dimensions": [int(image.shape[1]), int(image.shape[0])],
        "headers": headers,
        "image": image,
    }


def _match_summary(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    common_size = (640, 360)
    normalized_first = cv2.resize(first, common_size, interpolation=cv2.INTER_AREA)
    normalized_second = cv2.resize(second, common_size, interpolation=cv2.INTER_AREA)
    match = _match(normalized_first, normalized_second)
    return {
        key: match.get(key)
        for key in ("verified", "code", "analysis_size", "inliers", "overlap", "shift_x", "shift_y", "displacement", "model_candidates")
    }


def _binding(source: dict[str, Any], profiles: dict[str, Any]) -> dict[str, Any]:
    token = str(source.get("origin_profile_token") or "")
    name = str(source.get("origin_profile_name") or "")
    if not profiles.get("completed"):
        return {"status": "unavailable", "reason": "onvif_get_profiles_failed"}
    matched = [
        profile
        for profile in profiles["profiles"]
        if (token and profile["token"] == token) or (not token and name and profile["name"] == name)
    ]
    if token and len(matched) == 1:
        return {"status": "exact", "profile_token": matched[0]["token"]}
    if not token and not name:
        return {"status": "missing", "reason": "configured_source_has_no_onvif_profile_binding"}
    return {"status": "unresolved", "reason": "configured_binding_did_not_match_get_profiles"}


def _classification(
    main_binding: dict[str, Any],
    sub_binding: dict[str, Any],
    visual: dict[str, Any] | None,
) -> dict[str, str]:
    if main_binding["status"] != "exact" or sub_binding["status"] != "exact":
        return {
            "value": "inconclusive",
            "reason": "visual similarity cannot establish optical-profile equivalence without exact bindings for both configured streams",
        }
    if visual is not None and visual.get("verified"):
        return {
            "value": "inconclusive",
            "reason": "exact ONVIF bindings and one distributed stationary correspondence establish compatibility, but the E2 protocol still requires held-out directions before equivalence across the optical range",
        }
    return {
        "value": "inconclusive",
        "reason": "exact ONVIF bindings exist but stationary visual correspondence was not verified",
    }


def _without_image(sample: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sample.items() if key != "image"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--freshness-mode", choices=("decoder", "fallback"), default="decoder")
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    started = time.monotonic()
    camera = _read_camera()
    sources = _source_summary(camera)
    profiles = asyncio.run(_onvif_profiles(camera))
    samples = {
        source_id: _fetch_snapshot(
            arguments.base_url, source_id, freshness_mode=arguments.freshness_mode
        )
        for source_id in SOURCE_IDS
    }
    main = samples["profile_1"]
    sub = samples["profile_2"]
    visual = _match_summary(main["image"], sub["image"]) if main.get("obtained") and sub.get("obtained") else None
    bindings = {source_id: _binding(sources[source_id], profiles) for source_id in SOURCE_IDS}
    classification = _classification(bindings["profile_1"], bindings["profile_2"], visual)
    report = {
        "experiment_id": "E2",
        "executed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "camera": {"id": CAMERA_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "snapshot_freshness_mode": arguments.freshness_mode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "sources": sources,
        "onvif_get_profiles": profiles,
        "source_bindings": bindings,
        "samples": {source_id: _without_image(sample) for source_id, sample in samples.items()},
        "visual_correspondence_normalized_640x360": visual,
        "classification": classification,
        "acceptance": {
            "onvif_inventory_observed": bool(profiles.get("completed")),
            "both_samples_obtained": bool(main.get("obtained") and sub.get("obtained")),
            "no_camera_images_persisted": True,
        },
    }
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"classification": classification, "acceptance": report["acceptance"]}, sort_keys=True))


if __name__ == "__main__":
    main()
