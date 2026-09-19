"""One non-persistent stationary pair from canonical ONVIF profiles for E2."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from toposync_ext_cameras.onvif.client import OnvifClient
from toposync_ext_cameras.panorama_scan import _match
from toposync_ext_cameras.plugin import _ffmpeg_snapshot, _rtsp_url_with_auth


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
TOKENS = ("profile_1", "profile_2")


async def _capture_pair() -> dict[str, dict]:
    settings = json.loads(Path(".toposync-data/config.json").read_text())
    camera = next(
        device
        for device in settings["settings"]["extensions"]["com.toposync.cameras"]["devices"]
        if device.get("id") == CAMERA_ID
    )
    onvif = camera["onvif"]
    client = OnvifClient(
        xaddr=str(onvif.get("xaddr") or ""),
        username=str(onvif.get("username") or ""),
        password=str(onvif.get("password") or ""),
        timeout_s=5.0,
    )
    captures = {}
    for token in TOKENS:
        uri = await client.get_stream_uri(str(onvif.get("media_xaddr") or ""), profile_token=token)
        authenticated = _rtsp_url_with_auth(
            uri, str(onvif.get("username") or ""), str(onvif.get("password") or "")
        )
        snapshot = await _ffmpeg_snapshot(
            authenticated,
            timeout_ms=10000,
            transport_policy="tcp",
            capture_mode_policy="auto",
            codec_hint="H264",
        )
        image = cv2.imdecode(np.frombuffer(snapshot.blob, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"Canonical {token} snapshot was undecodable")
        captures[token] = {
            "image": image,
            "sha256": hashlib.sha256(snapshot.blob).hexdigest(),
            "dimensions": [int(image.shape[1]), int(image.shape[0])],
            "transport": snapshot.transport,
            "capture_mode": snapshot.capture_mode,
            "source": snapshot.source,
        }
    return captures


def main() -> None:
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    captures = asyncio.run(_capture_pair())
    main_image = cv2.resize(captures["profile_1"]["image"], (640, 360), interpolation=cv2.INTER_AREA)
    sub_image = cv2.resize(captures["profile_2"]["image"], (640, 360), interpolation=cv2.INTER_AREA)
    matching = _match(main_image, sub_image)
    report = {
        "experiment_id": "E2",
        "attempt": "canonical_stationary_pair",
        "camera": {"id": CAMERA_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "profiles": {
            token: {key: value for key, value in capture.items() if key != "image"}
            for token, capture in captures.items()
        },
        "visual_correspondence_normalized_640x360": {
            key: matching.get(key)
            for key in ("verified", "code", "inliers", "overlap", "shift_x", "shift_y", "displacement", "model_candidates")
        },
        "classification": {
            "value": "inconclusive",
            "compatible_stationary_view": bool(matching.get("verified")),
            "reason": "One stationary pair can support visual compatibility, but cannot prove equal field of view through pan, tilt, and zoom. The configured substream still lacks its exact profile binding.",
        },
    }
    (DIRECTORY / "report-canonical-stationary-pair.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report["classification"], sort_keys=True))


if __name__ == "__main__":
    main()
