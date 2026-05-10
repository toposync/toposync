from __future__ import annotations


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
