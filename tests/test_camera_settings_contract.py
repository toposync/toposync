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


def test_normalize_onvif_legacy_credentials_move_to_onvif_config() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                    "channels": [
                        {
                            "id": "video_main",
                            "modality": "video",
                            "is_default": True,
                            "connection_type": "onvif",
                            "rtsp_url": "rtsp://192.168.0.10/main",
                            "username": "camera-user",
                            "password": "camera-pass",
                            "onvif": {"xaddr": "192.168.0.10"},
                        }
                    ],
                }
            ]
        }
    )

    channel = normalized["devices"][0]["channels"][0]
    assert channel["stream_profile"] == "onvif"
    assert channel["stream_username"] == ""
    assert channel["stream_password"] == ""
    assert channel["username"] == ""
    assert channel["password"] == ""
    assert channel["onvif"]["username"] == "camera-user"
    assert channel["onvif"]["password"] == "camera-pass"


def test_normalize_rtsp_legacy_credentials_move_to_stream_credentials() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "devices": [
                {
                    "id": "front",
                    "name": "Front",
                    "channels": [
                        {
                            "id": "video_main",
                            "modality": "video",
                            "is_default": True,
                            "connection_type": "rtsp",
                            "rtsp_url": "rtsp://127.0.0.1:8554/front",
                            "username": "stream-user",
                            "password": "stream-pass",
                        }
                    ],
                }
            ]
        }
    )

    channel = normalized["devices"][0]["channels"][0]
    assert channel["stream_profile"] == "custom"
    assert channel["stream_username"] == "stream-user"
    assert channel["stream_password"] == "stream-pass"
    assert channel["username"] == ""
    assert channel["password"] == ""


def test_normalize_prefers_devices_when_legacy_cameras_key_remains() -> None:
    from toposync_ext_cameras.settings import normalize_cameras_settings

    normalized = normalize_cameras_settings(
        {
            "schema_version": 2,
            "cameras": [{"id": "deleted", "name": "Deleted legacy camera"}],
            "devices": [
                {
                    "id": "kept",
                    "name": "Kept camera",
                    "channels": [
                        {
                            "id": "video_main",
                            "modality": "video",
                            "is_default": True,
                            "connection_type": "rtsp",
                            "rtsp_url": "rtsp://127.0.0.1:8554/kept",
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
                "cameras": [
                    {"id": "front", "name": "Front"},
                    {"id": "back", "name": "Back"},
                ],
            },
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert [item["id"] for item in res.json()["cameras"]] == ["front", "back"]

        res = client.patch(
            "/api/settings/extensions/com.toposync.cameras",
            json={
                "schema_version": 2,
                "devices": [
                    {
                        "id": "back",
                        "name": "Back",
                        "kind": "camera",
                        "channels": [
                            {
                                "id": "video_main",
                                "name": "Main video",
                                "modality": "video",
                                "enabled": True,
                                "is_default": True,
                                "connection_type": "rtsp",
                                "transport": "rtsp",
                                "stream_profile": "custom",
                                "rtsp_url": "",
                                "stream_username": "",
                                "stream_password": "",
                                "fps": 5,
                                "onvif": None,
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
            json={"schema_version": 2, "devices": []},
        )
        assert res.status_code == 200

        res = client.get("/api/cameras/index")
        assert res.status_code == 200
        assert res.json()["cameras"] == []
