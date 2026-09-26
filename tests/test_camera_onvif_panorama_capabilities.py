from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

from toposync_ext_cameras.onvif import client as onvif


POSITION_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
VELOCITY_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
ZOOM_POSITION_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"


def envelope(body: str, *, header: str = "", soap_namespace: str = onvif.SOAP12_NS) -> bytes:
    return (
        f'<s:Envelope xmlns:s="{soap_namespace}" xmlns:tt="{onvif.TT_NS}" '
        f'xmlns:tptz="{onvif.PTZ_NS}" xmlns:trt="{onvif.TRT_NS}" xmlns:tds="{onvif.TDS_NS}">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def space(uri: str = POSITION_SPACE, *, minimum: str = "-1", maximum: str = "1") -> str:
    return (
        f"<tt:URI>{uri}</tt:URI>"
        f"<tt:XRange><tt:Min>{minimum}</tt:Min><tt:Max>{maximum}</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>"
    )


def configuration(*, token: str = "ptz-main", limits: str = "", default: str = POSITION_SPACE) -> str:
    return (
        f'<tptz:PTZConfiguration token="{token}">'
        "<tt:NodeToken>node-main</tt:NodeToken>"
        f"<tt:DefaultAbsolutePantTiltPositionSpace>{default}</tt:DefaultAbsolutePantTiltPositionSpace>"
        f"{limits}</tptz:PTZConfiguration>"
    )


def responses() -> dict[str, str]:
    return {
        "GetConfigurations": f"<tptz:GetConfigurationsResponse>{configuration()}</tptz:GetConfigurationsResponse>",
        "GetConfigurationOptions": (
            "<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions><tt:Spaces>"
            f"<tt:AbsolutePanTiltPositionSpace>{space()}</tt:AbsolutePanTiltPositionSpace>"
            f"<tt:ContinuousPanTiltVelocitySpace>{space(VELOCITY_SPACE)}</tt:ContinuousPanTiltVelocitySpace>"
            "</tt:Spaces></tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>"
        ),
        "GetNodes": (
            '<tptz:GetNodesResponse><tptz:PTZNode token="node-other">'
            "<tt:MaximumNumberOfPresets>99</tt:MaximumNumberOfPresets></tptz:PTZNode>"
            '<tptz:PTZNode token="node-main"><tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>'
            "<tt:HomeSupported>false</tt:HomeSupported></tptz:PTZNode></tptz:GetNodesResponse>"
        ),
        "GetProfiles": (
            '<trt:GetProfilesResponse><trt:Profiles token="profile-main">'
            '<tt:Name>Main</tt:Name><tt:PTZConfiguration token="ptz-main" />'
            "</trt:Profiles></trt:GetProfilesResponse>"
        ),
    }


def install_transport(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, str]) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    async def post(**kwargs) -> bytes:
        method = kwargs["soap_action"].rsplit("/", 1)[-1]
        calls.append((method, kwargs["body"]))
        if method not in payloads:
            raise onvif.OnvifError("unsupported operation with sensitive device detail")
        namespace = onvif.SOAP12_NS if kwargs["soap_version"] == "1.2" else onvif.SOAP11_NS
        # A misleading Header must never create a profile, configuration or node.
        return envelope(payloads[method], header=configuration(token="header-only"), soap_namespace=namespace)

    monkeypatch.setattr(onvif, "_http_post_soap", post)
    return calls


def discover(client: onvif.OnvifClient | None = None, **kwargs) -> dict:
    return asyncio.run((client or onvif.OnvifClient("device", auth_mode="none")).get_ptz_capabilities(
        "http://camera.test/ptz", profile_token="profile-main", **kwargs,
    ))


def zoom_space(uri: str = ZOOM_POSITION_SPACE, *, minimum: str = "0", maximum: str = "1") -> str:
    return (
        f"<tt:URI>{uri}</tt:URI>"
        f"<tt:XRange><tt:Min>{minimum}</tt:Min><tt:Max>{maximum}</tt:Max></tt:XRange>"
    )


def add_zoom(
    payloads: dict[str, str], *, default: str = ZOOM_POSITION_SPACE,
    advertised: str | None = None, limits: str = "",
) -> None:
    payloads["GetConfigurations"] = payloads["GetConfigurations"].replace(
        "</tptz:PTZConfiguration>",
        f"<tt:DefaultAbsoluteZoomPositionSpace>{default}</tt:DefaultAbsoluteZoomPositionSpace>"
        f"{limits}</tptz:PTZConfiguration>",
    )
    payloads["GetConfigurationOptions"] = payloads["GetConfigurationOptions"].replace(
        "</tt:Spaces>",
        f"<tt:AbsoluteZoomPositionSpace>{advertised if advertised is not None else zoom_space()}"
        "</tt:AbsoluteZoomPositionSpace></tt:Spaces>",
    )


def test_capabilities_use_exact_profile_configuration_and_node(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    limits = f"<tt:PanTiltLimits><tt:Range>{space(minimum='-0.7', maximum='0.4')}</tt:Range></tt:PanTiltLimits>"
    payloads["GetConfigurations"] = (
        f"<tptz:GetConfigurationsResponse>{configuration(token='wrong')}{configuration(limits=limits)}"
        "</tptz:GetConfigurationsResponse>"
    )
    calls = install_transport(monkeypatch, payloads)
    client = onvif.OnvifClient("device", auth_mode="none")
    profiles = asyncio.run(client.get_profiles("http://camera.test/media"))
    assert profiles[0].ptz_configuration_token == "ptz-main"
    result = discover(client)
    assert result["status"] == "verified"
    assert result["absolute"] is True and result["continuous"] is True
    assert result["relative"] is False
    assert result["limits"]["pan"] == {
        "min": -0.7, "max": 0.4, "space": POSITION_SPACE, "normalized": True,
    }
    assert result["limits_kind"] == "configuration"
    assert result["presets"] == {"maximum_count": 8, "home_supported": False}
    assert {method for method, _ in calls} == {"GetProfiles", "GetConfigurations", "GetNodes", "GetConfigurationOptions"}
    options_body = next(body for method, body in calls if method == "GetConfigurationOptions")
    assert ET.fromstring(options_body).findtext(f".//{{{onvif.PTZ_NS}}}ConfigurationToken") == "ptz-main"


def test_no_configuration_binding_does_not_guess_or_make_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_transport(monkeypatch, responses())
    result = discover()
    assert result["status"] == "unknown"
    assert result["absolute"] is None
    assert result["limits"]["pan"] is None
    assert result["reasons"] == ["missing_profile_configuration"]
    assert calls == []


def test_configuration_conflict_with_discovered_profile_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_transport(monkeypatch, responses())
    client = onvif.OnvifClient("device", auth_mode="none")
    asyncio.run(client.get_profiles("http://camera.test/media"))
    result = discover(client, configuration_token="another-lens")
    assert result["reasons"] == ["profile_configuration_mismatch"]
    assert [method for method, _ in calls] == ["GetProfiles"]


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_exact_configuration_stays_unknown(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    payloads = responses()
    payloads["GetConfigurations"] = (
        f"<tptz:GetConfigurationsResponse>{configuration() * count}</tptz:GetConfigurationsResponse>"
    )
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["status"] == "unknown"
    assert result["reasons"] == ["configuration_not_unique"]


@pytest.mark.parametrize("minimum,maximum", [("NaN", "1"), ("-inf", "1"), ("-1", "inf"), ("2", "1"), ("", "1")])
def test_invalid_ranges_do_not_become_capabilities(monkeypatch: pytest.MonkeyPatch, minimum: str, maximum: str) -> None:
    payloads = responses()
    payloads["GetConfigurationOptions"] = payloads["GetConfigurationOptions"].replace(
        space(), space(minimum=minimum, maximum=maximum),
    )
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute"] is None
    assert result["limits"]["pan"] is None
    assert "invalid_absolute_space" in result["reasons"]


def test_generic_pan_tilt_space_uri_must_match_the_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = responses()
    payloads["GetConfigurationOptions"] = payloads["GetConfigurationOptions"].replace(
        f"<tt:ContinuousPanTiltVelocitySpace>{space(VELOCITY_SPACE)}",
        f"<tt:ContinuousPanTiltVelocitySpace>{space(POSITION_SPACE)}",
    )
    install_transport(monkeypatch, payloads)

    result = discover(configuration_token="ptz-main")

    assert result["absolute"] is True
    assert result["continuous"] is None
    assert result["spaces"]["continuous"] == []
    assert "invalid_continuous_space" in result["reasons"]


def test_custom_space_is_preserved_without_unit_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {method: body.replace(POSITION_SPACE, "urn:vendor:pan-steps") for method, body in responses().items()}
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["limits"]["pan"] == {
        "min": -1.0, "max": 1.0, "space": "urn:vendor:pan-steps", "normalized": False,
    }
    assert result["limits_kind"] == "space"


def test_missing_default_does_not_choose_first_space(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    payloads["GetConfigurations"] = f"<tptz:GetConfigurationsResponse>{configuration(default='')}</tptz:GetConfigurationsResponse>"
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute"] is True
    assert result["limits"]["pan"] is None
    assert result["status"] == "partial"
    assert result["reasons"] == ["absolute_default_space_not_unique"]


def test_unavailable_reads_return_unknown_without_device_error_details(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    del payloads["GetNodes"]
    del payloads["GetConfigurationOptions"]
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["status"] == "unknown"
    assert result["absolute"] is None
    assert result["presets"]["maximum_count"] is None
    assert result["reasons"] == ["configuration_options_unavailable", "nodes_unavailable"]
    assert "sensitive" not in str(result)


def test_profiles_and_device_information_ignore_header_and_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_profiles = responses()["GetProfiles"]
    fake_profiles = actual_profiles.replace("profile-main", "profile-header").replace("ptz-main", "ptz-header")
    parsed = onvif._parse_profiles(envelope(actual_profiles, header=fake_profiles), soap_ns=onvif.SOAP12_NS)
    assert [(profile.token, profile.ptz_configuration_token) for profile in parsed] == [("profile-main", "ptz-main")]

    async def post(**kwargs) -> bytes:
        assert kwargs["soap_action"].endswith("/GetDeviceInformation")
        return envelope(
            "<tds:GetDeviceInformationResponse><tds:Manufacturer>TP-Link</tds:Manufacturer>"
            "<tds:Model>C530WS</tds:Model><tds:FirmwareVersion>1.3.5</tds:FirmwareVersion>"
            "<tds:SerialNumber>private-serial</tds:SerialNumber><tds:HardwareId>private-hardware</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>",
            header="<tds:Manufacturer>Incorrect</tds:Manufacturer>",
        )

    monkeypatch.setattr(onvif, "_http_post_soap", post)
    result = asyncio.run(onvif.OnvifClient("camera.test", auth_mode="none").get_device_information())
    assert result == {"manufacturer": "TP-Link", "model": "C530WS", "firmware_version": "1.3.5"}


def test_read_only_discovery_negotiates_soap11(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    payloads = responses()

    async def post(**kwargs) -> bytes:
        calls.append(kwargs["soap_version"])
        if kwargs["soap_version"] == "1.2":
            raise onvif.OnvifError("unsupported SOAP version")
        return envelope(payloads[kwargs["soap_action"].rsplit("/", 1)[-1]], soap_namespace=onvif.SOAP11_NS)

    monkeypatch.setattr(onvif, "_http_post_soap", post)
    assert discover(configuration_token="ptz-main")["status"] == "verified"
    assert "1.1" in calls


@pytest.mark.parametrize("soap_namespace", [onvif.SOAP11_NS, onvif.SOAP12_NS])
@pytest.mark.parametrize("reported", [False, True])
def test_status_preserves_reported_position_spaces_without_guessing(soap_namespace: str, reported: bool) -> None:
    pan_attribute = ' space="urn:vendor:pan-degrees"' if reported else ""
    zoom_attribute = ' space="urn:vendor:focal-millimeters"' if reported else ""
    payload = envelope(
        "<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position>"
        f'<tt:PanTilt x="37.5" y="-12"{pan_attribute}/>'
        f'<tt:Zoom x="24"{zoom_attribute}/>'
        "</tt:Position><tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt></tt:MoveStatus>"
        "</tptz:PTZStatus></tptz:GetStatusResponse>", soap_namespace=soap_namespace,
    )
    status = onvif._parse_ptz_status(payload, soap_ns=soap_namespace)
    assert (status.pan, status.tilt, status.zoom, status.move_status) == (37.5, -12.0, 24.0, "IDLE")
    assert status.pan_tilt_space == ("urn:vendor:pan-degrees" if reported else "")
    assert status.zoom_space == ("urn:vendor:focal-millimeters" if reported else "")
    assert onvif.OnvifPtzStatus(.1, .2, .3).pan_tilt_space == ""


def test_absolute_zoom_selects_matching_default_and_intersects_configuration_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    add_zoom(payloads, limits=f"<tt:ZoomLimits><tt:Range>{zoom_space(minimum='.2', maximum='.7')}</tt:Range></tt:ZoomLimits>")
    payloads["GetConfigurationOptions"] = payloads["GetConfigurationOptions"].replace(
        "<tt:AbsoluteZoomPositionSpace>",
        f"<tt:AbsoluteZoomPositionSpace>{zoom_space('urn:vendor:millimeters', maximum='80')}"
        "</tt:AbsoluteZoomPositionSpace><tt:AbsoluteZoomPositionSpace>", 1,
    )
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute_zoom"] is True
    assert result["defaults"]["absolute_zoom"] == ZOOM_POSITION_SPACE
    assert result["limits"]["zoom"] == {
        "min": .2, "max": .7, "space": ZOOM_POSITION_SPACE, "normalized": True,
    }
    assert result["status"] == "verified"


@pytest.mark.parametrize("default,duplicate", [("", False), ("urn:missing", False), (ZOOM_POSITION_SPACE, True)])
def test_absolute_zoom_missing_or_ambiguous_default_has_no_usable_limits(monkeypatch: pytest.MonkeyPatch, default: str, duplicate: bool) -> None:
    payloads = responses()
    add_zoom(payloads, default=default)
    if duplicate:
        payloads["GetConfigurationOptions"] = payloads["GetConfigurationOptions"].replace(
            "</tt:Spaces>", f"<tt:AbsoluteZoomPositionSpace>{zoom_space()}</tt:AbsoluteZoomPositionSpace></tt:Spaces>",
        )
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute_zoom"] is True
    assert result["limits"]["zoom"] is None
    assert result["reasons"] == ["absolute_zoom_default_space_not_unique"]


@pytest.mark.parametrize("minimum,maximum", [("NaN", "1"), ("0", "inf"), ("2", "1"), ("", "1")])
def test_absolute_zoom_invalid_range_is_unknown(monkeypatch: pytest.MonkeyPatch, minimum: str, maximum: str) -> None:
    payloads = responses()
    add_zoom(payloads, advertised=zoom_space(minimum=minimum, maximum=maximum))
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute_zoom"] is None
    assert result["limits"]["zoom"] is None
    assert result["reasons"] == ["invalid_absolute_zoom_space"]


@pytest.mark.parametrize("uri,minimum,maximum", [
    ("urn:vendor:focal-millimeters", "8", "80"),
    (ZOOM_POSITION_SPACE, "-1", "1"),
    (ZOOM_POSITION_SPACE, "0", "10"),
])
def test_absolute_zoom_nonstandard_units_are_preserved_without_normalization(monkeypatch: pytest.MonkeyPatch, uri: str, minimum: str, maximum: str) -> None:
    payloads = responses()
    add_zoom(payloads, default=uri, advertised=zoom_space(uri, minimum=minimum, maximum=maximum))
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["limits"]["zoom"] == {
        "min": float(minimum), "max": float(maximum), "space": uri, "normalized": False,
    }


@pytest.mark.parametrize("limit,reason", [
    (zoom_space("urn:another-space"), "invalid_configuration_zoom_limits"),
    (zoom_space(minimum="1.1", maximum="2"), "configuration_zoom_limits_outside_space"),
])
def test_absolute_zoom_rejects_incompatible_configuration_limits(monkeypatch: pytest.MonkeyPatch, limit: str, reason: str) -> None:
    payloads = responses()
    add_zoom(payloads, limits=f"<tt:ZoomLimits><tt:Range>{limit}</tt:Range></tt:ZoomLimits>")
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["limits"]["zoom"] is None
    assert result["reasons"] == [reason]


def test_absolute_zoom_is_independent_of_pan_tilt_support(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    payloads["GetConfigurationOptions"] = (
        "<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions><tt:Spaces>"
        "</tt:Spaces></tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>"
    )
    add_zoom(payloads)
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute"] is False
    assert result["absolute_zoom"] is True
    assert result["limits"]["zoom"]["normalized"] is True


def test_absent_absolute_zoom_is_unsupported_but_unavailable_discovery_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = responses()
    install_transport(monkeypatch, payloads)
    result = discover(configuration_token="ptz-main")
    assert result["absolute_zoom"] is False
    assert result["limits"]["zoom"] is None
    del payloads["GetConfigurationOptions"]
    result = discover(configuration_token="ptz-main")
    assert result["absolute_zoom"] is None


@pytest.mark.parametrize('minimum,maximum,expected', [
    ('PT1S', 'PT10S', {'min': 1, 'max': 10}),
    ('PT0.05S', 'PT1M30S', {'min': .05, 'max': 90}),
    ('PT0S', 'PT2S', {'min': 0, 'max': 2}),
    ('P1D', 'P2D', {'min': 86400, 'max': 172800}),
    ('PT10S', 'PT1S', None), ('-PT1S', 'PT10S', None),
    ('PT0S', 'PT0S', None), ('P1M', 'P2M', None), ('PTNaNS', 'PT10S', None),
])
def test_published_timeout_is_duration_not_motor_pulse(monkeypatch, minimum, maximum, expected):
    payloads = responses()
    payloads['GetConfigurationOptions'] = payloads['GetConfigurationOptions'].replace(
        '</tt:Spaces>', '</tt:Spaces><tt:PTZTimeout>'
        f'<tt:Min>{minimum}</tt:Min><tt:Max>{maximum}</tt:Max></tt:PTZTimeout>',
    )
    install_transport(monkeypatch, payloads)
    assert discover(configuration_token='ptz-main')['continuous_timeout_s'] == expected


@pytest.mark.parametrize("known_media", [None, "http://camera.test/media"])
@pytest.mark.parametrize("published_timeout", [False, True])
def test_timeout_reuses_known_media_but_still_discovers_profile_safety(monkeypatch, known_media, published_timeout):
    async def run():
        payloads = responses()
        if published_timeout:
            payloads['GetConfigurationOptions'] = payloads['GetConfigurationOptions'].replace(
                '</tt:Spaces>', '</tt:Spaces><tt:PTZTimeout><tt:Min>PT1S</tt:Min>'
                '<tt:Max>PT10S</tt:Max></tt:PTZTimeout>',
            )
        calls = install_transport(monkeypatch, payloads)
        discovery = []

        async def capabilities(_self):
            discovery.append(True)
            return 'http://camera.test/media', 'http://camera.test/ptz'

        monkeypatch.setattr(onvif.OnvifClient, 'get_capabilities', capabilities)
        client = onvif.OnvifClient('device', auth_mode='none')
        result = await client.continuous_move_timeout(
            'http://camera.test/ptz', profile_token='profile-main', requested_s=.3,
            media_xaddr=known_media,
        )
        assert result == (1.0 if published_timeout else None)
        assert len(discovery) == (0 if known_media else 1)
        assert [method for method, _ in calls] == ['GetProfiles', 'GetConfigurationOptions']

    asyncio.run(run())


def test_device_timeout_clamps_separately_from_controller_stop(monkeypatch, tmp_path):
    from toposync_ext_cameras.ptz_controller import PtzController

    async def run():
        payloads = responses()
        payloads['GetConfigurationOptions'] = payloads['GetConfigurationOptions'].replace(
            '</tt:Spaces>', '</tt:Spaces><tt:PTZTimeout><tt:Min>PT1S</tt:Min>'
            '<tt:Max>PT10S</tt:Max></tt:PTZTimeout>',
        )
        payloads.update(ContinuousMove='<tptz:ContinuousMoveResponse/>', Stop='<tptz:StopResponse/>')
        calls = install_transport(monkeypatch, payloads)
        client = onvif.OnvifClient('device', auth_mode='none')
        endpoint = 'http://camera.test/ptz'
        await client.get_profiles('http://camera.test/media')
        client._ptz_transport_cache[(endpoint, 'profile-main')] = ('1.2', onvif.SOAP12_NS, 'none')
        stopped = asyncio.Event()
        requested = []

        async def execute(**kwargs):
            command = kwargs['command']
            if command['kind'] == 'continuous_move':
                requested.append(command['timeout_s'])
                device_timeout = await client.continuous_move_timeout(
                    endpoint, profile_token='profile-main', requested_s=command['timeout_s'],
                )
                await client.continuous_move(endpoint, profile_token='profile-main',
                                             pan=.1, tilt=0, zoom=0, timeout_s=device_timeout)
            else:
                await client.stop(endpoint, profile_token='profile-main', pan_tilt=True, zoom=False)
                stopped.set()
            return {'ok': True}

        controller = PtzController(state_path=tmp_path / 'state.json', resolve_device=lambda _: 'head',
                                   resolve_source=lambda _camera, source: source or 'main',
                                   execute_command=execute, get_status=lambda **_: {})
        lease = await controller.acquire(camera_id='camera', owner_kind='manual', owner_id='test', ttl_s=15)
        try:
            await controller.submit(lease_id=lease['lease_id'], fence=lease['fence'], command_id='pulse',
                                    command={'kind': 'continuous_move', 'pan': .1, 'timeout_s': .05})
            await asyncio.wait_for(stopped.wait(), .5)
            assert requested == [.05]
            body = next(body for method, body in calls if method == 'ContinuousMove')
            assert ET.fromstring(body).findtext(f'.//{{{onvif.PTZ_NS}}}Timeout') == 'PT1S'
            assert await client.continuous_move_timeout(endpoint, profile_token='profile-main', requested_s=20) == 10
            assert [method for method, _ in calls].count('GetConfigurationOptions') == 1
        finally:
            await controller.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize(
    "minimum,maximum,requested,expected",
    [
        (1.0, 10.0, 0.6, 1.0),
        (1.0, 10.0, 1.2, 2.0),
        (1.0, 10.0, 2.0, 2.0),
        (0.1, 1.5, 1.2, 1.2),
        (1.0, 10.0, 20.0, 10.0),
    ],
)
def test_device_timeout_prefers_compatible_whole_seconds(
    minimum: float,
    maximum: float,
    requested: float,
    expected: float,
) -> None:
    async def run() -> None:
        client = onvif.OnvifClient("device", auth_mode="none")
        endpoint = "http://camera.test/ptz"
        profile = "profile-main"
        client._continuous_timeout_ranges[(endpoint, profile)] = {
            "min": minimum,
            "max": maximum,
        }
        assert await client.continuous_move_timeout(
            endpoint,
            profile_token=profile,
            requested_s=requested,
        ) == expected

    asyncio.run(run())


def test_continuous_move_body_serializes_whole_second_timeout() -> None:
    async def run() -> None:
        client = onvif.OnvifClient("device", auth_mode="none")
        endpoint = "http://camera.test/ptz"
        profile = "profile-main"
        client._continuous_timeout_ranges[(endpoint, profile)] = {"min": 1.0, "max": 10.0}
        timeout = await client.continuous_move_timeout(
            endpoint,
            profile_token=profile,
            requested_s=1.2,
        )
        body = onvif._tptz_continuous_move_body(
            profile,
            pan=0.1,
            tilt=0.0,
            zoom=0.0,
            timeout_s=timeout,
        )
        assert ET.fromstring(body).findtext(f".//{{{onvif.PTZ_NS}}}Timeout") == "PT2S"

    asyncio.run(run())


@pytest.mark.parametrize(
    "timeout,expected",
    [
        (1.2344, "PT1.2344S"),
        (0.0004, "PT0.0004S"),
        (2.0, "PT2S"),
    ],
)
def test_continuous_move_body_preserves_positive_finite_timeout(
    timeout: float,
    expected: str,
) -> None:
    body = onvif._tptz_continuous_move_body(
        "profile-main",
        pan=0.1,
        tilt=0.0,
        zoom=0.0,
        timeout_s=timeout,
    )
    assert ET.fromstring(body).findtext(f".//{{{onvif.PTZ_NS}}}Timeout") == expected


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), 0.0, -0.1])
def test_continuous_move_body_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(onvif.OnvifError, match="Invalid continuous movement timeout"):
        onvif._tptz_continuous_move_body(
            "profile-main",
            pan=0.1,
            tilt=0.0,
            zoom=0.0,
            timeout_s=timeout,
        )


def test_continuous_move_body_omits_unspecified_timeout() -> None:
    body = onvif._tptz_continuous_move_body(
        "profile-main",
        pan=0.1,
        tilt=0.0,
        zoom=0.0,
        timeout_s=None,
    )
    assert ET.fromstring(body).find(f".//{{{onvif.PTZ_NS}}}Timeout") is None


def test_unpublished_device_timeout_uses_default_and_rejects_unbounded_request(monkeypatch):
    async def run():
        install_transport(monkeypatch, responses())
        client = onvif.OnvifClient('device', auth_mode='none')
        await client.get_profiles('http://camera.test/media')
        assert await client.continuous_move_timeout('http://camera.test/ptz', profile_token='profile-main', requested_s=.2) is None
        with pytest.raises(onvif.OnvifError):
            await client.continuous_move_timeout('http://camera.test/ptz', profile_token='profile-main', requested_s=float('nan'))
    asyncio.run(run())
