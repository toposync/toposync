from __future__ import annotations

from dataclasses import dataclass

from toposync_ext_streaming.api.models import StreamingCameraIngestSettings
from toposync_ext_streaming.streaming.camera_ingest import build_camera_ingest_definitions, build_camera_ingest_path_configs
from toposync_ext_streaming.streaming.mediamtx_config import MediaMTXResolvedPorts, render_mediamtx_config


@dataclass(slots=True)
class _AppSettingsStub:
    extensions: dict


def test_build_camera_ingest_definitions_applies_auth_and_normalizes_path() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "cameras": [
                    {
                        "id": "Front Door",
                        "rtsp_url": "rtsp://10.0.0.10/live",
                        "username": "user",
                        "password": "pass",
                    }
                ]
            }
        }
    )

    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)

    assert "Front Door" in ingest_by_id
    ingest = ingest_by_id["Front Door"]
    assert ingest.path_slug == "ingest-front-door"
    assert ingest.source_rtsp_url == "rtsp://user:pass@10.0.0.10/live"


def test_build_camera_ingest_path_configs_renders_source_and_on_demand() -> None:
    app_settings = _AppSettingsStub(
        extensions={
            "com.toposync.cameras": {
                "cameras": [
                    {
                        "id": "cam1",
                        "rtsp_url": "rtsp://10.0.0.10/live",
                    }
                ]
            }
        }
    )
    ingest_settings = StreamingCameraIngestSettings(enabled=True, path_prefix="ingest")
    ingest_by_id = build_camera_ingest_definitions(app_settings=app_settings, ingest_settings=ingest_settings)
    path_configs = build_camera_ingest_path_configs(ingest_by_id)

    assert path_configs == {
        "ingest-cam1": {
            "source": "rtsp://10.0.0.10/live",
            "sourceOnDemand": True,
        }
    }

    config_text = render_mediamtx_config(
        bind_host="127.0.0.1",
        ports=MediaMTXResolvedPorts(rtsp=8554, hls=8888, webrtc=8889, api=9997),
        paths=["ingest-cam1", "output-main"],
        enable_webrtc=True,
        path_configs=path_configs,
    )

    assert "paths:" in config_text
    assert "  ingest-cam1:" in config_text
    assert "    source: 'rtsp://10.0.0.10/live'" in config_text
    assert "    sourceOnDemand: true" in config_text
    assert "  output-main: {}" in config_text

