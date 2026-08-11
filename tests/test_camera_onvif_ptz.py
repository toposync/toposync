from __future__ import annotations

import asyncio

import pytest


def test_onvif_ptz_status_aggregates_axis_move_status_without_position() -> None:
    from toposync_ext_cameras.onvif.client import SOAP12_NS, _parse_ptz_status

    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tptz:GetStatusResponse>
      <tptz:PTZStatus>
        <tt:MoveStatus>
          <tt:PanTilt>IDLE</tt:PanTilt>
          <tt:Zoom>IDLE</tt:Zoom>
        </tt:MoveStatus>
        <tt:UtcTime>2026-08-10T21:00:00Z</tt:UtcTime>
      </tptz:PTZStatus>
    </tptz:GetStatusResponse>
  </s:Body>
</s:Envelope>
"""

    status = _parse_ptz_status(payload, soap_ns=SOAP12_NS)

    assert status.pan is None
    assert status.tilt is None
    assert status.zoom is None
    assert status.move_status == "IDLE"


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

    set_preset_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Body>
    <tptz:SetPresetResponse>
      <tptz:PresetToken>temporary-42</tptz:PresetToken>
    </tptz:SetPresetResponse>
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

    set_preset_envelope = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Body>
    <tptz:SetPresetResponse>
      <tptz:PresetToken>toposync-guard</tptz:PresetToken>
    </tptz:SetPresetResponse>
  </s:Body>
</s:Envelope>
"""
    calls = {"set_preset": 0}

    async def fake_post_soap(*, url: str, body: bytes, timeout_s: float, soap_action: str | None, soap_version: str) -> bytes:
        _ = url, body, timeout_s, soap_version
        action = soap_action or ""
        if action.endswith("/GetPresets"):
            return presets_xml
        if action.endswith("/GetStatus"):
            return status_xml
        if action.endswith("/SetPreset"):
            assert b"<tptz:SetPreset" in body
            assert b"<tptz:ProfileToken>profile-main</tptz:ProfileToken>" in body
            assert b"<tptz:PresetName>Temporary &amp; safe</tptz:PresetName>" in body
            assert b"<tptz:PresetToken>requested&lt;slot&gt;</tptz:PresetToken>" in body
            return set_preset_xml
        if action.endswith("/RemovePreset"):
            assert b"<tptz:RemovePreset" in body
            assert b"<tptz:ProfileToken>profile-main</tptz:ProfileToken>" in body
            assert b"<tptz:PresetToken>temporary-42</tptz:PresetToken>" in body
            return ok_envelope
        if action.endswith("/GotoPreset"):
            return ok_envelope
        if action.endswith("/AbsoluteMove"):
            assert b"<tptz:AbsoluteMove" in body
            assert b'<tt:PanTilt x="0.250000" y="-0.250000" />' in body
            assert b'<tt:Zoom x="0.500000" />' in body
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
        created_preset = await client.set_preset(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            preset_name="Guard",
            preset_token="toposync-guard",
        )
        assert created_preset == "toposync-guard"
        assert calls["set_preset"] == 1

        status = await client.get_ptz_status("http://192.168.0.10/onvif/ptz_service", profile_token="profile-main")
        assert status.pan == pytest.approx(0.5)
        assert status.tilt == pytest.approx(0.25)
        assert status.zoom == pytest.approx(0.0)
        assert status.move_status == "IDLE"
        assert status.utc_time == "2026-01-01T00:00:00Z"

        preset_token = await client.set_preset(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            preset_name="Temporary & safe",
            preset_token="requested<slot>",
        )
        assert preset_token == "temporary-42"
        await client.remove_preset(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            preset_token=preset_token,
        )
        await client.goto_preset(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            preset_token="home",
        )
        await client.absolute_move(
            "http://192.168.0.10/onvif/ptz_service",
            profile_token="profile-main",
            pan=0.25,
            tilt=-0.25,
            zoom=0.5,
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


def test_onvif_preset_mutations_are_not_retried_after_ambiguous_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif.client import OnvifClient, OnvifError
    import toposync_ext_cameras.onvif.client as onvif_mod

    presets_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Body><tptz:GetPresetsResponse /></s:Body>
</s:Envelope>
"""
    status_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tptz:GetStatusResponse>
      <tptz:PTZStatus><tt:MoveStatus>IDLE</tt:MoveStatus></tptz:PTZStatus>
    </tptz:GetStatusResponse>
  </s:Body>
</s:Envelope>
"""
    get_presets_attempts: list[tuple[str, str]] = []
    get_status_attempts: list[tuple[str, str]] = []
    mutation_calls = {
        "SetPreset": 0,
        "RemovePreset": 0,
        "GotoPreset": 0,
        "AbsoluteMove": 0,
        "ContinuousMove": 0,
        "RelativeMove": 0,
        "Stop": 0,
    }

    def request_auth(body: bytes) -> str:
        if b"#PasswordDigest" in body:
            return "digest"
        if b"#PasswordText" in body:
            return "text"
        return "none"

    async def fake_post_soap(
        *,
        url: str,
        body: bytes,
        timeout_s: float,
        soap_action: str | None,
        soap_version: str,
    ) -> bytes:
        _ = url, timeout_s
        action = str(soap_action or "").rsplit("/", 1)[-1]
        auth = request_auth(body)
        if action == "GetPresets":
            get_presets_attempts.append((soap_version, auth))
            if (soap_version, auth) == ("1.1", "text"):
                return presets_xml
            raise OnvifError("Unauthorized preflight transport")
        if action == "GetStatus":
            get_status_attempts.append((soap_version, auth))
            if (soap_version, auth) == ("1.1", "text"):
                return status_xml
            raise OnvifError("Unauthorized preflight transport")
        if action in mutation_calls:
            mutation_calls[action] += 1
            assert (soap_version, auth) == ("1.1", "text")
            raise TimeoutError(f"{action} response timed out after send")
        raise RuntimeError(f"Unexpected ONVIF action: {soap_action}")

    monkeypatch.setattr(onvif_mod, "_http_post_soap", fake_post_soap)

    async def scenario() -> None:
        preset_client = OnvifClient(
            xaddr="http://192.168.0.10/onvif/device_service",
            username="admin",
            password="secret",
            timeout_s=1.0,
        )
        with pytest.raises(OnvifError, match="SetPreset response timed out after send"):
            await preset_client.set_preset(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                preset_name="Temporary restore",
            )
        with pytest.raises(OnvifError, match="RemovePreset response timed out after send"):
            await preset_client.remove_preset(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                preset_token="temporary-42",
            )

        movement_client = OnvifClient(
            xaddr="http://192.168.0.10/onvif/device_service",
            username="admin",
            password="secret",
            timeout_s=1.0,
        )
        with pytest.raises(OnvifError, match="GotoPreset response timed out after send"):
            await movement_client.goto_preset(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                preset_token="home",
            )
        with pytest.raises(OnvifError, match="AbsoluteMove response timed out after send"):
            await movement_client.absolute_move(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                pan=0.1,
                tilt=-0.1,
                zoom=0.2,
            )
        with pytest.raises(OnvifError, match="ContinuousMove response timed out after send"):
            await movement_client.continuous_move(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                pan=0.1,
                tilt=0.0,
                zoom=0.0,
                timeout_s=0.2,
            )
        with pytest.raises(OnvifError, match="RelativeMove response timed out after send"):
            await movement_client.relative_move(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                pan=0.1,
                tilt=0.0,
                zoom=0.0,
            )
        with pytest.raises(OnvifError, match="Stop response timed out after send"):
            await movement_client.stop(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
            )

    asyncio.run(scenario())

    assert get_presets_attempts == [
        ("1.2", "digest"),
        ("1.2", "text"),
        ("1.2", "none"),
        ("1.1", "digest"),
        ("1.1", "text"),
    ]
    assert get_status_attempts == get_presets_attempts
    assert mutation_calls == {
        "SetPreset": 1,
        "RemovePreset": 1,
        "GotoPreset": 1,
        "AbsoluteMove": 1,
        "ContinuousMove": 1,
        "RelativeMove": 1,
        "Stop": 1,
    }


def test_onvif_preset_mutations_do_not_retry_ambiguous_response_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif.client import (
        OnvifAmbiguousMutationError,
        OnvifClient,
    )
    import toposync_ext_cameras.onvif.client as onvif_mod

    presets_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Body><tptz:GetPresetsResponse /></s:Body>
</s:Envelope>
"""
    mutation_calls = {"SetPreset": 0, "RemovePreset": 0}

    async def fake_post_soap(
        *,
        url: str,
        body: bytes,
        timeout_s: float,
        soap_action: str | None,
        soap_version: str,
    ) -> bytes:
        _ = url, body, timeout_s, soap_version
        action = str(soap_action or "").rsplit("/", 1)[-1]
        if action == "GetPresets":
            return presets_xml
        if action in mutation_calls:
            mutation_calls[action] += 1
            return b"<truncated-response"
        raise RuntimeError(f"Unexpected ONVIF action: {soap_action}")

    monkeypatch.setattr(onvif_mod, "_http_post_soap", fake_post_soap)

    async def scenario() -> None:
        client = OnvifClient(xaddr="http://192.168.0.10/onvif/device_service")
        with pytest.raises(OnvifAmbiguousMutationError, match="Invalid ONVIF XML"):
            await client.set_preset(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                preset_name="Temporary restore",
            )
        with pytest.raises(OnvifAmbiguousMutationError, match="Invalid ONVIF XML"):
            await client.remove_preset(
                "http://192.168.0.10/onvif/ptz_service",
                profile_token="profile-main",
                preset_token="temporary-42",
            )

    asyncio.run(scenario())

    assert mutation_calls == {"SetPreset": 1, "RemovePreset": 1}
