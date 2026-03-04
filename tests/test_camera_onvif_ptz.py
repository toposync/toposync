from __future__ import annotations

import asyncio

import pytest


def test_onvif_client_ptz_presets_status_and_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    from toposync_ext_cameras.onvif.client import OnvifClient
    import toposync_ext_cameras.onvif.client as onvif_mod

    presets_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tptz:GetPresetsResponse>
      <tptz:Preset token="home">
        <tt:Name>Home</tt:Name>
        <tt:PTZPosition>
          <tt:PanTilt x="0.100" y="-0.200" />
          <tt:Zoom x="0.300" />
        </tt:PTZPosition>
      </tptz:Preset>
      <tptz:Preset token="door">
        <tt:Name>Door</tt:Name>
      </tptz:Preset>
    </tptz:GetPresetsResponse>
  </s:Body>
</s:Envelope>
"""

    status_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tptz:GetStatusResponse>
      <tptz:PTZStatus>
        <tt:Position>
          <tt:PanTilt x="0.500" y="0.250" />
          <tt:Zoom x="0.000" />
        </tt:Position>
        <tt:MoveStatus>IDLE</tt:MoveStatus>
        <tt:Error></tt:Error>
        <tt:UtcTime>2026-01-01T00:00:00Z</tt:UtcTime>
      </tptz:PTZStatus>
    </tptz:GetStatusResponse>
  </s:Body>
</s:Envelope>
"""

    ok_envelope = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Body>
    <tptz:Ok />
  </s:Body>
</s:Envelope>
"""

    async def fake_post_soap(*, url: str, body: bytes, timeout_s: float, soap_action: str | None, soap_version: str) -> bytes:
        _ = url, body, timeout_s, soap_version
        action = soap_action or ""
        if action.endswith("/GetPresets"):
            return presets_xml
        if action.endswith("/GetStatus"):
            return status_xml
        if action.endswith("/GotoPreset"):
            return ok_envelope
        if action.endswith("/ContinuousMove"):
            return ok_envelope
        if action.endswith("/RelativeMove"):
            return ok_envelope
        if action.endswith("/Stop"):
            return ok_envelope
        raise RuntimeError(f"Unexpected ONVIF PTZ action: {soap_action}")

    monkeypatch.setattr(onvif_mod, "_http_post_soap", fake_post_soap)

    async def scenario() -> None:
        client = OnvifClient(
            xaddr="http://192.168.0.10/onvif/device_service",
            username="admin",
            password="secret",
            timeout_s=1.0,
        )
        presets = await client.get_ptz_presets("http://192.168.0.10/onvif/ptz_service", profile_token="profile-main")
        assert [p.token for p in presets] == ["home", "door"]
        assert presets[0].name == "Home"
        assert presets[0].pan == pytest.approx(0.1)
        assert presets[0].tilt == pytest.approx(-0.2)
        assert presets[0].zoom == pytest.approx(0.3)

        status = await client.get_ptz_status("http://192.168.0.10/onvif/ptz_service", profile_token="profile-main")
        assert status.pan == pytest.approx(0.5)
        assert status.tilt == pytest.approx(0.25)
        assert status.zoom == pytest.approx(0.0)
        assert status.move_status == "IDLE"
        assert status.utc_time == "2026-01-01T00:00:00Z"

        await client.goto_preset(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            preset_token="home",
        )
        await client.continuous_move(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            pan=0.4,
            tilt=-0.1,
            zoom=0.0,
            timeout_s=0.5,
        )
        await client.relative_move(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            pan=0.1,
            tilt=0.0,
            zoom=0.0,
        )
        await client.stop("http://192.168.0.10/onvif/ptz_service", profile_token="profile-main", pan_tilt=True, zoom=True)

    asyncio.run(scenario())
