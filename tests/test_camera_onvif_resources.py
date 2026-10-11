"""Protocol contracts with synthetic devices; never evidence about a camera model."""
import asyncio
from dataclasses import replace
import time

import pytest

from toposync_ext_cameras.onvif.events import (
    OnvifCameraEventContext, OnvifEventMessage,
    OnvifPullPointSubscription, _renew_delay,
    parse_get_event_properties, parse_pull_messages,
)
from toposync_ext_cameras.onvif.capabilities import OnvifCapabilitiesCatalog, camera_service_address
from toposync_ext_cameras.onvif.client import OnvifError, OnvifProfile
from toposync_ext_cameras.pipelines.operators import OnvifStateGateRuntime
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.services import ServiceRegistry
from test_camera_onvif_event_demand import manager_and_transport


def test_namespace_aliases_preserve_identity_and_collisions():
    catalog = b'''<TopicSet xmlns:a="urn:one" xmlns:b="urn:two"><a:Rule><Motion>
      <MessageDescription IsProperty="true"><Data><SimpleItemDescription Name="Active" Type="xs:boolean"/></Data></MessageDescription>
      </Motion></a:Rule><b:Rule><Motion><MessageDescription IsProperty="true"><Data><SimpleItemDescription Name="Active" Type="xs:boolean"/></Data></MessageDescription></Motion></b:Rule></TopicSet>'''
    descriptors = parse_get_event_properties(catalog)
    assert len(descriptors) == 2
    assert {item.qualified_topic for item in descriptors} == {"{urn:one}Rule/Motion", "{urn:two}Rule/Motion"}
    messages = parse_pull_messages(b'''<PullMessagesResponse xmlns:other="urn:one"><NotificationMessage>
      <Topic>other:Rule/Motion</Topic><Message><Message PropertyOperation="Changed"><Data><SimpleItem Name="Active" Value="true"/></Data></Message></Message>
      </NotificationMessage></PullMessagesResponse>''')
    assert messages[0].qualified_topic == descriptors[0].qualified_topic


def test_distinct_sources_remain_ambiguous_for_legacy_selection_and_deleted_becomes_unknown():
    async def scenario():
        manager, transport = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        for source, value in [("one", "true"), ("two", "false")]:
            manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion",
                operation="Changed", source={"Sensor": source}, data={"Active": value})])
        assert not (await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active"))["known"]
        result = await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active", source={"Sensor": "one"}, key={})
        assert result["value"] is True
        manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", operation="Deleted", source={"Sensor": "one"})])
        assert not (await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active", source={"Sensor": "one"}))["known"]
        await manager.shutdown()
    asyncio.run(scenario())


def test_empty_catalog_is_cached_and_config_change_replaces_observer():
    async def scenario():
        manager, transport = manager_and_transport()
        calls = []
        async def empty(address):
            calls.append(address)
            return []
        transport.get_event_properties = empty
        await manager.list_descriptors("controlled")
        await manager.list_descriptors("controlled")
        assert len(calls) == 1
        old = manager._runtimes["controlled"]
        manager._resolve_context = lambda _: replace(old.context, password="new-private-password")
        await manager.list_descriptors("controlled")
        assert manager._runtimes["controlled"] is not old
        assert len(calls) == 2
        await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("available", [False, True])
def test_unknown_state_uses_fail_open_even_before_synchronization(available):
    services = ServiceRegistry()
    async def snapshot(**kwargs):
        return {"available": available, "known": False, "value": None}
    services.register("cameras.onvif_events.snapshot", snapshot)
    gate = OnvifStateGateRuntime({"camera_id": "camera", "fail_open": True}, PipelineRuntimeDependencies(services=services))
    assert asyncio.run(gate._resolve_gate_state())[0] is True


def test_short_property_activation_survives_poll_with_changed_or_initialized(monkeypatch):
    async def scenario():
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        for value in ["true", "false"]:
            manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", operation="Changed", data={"Active": value})])
        state = next(iter(runtime.states.values()))
        assert state.value is False and state.last_active_ts > 0
        services = ServiceRegistry()
        async def snapshot(**kwargs):
            return state.as_dict(camera_id="controlled", available=True)
        services.register("cameras.onvif_events.snapshot", snapshot)
        gate = OnvifStateGateRuntime({"fail_open": True, "hold_seconds": 5}, PipelineRuntimeDependencies(services=services))
        assert (await gate._resolve_gate_state())[0] is True
        state.last_active_ts = time.time() - 6
        state.last_active_monotonic = time.monotonic() - 6
        state.last_inactive_monotonic = time.monotonic() - 6
        gate._hold_until_ts = 0
        assert (await gate._resolve_gate_state())[0] is False
        runtime.states.clear()
        for value in ["true", "false"]:
            manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", operation="Initialized", data={"Active": value})])
        assert next(iter(runtime.states.values())).last_active_ts > 0
        assert [event.state_changed for event in list(runtime.events)[-2:]] == [False, True]
        await manager.shutdown()
    asyncio.run(scenario())


def test_subscription_duration_uses_device_clock_and_expiration():
    assert _renew_delay(OnvifPullPointSubscription("address", "2000-01-01T12:00:00Z", "2000-01-01T12:00:10Z")) == 8
    with pytest.raises(OnvifError):
        _renew_delay(OnvifPullPointSubscription("address", "2000-01-01T12:00:00Z", "2000-01-01T11:59:59Z"))


@pytest.mark.parametrize("address", ["file:///tmp/private", "http://other.invalid/events", "http://user:password@camera.invalid/events"])
def test_external_service_addresses_do_not_receive_credentials(address):
    with pytest.raises(OnvifError):
        camera_service_address("http://camera.invalid/device", address)


def test_catalog_partial_results_cache_and_no_mutating_calls(monkeypatch):
    from toposync_ext_cameras.onvif import capabilities as module
    calls = []
    class ReadOnlyClient:
        def __init__(self, **kwargs):
            pass
        async def get_device_information(self):
            calls.append("identification")
            return {"manufacturer": "Synthetic", "model": "Fixture", "firmware_version": "1"}
        async def get_capabilities(self):
            calls.append("capabilities")
            return "http://camera.invalid/media", None
        async def get_profiles(self, address):
            calls.append("profiles")
            return [OnvifProfile("profile", "Main", video_source_token="one", audio_encoding="AAC")]
    class Events:
        def __init__(self, client):
            pass
        async def get_services(self):
            raise OnvifError("private-password must not be returned")
    class Manager:
        async def list_descriptors(self, *args, **kwargs):
            return {"events": []}
    monkeypatch.setattr(module, "OnvifClient", ReadOnlyClient)
    monkeypatch.setattr(module, "OnvifEventsClient", Events)
    context = OnvifCameraEventContext("camera", "Camera", "http://camera.invalid/device")
    async def resolve(camera_id):
        return context
    async def scenario():
        catalog = OnvifCapabilitiesCatalog(resolve, Manager())
        result = await catalog.inspect("camera")
        assert result["status"] == "partial" and result["sections"]["services"] == "unknown"
        assert result["profiles"][0]["audio_encoding"] == "AAC"
        assert "private-password" not in str(result)
        await catalog.inspect("camera")
        assert calls == ["identification", "capabilities", "profiles"]
        await catalog.inspect("camera", refresh=True)
        assert len(calls) == 6
    asyncio.run(scenario())


def test_soap_payload_limits_entities_and_redirects():
    import io
    from toposync_ext_cameras.onvif.client import _read_soap_payload, _parse_xml, _NoSoapRedirects, _MAX_SOAP_RESPONSE_BYTES
    with pytest.raises(OnvifError):
        _read_soap_payload(io.BytesIO(b"x" * (_MAX_SOAP_RESPONSE_BYTES + 1)))
    with pytest.raises(OnvifError):
        _parse_xml(b'<!DOCTYPE x [<!ENTITY value "private">]><x>&value;</x>')
    with pytest.raises(OnvifError):
        _NoSoapRedirects().redirect_request(None, None, 302, None, None, "http://external.invalid")


def test_optimized_source_preserves_annotated_activity_and_independent_consumer(monkeypatch, record_property):
    from test_camera_source_cadence import setup_sources
    from types import SimpleNamespace
    from toposync.runtime.pipelines import Packet

    async def replay(optimized):
        clock, capture, sources, context = setup_sources(monkeypatch)
        source, independent = sources
        services = ServiceRegistry()
        state = {"available": True, "known": True, "value": False}
        async def snapshot(**kwargs):
            return dict(state)
        services.register("cameras.onvif_events.snapshot", snapshot)
        gate = OnvifStateGateRuntime({"camera_id": "cadence", "fail_open": True, "hold_seconds": 30},
                                     PipelineRuntimeDependencies(services=services))
        class Channel:
            pending = None
            async def get(self, **kwargs):
                packet, self.pending = self.pending, None
                return SimpleNamespace(accepted=packet is not None, item=packet)
        channel = Channel()
        if optimized:
            context.inputs["gate"] = channel
        own = []
        live = []
        annotated = []
        for tick in range(1800):
            clock.now = tick / 20
            # Frozen truth: a short passage, then a stationary subject.
            present = 20 <= clock.now < 22 or 72 <= clock.now < 80
            state.update(available=not 60 <= clock.now < 70, value=present)
            if optimized:
                opened, _ = await gate._resolve_gate_state()
                channel.pending = Packet.create(stream_id="gate", payload={"gate_open": opened})
            packet = await source.produce(context)
            if packet is not None:
                own.append(clock.now)
                if present:
                    annotated.append(clock.now)
            independent_context = SimpleNamespace(**{**vars(context), "inputs": {}, "node_id": "independent"})
            if await independent.produce(independent_context) is not None:
                live.append(clock.now)
        for item in sources:
            await item.shutdown()
        return own, live, annotated, capture.released

    for repetition in range(3):
        baseline = asyncio.run(replay(False))
        optimized = asyncio.run(replay(True))
        assert len(optimized[0]) < len(baseline[0])
        assert optimized[1] == baseline[1]  # Separate live consumer keeps its own cadence.
        # Reopening restarts the consumer cadence; compare annotated coverage and latency, not clock phase.
        for start, end in [(20, 22), (72, 80)]:
            actual = [t for t in optimized[2] if start <= t < end]
            reference = [t for t in baseline[2] if start <= t < end]
            assert len(actual) >= len(reference)
            assert actual[0] - start <= 1 / 5  # Source contract: at least five frames per second.
            assert actual[-1] >= end - 1 / 5
        assert not any(t < 20 or 52 <= t < 60 or 70 <= t < 72 for t in optimized[0])
        assert sum(60 <= t < 70 for t in optimized[0]) == sum(60 <= t < 70 for t in baseline[0])
        assert len(optimized[3]) > len(baseline[3])  # Recipe demand released on closure.
        import json
        record_property(f"comparison_{repetition + 1}", json.dumps({
            "synthetic_seconds": 90, "baseline_frames": len(baseline[0]), "optimized_frames": len(optimized[0]),
            "baseline_annotated_frames": len(baseline[2]), "optimized_annotated_frames": len(optimized[2]),
            "baseline_independent_frames": len(baseline[1]), "optimized_independent_frames": len(optimized[1]),
            "baseline_releases": len(baseline[3]), "optimized_releases": len(optimized[3]),
            "real_inference": False, "physical_camera": False,
        }))


def test_legacy_namespace_ambiguity_and_reordered_state_do_not_select_wrong_sensor():
    async def scenario():
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        descriptor = runtime.descriptors[0]
        runtime.descriptors = [replace(descriptor, qualified_topic="{urn:one}Motion"), replace(descriptor, qualified_topic="{urn:two}Motion")]
        for value, timestamp in [("false", "2026-10-10T12:00:02Z"), ("true", "2026-10-10T12:00:01Z")]:
            manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", qualified_topic="{urn:one}Motion",
                utc_time=timestamp, operation="Changed", data={"Active": value})])
        assert not (await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active"))["known"]
        selected = await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active", qualified_topic="{urn:one}Motion")
        assert selected["value"] is False and selected["last_active_ts"] is None
        await manager.shutdown()
    asyncio.run(scenario())


def test_event_queue_reports_gaps_instead_of_silently_replaying_incomplete_history():
    async def scenario():
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", data={"Active": "true"}) for _ in range(515)])
        assert (await manager.recent_events(camera_id="controlled", after_sequence=1))["gap"] is True
        await manager.shutdown()
    asyncio.run(scenario())


def test_rtsp_profile_matching_ignores_credentials_but_preserves_channel(monkeypatch):
    from toposync_ext_cameras.onvif import capabilities as module
    class Client:
        def __init__(self, **kwargs):
            pass
        async def get_capabilities(self):
            return "http://camera.invalid/media", None
        async def get_stream_uri(self, address, *, profile_token):
            assert profile_token == "main"
            return "rtsp://camera.invalid:554/channel/one"
    monkeypatch.setattr(module, "OnvifClient", Client)
    async def resolve(camera_id):
        return OnvifCameraEventContext(camera_id, "Camera", "http://camera.invalid/device")
    async def scenario():
        catalog = OnvifCapabilitiesCatalog(resolve, None)
        assert await catalog.matches_stream("camera", profile_token="main", rtsp_url="rtsp://user:password@camera.invalid/channel/one")
        assert not await catalog.matches_stream("camera", profile_token="main", rtsp_url="rtsp://camera.invalid/channel/two")
        relay = "rtsp://relay.invalid/camera-main"
        binding = {"profile_token": "main", "rtsp_url": relay,
                   "upstream_rtsp_url": "rtsp://camera.invalid/channel/one"}
        assert not await catalog.matches_stream("camera", profile_token="main", rtsp_url=relay)
        assert await catalog.matches_stream("camera", profile_token="main", rtsp_url=relay, binding=binding)
        for replacement in ({"profile_token": "another-profile"}, {"rtsp_url": "rtsp://relay.invalid/another-camera"},
                            {"upstream_rtsp_url": "rtsp://camera.invalid/channel/two"}):
            assert not await catalog.matches_stream("camera", profile_token="main", rtsp_url=relay,
                                                   binding={**binding, **replacement})

    asyncio.run(scenario())


def test_catalog_refresh_cannot_certify_disconnected_property_state():
    async def scenario():
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        manager._record_messages(runtime, [OnvifEventMessage(sequence=0, topic="Motion", data={"Active": "false"})])
        runtime.watch_available = False
        # Refreshing discovery is not a successful PullMessages exchange.
        await manager.list_descriptors("controlled", refresh=True)
        result = await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")
        assert result["available"] is False
        await manager.shutdown()
    asyncio.run(scenario())
