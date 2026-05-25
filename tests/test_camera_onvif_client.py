from __future__ import annotations

import asyncio

import pytest


def test_normalize_onvif_xaddr_accepts_plain_host() -> None:
    from toposync_ext_cameras.onvif import normalize_onvif_xaddr

    assert normalize_onvif_xaddr("192.168.0.10") == "http://192.168.0.10/onvif/device_service"
    assert normalize_onvif_xaddr("192.168.0.10:8080") == "http://192.168.0.10:8080/onvif/device_service"


def test_normalize_onvif_xaddr_adds_default_path_when_missing() -> None:
    from toposync_ext_cameras.onvif import normalize_onvif_xaddr

    assert normalize_onvif_xaddr("http://192.168.0.10") == "http://192.168.0.10/onvif/device_service"
    assert normalize_onvif_xaddr("http://192.168.0.10/") == "http://192.168.0.10/onvif/device_service"


def test_onvif_xaddr_candidates_try_common_ports_for_plain_host() -> None:
    from toposync_ext_cameras.onvif import onvif_xaddr_candidates

    assert onvif_xaddr_candidates("192.168.0.10") == [
        "http://192.168.0.10/onvif/device_service",
        "http://192.168.0.10:2020/onvif/device_service",
        "http://192.168.0.10:8000/onvif/device_service",
        "http://192.168.0.10:8080/onvif/device_service",
        "http://192.168.0.10:8899/onvif/device_service",
    ]


def test_onvif_xaddr_candidates_keep_explicit_port_or_path() -> None:
    from toposync_ext_cameras.onvif import onvif_xaddr_candidates

    assert onvif_xaddr_candidates("192.168.0.10:2020") == [
        "http://192.168.0.10:2020/onvif/device_service"
    ]
    assert onvif_xaddr_candidates("http://192.168.0.10/custom/service") == [
        "http://192.168.0.10/custom/service"
    ]


def test_normalize_rtsp_url_strips_credentials() -> None:
    from toposync_ext_cameras.onvif import normalize_rtsp_url

    assert normalize_rtsp_url("rtsp://user:pass@192.168.0.10/stream") == "rtsp://192.168.0.10/stream"
    assert normalize_rtsp_url("rtsp://192.168.0.10/stream") == "rtsp://192.168.0.10/stream"


def test_onvif_client_parses_capabilities_profiles_and_stream_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    from toposync_ext_cameras.onvif.client import OnvifClient
    import toposync_ext_cameras.onvif.client as onvif_mod

    capabilities_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Media><tt:XAddr>http://192.168.0.10/onvif/media_service</tt:XAddr></tt:Media>
        <tt:PTZ><tt:XAddr>http://192.168.0.10/onvif/ptz_service</tt:XAddr></tt:PTZ>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </s:Body>
</s:Envelope>
"""

    profiles_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="profile-main">
        <tt:Name>Main</tt:Name>
        <tt:VideoEncoderConfiguration token="enc-main">
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
          <tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit></tt:RateControl>
        </tt:VideoEncoderConfiguration>
        <tt:PTZConfiguration token="ptz-main" />
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>
"""

    stream_uri_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>rtsp://user:pass@192.168.0.10/stream1</tt:Uri>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>
  </s:Body>
</s:Envelope>
"""

    async def fake_post_soap(*, url: str, body: bytes, timeout_s: float, soap_action: str | None, soap_version: str) -> bytes:
        _ = url, body, timeout_s, soap_version
        action = soap_action or ""
        if action.endswith("/GetCapabilities"):
            return capabilities_xml
        if action.endswith("/GetProfiles"):
            return profiles_xml
        if action.endswith("/GetStreamUri"):
            return stream_uri_xml
        raise RuntimeError(f"Unexpected ONVIF action: {soap_action}")

    monkeypatch.setattr(onvif_mod, "_http_post_soap", fake_post_soap)

    async def scenario() -> tuple[str | None, str | None, str, str]:
        client = OnvifClient(
            xaddr="http://192.168.0.10/onvif/device_service",
            username="admin",
            password="secret",
            timeout_s=1.0,
        )
        media_xaddr, ptz_xaddr = await client.get_capabilities()
        assert media_xaddr == "http://192.168.0.10/onvif/media_service"
        assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"

        profiles = await client.get_profiles(media_xaddr or "")
        assert len(profiles) == 1
        assert profiles[0].token == "profile-main"
        assert profiles[0].name == "Main"
        assert profiles[0].encoding == "H264"
        assert profiles[0].width == 1920
        assert profiles[0].height == 1080
        assert profiles[0].fps == 15
        assert profiles[0].has_ptz is True

        uri = await client.get_stream_uri(media_xaddr or "", profile_token=profiles[0].token)
        return media_xaddr, ptz_xaddr, profiles[0].token, uri

    media_xaddr, ptz_xaddr, token, uri = asyncio.run(scenario())
    assert media_xaddr == "http://192.168.0.10/onvif/media_service"
    assert ptz_xaddr == "http://192.168.0.10/onvif/ptz_service"
    assert token == "profile-main"
    assert uri == "rtsp://192.168.0.10/stream1"

