from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod


def _create_client_with_cameras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

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


def test_normalize_onvif_camera_keeps_control_and_sources() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                        "control": {"type": "onvif"},
                        "onvif": {
                            "xaddr": "192.168.0.10",
                            "username": "camera-user",
                            "password": "camera-pass",
                        },
                        "sources": [
                            {
                                "id": "main",
                                "kind": "video",
                                "is_default": True,
                                "origin": {
                                    "type": "onvif_profile",
                                    "rtsp_url": "rtsp://192.168.0.10/main",
                                    "profile_token": "profile-main",
                                },
                            }
                        ],
                }
            ]
        }
    )

    device = normalized["devices"][0]
    assert device["control"]["type"] == "onvif"
    assert device["onvif"]["username"] == "camera-user"
    assert device["onvif"]["password"] == "camera-pass"
    source = device["sources"][0]
    assert source["origin"]["type"] == "onvif_profile"
    assert source["origin"]["profile_token"] == "profile-main"


def test_normalize_manual_camera_keeps_rtsp_source_credentials() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                        "control": {"type": "none"},
                        "sources": [
                            {
                                "id": "main",
                                "kind": "video",
                                "is_default": True,
                                "origin": {
                                    "type": "rtsp",
                                    "rtsp_url": "rtsp://127.0.0.1:8554/front",
                                    "stream_username": "stream-user",
                                    "stream_password": "stream-pass",
                                },
                            }
                        ],
                }
            ]
        }
    )

    source = normalized["devices"][0]["sources"][0]
    assert source["origin"]["type"] == "rtsp"
    assert source["origin"]["stream_username"] == "stream-user"
    assert source["origin"]["stream_password"] == "stream-pass"


def test_onvif_inspect_falls_back_to_common_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toposync_ext_cameras.onvif import OnvifError, OnvifProfile
    import toposync_ext_cameras.plugin as cameras_plugin

    attempts: list[str] = []

    class FakeOnvifClient:
        def __init__(
            self,
            *,
            xaddr: str,
            username: str,
            password: str,
            timeout_s: float,
            auth_mode: str,
        ) -> None:
            self.xaddr = xaddr
            self.username = username
            self.password = password
            self.timeout_s = timeout_s
            self.auth_mode = auth_mode

        async def get_capabilities(self) -> tuple[str | None, str | None]:
            attempts.append(self.xaddr)
            if ":2020/" not in self.xaddr:
                raise OnvifError("<urlopen error [Errno 111] Connection refused>")
            return (
                "http://192.168.0.10:2020/onvif/service",
                "http://192.168.0.10:2020/onvif/service",
            )

        async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:
            assert media_xaddr == "http://192.168.0.10:2020/onvif/service"
            return [
                OnvifProfile(
                    token="profile_1",
                    name="mainStream",
                    encoding="H264",
                    width=1920,
                    height=1080,
                    fps=25,
                )
            ]

        async def get_stream_uri(self, media_xaddr: str, *, profile_token: str) -> str:
            assert media_xaddr == "http://192.168.0.10:2020/onvif/service"
            assert profile_token == "profile_1"
            return "rtsp://192.168.0.10/stream1"

    monkeypatch.setattr(cameras_plugin, "OnvifClient", FakeOnvifClient)
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/cameras/onvif/inspect",
            json={
                "xaddr": "192.168.0.10",
                "username": "camera",
                "password": "secret",
                "timeout_ms": 500,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert attempts[:2] == [
        "http://192.168.0.10/onvif/device_service",
        "http://192.168.0.10:2020/onvif/device_service",
    ]
    assert body["xaddr"] == "http://192.168.0.10:2020/onvif/device_service"
    assert body["media_xaddr"] == "http://192.168.0.10:2020/onvif/service"
    assert body["profiles"][0]["stream_uri"] == "rtsp://192.168.0.10/stream1"


def test_normalize_ignores_legacy_cameras_key() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "schema_version": 2,
            "cameras": [{"id": "deleted", "name": "Deleted legacy camera"}],
            "devices": [
                {
                    "id": "kept",
                    "name": "Kept camera",
                    "control": {"type": "none"},
                    "sources": [
                        {
                            "id": "main",
                            "kind": "video",
                            "is_default": True,
                            "origin": {"type": "rtsp", "rtsp_url": "rtsp://127.0.0.1:8554/kept"},
                        }
                    ],
                }
            ],
        }
    )

    assert [item["id"] for item in normalized["devices"]] == ["kept"]


def test_camera_helpers_do_not_fall_back_to_legacy_cameras_when_devices_key_exists() -> None:
    from toposync.runtime.pipelines.migration_legacy_cameras import extract_legacy_camera_rules
    from toposync.runtime.pipelines.templates import camera_names_by_id

    extensions = {
        "com.toposync.cameras": {
            "devices": [],
            "cameras": [
                {
                    "id": "deleted",
                    "name": "Deleted legacy camera",
                    "enabled": True,
                    "detections": [{"id": "legacy-motion", "trigger": {"kind": "motion"}}],
                },
            ],
        }
    }

    assert camera_names_by_id(extensions) == {}
    assert extract_legacy_camera_rules({"extensions": extensions}) == []


def test_camera_index_uses_devices_after_deleting_legacy_camera(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 4,
                "devices": [
                    {
                        "id": "back",
                        "name": "Back",
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
                                "origin": {"type": "rtsp", "rtsp_url": ""},
                                "video": {"fps": 5},
                                "ingest": {"mode": "centralized", "host_server_id": "local"},
                                "metadata": {},
                            }
                        ],
                        "metadata": {},
                    }
                ],
            },
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert [item["id"] for item in res.json()["cameras"]] == ["back"]

        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={"schema_version": 4, "devices": []},
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert res.json()["cameras"] == []
