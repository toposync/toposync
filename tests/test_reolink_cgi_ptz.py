from __future__ import annotations

import asyncio
import copy
import urllib.parse

import pytest


@pytest.mark.parametrize("mutation", [
    None,
    (("value", "ZoomFocus", "channel"), 1),
    (("range", "ZoomFocus", "channel"), False),
    (("range", "ZoomFocus", "channel"), 2),
    (("value", "ZoomFocus", "zoom", "pos"), True),
    (("value", "ZoomFocus", "zoom", "pos"), "4565"),
    (("value", "ZoomFocus", "zoom", "pos"), float("nan")),
    (("value", "ZoomFocus", "zoom", "pos"), 6001),
    (("value", "ZoomFocus", "zoom", "pos"), 999),
    (("range", "ZoomFocus", "zoom", "pos", "min"), None),
    (("range", "ZoomFocus", "zoom", "pos", "max"), float("inf")),
    (("range", "ZoomFocus", "zoom", "pos", "min"), 6000),
    (("range", "ZoomFocus", "zoom", "pos"), []),
    (("value", "ZoomFocus", "zoom"), []),
    (("range",), None),
])
def test_native_zoom_reading_is_channel_bound_and_never_normalized(monkeypatch, mutation):
    import toposync_ext_cameras.onvif.reolink_cgi as module

    response = copy.deepcopy({
        "cmd": "GetZoomFocus", "code": 0,
        "value": {"ZoomFocus": {"channel": 0, "zoom": {"pos": 4565}}},
        "range": {"ZoomFocus": {"channel": 0, "zoom": {"pos": {"min": 1000, "max": 6000}}}},
    })
    if mutation:
        keys, value = mutation
        container = response
        for key in keys[:-1]:
            container = container[key]
        container[keys[-1]] = value
    commands = []

    def post(*, url, payload, timeout_s):
        command = payload[0]["cmd"]
        commands.append(command)
        if command == "Login":
            return [{"cmd": command, "code": 0, "value": {"Token": {"name": "test"}}}]
        if command == "Logout":
            return [{"cmd": command, "code": 0}]
        assert command == "GetZoomFocus"
        assert payload == [{"cmd": command, "action": 1, "param": {"channel": 0}}]
        return [response]

    monkeypatch.setattr(module, "_post_json", post)
    client = module.ReolinkCgiClient("http://camera")
    if mutation:
        with pytest.raises(module.ReolinkCgiError):
            asyncio.run(client.get_native_zoom_position())
    else:
        reading = asyncio.run(client.get_native_zoom_position())
        assert (reading.position, reading.minimum, reading.maximum, reading.channel) == (4565, 1000, 6000, 0)
    assert commands == ["Login", "GetZoomFocus", "Logout"]


@pytest.mark.parametrize("channel", [True, -1, "0", 0.5])
def test_native_zoom_invalid_channel_never_connects(monkeypatch, channel):
    import toposync_ext_cameras.onvif.reolink_cgi as module

    def unexpected(**kwargs):
        pytest.fail("Invalid channel must not connect")

    monkeypatch.setattr(module, "_post_json", unexpected)
    with pytest.raises(module.ReolinkCgiError):
        asyncio.run(module.ReolinkCgiClient("http://camera").get_native_zoom_position(channel=channel))


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
