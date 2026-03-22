from __future__ import annotations

import asyncio

import pytest


def test_resolve_onvif_rtsp_url_cached_auto_selects_profile_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as ops
    from toposync_ext_cameras.onvif import OnvifProfile

    ops._ONVIF_STREAM_CACHE.clear()
    ops._ONVIF_STREAM_LOCKS.clear()

    class FakeClient:
        created = 0
        capabilities_calls = 0
        profiles_calls = 0
        stream_calls = 0

        def __init__(self, *, xaddr: str, username: str, password: str, timeout_s: float, auth_mode: str) -> None:  # noqa: ARG002
            _ = xaddr, username, password, timeout_s, auth_mode
            type(self).created += 1

        async def get_capabilities(self) -> tuple[str | None, str | None]:
            type(self).capabilities_calls += 1
            return "http://192.168.0.10/onvif/media_service", None

        async def get_profiles(self, media_xaddr: str) -> list[OnvifProfile]:  # noqa: ARG002
            type(self).profiles_calls += 1
            # The resolver should pick the highest-res H264 profile ("main").
            return [
                OnvifProfile(token="low", name="Low", encoding="H264", width=640, height=360, fps=15, has_ptz=False),
                OnvifProfile(token="main", name="Main", encoding="H264", width=1920, height=1080, fps=15, has_ptz=True),
            ]

        async def get_stream_uri(self, media_xaddr: str, *, profile_token: str) -> str:  # noqa: ARG002
            type(self).stream_calls += 1
            assert profile_token == "main"
            return "rtsp://192.168.0.10/stream1"

    monkeypatch.setattr(ops, "OnvifClient", FakeClient)

    camera = {
        "username": "admin",
        "password": "secret",
        "onvif": {"xaddr": "192.168.0.10"},
    }

    rtsp1 = asyncio.run(ops._resolve_onvif_rtsp_url_cached(camera_id="cam-1", camera=camera))
    rtsp2 = asyncio.run(ops._resolve_onvif_rtsp_url_cached(camera_id="cam-1", camera=camera))

    assert rtsp1 == "rtsp://192.168.0.10/stream1"
    assert rtsp2 == rtsp1
    assert FakeClient.created == 1
    assert FakeClient.capabilities_calls == 1
    assert FakeClient.profiles_calls == 1
    assert FakeClient.stream_calls == 1


def test_resolve_onvif_rtsp_url_cached_respects_profile_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import toposync_ext_cameras.pipelines.operators as ops

    ops._ONVIF_STREAM_CACHE.clear()
    ops._ONVIF_STREAM_LOCKS.clear()

    class FakeClient:
        stream_calls = 0

        def __init__(self, *, xaddr: str, username: str, password: str, timeout_s: float, auth_mode: str) -> None:  # noqa: ARG002
            _ = xaddr, username, password, timeout_s, auth_mode

        async def get_capabilities(self) -> tuple[str | None, str | None]:
            return "http://192.168.0.10/onvif/media_service", None

        async def get_profiles(self, media_xaddr: str):  # noqa: ANN001, ARG002
            raise AssertionError("get_profiles should not be called when profile_token is configured")

        async def get_stream_uri(self, media_xaddr: str, *, profile_token: str) -> str:  # noqa: ARG002
            type(self).stream_calls += 1
            assert profile_token == "configured-token"
            return "rtsp://192.168.0.10/stream2"

    monkeypatch.setattr(ops, "OnvifClient", FakeClient)

    camera = {
        "username": "admin",
        "password": "secret",
        "onvif": {"xaddr": "192.168.0.10", "profile_token": "configured-token"},
    }

    rtsp = asyncio.run(ops._resolve_onvif_rtsp_url_cached(camera_id="cam-2", camera=camera))
    assert rtsp == "rtsp://192.168.0.10/stream2"
    assert FakeClient.stream_calls == 1

