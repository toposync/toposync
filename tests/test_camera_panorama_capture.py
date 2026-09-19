from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from toposync_ext_cameras.onvif.client import OnvifProfile, OnvifPtzStatus
from toposync_ext_cameras.onvif.reolink_cgi import ReolinkCgiClient, ReolinkCgiError
from toposync_ext_cameras.panorama_capture import PanoramaCamera, PanoramaCaptureError
from toposync_ext_cameras.processing.frame_grabber import CaptureFrameSample
from toposync_ext_cameras.ptz_controller import (
    PtzControlError,
    PtzFailureCode,
    PtzFailureStage,
)


class Services:
    def __init__(self):
        self.calls = []
        self.active = None
        self.binding_current = True
        self.stale = False
        self.presets = []

    async def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == "cameras.control.acquire":
            self.active = {"lease_id": "lease", "fence": 7}
            return self.active.copy()
        if name == "cameras.control.snapshot":
            return {"active_lease": self.active, "transport_binding_current": self.binding_current}
        if name == "cameras.control.submit":
            return {
                "accepted": True, "stale_after_execution": self.stale,
                **{key: kwargs[key] for key in ("lease_id", "fence", "command_id")},
            }
        if name == "cameras.ptz.set_preset":
            token = "temporary" if not self.presets else f"temporary-{len(self.presets) + 1}"
            result = {"token": token, "name": kwargs["preset_name"]}
            self.presets.append(result)
            return result
        if name == "cameras.ptz.list_presets":
            return self.presets
        if name == "cameras.ptz.remove_preset":
            self.presets = [item for item in self.presets if item["token"] != kwargs["preset_token"]]
        return {"ok": True}


@pytest.mark.parametrize("condition", ["transient_stop", "replacement_owner", "fault", "persistent"])
def test_renewal_retries_only_while_the_same_fenced_lease_is_current(condition):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        original_call = services.call
        attempts = []

        async def call(name, **kwargs):
            if name == "cameras.control.renew":
                attempts.append(kwargs)
                if len(attempts) == 1 or condition == "persistent":
                    if condition == "replacement_owner":
                        services.active = {"lease_id": "replacement", "fence": 8}
                    raise RuntimeError("Renewal unavailable during Stop")
            result = await original_call(name, **kwargs)
            if name == "cameras.control.snapshot":
                result["state"] = "fault" if condition == "fault" else "stopping"
            return result

        services.call = call
        try:
            if condition == "transient_stop":
                await camera.renew()
                assert camera._control_error is None
                assert len(attempts) == 2
            else:
                with pytest.raises(PanoramaCaptureError, match="control_lost"):
                    await camera.renew()
                assert len(attempts) == (3 if condition == "persistent" else 1)
            assert all(item["lease_id"] == "lease" and item["fence"] == 7 for item in attempts)
            assert not any(name == "cameras.control.submit" for name, _ in services.calls)
        finally:
            await camera.close()

    asyncio.run(run())


class CaptureService:
    def __init__(self):
        self.requests = []
        self.released = []
        self.fail_configured = False
        self.sample = CaptureFrameSample(
            frame=np.zeros((12, 20, 3), dtype=np.uint8),
            source_received_monotonic=123.4, generation=3, sequence=1,
            capture_instance="capture-one",
        )

    async def open(self, request, dependencies):
        self.requests.append(request)
        if self.fail_configured and not request.rtsp_url:
            raise RuntimeError("rtsp://do-not-expose:secret@camera")
        return SimpleNamespace(
            lease_id=f"video-{len(self.requests)}",
            grabber=SimpleNamespace(get_latest_sample=lambda: self.sample),
        )

    async def release(self, lease_id):
        self.released.append(lease_id)


def environment():
    configuration = {
        "devices": [{
            "id": "camera", "name": "Camera", "enabled": True,
            "control": {"type": "onvif"},
            "onvif": {"xaddr": "http://camera/onvif/device_service", "username": "user", "password": "secret"},
            "sources": [{
                "id": "wide", "enabled": True, "is_default": True,
                "origin": {"type": "onvif_profile", "profile_token": "profile", "has_ptz": True},
                "video": {"width": 20, "height": 12},
            }],
        }]
    }
    settings = SimpleNamespace(extensions={"com.toposync.cameras": configuration})
    dependencies = SimpleNamespace(config_store=SimpleNamespace(get_settings=AsyncMock(return_value=settings)))
    source_space = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
    client = SimpleNamespace(
        username="camera-user", password="camera-password", xaddr="http://camera", timeout_s=3,
        get_capabilities=AsyncMock(return_value=("http://camera/media", "http://camera/ptz")),
        get_profiles=AsyncMock(return_value=[OnvifProfile("profile", "wide", width=20, height=12, has_ptz=True)]),
        get_device_information=AsyncMock(return_value={"manufacturer": "TP-Link", "model": "C530WS"}),
        get_ptz_status=AsyncMock(return_value=OnvifPtzStatus(pan=.2, tilt=-.3, move_status="UNKNOWN", error="0")),
        get_stream_uri=AsyncMock(return_value="rtsp://camera/exact-profile"),
        get_ptz_capabilities=AsyncMock(return_value={
            "absolute": True, "continuous": True, "relative": True,
            "spaces": {
                "absolute": [{"uri": source_space, "normalized": True}],
                "continuous": [{"uri": "velocity-space", "normalized": True}],
                "relative": [{"uri": "translation-space", "normalized": True}],
            },
            "defaults": {
                "absolute": source_space, "continuous": "velocity-space", "relative": "translation-space",
            },
            "continuous_timeout_s": {"min": 0.05, "max": 2.0},
            "limits": {"pan": {"min": -1, "max": 1}, "tilt": {"min": -.8, "max": 1}},
        }),
    )
    services, capture = Services(), CaptureService()
    resolver = AsyncMock(return_value=(client, "http://camera/ptz", "profile", "wide", None))
    camera = PanoramaCamera(
        camera_id="camera", source_id="wide", owner_id="job", services=services,
        capture_service=capture, dependencies=dependencies, resolve_onvif=resolver,
    )
    return camera, configuration, client, services, capture


@pytest.mark.parametrize("model", ["C530WS", "Tapo C530WS"])
def test_discovery_is_read_only_and_preserves_unknown_axes(model):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_device_information.return_value["model"] = model
        result = await camera.discover()
        assert result["absolute_supported"] is True
        assert result["position"]["pan"] == .2
        assert result["position"]["tilt"] == -.3
        assert result["position"]["zoom"] is None
        assert result["position"]["position_provenance"]["pan_tilt"]["source"] == "ONVIF.GetStatus"
        assert result["position"]["position_provenance"]["native"] is None
        assert result["source_identity"]["zoom"] is None
        assert result["source_identity"]["zoom_state"] == "unreported"
        assert result["position"]["move_status"] == "UNKNOWN"
        assert result["position"]["error"] == ""
        assert services.calls == []
        client.get_device_information.return_value = {"manufacturer": "Other"}
        camera._capabilities = None
        assert (await camera.discover())["position"]["error"] == "0"
    asyncio.run(run())


def test_literal_zero_error_is_not_normalized_for_another_tapo_model():
    async def run():
        camera, _, client, _, _ = environment()
        client.get_device_information.return_value["model"] = "Tapo C200"
        assert (await camera.discover())["position"]["error"] == "0"
    asyncio.run(run())


def test_all_motion_uses_fence_and_never_changes_zoom():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        try:
            await camera.move_absolute(pan=.3, tilt=.4)
            await camera.move_velocity(pan=.25, timeout_s=.5)
            await camera.stop()
            submits = [parameters for name, parameters in services.calls if name.endswith("submit")]
            assert len(submits) == 3
            assert all(item["lease_id"] == "lease" and item["fence"] == 7 for item in submits)
            assert "zoom" not in submits[0]["command"]
            assert submits[1]["command"]["zoom"] == 0
            assert submits[1]["command"]["allow_relative_fallback"] is False
            assert submits[2]["command"] == {"kind": "stop", "pan_tilt": True, "zoom": False}
            with pytest.raises(PanoramaCaptureError, match="outside_camera_limits"):
                await camera.move_absolute(pan=0, tilt=-.9)
            with pytest.raises(PanoramaCaptureError, match="invalid_movement_duration"):
                await camera.move_velocity(pan=.5, timeout_s=60)
        finally:
            await camera.close()
    asyncio.run(run())


def test_preemption_and_stale_receipts_never_report_success():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        try:
            services.stale = True
            with pytest.raises(PanoramaCaptureError, match="movement_unconfirmed"):
                await camera.stop()
            services.active = {"lease_id": "another-owner", "fence": 8}
            count = len(services.calls)
            with pytest.raises(PanoramaCaptureError, match="control_lost"):
                await camera.stop()
            assert not any(name.endswith("submit") for name, _ in services.calls[count:])
        finally:
            await camera.close()
    asyncio.run(run())


def test_submit_propagates_only_sanitized_ptz_failure_diagnostics():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        original_call = services.call

        async def call(name, **kwargs):
            if name == "cameras.control.submit":
                raise PtzControlError(
                    "http://admin:secret@camera/private",
                    failure_code=PtzFailureCode.TRANSPORT_TIMEOUT,
                    failure_stage=PtzFailureStage.COMMAND_DISPATCH,
                )
            return await original_call(name, **kwargs)

        services.call = call
        try:
            with pytest.raises(PanoramaCaptureError) as raised:
                await camera.move_velocity(pan=0.1, timeout_s=0.3)
            error = raised.value
            assert error.code == "movement_unconfirmed"
            assert error.ptz_failure_code == "transport_timeout"
            assert error.ptz_failure_stage == "command_dispatch"
            assert "admin" not in str(error)
            assert "secret" not in str(error)
            assert "http://" not in str(error)
        finally:
            await camera.close()

    asyncio.run(run())


@pytest.mark.parametrize("preempted", [False, True])
def test_stop_retry_handles_watchdog_but_never_stops_a_new_owner(preempted):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        original = services.call
        submissions = []

        async def call(name, **kwargs):
            if name == "cameras.control.submit":
                submissions.append(kwargs)
                if len(submissions) == 1:
                    if preempted:
                        services.active = {"lease_id": "new-owner", "fence": 8}
                    raise RuntimeError("Device is stopping")
            return await original(name, **kwargs)

        services.call = call
        try:
            if preempted:
                with pytest.raises(PanoramaCaptureError, match="control_lost"):
                    await camera.stop()
                assert len(submissions) == 1
            else:
                assert (await camera.stop())["accepted"]
                assert len(submissions) == 2
                assert all(item["fence"] == 7 and item["command"]["kind"] == "stop" for item in submissions)
                assert submissions[0]["command"]["zoom"] is False
                assert submissions[1]["command"]["zoom"] is True
        finally:
            await camera.close()

    asyncio.run(run())


def test_configuration_change_blocks_moves_but_allows_owned_stop():
    async def run():
        camera, configuration, _, services, _ = environment()
        await camera.acquire()
        try:
            configuration["devices"][0]["onvif"]["password"] = "changed"
            services.binding_current = False
            camera._control_error = "renewal_failed"
            with pytest.raises(PanoramaCaptureError, match="renewal_failed"):
                await camera.move_absolute(pan=0, tilt=0)
            assert (await camera.stop())["accepted"]
            camera._control_error = None
            with pytest.raises(PanoramaCaptureError, match="camera_configuration_changed"):
                await camera.move_absolute(pan=0, tilt=0)
            assert (await camera.stop())["accepted"]
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("origin_type", ["onvif_profile", "rtsp"])
def test_direct_fallback_is_exact_profile_ephemeral_and_does_not_forge_media_time(origin_type):
    async def run():
        camera, configuration, client, services, capture = environment()
        configuration["devices"][0]["sources"][0]["origin"].update(
            type=origin_type, rtsp_url="rtsp://relay/configured-source",
            stream_username="relay-user", stream_password="relay-password",
        )
        before = copy.deepcopy(configuration)
        capture.fail_configured = True
        try:
            result = await camera.frame(timeout_s=.05)
            assert result["media_time"] is None
            assert result["received_monotonic"] == 123.4
            assert result["sequence"] == 1 and result["generation"] == 3
            assert result["capture_instance"] == "capture-one"
            assert result["capture_evidence"]["capture_instance"] == "capture-one"
            assert result["physical_timestamp_verified"] is False
            assert result["transport"] == "direct"
            assert result["transport_fallback_reason"] == "video_unavailable"
            client.get_stream_uri.assert_awaited_once_with("http://camera/media", profile_token="profile")
            assert len(capture.requests) == 2
            assert capture.requests[1].rtsp_url == "rtsp://camera/exact-profile"
            assert capture.requests[1].camera_id == "camera"
            assert capture.requests[1].source_id == "wide"
            assert capture.requests[1].username == "camera-user"
            assert capture.requests[1].password == "camera-password"
            assert configuration == before
            assert services.calls == []
            result["image"][:] = 255
            assert not capture.sample.frame.any()
            with pytest.raises(PanoramaCaptureError, match="fresh_frame_unavailable"):
                await camera.frame(timeout_s=.025)
        finally:
            await camera.close()
        assert capture.released == ["video-2"]
    asyncio.run(run())


def test_frame_identity_distinguishes_capture_instances_with_reused_counters():
    async def run():
        camera, _, _, _, capture = environment()
        try:
            first = await camera.frame(timeout_s=.05)
            capture.sample = replace(capture.sample, capture_instance="capture-two")
            second = await camera.frame(timeout_s=.05)
            assert first["capture_instance"] == "capture-one"
            assert second["capture_instance"] == "capture-two"
            assert (first["generation"], first["sequence"]) == (
                second["generation"],
                second["sequence"],
            )
        finally:
            await camera.close()

    asyncio.run(run())


def test_direct_fallback_rejects_rtsp_without_explicit_profile_binding():
    async def run():
        camera, configuration, client, services, capture = environment()
        configuration["devices"][0]["sources"][0]["origin"].update(
            type="rtsp", profile_token=None, rtsp_url="rtsp://relay/configured-source",
        )
        before = copy.deepcopy(configuration)
        capture.fail_configured = True
        try:
            with pytest.raises(PanoramaCaptureError, match="direct_source_binding_unverified"):
                await camera.frame(timeout_s=.05)
            client.get_stream_uri.assert_not_awaited()
            assert len(capture.requests) == 1
            assert configuration == before
            assert services.calls == []
        finally:
            await camera.close()
    asyncio.run(run())


def test_wrong_profile_blocks_capture_and_control():
    async def run():
        camera, _, client, services, capture = environment()
        client.get_profiles.return_value = [OnvifProfile("another", "tele", has_ptz=True)]
        with pytest.raises(PanoramaCaptureError, match="source_binding_unverified"):
            await camera.acquire()
        assert not services.calls and not capture.requests
    asyncio.run(run())


def test_unsupported_units_never_become_normalized_motion():
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_capabilities.return_value["spaces"]["continuous"][0]["normalized"] = False
        await camera.acquire()
        try:
            with pytest.raises(PanoramaCaptureError, match="normalized_motion_unavailable"):
                await camera.move_velocity(pan=.5)
            assert not any(name.endswith("submit") for name, _ in services.calls)
        finally:
            await camera.close()
    asyncio.run(run())


def test_continuous_motion_requires_a_bounded_device_failsafe():
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_capabilities.return_value["continuous_timeout_s"] = None
        capabilities = await camera.discover()
        assert capabilities["continuous_supported"] is True
        assert capabilities["velocity_supported"] is False
        await camera.acquire()
        try:
            with pytest.raises(PanoramaCaptureError, match="normalized_motion_unavailable"):
                await camera.move_velocity(pan=0.1)
            assert not any(name.endswith("submit") for name, _ in services.calls)
        finally:
            await camera.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "timeout_bounds",
    [
        {"min": 2.1, "max": 10.0},
        {"min": -0.1, "max": 1.0},
        {"min": float("nan"), "max": 1.0},
        {"min": 1.0, "max": 0.5},
        {"min": 0.0, "max": 0.0},
    ],
)
def test_invalid_device_failsafe_ranges_never_enable_continuous_motion(timeout_bounds):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_capabilities.return_value["continuous_timeout_s"] = timeout_bounds
        capabilities = await camera.discover()
        assert capabilities["velocity_supported"] is False
        await camera.acquire()
        try:
            with pytest.raises(PanoramaCaptureError, match="normalized_motion_unavailable"):
                await camera.move_velocity(pan=0.1)
            assert not any(name.endswith("submit") for name, _ in services.calls)
        finally:
            await camera.close()

    asyncio.run(run())


@pytest.mark.parametrize("control", ["owned", "preempted", "stale_receipt"])
def test_relative_only_camera_never_submits_continuous_motion_and_preserves_fencing(control):
    async def run():
        camera, _, client, services, _ = environment()
        published = client.get_ptz_capabilities.return_value
        published["continuous"] = False
        published["spaces"]["continuous"] = []
        capabilities = await camera.discover()
        assert capabilities["relative_supported"] is True
        assert capabilities["velocity_supported"] is False
        await camera.acquire()
        try:
            with pytest.raises(PanoramaCaptureError, match="normalized_motion_unavailable"):
                await camera.move_velocity(pan=.1)
            if control == "preempted":
                services.active = {"lease_id": "replacement", "fence": 8}
            services.stale = control == "stale_receipt"
            if control == "owned":
                await camera.move_relative(pan=.1)
            else:
                code = "control_lost" if control == "preempted" else "movement_unconfirmed"
                with pytest.raises(PanoramaCaptureError, match=code):
                    await camera.move_relative(pan=.1)
            submits = [parameters for name, parameters in services.calls if name.endswith("submit")]
            assert len(submits) == (0 if control == "preempted" else 1)
            for submitted in submits:
                assert submitted["lease_id"] == "lease" and submitted["fence"] == 7
                assert submitted["command"] == {
                    "kind": "relative_move", "pan": .1, "tilt": 0.0, "zoom": 0.0,
                }
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["continuous", "relative"])
@pytest.mark.parametrize(
    "bounds, requested, expected",
    [
        ((-.2, .3, -.4, .1), (.8, -.8), (.3, -.4)),
        ((-.2, .3, 0, 0), (.1, .1), None),
        ((-.2, .3, .1, .2), (.1, 0), None),
        ((0, .3, -.4, .1), (-.1, 0), None),
    ],
)
def test_motion_obeys_each_published_axis_without_inventing_movement(mode, bounds, requested, expected):
    async def run():
        camera, _, client, services, _ = environment()
        space = client.get_ptz_capabilities.return_value["spaces"][mode][0]
        space.update(x={"min": bounds[0], "max": bounds[1]}, y={"min": bounds[2], "max": bounds[3]})
        await camera.acquire()
        try:
            move = camera.move_velocity if mode == "continuous" else camera.move_relative
            if expected is None:
                with pytest.raises(PanoramaCaptureError, match="axis_movement_unavailable"):
                    await move(pan=requested[0], tilt=requested[1])
            else:
                await move(pan=requested[0], tilt=requested[1])
            submits = [parameters for name, parameters in services.calls if name.endswith("submit")]
            assert len(submits) == (0 if expected is None else 1)
            if submits:
                command = submits[0]["command"]
                assert (command["pan"], command["tilt"]) == expected
                assert command["kind"] == f"{mode}_move" and command["zoom"] == 0
                assert ("timeout_s" in command) is (mode == "continuous")
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["absolute", "continuous", "relative"])
def test_duplicate_default_space_is_not_supported(mode):
    async def run():
        camera, _, client, services, _ = environment()
        spaces = client.get_ptz_capabilities.return_value["spaces"][mode]
        spaces.append(copy.deepcopy(spaces[0]))
        capabilities = await camera.discover()
        assert capabilities[f"{mode}_supported"] is False
        assert not services.calls
    asyncio.run(run())


def test_return_position_or_temporary_preset_is_explicit_and_fenced():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        try:
            saved = await camera.save_return()
            assert {key: saved[key] for key in ("kind", "pan", "tilt", "zoom")} == {
                "kind": "absolute", "pan": .2, "tilt": -.3, "zoom": None,
            }
            assert saved["role"] == "original"
            assert saved["binding"]["profile_token"] == "profile"
            await camera.return_to(saved)
            camera._capabilities["absolute_supported"] = False
            saved = await camera.save_return("work")
            assert saved["kind"] == "preset"
            await camera.return_to(saved)
            assert services.calls[-1][1]["command"] == {"kind": "goto_preset", "preset_token": "temporary"}
            with pytest.raises(PanoramaCaptureError, match="return_owner_mismatch"):
                await camera.remove_return({**saved, "owner_id": "another"})
            services.presets[0]["name"] = "Repurposed preset"
            with pytest.raises(PanoramaCaptureError, match="return_preset_changed"):
                await camera.return_to(saved)
            with pytest.raises(PanoramaCaptureError, match="return_preset_changed"):
                await camera.remove_return(saved)
            services.presets[0]["name"] = saved["preset_name"]
            await camera.remove_return(saved)
            assert any(name == "cameras.ptz.remove_preset" for name, _ in services.calls)
            assert services.presets == []
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("value", [{"Ppos": 1584, "channel": 0}, {"channel": 0}])
def test_reolink_native_position_keeps_absent_axes_unknown(monkeypatch, value):
    async def session(self, callback):
        return await callback("session")

    command = AsyncMock(return_value={"value": {"PtzCurPos": value}})
    monkeypatch.setattr(ReolinkCgiClient, "_with_session", session)
    monkeypatch.setattr(ReolinkCgiClient, "_command", command)
    position = asyncio.run(ReolinkCgiClient("http://camera").get_current_position())
    assert position.pan == value.get("Ppos")
    assert position.tilt is None
    command.assert_awaited_once_with(
        "session", command="GetPtzCurPos", action=1, param={"PtzCurPos": {"channel": 0}}
    )


@pytest.mark.parametrize("value", [
    {"channel": 1, "Ppos": 4}, {"channel": 0, "Ppos": True},
    {"channel": 0, "Ppos": "nan"}, {"channel": 0, "Ppos": "invalid"},
])
def test_reolink_rejects_wrong_channel_and_non_numeric_positions(monkeypatch, value):
    async def session(self, callback):
        return await callback("session")

    monkeypatch.setattr(ReolinkCgiClient, "_with_session", session)
    monkeypatch.setattr(ReolinkCgiClient, "_command", AsyncMock(return_value={"value": {"PtzCurPos": value}}))
    with pytest.raises(ReolinkCgiError):
        asyncio.run(ReolinkCgiClient("http://camera").get_current_position())


def test_reolink_motion_automation_is_read_only_and_preserves_unknown(monkeypatch):
    async def session(self, callback):
        return await callback("session")

    command = AsyncMock(side_effect=[
        {"value": {"AiCfg": {"channel": 0, "bSmartTrack": 1}}},
        {"value": {"PtzGuard": {"channel": 0}}},
    ])
    monkeypatch.setattr(ReolinkCgiClient, "_with_session", session)
    monkeypatch.setattr(ReolinkCgiClient, "_command", command)
    status = asyncio.run(ReolinkCgiClient("http://camera").get_motion_automation())
    assert status == {"auto_tracking": True, "automatic_return": None}
    assert [call.kwargs["command"] for call in command.await_args_list] == ["GetAiCfg", "GetPtzGuard"]


def test_reolink_motion_automation_accepts_direct_firmware_payload(monkeypatch):
    async def session(self, callback):
        return await callback("session")

    command = AsyncMock(side_effect=[
        {"value": {"channel": 0, "bSmartTrack": 0, "aiTrack": 2}},
        {"value": {"channel": 0, "benable": 0}},
    ])
    monkeypatch.setattr(ReolinkCgiClient, "_with_session", session)
    monkeypatch.setattr(ReolinkCgiClient, "_command", command)
    status = asyncio.run(ReolinkCgiClient("http://camera").get_motion_automation())
    assert status == {"auto_tracking": False, "automatic_return": False}


def test_native_position_provenance_does_not_invent_tilt_or_degrees():
    async def run():
        camera, _, _, _, _ = environment()
        await camera.discover()
        camera._reolink = SimpleNamespace(get_current_position=AsyncMock(
            return_value=SimpleNamespace(pan=1227, tilt=None)))
        pose = await camera.position()
        assert pose["native_pan"] == 1227 and pose["native_tilt"] is None
        native = pose["position_provenance"]["native"]
        assert native["source"] == "Reolink.GetPtzCurPos"
        assert native["units"] == "device_native" and native["degrees_conversion_verified"] is False
        assert native["observed_monotonic"] >= pose["observed_monotonic"]
    asyncio.run(run())


def test_detected_external_automation_blocks_control_without_switching_it(monkeypatch):
    monkeypatch.setattr(ReolinkCgiClient, "get_motion_automation", AsyncMock(return_value={
        "auto_tracking": True, "automatic_return": False,
    }))
    monkeypatch.setattr(ReolinkCgiClient, "get_current_position", AsyncMock(side_effect=ReolinkCgiError("unavailable")))

    async def run():
        camera, _, client, services, _ = environment()
        client.get_device_information.return_value = {"manufacturer": "Reolink"}
        with pytest.raises(PanoramaCaptureError, match="external_automation_active"):
            await camera.acquire()
        assert services.calls == []
    asyncio.run(run())


@pytest.mark.parametrize("maximum", [None, 0, 1, 2])
def test_destination_capacity_preserves_original_and_creates_each_role_once(maximum):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        camera._capabilities["presets"] = {"maximum_count": maximum}
        try:
            if maximum == 0:
                with pytest.raises(PanoramaCaptureError, match="return_capacity_unavailable"):
                    await camera.save_return()
            else:
                original = await camera.save_return()
                assert await camera.save_return() == original
                if maximum == 1:
                    with pytest.raises(PanoramaCaptureError, match="return_capacity_unavailable"):
                        await camera.save_return("work")
                else:
                    work = await camera.save_return("work")
                    assert await camera.save_return("work") == work
                    assert original["preset_token"] != work["preset_token"]
                    assert original["preset_name"] != work["preset_name"]
                    assert original["preset_name"][:6] != work["preset_name"][:6]
                    await camera.return_to(original)
                    assert services.calls[-1][1]["command"]["preset_token"] == original["preset_token"]
                    await camera.return_to(work)
                    assert services.calls[-1][1]["command"]["preset_token"] == work["preset_token"]
            creations = [kwargs for name, kwargs in services.calls if name == "cameras.ptz.set_preset"]
            assert len(creations) == (2 if maximum is None else min(2, maximum))
            assert len({item["idempotency_key"] for item in creations}) == len(creations)
        finally:
            await camera.close()
    asyncio.run(run())


def test_work_does_not_consume_the_only_preset_slot_before_original():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities.update(absolute_supported=False, presets={"maximum_count": 1})
        try:
            with pytest.raises(PanoramaCaptureError, match="return_capacity_unavailable"):
                await camera.save_return("work")
            assert not any(name == "cameras.ptz.set_preset" for name, _ in services.calls)
            assert (await camera.save_return())["role"] == "original"
        finally:
            await camera.close()
    asyncio.run(run())


def test_managed_return_cleanup_preserves_user_presets_at_camera_capacity():
    async def run():
        camera, _, _, services, _ = environment()
        services.presets = [
            {"token": f"original-{index}", "name": f"Pano O {index:016x}"}
            for index in range(30)
        ] + [
            {"token": f"work-{index}", "name": f"Pano W {index:016x}"}
            for index in range(20)
        ] + [
            {"token": f"legacy-{index}", "name": f"Panorama {index:020x}"}
            for index in range(12)
        ] + [
            {"token": "user-home", "name": "Casa"},
            {"token": "user-gate", "name": "Portão"},
        ]

        assert await camera.remove_managed_returns() == 62
        assert services.presets == [
            {"token": "user-home", "name": "Casa"},
            {"token": "user-gate", "name": "Portão"},
        ]
        removals = [kwargs for name, kwargs in services.calls if name == "cameras.ptz.remove_preset"]
        assert len(removals) == 62
        assert all(item["expected_preset_name"].startswith(("Pano ", "Panorama ")) for item in removals)
        assert not any(name.startswith("cameras.control.") for name, _ in services.calls)

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["lost_response", "late_inventory", "never_created", "authorization", "unsupported", "reused_token", "duplicate_name"])
def test_preset_creation_reconciles_inventory_without_repeating_mutation(outcome):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        original_call = services.call
        creations = 0
        if outcome == "reused_token":
            services.presets = [{"token": "existing", "name": "User preset"}]
        async def call(name, **kwargs):
            nonlocal creations
            if name == "cameras.ptz.set_preset":
                creations += 1
                if outcome in {"lost_response", "duplicate_name"}:
                    await original_call(name, **kwargs)
                    if outcome == "duplicate_name":
                        services.presets.append({"token": "second", "name": kwargs["preset_name"]})
                if outcome == "reused_token":
                    services.presets[0]["name"] = kwargs["preset_name"]
                    return {"token": "existing", "name": kwargs["preset_name"]}
                if outcome in {"authorization", "unsupported"}:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=403 if outcome == "authorization" else 501, detail="secret")
                raise TimeoutError("rtsp://secret@camera")
            return await original_call(name, **kwargs)
        services.call = call
        try:
            if outcome == "lost_response":
                saved = await camera.save_return()
                assert saved["preset_token"] == "temporary"
                assert await camera.save_return() == saved
            else:
                code = {
                    "late_inventory": "return_creation_unconfirmed", "never_created": "return_creation_unconfirmed",
                    "authorization": "return_authorization_failed", "unsupported": "return_unavailable",
                    "reused_token": "return_preset_changed", "duplicate_name": "return_preset_ambiguous",
                }[outcome]
                with pytest.raises(PanoramaCaptureError, match=code):
                    await camera.save_return()
                pending = camera.pending_return_destinations()
                assert len(pending) == 1
                assert pending[0]["role"] == "original"
                if outcome == "late_inventory":
                    services.presets = [{"token": "late", "name": pending[0]["preset_name"]}]
                    assert (await camera.save_return())["preset_token"] == "late"
                else:
                    with pytest.raises(PanoramaCaptureError, match=code):
                        await camera.save_return()
                assert not any(name == "cameras.ptz.remove_preset" for name, _ in services.calls)
            assert creations == 1
        finally:
            await camera.close()
    asyncio.run(run())


def test_absolute_return_survives_persisted_checkpoint_and_adapter_recreation():
    import json
    from toposync_ext_cameras.source_panorama import _safe

    async def run():
        first, _, _, _, _ = environment()
        await first.acquire()
        try:
            saved = json.loads(json.dumps(_safe(await first.save_return())))
        finally:
            await first.close()
        reopened, _, _, services, _ = environment()
        await reopened.acquire()
        try:
            await reopened.return_to(saved)
            commands = [value["command"] for name, value in services.calls if name == "cameras.control.submit"]
            assert commands == [{"kind": "absolute_move", "pan": .2, "tilt": -.3}]
            with pytest.raises(PanoramaCaptureError, match="return_binding_changed"):
                await reopened.return_to({**saved, "space": "unrecognized-coordinate-system"})
        finally:
            await reopened.close()

    asyncio.run(run())


@pytest.mark.parametrize("existing_names", [1, 2])
def test_existing_role_name_never_adopts_an_unproven_preset(existing_names):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        services.presets = [{"token": str(index), "name": camera._return_name()} for index in range(existing_names)]
        try:
            with pytest.raises(PanoramaCaptureError, match="return_owner_mismatch"):
                await camera.save_return()
            assert not any(name == "cameras.ptz.set_preset" for name, _ in services.calls)
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["source_id", "profile_token", "configuration_token", "node_token", "width", "configuration_signature"])
def test_return_and_cleanup_reject_changed_destination_identity(changed):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        try:
            saved = await camera.save_return("work")
            saved["binding"] = {**saved["binding"], changed: "changed"}
            count = len(services.calls)
            with pytest.raises(PanoramaCaptureError, match="return_binding_changed"):
                await camera.return_to(saved)
            with pytest.raises(PanoramaCaptureError, match="return_binding_changed"):
                await camera.remove_return(saved)
            assert not any(name in {"cameras.control.submit", "cameras.ptz.remove_preset"} for name, _ in services.calls[count:])
        finally:
            await camera.close()
    asyncio.run(run())


def test_legacy_original_preset_remains_usable_but_work_requires_binding():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        saved = {"kind": "preset", "owner_id": camera.owner_id,
                 "preset_name": f"Panorama {camera.owner_id[:20]}", "preset_token": "legacy"}
        services.presets = [{"token": "legacy", "name": saved["preset_name"]}]
        try:
            await camera.return_to(saved)
            assert services.calls[-1][1]["command"]["preset_token"] == "legacy"
            with pytest.raises(PanoramaCaptureError, match="return_binding_unverified"):
                await camera.return_to({**saved, "role": "work"})
            await camera.remove_return(saved)
            assert not services.presets
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("supported,current_zoom,expected", [(True, .8, .2), (False, .2, None), (False, .8, "error"), (False, None, "error")])
def test_absolute_return_restores_zoom_only_with_an_explicit_supported_space(supported, current_zoom, expected):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=.2)
        await camera.acquire()
        camera._capabilities["absolute_zoom_supported"] = supported
        camera._capabilities["defaults"]["absolute_zoom"] = "normalized-zoom" if supported else None
        camera._capabilities["limits"]["zoom"] = {"min": 0, "max": 1}
        try:
            saved = await camera.save_return()
            client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=current_zoom)
            before = len(services.calls)
            if expected == "error":
                with pytest.raises(PanoramaCaptureError, match="return_optical_state_mismatch"):
                    await camera.return_to(saved)
                assert not any(name == "cameras.control.submit" for name, _ in services.calls[before:])
            else:
                await camera.return_to(saved)
                command = services.calls[-1][1]["command"]
                assert command.get("zoom") == expected
        finally:
            await camera.close()
    asyncio.run(run())


def test_pending_creation_cleanup_reconciles_owned_inventory_after_restart():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        original_call = services.call
        async def call(name, **kwargs):
            if name == "cameras.ptz.set_preset":
                raise TimeoutError("uncertain")
            return await original_call(name, **kwargs)
        services.call = call
        try:
            with pytest.raises(PanoramaCaptureError, match="return_creation_unconfirmed"):
                await camera.save_return("work")
            pending = camera.pending_return_destinations()[0]
            services.presets = [{"token": "late", "name": pending["preset_name"]}, {"token": "user", "name": "User"}]
            await camera.remove_return(pending)
            assert services.presets == [{"token": "user", "name": "User"}]
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("status_space", ["", "custom-pan-degrees"])
def test_reported_position_space_is_not_mistaken_for_normalized_coordinates(status_space):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, pan_tilt_space=status_space)
        await camera.acquire()
        try:
            saved = await camera.save_return()
            assert saved["kind"] == ("preset" if status_space else "absolute")
            assert not any(name == "cameras.control.submit" for name, _ in services.calls)
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("space,limits,expected", [("generic", {"min": 0, "max": 1}, True), ("custom", {"min": 0, "max": 1}, False), ("generic", None, False)])
def test_zoom_support_requires_selected_normalized_space_and_usable_limits(space, limits, expected):
    async def run():
        camera, _, client, _, _ = environment()
        capabilities = client.get_ptz_capabilities.return_value
        capabilities["absolute_zoom"] = True
        capabilities["spaces"]["absolute_zoom"] = [{"uri": space, "normalized": space == "generic"}]
        capabilities["defaults"]["absolute_zoom"] = space
        capabilities["limits"]["zoom"] = limits
        assert (await camera.discover())["absolute_zoom_supported"] is expected
    asyncio.run(run())


@pytest.mark.parametrize("actually_removed", [True, False])
def test_cleanup_confirms_inventory_after_lost_remove_response(actually_removed):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        try:
            saved = await camera.save_return()
            original_call = services.call
            deletions = []
            async def call(name, **kwargs):
                if name == "cameras.ptz.remove_preset":
                    deletions.append(kwargs)
                    if actually_removed:
                        await original_call(name, **kwargs)
                    raise TimeoutError("uncertain")
                return await original_call(name, **kwargs)
            services.call = call
            if actually_removed:
                await camera.remove_return(saved)
                assert not services.presets
            else:
                with pytest.raises(PanoramaCaptureError, match="return_cleanup_unconfirmed"):
                    await camera.remove_return(saved)
                assert services.presets
            assert len(deletions) == 1
        finally:
            await camera.close()
    asyncio.run(run())



def test_contradictory_creation_readback_never_removes_the_other_token():
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        original_call = services.call
        async def call(name, **kwargs):
            if name == "cameras.ptz.set_preset":
                services.presets = [{"token": "observed", "name": kwargs["preset_name"]}]
                return {"token": "reported", "name": kwargs["preset_name"]}
            return await original_call(name, **kwargs)
        services.call = call
        try:
            with pytest.raises(PanoramaCaptureError, match="return_preset_changed"):
                await camera.save_return()
            pending = camera.pending_return_destinations()[0]
            with pytest.raises(PanoramaCaptureError, match="return_cleanup_unconfirmed"):
                await camera.remove_return(pending)
            assert not any(name == "cameras.ptz.remove_preset" for name, _ in services.calls)
        finally:
            await camera.close()
    asyncio.run(run())



@pytest.mark.parametrize("destination", ["preset", "absolute"])
def test_stop_includes_zoom_after_a_return_that_can_change_optics(destination):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=.2)
        await camera.acquire()
        camera._capabilities.update(absolute_supported=destination == "absolute", absolute_zoom_supported=True)
        camera._capabilities["limits"]["zoom"] = {"min": 0, "max": 1}
        try:
            saved = await camera.save_return()
            client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=.8)
            await camera.return_to(saved)
            await camera.stop()
            assert services.calls[-1][1]["command"] == {"kind": "stop", "pan_tilt": True, "zoom": True}
            await camera.stop()
            assert services.calls[-1][1]["command"]["zoom"] is False
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["control_lost", "cancelled"])
def test_final_preset_guard_keeps_ownership_until_destination_delivery(interruption):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        guard = camera._guard
        async def interrupted_guard(**kwargs):
            if services.presets:
                if interruption == "cancelled":
                    raise asyncio.CancelledError()
                raise PanoramaCaptureError("control_lost")
            return await guard(**kwargs)
        camera._guard = interrupted_guard
        try:
            expected = asyncio.CancelledError if interruption == "cancelled" else PanoramaCaptureError
            with pytest.raises(expected):
                await camera.save_return("work")
            pending = camera.pending_return_destinations()
            assert len(pending) == 1
            assert pending[0]["resolved_token"] == "temporary"
            assert pending[0]["role"] == "work"
            camera._guard = guard
            await camera.remove_return(pending[0])
            assert not camera.pending_return_destinations()
            assert not services.presets
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("persist_fails", [False, True])
def test_preset_creation_persists_full_intent_before_remote_mutation(persist_fails):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        services.presets = [{"token": "user", "name": "User"}]
        persisted = []
        async def before_create(pending):
            assert not any(name == "cameras.ptz.set_preset" for name, _ in services.calls)
            assert pending["baseline_tokens"] == ["user"]
            assert pending["role"] == "original"
            assert pending["owner_id"] == camera.owner_id
            assert pending["binding"]["profile_token"] == "profile"
            assert pending["preset_name"] == camera._return_name()
            persisted.append(copy.deepcopy(pending))
            if persist_fails:
                raise OSError("checkpoint unavailable")
        try:
            if persist_fails:
                with pytest.raises(OSError, match="checkpoint unavailable"):
                    await camera.save_return(before_create=before_create)
                assert not any(name == "cameras.ptz.set_preset" for name, _ in services.calls)
            else:
                assert (await camera.save_return(before_create=before_create))["kind"] == "preset"
            assert len(persisted) == 1
        finally:
            await camera.close()
    asyncio.run(run())


@pytest.mark.parametrize("current_space,current_zoom", [("focal-mm", 24), ("focal-mm", 48), ("other-space", 24)])
def test_unchanged_custom_zoom_does_not_prevent_pan_tilt_return(current_space, current_zoom):
    async def run():
        camera, _, client, services, _ = environment()
        client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=24, zoom_space="focal-mm")
        await camera.acquire()
        try:
            saved = await camera.save_return()
            client.get_ptz_status.return_value = replace(client.get_ptz_status.return_value, zoom=current_zoom, zoom_space=current_space)
            before = len(services.calls)
            if current_space == "focal-mm" and current_zoom == 24:
                await camera.return_to(saved)
                assert "zoom" not in services.calls[-1][1]["command"]
            else:
                with pytest.raises(PanoramaCaptureError, match="return_optical_state_mismatch"):
                    await camera.return_to(saved)
                assert not any(name == "cameras.control.submit" for name, _ in services.calls[before:])
        finally:
            await camera.close()
    asyncio.run(run())



@pytest.mark.parametrize("change", [None, "owner_id", "role", "binding", "preset_name"])
@pytest.mark.parametrize("resolved", [False, True])
def test_pending_cleanup_with_no_matching_preset_validates_ownership_before_forgetting(change, resolved):
    async def run():
        camera, _, _, services, _ = environment()
        await camera.acquire()
        camera._capabilities["absolute_supported"] = False
        original_call = services.call
        async def call(name, **kwargs):
            if name == "cameras.ptz.set_preset":
                raise TimeoutError("uncertain")
            return await original_call(name, **kwargs)
        services.call = call
        try:
            with pytest.raises(PanoramaCaptureError, match="return_creation_unconfirmed"):
                await camera.save_return("work")
            if resolved:
                camera._return_creations["work"]["resolved_token"] = "gone"
            pending = camera.pending_return_destinations()[0]
            if change is not None:
                pending = {**pending, change: "different"}
                with pytest.raises(PanoramaCaptureError):
                    await camera.remove_return(pending)
                assert len(camera.pending_return_destinations()) == 1
            else:
                await camera.remove_return(pending)
                assert not camera.pending_return_destinations()
            assert not any(name == "cameras.ptz.remove_preset" for name, _ in services.calls)
        finally:
            await camera.close()
    asyncio.run(run())
