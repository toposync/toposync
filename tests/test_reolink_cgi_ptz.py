from __future__ import annotations

import asyncio
import urllib.parse

import pytest


def test_reolink_cgi_presets_are_namespaced_and_recalled_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toposync_ext_cameras.onvif.reolink_cgi import ReolinkCgiClient
    import toposync_ext_cameras.onvif.reolink_cgi as reolink_mod

    slots = [
        {"id": 0, "name": "Toposync Guarda", "enable": 1},
        {"id": 1, "name": "pos2", "enable": 0},
    ]
    calls: list[tuple[str, list[dict[str, object]]]] = []

    def fake_post_json(*, url: str, payload: list[dict[str, object]], timeout_s: float) -> object:
        _ = timeout_s
        command = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("cmd", [""])[0]
        calls.append((command, payload))
        if command == "Login":
            return [{"cmd": "Login", "code": 0, "value": {"Token": {"name": "redacted"}}}]
        if command == "GetPtzPreset":
            return [{"cmd": command, "code": 0, "value": {"PtzPreset": slots}}]
        if command == "SetPtzPreset":
            data = payload[0]["param"]
            assert isinstance(data, dict)
            preset = data["PtzPreset"]
            assert isinstance(preset, dict)
            slots[int(preset["id"])] = {
                "id": int(preset["id"]),
                "name": str(preset["name"]),
                "enable": int(preset["enable"]),
            }
            return [{"cmd": command, "code": 0, "value": {"rspCode": 200}}]
        if command == "PtzCtrl":
            data = payload[0]["param"]
            assert data == {"channel": 0, "op": "ToPos", "id": 0, "speed": 4}
            return [{"cmd": command, "code": 0, "value": {"rspCode": 200}}]
        if command == "Logout":
            return [{"cmd": command, "code": 0, "value": {"rspCode": 200}}]
        raise AssertionError(f"unexpected CGI command {command}")

    monkeypatch.setattr(reolink_mod, "_post_json", fake_post_json)

    async def scenario() -> None:
        client = ReolinkCgiClient(
            device_xaddr="http://192.168.0.10/onvif/device_service",
            username="admin",
            password="secret",
        )
        presets = await client.list_presets()
        assert [(preset.token, preset.name) for preset in presets] == [
            ("reolink:0", "Toposync Guarda")
        ]
        all_slots = await client.list_presets(include_disabled=True)
        assert [(preset.token, preset.enabled) for preset in all_slots] == [
            ("reolink:0", True),
            ("reolink:1", False),
        ]
        created = await client.set_current_position_preset(name="Toposync Porta")
        assert (created.token, created.name, created.enabled) == (
            "reolink:1",
            "Toposync Porta",
            True,
        )
        await client.goto_preset(preset_token="reolink:0")

    asyncio.run(scenario())
    assert [command for command, _payload in calls].count("PtzCtrl") == 1


def test_reolink_cgi_rejects_unscoped_or_out_of_range_preset_tokens() -> None:
    from toposync_ext_cameras.onvif.reolink_cgi import ReolinkCgiError, parse_reolink_preset_token

    assert parse_reolink_preset_token("reolink:0") == 0
    with pytest.raises(ReolinkCgiError):
        parse_reolink_preset_token("0")
    with pytest.raises(ReolinkCgiError):
        parse_reolink_preset_token("reolink:64")
