"""E4 deterministic contract experiment for the production CameraHub."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from toposync_ext_cameras.processing.camera_hub import CameraHub


SCRIPT_DIRECTORY = Path(__file__).resolve().parent


@dataclass
class Reader:
    url: str
    target_fps: float
    backend: str
    stopped: bool = False

    starts: int = 0
    stops: int = 0

    def __init__(self, url: str, *, target_fps: float, backend: str) -> None:
        self.url = url
        self.target_fps = target_fps
        self.backend = backend
        self.stopped = False

    def start(self) -> "Reader":
        type(self).starts += 1
        return self

    def stop(self) -> None:
        self.stopped = True
        type(self).stops += 1

    def metrics_snapshot(self) -> dict[str, float]:
        return {"target_fps": self.target_fps}


async def _same_key(first_fps: float, second_fps: float) -> dict:
    Reader.starts = Reader.stops = 0
    hub = CameraHub(frame_grabber_factory=Reader)
    first = await hub.acquire(key="camera:garage:main", rtsp_url="rtsp://redacted/main", target_fps=first_fps, backend="auto")
    second = await hub.acquire(key="camera:garage:main", rtsp_url="rtsp://redacted/main", target_fps=second_fps, backend="auto")
    during = await hub.snapshot()
    await hub.release(key="camera:garage:main")
    after_one_release = await hub.snapshot()
    await hub.release(key="camera:garage:main")
    after_all_releases = await hub.snapshot()
    return {
        "first_request_fps": first_fps,
        "second_request_fps": second_fps,
        "same_grabber": first is second,
        "factory_starts": Reader.starts,
        "shared_target_fps": second.target_fps,
        "highest_requested_fps": max(first_fps, second_fps),
        "highest_requirement_met": second.target_fps >= max(first_fps, second_fps),
        "entries_during": len(during),
        "entries_after_one_release": len(after_one_release),
        "entries_after_all_releases": len(after_all_releases),
        "factory_stops": Reader.stops,
    }


async def _different_keys() -> dict:
    Reader.starts = Reader.stops = 0
    hub = CameraHub(frame_grabber_factory=Reader)
    first = await hub.acquire(key="camera:garage:main", rtsp_url="rtsp://redacted/main", target_fps=5.0, backend="auto")
    second = await hub.acquire(key="camera:garage:sub", rtsp_url="rtsp://redacted/sub", target_fps=5.0, backend="auto")
    await hub.release(key="camera:garage:main")
    await hub.release(key="camera:garage:sub")
    return {"same_grabber": first is second, "factory_starts": Reader.starts, "factory_stops": Reader.stops}


def main() -> None:
    low_then_high = asyncio.run(_same_key(5.0, 12.0))
    high_then_low = asyncio.run(_same_key(12.0, 5.0))
    different = asyncio.run(_different_keys())
    result = {
        "experiment_id": "E4",
        "camera_network_access": False,
        "ptz_commands_issued": 0,
        "same_key_low_then_high": low_then_high,
        "same_key_high_then_low": high_then_low,
        "different_keys": different,
        "decision": {
            "current_contract_passes": bool(low_then_high["highest_requirement_met"]),
            "failure_mode": (
                None if low_then_high["highest_requirement_met"]
                else "A low-frequency first consumer fixes the shared reader below a later panorama consumer requirement."
            ),
            "required_design": "Track active requested frequencies per hub key and restart/upgrade safely, or refuse sharing when the current decoder cannot meet the new requirement.",
        },
    }
    (SCRIPT_DIRECTORY / "report-after-frequency-upgrade.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
