from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any
from urllib import error as urllib_error

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import (
    MediaMtxEngineManager,
    MediaMtxEngineStatus,
    MediaMtxPorts,
)
from toposync_ext_streaming.streaming.publisher_manager import PublisherManager
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


class _EngineManagerStub(MediaMtxEngineManager):
    def __init__(self, *, data_dir: Path, running: bool) -> None:
        super().__init__(data_dir=data_dir)
        self._running = bool(running)

    async def get_status(self) -> MediaMtxEngineStatus:
        return MediaMtxEngineStatus(
            running=self._running,
            pid=123 if self._running else None,
            uptime_seconds=12.0 if self._running else None,
            started_at_unix=1_700_000_000.0 if self._running else None,
            bind_host="127.0.0.1",
            ports=MediaMtxPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997, rtp=50000, rtcp=50001),
            last_error=None,
            mediamtx_version="test",
            platform="test",
            binary_path=None,
            config_path=None,
            log_path=None,
            test_path="test",
            warnings=(),
            restart_count=0,
        )


class _UrlOpenResponse:
    def __init__(self, *, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self) -> "_UrlOpenResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    def read(self) -> bytes:
        return self._body


def _create_client(tmp_path: Path, *, engine_running: bool = True) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = _EngineManagerStub(
        data_dir=paths.data_dir, running=engine_running
    )
    app.state.streaming_runtime_state = TransmissionRuntimeState()
    app.state.streaming_publisher_manager = PublisherManager(data_dir=paths.data_dir)
    app.include_router(create_streaming_router())
    return TestClient(app)


def _create_transmission(client: TestClient, *, outputs: list[dict[str, Any]]) -> str:
    response = client.post(
        "/api/streams/transmissions",
        json={
            "name": "Probe stream",
            "path": "probe-stream",
            "enabled": True,
            "outputs": outputs,
        },
    )
    assert response.status_code == 200
    return str(response.json()["id"])


def test_hls_probe_reports_ok_for_reachable_playlist_and_tail(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        url = str(request.full_url)
        if url.endswith("/probe-stream/index.m3u8"):
            return _UrlOpenResponse(
                status=200,
                body="#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n#EXTINF:2,\nseg7.ts\n",
            )
        if url.endswith("/probe-stream/seg7.ts"):
            return _UrlOpenResponse(status=206, body="")
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("toposync_ext_streaming.api.routes.urllib_request.urlopen", fake_urlopen)
    with _create_client(tmp_path) as client:
        transmission_id = _create_transmission(
            client,
            outputs=[
                {
                    "id": "hls_main",
                    "protocol": "hls",
                    "enabled": True,
                    "resolution": {"width": 320, "height": 180},
                }
            ],
        )

        response = client.get(f"/api/streams/transmissions/{transmission_id}/hls/probe")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["output_id"] == "hls_main"
    assert payload["playlist_reachable"] is True
    assert payload["media_sequence"] == 7
    assert payload["target_duration_seconds"] == 2.0
    assert payload["tail_segment_http_status"] == 206
    assert payload["tail_segment_reachable"] is True


def test_hls_probe_reports_no_hls_output(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        transmission_id = _create_transmission(
            client,
            outputs=[{"id": "webrtc", "protocol": "webrtc", "enabled": True}],
        )

        response = client.get(f"/api/streams/transmissions/{transmission_id}/hls/probe")

    assert response.status_code == 200
    assert response.json()["status"] == "no_hls_output"


def test_hls_probe_reports_engine_stopped(tmp_path: Path) -> None:
    with _create_client(tmp_path, engine_running=False) as client:
        transmission_id = _create_transmission(
            client,
            outputs=[{"id": "hls_main", "protocol": "hls", "enabled": True}],
        )

        response = client.get(f"/api/streams/transmissions/{transmission_id}/hls/probe")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "engine_stopped"
    assert payload["url"].endswith("/probe-stream/index.m3u8")


def test_hls_probe_reports_playlist_unreachable(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        url = str(request.full_url)
        raise urllib_error.HTTPError(url, 503, "Unavailable", hdrs={}, fp=None)

    monkeypatch.setattr("toposync_ext_streaming.api.routes.urllib_request.urlopen", fake_urlopen)
    with _create_client(tmp_path) as client:
        transmission_id = _create_transmission(
            client,
            outputs=[{"id": "hls_main", "protocol": "hls", "enabled": True}],
        )

        response = client.get(f"/api/streams/transmissions/{transmission_id}/hls/probe")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "playlist_unreachable"
    assert payload["playlist_reachable"] is False


def test_hls_probe_reports_tail_unavailable(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        url = str(request.full_url)
        if url.endswith("/probe-stream/index.m3u8"):
            return _UrlOpenResponse(
                status=200,
                body="#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:8\n#EXTINF:2,\nseg8.ts\n",
            )
        if url.endswith("/probe-stream/seg8.ts"):
            raise urllib_error.HTTPError(url, 404, "Not found", hdrs={}, fp=None)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("toposync_ext_streaming.api.routes.urllib_request.urlopen", fake_urlopen)
    with _create_client(tmp_path) as client:
        transmission_id = _create_transmission(
            client,
            outputs=[{"id": "hls_main", "protocol": "hls", "enabled": True}],
        )

        response = client.get(f"/api/streams/transmissions/{transmission_id}/hls/probe")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "tail_unavailable"
    assert payload["playlist_reachable"] is True
    assert payload["tail_segment_http_status"] == 404
    assert payload["tail_segment_reachable"] is False
