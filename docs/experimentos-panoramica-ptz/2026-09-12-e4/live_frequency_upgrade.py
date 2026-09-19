"""E4 production-service frequency upgrade sample with no PTZ operation."""

from __future__ import annotations

import asyncio
import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync_ext_cameras.capture_service import CameraCaptureRequest
from toposync_ext_cameras.pipelines.operators import get_global_camera_capture_service
from toposync_ext_cameras.settings import (
    get_camera_device,
    get_camera_source,
    get_camera_source_credentials,
    get_camera_source_origin,
    normalize_cameras_settings,
)


DIRECTORY = Path(__file__).resolve().parent
DATA_DIRECTORY = Path(".toposync-data")
CAMERA_ID = "camera_3_177980"
SOURCE_ID = "profile_1"


def _safe_error(error: BaseException) -> str:
    value = str(error)
    value = re.sub(r"(?:https?|rtsp)://[^\s\"']+", "[endpoint]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", value)
    return value[:300]


async def _fresh_frame(service: Any, lease_id: str, *, timeout_seconds: float = 5.0) -> tuple[Any, float]:
    started = time.monotonic()
    latest = await service.get_latest(lease_id)
    while not latest.fresh and time.monotonic() - started < timeout_seconds:
        await asyncio.sleep(0.2)
        latest = await service.get_latest(lease_id)
    return latest, round((time.monotonic() - started) * 1000, 1)


async def _run(*, configured_resolution: bool) -> dict[str, Any]:
    paths = UserDataPaths(
        data_dir=DATA_DIRECTORY,
        config_path=DATA_DIRECTORY / "config.json",
        files_dir=DATA_DIRECTORY / "files",
    )
    store = ConfigStore(paths=paths)
    await store.load()
    dependencies = PipelineRuntimeDependencies(config_store=store)
    service = get_global_camera_capture_service()
    settings = await store.get_settings()
    extension = normalize_cameras_settings(settings.extensions.get("com.toposync.cameras", {}))
    camera = get_camera_device(extension, camera_id=CAMERA_ID)
    source = get_camera_source(camera, source_id=SOURCE_ID, kind="video", enabled_only=True) if camera else None
    origin = get_camera_source_origin(source) if source else {}
    rtsp_url = str(origin.get("rtsp_url") or "").strip()
    username, password = get_camera_source_credentials(camera, source) if camera and source else ("", "")
    low = high = None
    report: dict[str, Any] = {
        "experiment_id": "E4",
        "attempt": "production_capture_service_frequency_upgrade",
        "camera": {"id": CAMERA_ID, "source_id": SOURCE_ID, "label": "Garagem"},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "source_resolution": (
            "configured_source" if configured_resolution
            else "direct_in_memory_from_configured_onvif_profile"
        ),
    }
    try:
        if not rtsp_url:
            raise RuntimeError("configured source did not provide a direct RTSP origin")
        source_arguments = (
            {}
            if configured_resolution
            else {"rtsp_url": rtsp_url, "username": username, "password": password}
        )
        low = await service.open(
            CameraCaptureRequest(
                owner_id="experiment:e4:low",
                camera_id=CAMERA_ID,
                source_id=SOURCE_ID,
                fps=5.0,
                **source_arguments,
            ),
            dependencies,
        )
        low_target_before = float(getattr(low.grabber, "target_fps", 0.0) or 0.0)
        high = await service.open(
            CameraCaptureRequest(
                owner_id="experiment:e4:high",
                camera_id=CAMERA_ID,
                source_id=SOURCE_ID,
                fps=12.0,
                **source_arguments,
            ),
            dependencies,
        )
        (low_frame, low_wait_ms), (high_frame, high_wait_ms) = await asyncio.gather(
            _fresh_frame(service, low.lease_id), _fresh_frame(service, high.lease_id)
        )
        report["result"] = {
            "same_proxy": low.grabber is high.grabber,
            "low_target_before_upgrade": low_target_before,
            "low_target_after_upgrade": float(getattr(low.grabber, "target_fps", 0.0) or 0.0),
            "high_target_after_upgrade": float(getattr(high.grabber, "target_fps", 0.0) or 0.0),
            "low_hub_key_equals_high": low.hub_key == high.hub_key,
            "low_frame": {
                "fresh": bool(low_frame.fresh),
                "dimensions": [low_frame.width, low_frame.height],
                "released": bool(low_frame.released),
                "waited_ms": low_wait_ms,
            },
            "high_frame": {
                "fresh": bool(high_frame.fresh),
                "dimensions": [high_frame.width, high_frame.height],
                "released": bool(high_frame.released),
                "waited_ms": high_wait_ms,
            },
        }
    except Exception as error:  # bounded live check, no retry
        report["result"] = {"error": _safe_error(error)}
    finally:
        for lease in (high, low):
            if lease is not None:
                await service.release(lease.lease_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configured-resolution", action="store_true")
    arguments = parser.parse_args()
    report = asyncio.run(_run(configured_resolution=arguments.configured_resolution))
    suffix = "configured" if arguments.configured_resolution else "direct"
    (DIRECTORY / f"report-live-frequency-upgrade-{suffix}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report.get("result", {}), sort_keys=True))


if __name__ == "__main__":
    main()
