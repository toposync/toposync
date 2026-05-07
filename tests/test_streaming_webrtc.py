from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import (
    MediaMtxEngineManager,
    MediaMtxEngineStatus,
    MediaMtxPorts,
)
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config


class _WriterBridgeStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def prime_transmission_demand(self, transmission_id: str, *, ttl_s: float | None = None) -> int:
        self.calls.append((transmission_id, ttl_s))
        return 1


class _EngineManagerStub(MediaMtxEngineManager):
    def __init__(self, *, data_dir: Path, running: bool = True, ports: MediaMtxPorts | None = None) -> None:
        super().__init__(data_dir=data_dir)
        self._running = running
        self._stub_ports = ports or MediaMtxPorts(
            rtsp=18758,
            hls=18759,
            webrtc=18760,
            api=18761,
            rtp=50000,
            rtcp=50001,
        )

    async def get_status(self) -> MediaMtxEngineStatus:
        return MediaMtxEngineStatus(
            running=self._running,
            pid=123 if self._running else None,
            uptime_seconds=12.0 if self._running else None,
            started_at_unix=1_700_000_000.0 if self._running else None,
            bind_host="127.0.0.1",
            ports=self._stub_ports,
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
    status = 200
    headers = {"content-type": "application/vnd.apple.mpegurl"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_UrlOpenResponse":
        return self

    def __exit__(self, *args: object) -> None:
        _ = args

    def read(self) -> bytes:
        return self._body


def _create_client(
    tmp_path: Path,
    *,
    engine_manager: MediaMtxEngineManager | None = None,
) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = engine_manager or MediaMtxEngineManager(
        data_dir=paths.data_dir
    )
    app.include_router(create_streaming_router())
    return TestClient(app)


def test_transmission_urls_include_webrtc_whep_url(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "WebRTC stream",
                "path": "camera-main",
                "outputs": [
                    {
                        "id": "main_webrtc",
                        "protocol": "webrtc",
                        "enabled": True,
                    }
                ],
            },
        )
        assert created_res.status_code == 200
        created = created_res.json()
        transmission_id = str(created["id"])
        transmission_path = str(created["path"])

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")
        assert urls_res.status_code == 200
        payload = urls_res.json()

        outputs = payload.get("outputs")
        assert isinstance(outputs, list)
        assert len(outputs) == 1
        output = outputs[0]
        assert output["protocol"] == "webrtc"
        assert output["resolved_engine_path"] == transmission_path
        assert output["url"] == f"http://127.0.0.1:8889/{transmission_path}/whep"
        assert "Engine is not running. URLs are based on preferred ports." in payload.get("warnings", [])


def test_transmission_urls_use_request_host_when_exposed_to_lan(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        settings_res = client.patch(
            "/api/streams/settings",
            json={
                "engine": {
                    "expose_to_lan": True,
                    "preferred_ports": {"hls": 18759, "webrtc": 18760},
                }
            },
        )
        assert settings_res.status_code == 200

        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "LAN stream",
                "path": "lan-main",
                "outputs": [{"id": "main_webrtc", "protocol": "webrtc", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls",
            headers={"host": "192.168.0.100:18756"},
        )
        assert urls_res.status_code == 200
        payload = urls_res.json()

        assert payload["outputs"][0]["url"] == "http://192.168.0.100:18760/lan-main/whep"


def test_home_assistant_contract_returns_proxied_hls_url(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_DIRECT_API_PORT", "18756")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_RTSP_PORT", "18758")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_HLS_PORT", "18759")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_WEBRTC_PORT", "18760")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_WEBRTC_UDP_PORT", "18762")
    monkeypatch.setenv("TOPOSYNC_STREAMING_WEBRTC_LOCAL_UDP_ADDRESS", ":18762")
    monkeypatch.setenv("TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH", "1")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    engine_manager = _EngineManagerStub(
        data_dir=tmp_path / "data",
        ports=MediaMtxPorts(
            rtsp=18758,
            hls=18888,
            webrtc=18760,
            api=18761,
            rtp=50000,
            rtcp=50001,
        ),
    )

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "LAN HLS stream",
                "path": "lan-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls",
            headers={"host": "192.168.0.100:18756"},
        )

    assert urls_res.status_code == 200
    payload = urls_res.json()
    assert payload["outputs"][0]["url"] == (
        "http://192.168.0.100:18756/api/streams/media/hls/lan-main/index.m3u8"
    )
    assert payload["blocking_errors"] == []
    assert payload["network_contract"]["status"] == "ok"
    assert payload["network_contract"]["public_hls_mode"] == "proxy"
    assert payload["network_contract"]["actual_ports"]["hls"] == 18888


def test_hls_output_is_omitted_when_direct_hls_port_mismatches_in_fail_mode(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_HLS_PORT", "18759")
    monkeypatch.setenv("TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH", "1")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "direct")
    engine_manager = _EngineManagerStub(
        data_dir=tmp_path / "data",
        ports=MediaMtxPorts(
            rtsp=18758,
            hls=18888,
            webrtc=18760,
            api=18761,
            rtp=50000,
            rtcp=50001,
        ),
    )

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Mismatched HLS stream",
                "path": "mismatch-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")

    assert urls_res.status_code == 200
    payload = urls_res.json()
    assert payload["outputs"] == []
    assert payload["network_contract"]["status"] == "port_mismatch"
    assert payload["blocking_errors"] == [
        "HLS active port 18888 does not match expected add-on port 18759."
    ]


def test_direct_streaming_port_mismatch_is_reported_in_network_contract(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_RTSP_PORT", "18758")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_WEBRTC_PORT", "18760")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    engine_manager = _EngineManagerStub(
        data_dir=tmp_path / "data",
        ports=MediaMtxPorts(
            rtsp=19998,
            hls=18759,
            webrtc=19999,
            api=18761,
            rtp=50000,
            rtcp=50001,
        ),
    )

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        status_res = client.get("/api/streams/engine/status")

    assert status_res.status_code == 200
    payload = status_res.json()
    assert payload["network_contract"]["status"] == "port_mismatch"
    assert "RTSP active port 19998 does not match expected add-on port 18758." in payload[
        "network_contract"
    ]["warnings"]
    assert "WebRTC active port 19999 does not match expected add-on port 18760." in payload[
        "network_contract"
    ]["warnings"]


def test_engine_status_uses_hls_proxy_url_when_contract_requests_proxy(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_DIRECT_API_PORT", "18756")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    engine_manager = _EngineManagerStub(data_dir=tmp_path / "data")

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        status_res = client.get(
            "/api/streams/engine/status",
            headers={"host": "homeassistant.local:18756"},
        )

    assert status_res.status_code == 200
    payload = status_res.json()
    assert payload["urls"]["hls_url"] == (
        "http://homeassistant.local:18756/api/streams/media/hls/test/index.m3u8"
    )
    assert payload["network_contract"]["public_hls_mode"] == "proxy"


def test_hls_media_proxy_forwards_to_active_hls_port(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        requested_urls.append(str(request.full_url))
        return _UrlOpenResponse(b"#EXTM3U\n")

    monkeypatch.setattr("toposync_ext_streaming.api.routes.urllib_request.urlopen", fake_urlopen)
    engine_manager = _EngineManagerStub(
        data_dir=tmp_path / "data",
        ports=MediaMtxPorts(
            rtsp=18758,
            hls=18888,
            webrtc=18760,
            api=18761,
            rtp=50000,
            rtcp=50001,
        ),
    )

    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        response = client.get("/api/streams/media/hls/lan-main/index.m3u8")

    assert response.status_code == 200
    assert response.text == "#EXTM3U\n"
    assert requested_urls == ["http://127.0.0.1:18888/lan-main/index.m3u8"]


def test_transmission_urls_primes_demand_hint(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        bridge = _WriterBridgeStub()
        client.app.state.streaming_writer_bridge = bridge

        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Prime stream",
                "path": "prime-main",
                "outputs": [{"id": "prime_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")
        assert urls_res.status_code == 200
        assert bridge.calls == [(transmission_id, None)]

        prime_res = client.post(f"/api/streams/transmissions/{transmission_id}/demand/prime")
        assert prime_res.status_code == 200
        payload = prime_res.json()
        assert payload["primed"] is True
        assert payload["primed_outputs"] == 1
        assert bridge.calls[-1][0] == transmission_id


def test_engine_status_exposes_webrtc_port_and_test_url(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        status_res = client.get("/api/streams/engine/status")
        assert status_res.status_code == 200
        payload = status_res.json()
        ports = payload.get("ports")
        urls = payload.get("urls")
        assert isinstance(ports, dict)
        assert isinstance(urls, dict)
        assert ports.get("webrtc") == 8889
        assert urls.get("webrtc_url") == "http://127.0.0.1:8889/test/whep"


def test_mediamtx_config_renders_webrtc_and_optional_ice_servers() -> None:
    config = render_mediamtx_config(
        bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997),
        paths=["camera-main"],
        enable_webrtc=True,
        webrtc_ice_servers=["stun:stun.l.google.com:19302", "turn:username:password@turn.example.com:3478"],
        webrtc_additional_hosts=["192.168.0.100", "homeassistant.local"],
        webrtc_local_udp_address=":18762",
    )

    assert "webrtc: true" in config
    assert "webrtcAddress: 127.0.0.1:8889" in config
    assert "webrtcAdditionalHosts: ['192.168.0.100', 'homeassistant.local']" in config
    assert "webrtcLocalUDPAddress: ':18762'" in config
    assert "webrtcICEServers2:" in config
    assert "stun:stun.l.google.com:19302" in config
    assert "turn:username:password@turn.example.com:3478" in config
