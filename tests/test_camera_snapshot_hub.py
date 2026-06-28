from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
import toposync_ext_cameras.plugin as cameras_plugin


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_CAMERA_SNAPSHOT_TTL_S", "-1")
    monkeypatch.setenv("TOPOSYNC_CAMERA_SNAPSHOT_WARM_WAIT_MS", "50")
    monkeypatch.setenv("TOPOSYNC_CAMERA_SNAPSHOT_WARM_LEASE_TTL_S", "0.05")

    monkeypatch.setattr(
        ext_manager_mod,
        "_iter_entry_points",
        lambda _group: [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            ),
        ],
    )
    return TestClient(create_app())


def _save_rtsp_camera(client: TestClient) -> None:
    response = client.patch(
        "/api/settings/extensions/com.toposync.cameras",
        json={
            "schema_version": 4,
            "devices": [
                {
                    "id": "lab_rtsp_camera",
                    "name": "Lab RTSP",
                    "kind": "camera",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "name": "Main",
                            "kind": "video",
                            "enabled": True,
                            "is_default": True,
                            "role": "main",
                            "view_id": "main",
                            "origin": {
                                "type": "rtsp",
                                "rtsp_url": "rtsp://rtsp-server:8554/onboarding",
                            },
                            "video": {"width": 1280, "height": 720, "fps": 15, "codec": "H264"},
                            "ingest": {"mode": "direct", "host_server_id": "local"},
                            "metadata": {},
                        }
                    ],
                    "metadata": {},
                }
            ],
        },
    )
    assert response.status_code == 200


class _FakeGrabber:
    backend_name = "fake"

    def __init__(self) -> None:
        self.frame = np.zeros((8, 12, 3), dtype=np.uint8)
        self.frame[:, :, 1] = 180

    def get_latest(self) -> tuple[Any, float]:
        return self.frame, time.time()


class _FakeCameraHub:
    def __init__(self) -> None:
        self.grabber = _FakeGrabber()
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls: list[str] = []

    async def acquire(self, **kwargs: Any) -> _FakeGrabber:
        self.acquire_calls.append(dict(kwargs))
        return self.grabber

    async def release(self, *, key: str) -> None:
        self.release_calls.append(key)


def test_camera_snapshot_uses_warm_hub_reuses_lease_and_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_hub = _FakeCameraHub()
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    async def fail_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        raise AssertionError("camera snapshot should use the warm camera hub before FFmpeg")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", fail_ffmpeg)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        _save_rtsp_camera(client)

        first = client.get("/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main")
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/jpeg"
        assert first.content.startswith(b"\xff\xd8")
        assert first.headers["x-toposync-snapshot-backend"] == "camera-hub"
        assert first.headers["x-toposync-snapshot-transport"] == "shared"
        assert "x-toposync-snapshot-frame-age-seconds" in first.headers
        assert len(fake_hub.acquire_calls) == 1

        second = client.get("/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main")
        assert second.status_code == 200
        assert len(fake_hub.acquire_calls) == 1

        deadline = time.time() + 1.0
        while not fake_hub.release_calls and time.time() < deadline:
            time.sleep(0.02)
        assert fake_hub.release_calls == ["camera:lab_rtsp_camera:source:main:auto"]

        third = client.get("/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main")
        assert third.status_code == 200
        assert len(fake_hub.acquire_calls) == 2
