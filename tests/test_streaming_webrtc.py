from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse

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
        self.calls: list[tuple[str, Any, Any, Any]] = []

    async def prime_transmission_demand(
        self,
        transmission_id: str,
        *,
        ttl_s: float | None = None,
        output_id: str | None = None,
        quality_profile_id: str | None = None,
    ) -> int:
        self.calls.append((transmission_id, ttl_s, output_id, quality_profile_id))
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
                    "webrtc_additional_hosts": ["192.168.0.100"],
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


def test_webrtc_url_is_omitted_when_exposed_host_is_not_in_additional_hosts(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        settings_res = client.patch(
            "/api/streams/settings",
            json={
                "engine": {
                    "expose_to_lan": True,
                    "preferred_ports": {"webrtc": 18760, "webrtc_udp": 18762},
                    "webrtc_additional_hosts": ["homeassistant.local"],
                }
            },
        )
        assert settings_res.status_code == 200

        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Blocked WHEP stream",
                "path": "blocked-whep",
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
        assert payload["outputs"] == []
        assert payload["network_contract"]["actual_ports"]["webrtc_udp"] == 18762
        assert any("WebRTC WHEP host" in message for message in payload["warnings"])


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
    output = payload["outputs"][0]
    assert output["url"].startswith(
        "http://192.168.0.100:18756/api/streams/media/hls/lan-main/index.m3u8?media_token="
    )
    assert output["requires_auth"] is False
    assert output["media_auth_type"] == "signed_url"
    assert output["url_expires_at_unix"] > output["renew_after_unix"]
    assert payload["blocking_errors"] == []
    assert payload["network_contract"]["status"] == "ok"
    assert payload["network_contract"]["public_hls_mode"] == "proxy"
    assert payload["network_contract"]["expected_ports"].get("hls") is None
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
        settings_res = client.patch(
            "/api/streams/settings",
            json={"engine": {"media_auth": {"mode": "open"}}},
        )
        assert settings_res.status_code == 200
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


def test_signed_hls_treats_hls_port_as_internal_without_env_proxy(tmp_path: Path) -> None:
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
                "name": "Signed HLS stream",
                "path": "signed-main",
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
    assert payload["outputs"][0]["url"].startswith(
        "http://192.168.0.100:18756/api/streams/media/hls/signed-main/index.m3u8?media_token="
    )
    assert payload["network_contract"]["public_hls_mode"] == "proxy"
    assert payload["network_contract"]["expected_ports"].get("hls") is None
    assert payload["network_contract"]["actual_ports"]["hls"] == 18888
    assert payload["blocking_errors"] == []


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


def test_ingress_hls_url_contract_uses_public_base_path(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        requested_urls.append(str(request.full_url))
        body = (
            b"#EXTM3U\n"
            b"#EXT-X-MEDIA-SEQUENCE:7\n"
            b"#EXTINF:1,\n"
            b"segment7.ts\n"
        )
        return _UrlOpenResponse(body)

    monkeypatch.setenv("TOPOSYNC_DEPLOYMENT_TARGET", "home_assistant_addon")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_DIRECT_API_PORT", "18756")
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    monkeypatch.setattr("toposync_ext_streaming.api.routes.urllib_request.urlopen", fake_urlopen)
    engine_manager = _EngineManagerStub(data_dir=tmp_path / "data")

    ingress_prefix = "/api/hassio_ingress/session-token"
    headers = {"host": "homeassistant.local:8090", "x-ingress-path": ingress_prefix}
    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Ingress HLS stream",
                "path": "ingress-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls",
            headers=headers,
        )
        assert urls_res.status_code == 200
        payload = urls_res.json()
        signed_url = payload["outputs"][0]["url"]
        assert signed_url.startswith(
            "http://homeassistant.local:8090/api/hassio_ingress/session-token/api/streams/media/hls/ingress-main/index.m3u8?media_token="
        )
        assert payload["public_base_path"] == ingress_prefix
        assert payload["media_url_origin"] == f"http://homeassistant.local:8090{ingress_prefix}"
        assert payload["network_contract"]["status"] == "ok"
        assert not any(
            "Direct API active port 8090" in warning
            for warning in payload["network_contract"]["warnings"]
        )

        parsed = urllib_parse.urlsplit(signed_url)
        backend_path = parsed.path.removeprefix(ingress_prefix)
        response = client.get(f"{backend_path}?{parsed.query}", headers=headers)

    assert response.status_code == 200
    assert requested_urls == ["http://127.0.0.1:18759/ingress-main/index.m3u8"]
    media_token = urllib_parse.parse_qs(parsed.query)["media_token"][0]
    assert (
        f"{ingress_prefix}/api/streams/media/hls/ingress-main/segment7.ts?media_token={media_token}"
        in response.text
    )


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
        settings_res = client.patch(
            "/api/streams/settings",
            json={"engine": {"media_auth": {"mode": "open"}}},
        )
        assert settings_res.status_code == 200
        response = client.get("/api/streams/media/hls/lan-main/index.m3u8")

    assert response.status_code == 200
    assert response.text == "#EXTM3U\n"
    assert requested_urls == ["http://127.0.0.1:18888/lan-main/index.m3u8"]


def test_signed_hls_media_proxy_rewrites_playlist_uris(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout=0):  # noqa: ANN001
        _ = timeout
        requested_urls.append(str(request.full_url))
        body = (
            b"#EXTM3U\n"
            b"#EXT-X-MAP:URI=\"init.mp4\"\n"
            b"#EXT-X-KEY:METHOD=AES-128,URI=\"keys/key.bin\"\n"
            b"#EXTINF:1,\n"
            b"segment1.ts\n"
        )
        return _UrlOpenResponse(body)

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
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Signed HLS stream",
                "path": "signed-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])
        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls",
            headers={"host": "toposync.example.test"},
        )
        signed_url = urls_res.json()["outputs"][0]["url"]

        response = client.get(signed_url.replace("http://toposync.example.test", ""))

    assert response.status_code == 200
    assert requested_urls == ["http://127.0.0.1:18888/signed-main/index.m3u8"]
    parsed = urllib_parse.urlsplit(signed_url)
    media_token = urllib_parse.parse_qs(parsed.query)["media_token"][0]
    assert f"/api/streams/media/hls/signed-main/init.mp4?media_token={media_token}" in response.text
    assert f"/api/streams/media/hls/signed-main/keys/key.bin?media_token={media_token}" in response.text
    assert f"/api/streams/media/hls/signed-main/segment1.ts?media_token={media_token}" in response.text


def test_signed_hls_media_proxy_rejects_missing_invalid_and_expired_token(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    engine_manager = _EngineManagerStub(data_dir=tmp_path / "data")
    with _create_client(tmp_path, engine_manager=engine_manager) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Signed HLS stream",
                "path": "signed-expiring",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])
        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")
        signed_output = urls_res.json()["outputs"][0]
        signed_url = str(signed_output["url"])

        missing_res = client.get("/api/streams/media/hls/signed-expiring/index.m3u8")
        invalid_res = client.get(
            "/api/streams/media/hls/signed-expiring/index.m3u8?media_token=bad"
        )
        monkeypatch.setattr(
            "toposync_ext_streaming.api.routes.time.time",
            lambda: float(signed_output["url_expires_at_unix"]) + 1.0,
        )
        expired_res = client.get(signed_url.replace("http://testserver", ""))

    assert missing_res.status_code == 401
    assert missing_res.json()["detail"] == "media_token_invalid"
    assert invalid_res.status_code == 401
    assert invalid_res.json()["detail"] == "media_token_invalid"
    assert expired_res.status_code == 401
    assert expired_res.json()["detail"] == "media_token_expired"


def test_open_hls_media_auth_preserves_plain_proxy_url(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE", "proxy")
    with _create_client(tmp_path, engine_manager=_EngineManagerStub(data_dir=tmp_path / "data")) as client:
        settings_res = client.patch(
            "/api/streams/settings",
            json={"engine": {"media_auth": {"mode": "open"}}},
        )
        assert settings_res.status_code == 200
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Open HLS stream",
                "path": "open-main",
                "outputs": [{"id": "main_hls", "protocol": "hls", "enabled": True}],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(f"/api/streams/transmissions/{transmission_id}/urls")

    assert urls_res.status_code == 200
    payload = urls_res.json()
    output = payload["outputs"][0]
    assert output["url"] == "http://testserver/api/streams/media/hls/open-main/index.m3u8"
    assert output["media_auth_type"] == "none"
    assert output["url_expires_at_unix"] is None
    assert any("Open HLS media access is enabled" in item for item in payload["warnings"])


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
        assert bridge.calls == [(transmission_id, None, None, None)]

        prime_res = client.post(f"/api/streams/transmissions/{transmission_id}/demand/prime")
        assert prime_res.status_code == 200
        payload = prime_res.json()
        assert payload["primed"] is True
        assert payload["primed_outputs"] == 1
        assert bridge.calls[-1][0] == transmission_id

        heartbeat_res = client.post(
            f"/api/streams/transmissions/{transmission_id}/demand/heartbeat",
            json={
                "playback_session_id": "session-1",
                "output_id": "prime_hls",
                "quality_profile_id": None,
                "transport": "hls",
            },
        )
        assert heartbeat_res.status_code == 200
        heartbeat_payload = heartbeat_res.json()
        assert heartbeat_payload["renewed"] is True
        assert heartbeat_payload["lease_seconds"] == 45.0
        assert bridge.calls[-1] == (transmission_id, 45.0, "prime_hls", None)


def test_transmission_urls_and_prime_accept_quality_profile_selection(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        bridge = _WriterBridgeStub()
        client.app.state.streaming_writer_bridge = bridge

        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Profile stream",
                "path": "profile-main",
                "outputs": [
                    {
                        "id": "hls_stable_apple_tv",
                        "protocol": "hls",
                        "enabled": True,
                        "quality_profile_id": "stable_apple_tv",
                        "resolution": {"width": 1280, "height": 720},
                        "fps_limit": 15,
                        "bitrate_kbps": 1800,
                    },
                    {
                        "id": "hls_diagnostic_low",
                        "protocol": "hls",
                        "enabled": True,
                        "quality_profile_id": "diagnostic_low",
                        "resolution": {"width": 426, "height": 240},
                        "fps_limit": 5,
                        "bitrate_kbps": 250,
                    },
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        urls_res = client.get(
            f"/api/streams/transmissions/{transmission_id}/urls?quality_profile_id=diagnostic_low"
        )
        assert urls_res.status_code == 200
        payload = urls_res.json()
        assert [item["output_id"] for item in payload["outputs"]] == ["hls_diagnostic_low"]
        assert payload["outputs"][0]["quality_profile_id"] == "diagnostic_low"
        assert bridge.calls[-1] == (transmission_id, None, None, "diagnostic_low")

        prime_res = client.post(
            f"/api/streams/transmissions/{transmission_id}/demand/prime?output_id=hls_stable_apple_tv"
        )
        assert prime_res.status_code == 200
        assert bridge.calls[-1] == (transmission_id, None, "hls_stable_apple_tv", None)


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
        assert ports.get("webrtc_udp") == 18762
        assert urls.get("webrtc_url") == "http://127.0.0.1:8889/test/whep"


def test_apply_webrtc_companion_creates_low_latency_output_and_preserves_hls(tmp_path: Path) -> None:
    with _create_client(tmp_path) as client:
        created_res = client.post(
            "/api/streams/transmissions",
            json={
                "name": "Companion stream",
                "path": "companion-main",
                "outputs": [
                    {
                        "id": "hls_stable_apple_tv",
                        "protocol": "hls",
                        "enabled": True,
                        "quality_profile_id": "stable_apple_tv",
                        "resolution": {"width": 1280, "height": 720},
                        "fps_limit": 15,
                        "bitrate_kbps": 1800,
                    },
                    {
                        "id": "custom_rtsp",
                        "protocol": "rtsp",
                        "enabled": True,
                    },
                ],
            },
        )
        assert created_res.status_code == 200
        transmission_id = str(created_res.json()["id"])

        apply_res = client.post(
            f"/api/streams/transmissions/{transmission_id}/webrtc/companion/apply"
        )
        assert apply_res.status_code == 200
        payload = apply_res.json()
        outputs = payload["transmission"]["outputs"]
        output_ids = {item["id"] for item in outputs}
        assert {"hls_stable_apple_tv", "custom_rtsp", "webrtc_low_latency"} <= output_ids
        companion = next(item for item in outputs if item["id"] == "webrtc_low_latency")
        assert companion["protocol"] == "webrtc"
        assert companion["resolution"] == {"width": 1280, "height": 720}
        assert companion["fps_limit"] == 15
        assert companion["bitrate_kbps"] == 1800
        assert companion["latency_profile"] == "ultra_low"
        assert companion["encoder_mode"] == "inherit"


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
