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
from toposync_ext_cameras.processing.frame_grabber import CaptureFrameSample


def _create_client_with_cameras(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    warm_wait_ms: int = 50,
    snapshot_ttl_s: float = -1.0,
) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setenv("TOPOSYNC_CAMERA_SNAPSHOT_TTL_S", str(snapshot_ttl_s))
    monkeypatch.setenv("TOPOSYNC_CAMERA_SNAPSHOT_WARM_WAIT_MS", str(warm_wait_ms))
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


class _PublishingFakeGrabber:
    backend_name = "fake"

    def __init__(self, *, initial_frame_ts: float) -> None:
        self.initial_frame = np.zeros((8, 12, 3), dtype=np.uint8)
        self.initial_frame[:, :, 1] = 80
        self.published_frame = np.zeros((8, 12, 3), dtype=np.uint8)
        self.published_frame[:, :, 1] = 220
        self.initial_frame_ts = initial_frame_ts
        self.initial_capture_monotonic = time.monotonic() - 5.0
        self.post_request_frame_timestamps: list[float] = []
        self.post_request_capture_monotonic: list[float] = []
        self.get_latest_calls = 0

    def get_latest(self) -> tuple[Any, float]:
        sample = self.get_latest_sample()
        return sample.frame, sample.published_at

    def get_latest_sample(self) -> CaptureFrameSample:
        self.get_latest_calls += 1
        if self.get_latest_calls == 1:
            return CaptureFrameSample(
                frame=self.initial_frame,
                published_at=self.initial_frame_ts,
                source_received_at=self.initial_frame_ts,
                source_received_monotonic=self.initial_capture_monotonic,
                generation=1,
                sequence=1,
                captured_at=self.initial_frame_ts,
                captured_monotonic=self.initial_capture_monotonic,
                physical_capture_verified=True,
            )
        if len(self.post_request_frame_timestamps) < 2:
            previous = (
                self.post_request_frame_timestamps[-1]
                if self.post_request_frame_timestamps
                else 0.0
            )
            self.post_request_frame_timestamps.append(max(time.time(), previous + 0.001))
            previous_monotonic = (
                self.post_request_capture_monotonic[-1]
                if self.post_request_capture_monotonic
                else 0.0
            )
            self.post_request_capture_monotonic.append(
                max(time.monotonic(), previous_monotonic + 0.001)
            )
        published_at = self.post_request_frame_timestamps[-1]
        captured_monotonic = self.post_request_capture_monotonic[-1]
        return CaptureFrameSample(
            frame=self.published_frame,
            published_at=published_at,
            source_received_at=published_at,
            source_received_monotonic=captured_monotonic,
            generation=1,
            sequence=1 + len(self.post_request_frame_timestamps),
            captured_at=published_at,
            captured_monotonic=captured_monotonic,
            physical_capture_verified=True,
        )


class _PreRequestCapturePublishingFakeGrabber:
    backend_name = "fake"

    def __init__(self, *, physical_capture_verified: bool = True) -> None:
        self.frame = np.zeros((8, 12, 3), dtype=np.uint8)
        self.captured_at = time.time() - 5.0
        self.captured_monotonic = time.monotonic() - 5.0
        self.physical_capture_verified = physical_capture_verified
        self.get_latest_calls = 0
        self.published_timestamps: list[float] = []

    def get_latest(self) -> tuple[Any, float]:
        sample = self.get_latest_sample()
        return sample.frame, sample.published_at

    def get_latest_sample(self) -> CaptureFrameSample:
        self.get_latest_calls += 1
        sequence = min(self.get_latest_calls, 3)
        if len(self.published_timestamps) < 3:
            previous = self.published_timestamps[-1] if self.published_timestamps else 0.0
            self.published_timestamps.append(max(time.time(), previous + 0.001))
        published_at = self.published_timestamps[sequence - 1]
        return CaptureFrameSample(
            frame=self.frame,
            published_at=published_at,
            source_received_at=published_at,
            source_received_monotonic=time.monotonic(),
            generation=1,
            sequence=sequence,
            captured_at=self.captured_at,
            captured_monotonic=self.captured_monotonic,
            physical_capture_verified=self.physical_capture_verified,
        )


class _UnverifiedFakeGrabber:
    backend_name = "fake"

    def __init__(self) -> None:
        self.frame = np.zeros((8, 12, 3), dtype=np.uint8)
        self.published_at = time.time()
        self.source_received_at = self.published_at - 0.001
        self.source_received_monotonic = time.monotonic()

    def get_latest(self) -> tuple[Any, float]:
        return self.frame, self.published_at

    def get_latest_sample(self) -> CaptureFrameSample:
        return CaptureFrameSample(
            frame=self.frame,
            published_at=self.published_at,
            source_received_at=self.source_received_at,
            source_received_monotonic=self.source_received_monotonic,
            generation=1,
            sequence=1,
        )


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


def test_camera_snapshot_fresh_drains_two_frames_newer_than_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_frame_ts = time.time() - 5.0
    grabber = _PublishingFakeGrabber(initial_frame_ts=initial_frame_ts)
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = grabber
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    async def fail_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        raise AssertionError("fresh snapshot should wait for the camera hub frame")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", fail_ffmpeg)

    with _create_client_with_cameras(
        tmp_path,
        monkeypatch,
        warm_wait_ms=300,
        snapshot_ttl_s=10.0,
    ) as client:
        _save_rtsp_camera(client)
        request_started_at = time.time()

        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=true"
        )
        cached_response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=false"
        )

    assert response.status_code == 200
    assert grabber.get_latest_calls == 3
    assert len(grabber.post_request_frame_timestamps) == 2
    first_frame_ts, second_frame_ts = grabber.post_request_frame_timestamps
    response_frame_ts = float(response.headers["x-toposync-snapshot-frame-timestamp"])
    assert initial_frame_ts < request_started_at < first_frame_ts < second_frame_ts
    assert response_frame_ts == pytest.approx(second_frame_ts, abs=1e-6)
    assert response.headers["x-toposync-snapshot-freshness"] == "verified"
    assert response.headers["x-toposync-snapshot-frame-generation"] == "1"
    assert response.headers["x-toposync-snapshot-frame-sequence"] == "3"
    assert float(response.headers["x-toposync-snapshot-captured-timestamp"]) == pytest.approx(
        second_frame_ts,
        abs=1e-6,
    )
    assert cached_response.status_code == 200
    assert cached_response.headers["x-toposync-snapshot-capture-evidence"] == "verified"
    assert "x-toposync-snapshot-freshness" not in cached_response.headers


def test_camera_snapshot_fresh_rejects_frames_captured_before_request_even_when_published_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grabber = _PreRequestCapturePublishingFakeGrabber()
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = grabber
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    async def fail_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        raise AssertionError("fresh snapshots must not use an unverified FFmpeg frame")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", fail_ffmpeg)

    with _create_client_with_cameras(tmp_path, monkeypatch, warm_wait_ms=250) as client:
        _save_rtsp_camera(client)
        request_started_at = time.time()
        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=true"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Fresh camera frame is temporarily unavailable"
    assert grabber.get_latest_calls >= 3
    assert grabber.captured_at < request_started_at
    assert len(grabber.published_timestamps) == 3
    assert all(published_at > request_started_at for published_at in grabber.published_timestamps)


def test_camera_snapshot_decoder_fresh_accepts_new_decoder_frames_without_physical_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grabber = _PreRequestCapturePublishingFakeGrabber(physical_capture_verified=False)
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = grabber
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    with _create_client_with_cameras(tmp_path, monkeypatch, warm_wait_ms=250) as client:
        _save_rtsp_camera(client)
        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=true&freshness=decoder"
        )

    assert response.status_code == 200
    assert response.headers["x-toposync-snapshot-capture-evidence"] == "unverified"
    assert response.headers["x-toposync-snapshot-freshness"] == "decoder"
    assert int(response.headers["x-toposync-snapshot-frame-sequence"]) >= 2
    assert grabber.get_latest_calls >= 2


def test_camera_snapshot_not_fresh_can_use_latest_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_frame_ts = time.time() - 5.0
    grabber = _PublishingFakeGrabber(initial_frame_ts=initial_frame_ts)
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = grabber
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    async def fail_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        raise AssertionError("latest camera hub frame should be used without FFmpeg")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", fail_ffmpeg)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        _save_rtsp_camera(client)

        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=false"
        )

    assert response.status_code == 200
    assert grabber.get_latest_calls == 1
    assert grabber.post_request_frame_timestamps == []
    assert float(response.headers["x-toposync-snapshot-frame-timestamp"]) == pytest.approx(
        initial_frame_ts,
        abs=1e-6,
    )


def test_camera_snapshot_fresh_declares_unverifiable_capture_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = _UnverifiedFakeGrabber()
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)
    ffmpeg_calls = 0

    async def count_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        nonlocal ffmpeg_calls
        ffmpeg_calls += 1
        raise AssertionError("fresh snapshots must not use an unverified FFmpeg frame")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", count_ffmpeg)

    with _create_client_with_cameras(tmp_path, monkeypatch, warm_wait_ms=10) as client:
        _save_rtsp_camera(client)
        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=true"
        )

    assert response.status_code == 503
    assert response.headers["x-toposync-snapshot-capture-evidence"] == "unverified"
    assert response.headers["x-toposync-snapshot-freshness"] == "unverifiable"
    assert response.json()["detail"] == (
        "Physical camera capture freshness cannot be verified by the active decoder; "
        "visual calibration was not allowed"
    )
    assert ffmpeg_calls == 0


def test_camera_snapshot_not_fresh_preserves_unverified_decoder_frame_and_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grabber = _UnverifiedFakeGrabber()
    fake_hub = _FakeCameraHub()
    fake_hub.grabber = grabber
    monkeypatch.setattr(cameras_plugin, "get_global_camera_hub", lambda: fake_hub)

    async def fail_ffmpeg(*_args: Any, **_kwargs: Any) -> cameras_plugin.RtspSnapshotResult:
        raise AssertionError("normal snapshots should keep using the warm decoder frame")

    monkeypatch.setattr(cameras_plugin, "_ffmpeg_snapshot", fail_ffmpeg)

    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        _save_rtsp_camera(client)
        response = client.get(
            "/api/cameras/cameras/lab_rtsp_camera/snapshot?source_id=main&fresh=false"
        )

    assert response.status_code == 200
    assert response.headers["x-toposync-snapshot-capture-evidence"] == "unverified"
    assert "x-toposync-snapshot-freshness" not in response.headers
    assert response.headers["x-toposync-snapshot-frame-generation"] == "1"
    assert response.headers["x-toposync-snapshot-frame-sequence"] == "1"
    assert float(
        response.headers["x-toposync-snapshot-source-received-timestamp"]
    ) == pytest.approx(grabber.source_received_at, abs=1e-6)
    assert float(
        response.headers["x-toposync-snapshot-source-received-monotonic"]
    ) == pytest.approx(grabber.source_received_monotonic, abs=1e-6)


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
