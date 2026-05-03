from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync_ext_streaming.api.routes import create_streaming_router
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config


class _WriterBridgeStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def prime_transmission_demand(self, transmission_id: str, *, ttl_s: float | None = None) -> int:
        self.calls.append((transmission_id, ttl_s))
        return 1


def _create_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )

    app = FastAPI()
    config_store = ConfigStore(paths=paths)
    app.state.config_store = config_store
    app.state.streaming_engine_manager = MediaMtxEngineManager(data_dir=paths.data_dir)
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
