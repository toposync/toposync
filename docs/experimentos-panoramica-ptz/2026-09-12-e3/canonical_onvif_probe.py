"""Probe canonical ONVIF stream URIs without retaining them or moving PTZ."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from toposync_ext_cameras.onvif.client import OnvifClient, OnvifError
from toposync_ext_cameras.plugin import _ffmpeg_rtsp_probe, _rtsp_url_with_auth


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"
TOKENS = ("profile_1", "profile_2")


def _safe(value: object) -> str:
    text = re.sub(r"(?:https?|rtsp)://[^\s\"']+", "[endpoint]", str(value))
    return re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", text)[:260]


async def _run() -> dict:
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
    results = {}
    for token in TOKENS:
        try:
            uri = await client.get_stream_uri(
                str(onvif.get("media_xaddr") or ""), profile_token=token
            )
            authenticated_uri = _rtsp_url_with_auth(
                uri, str(onvif.get("username") or ""), str(onvif.get("password") or "")
            )
            probe = await _ffmpeg_rtsp_probe(authenticated_uri, timeout_ms=8000)
            results[token] = {
                "status": probe.status,
                "latency_ms": probe.latency_ms,
                "backend": probe.backend,
                "source": probe.source,
                "transports_tested": probe.transports_tested,
                "error": _safe(probe.error) if probe.error else None,
            }
        except (OnvifError, ValueError) as error:
            results[token] = {"status": "probe_error", "error": _safe(error)}
    return results


def main() -> None:
    report = {
        "experiment_id": "E3",
        "attempt": "canonical_onvif_stream_probe",
        "camera": {"id": CAMERA_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "profiles": asyncio.run(_run()),
        "uris_persisted": False,
    }
    (DIRECTORY / "report-canonical-onvif.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report["profiles"], sort_keys=True))


if __name__ == "__main__":
    main()
