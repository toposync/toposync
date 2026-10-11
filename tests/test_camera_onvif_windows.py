"""Behavior fixtures derived from the direct Tapo trace and conventional ONVIF properties."""
import asyncio
import http.client
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines import Packet, PipelineRuntimeDependencies
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.onvif import events
from toposync_ext_cameras.onvif.recipe_optimization import CameraOnvifOptimization
from toposync_ext_cameras.pipelines import operators
from test_camera_onvif_event_demand import manager_and_transport
from test_camera_source_cadence import setup_sources


@pytest.mark.parametrize("operation,gap", [("Initialized", 5.643), ("Initialized", 1.556), ("Changed", 5.643)])
def test_property_window_remains_continuous_across_real_trace_gaps(monkeypatch, operation, gap):
    async def scenario():
        clock = SimpleNamespace(now=0.)
        fake_time = SimpleNamespace(time=lambda: 1000 + clock.now, monotonic=lambda: clock.now)
        monkeypatch.setattr(events, "time", fake_time)
        monkeypatch.setattr(operators, "time", fake_time)
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        services = ServiceRegistry()
        async def snapshot(**kwargs):
            return {**next(iter(runtime.states.values())).as_dict(camera_id="controlled", available=True), "session": 1}
        services.register("cameras.onvif_events.snapshot", snapshot)
        gate = operators.OnvifStateGateRuntime({"hold_seconds": 10}, PipelineRuntimeDependencies(services=services))
        def message(value):
            manager._record_messages(runtime, [events.OnvifEventMessage(0, "Motion", operation=operation, data={"Active": value})])
        message("true")
        assert runtime.events[-1].state_changed is False  # Synchronization, not an occurrence.
        assert (await gate._resolve_gate_state())[0]
        clock.now = 1
        message("false")
        assert runtime.events[-1].state_changed is True
        assert (await gate._resolve_gate_state())[0]
        clock.now = 1 + gap
        message("false")  # Duplicate does not extend the closing deadline.
        assert runtime.events[-1].state_changed is False
        assert (await gate._resolve_gate_state())[0]
        message("true")
        assert (await gate._resolve_gate_state())[0]
        clock.now = 100  # A silent active property remains active.
        assert (await gate._resolve_gate_state())[0]
        message("false")
        assert (await gate._resolve_gate_state())[0]
        clock.now = 105
        message("false")
        assert (await gate._resolve_gate_state())[0]
        clock.now = 110.01
        assert not (await gate._resolve_gate_state())[0]
        assert [e.operation for e in runtime.events] == [operation] * 6
    asyncio.run(scenario())


def test_declared_boolean_and_non_boolean_pulses_extend_without_state_deduplication(monkeypatch):
    async def scenario():
        clock = SimpleNamespace(now=0.)
        monkeypatch.setattr(operators, "time", SimpleNamespace(time=lambda: 1000 + clock.now, monotonic=lambda: clock.now))
        response = {"available": True, "session": 1, "events": []}
        services = ServiceRegistry()
        async def recent(**kwargs):
            return response
        services.register("cameras.onvif_events.recent_events", recent)
        gate = operators.OnvifStateGateRuntime({"mode": "pulse", "topic": "Occurrence", "item_name": "", "hold_seconds": 10}, PipelineRuntimeDependencies(services=services))
        source = operators.OnvifEventSourceRuntime({}, PipelineRuntimeDependencies(services=services))
        for sequence, value in [(1, "true"), (2, "true"), (3, "pressed")]:
            clock.now = (sequence - 1) * 8
            response["events"] = [{"sequence": sequence, "topic": "Occurrence", "is_property": False,
                                   "session": 1, "data": {"Value": value}, "received_at_ts": 1000 + clock.now}]
            assert (await gate._resolve_gate_state())[0]
            assert not source._is_duplicate_boolean_event(response["events"][0], item_name="Value", value=True)
        clock.now = 25
        assert (await gate._resolve_gate_state())[0]
        clock.now = 26.01
        assert not (await gate._resolve_gate_state())[0]
        response.update(session=2, events=[])
        assert not (await gate._resolve_gate_state())[0]
        response["events"] = [{"sequence": 4, "topic": "Occurrence", "is_property": None, "data": {"Value": "true"}, "received_at_ts": 1026.01}]
        assert not (await gate._resolve_gate_state())[0]
    asyncio.run(scenario())


def test_capture_minimum_starts_after_first_usable_frame_and_releases_only_own_lease(monkeypatch):
    async def scenario():
        clock, capture, sources, context = setup_sources(monkeypatch)
        source = operators.CameraSourceRuntime({"camera_id": "cadence", "fps": 5, "gate_min_observation_seconds": 5}, PipelineRuntimeDependencies())
        source._capture_service = capture
        class Gate:
            packet = Packet.create(stream_id="gate", payload={"gate_open": True, "onvif": {}})
            async def get(self, **kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)
        gate = Gate()
        context.inputs["gate"] = gate
        original = capture.get_latest
        async def delayed(*args, **kwargs):
            frame = await original(*args, **kwargs)
            if clock.now < 10:
                frame = replace(frame, frame=None)
            return frame
        capture.get_latest = delayed
        assert await source.produce(context) is None
        gate.packet = Packet.create(stream_id="gate", payload={"gate_open": False, "onvif": {}})
        clock.now = 2
        assert await source.produce(context) is None
        clock.now = 10
        packet = await source.produce(context)
        assert packet.metadata["onvif_observation_retained"] is True
        clock.now = 14.9
        assert await source.produce(context) is not None
        clock.now = 15.1
        assert await source.produce(context) is None
        assert capture.released == [source._capture_owner_id]
        # Another consumer of the shared decoder still delivers frames.
        context.inputs.clear()
        assert await sources[1].produce(context) is not None
        await sources[1].shutdown()
    asyncio.run(scenario())


def test_continuity_mode_keeps_capture_and_exposes_inactive_window(monkeypatch):
    async def scenario():
        clock, capture, _, context = setup_sources(monkeypatch)
        source = operators.CameraSourceRuntime({"camera_id": "cadence", "keep_capture_when_gate_closed": True}, PipelineRuntimeDependencies())
        source._capture_service = capture
        class Gate:
            packet = Packet.create(stream_id="gate", payload={"gate_open": False, "onvif": {}})
            async def get(self, **kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)
        context.inputs["gate"] = Gate()
        packet = await source.produce(context)
        assert packet.metadata["onvif_gate_open"] is False
        assert not capture.released
        await source.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("recovery_messages", [[], [events.OnvifEventMessage(0, "Motion", operation="Initialized", data={"Active": "false"})]])
def test_transient_pull_retries_same_subscription_and_preserves_only_recovery_states(recovery_messages):
    async def scenario():
        manager, transport = manager_and_transport()
        synced = []
        addresses = []
        second = asyncio.Event()
        unblock = asyncio.Event()
        async def sync(address):
            synced.append(address)
        async def pull(address, **kwargs):
            addresses.append(address)
            if len(addresses) == 1:
                manager._record_messages(manager._runtimes["controlled"], [
                    events.OnvifEventMessage(0, "Motion", operation="Initialized", data={"Active": "true"})
                ])
                raise events.OnvifTransientTransportError("interrupted")
            if len(addresses) == 2:
                return recovery_messages
            second.set()
            await unblock.wait()
            return []
        transport.set_synchronization_point = sync
        transport.pull_messages = pull
        try:
            await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")
            await asyncio.wait_for(second.wait(), 1.5)
            runtime = manager._runtimes["controlled"]
            assert transport.subscriptions == 1 and transport.unsubscribes == 0
            assert len(set(addresses)) == 1 and len(synced) == 2
            assert runtime.successful_pulls == 1 and runtime.failed_pulls == 1
            snapshot = await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")
            assert snapshot["available"] and snapshot["known"] == bool(recovery_messages)
            assert snapshot["value"] is (False if recovery_messages else None)
            if recovery_messages:
                assert runtime.events[-1].session == runtime.session
                assert runtime.events[-1].state_changed is False
        finally:
            await manager.shutdown()
        assert transport.unsubscribes == 1
        assert runtime.unsubscribe_status == "confirmed"
    asyncio.run(scenario())


@pytest.mark.parametrize("renewal_fails", [False, True])
def test_long_pull_gap_renews_same_subscription_without_restoring_stale_state(monkeypatch, renewal_fails):
    async def scenario():
        clock = SimpleNamespace(now=0.)
        monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: 1000 + clock.now, monotonic=lambda: clock.now))
        manager, transport = manager_and_transport()
        renewed, synced = [], []
        ready = asyncio.Event()
        async def sleep(runtime, seconds):
            clock.now += 121
            runtime.last_consumer_monotonic = clock.now
        async def renew(address):
            renewed.append(address)
            assert not manager._runtimes["controlled"].watch_available
            if renewal_fails and len(renewed) == 1:
                raise events.OnvifError("subscription expired")
            return events.OnvifPullPointSubscription(address)
        async def sync(address):
            synced.append(address)
        async def pull(address, **kwargs):
            transport.pulls += 1
            if transport.pulls <= 2:
                raise events.OnvifTransientTransportError("interrupted")
            if transport.pulls == 3:
                return [events.OnvifEventMessage(0, "Motion", operation="Initialized", data={"Active": "false"})]
            ready.set()
            await asyncio.Event().wait()
        manager._sleep_or_stop = sleep
        transport.renew, transport.set_synchronization_point, transport.pull_messages = renew, sync, pull
        try:
            await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")
            await asyncio.wait_for(ready.wait(), 1)
            snapshot = await manager.snapshot(camera_id="controlled", topic="Motion", item_name="Active")
            assert snapshot["available"] and snapshot["known"] and snapshot["value"] is False
            assert snapshot["session"] == (4 if renewal_fails else 2)
            assert len(renewed) == 2 and len(synced) == 4
            assert transport.subscriptions == (2 if renewal_fails else 1)
            assert transport.unsubscribes == int(renewal_fails)
        finally:
            await manager.shutdown()
        assert transport.unsubscribes == (2 if renewal_fails else 1)
    asyncio.run(scenario())


def test_failed_capture_does_not_retain_closed_demand_forever(monkeypatch):
    async def scenario():
        clock, capture, _, context = setup_sources(monkeypatch)
        source = operators.CameraSourceRuntime({"camera_id": "cadence", "gate_min_observation_seconds": 5}, PipelineRuntimeDependencies())
        source._capture_service = capture
        original = capture.get_latest
        async def empty(*args, **kwargs):
            return replace(await original(*args, **kwargs), frame=None)
        capture.get_latest = empty
        class Gate:
            packet = Packet.create(stream_id="gate", payload={"gate_open": True})
            async def get(self, **kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)
        gate = Gate()
        context.inputs["gate"] = gate
        await source.produce(context)
        gate.packet = Packet.create(stream_id="gate", payload={"gate_open": False})
        clock.now = 30.1
        assert await source.produce(context) is None
        assert capture.released == [source._capture_owner_id]
    asyncio.run(scenario())


def test_onvif_window_is_not_blocked_by_local_motion_filter(monkeypatch):
    async def scenario():
        clock, _, sources, context = setup_sources(monkeypatch)
        clock.now = 1
        packet = await sources[0].produce(context)
        packet.metadata["onvif_gate_open"] = True
        runtime = operators.MotionGateRuntime({"emit_when_idle": False, "hold_seconds": 0}, PipelineRuntimeDependencies())
        async def blocking(*args, **kwargs):
            return SimpleNamespace(active=False, score=0, bboxes01=[], last_latency_ms=0, fps=5)
        context.run_blocking = blocking
        result = await runtime.process_packet(packet, context)
        assert len(result) == 1 and result[0].metadata["motion_gate_open"] is True
        packet.metadata["onvif_gate_open"] = False
        assert await runtime.process_packet(packet, context) == []
        await sources[0].shutdown()
    asyncio.run(scenario())


def test_transport_classification_uses_exception_types_and_does_not_retry_authentication():
    assert events._transient_transport_error(URLError(http.client.RemoteDisconnected()))
    assert events._transient_transport_error(TimeoutError())
    assert not events._transient_transport_error(HTTPError("http://fixture.invalid", 401, "Unauthorized", {}, None))
    assert not events._transient_transport_error(RuntimeError("RemoteDisconnected"))


def test_uncached_pull_negotiates_transport_but_cached_interruption_does_not_renegotiate():
    async def scenario():
        calls = []
        async def call(**kwargs):
            calls.append(kwargs["auth"])
            if kwargs["auth"] == "none" or len(calls) > 2:
                raise http.client.RemoteDisconnected()
            return b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><PullMessagesResponse/></s:Body></s:Envelope>'
        client = SimpleNamespace(xaddr="http://fixture.invalid/device", username="", password="", _auth_attempts=lambda: ["none", "digest"], _call=call)
        cache = {}
        arguments = dict(url="http://fixture.invalid/pull", body_xml="", soap_action="", operation="PullMessages", transport_cache=cache)
        await events._call_event_operation(client, **arguments)
        assert calls == ["none", "digest"]
        with pytest.raises(events.OnvifTransientTransportError):
            await events._call_event_operation(client, **arguments)
        assert calls == ["none", "digest", "digest"]
        assert cache[(arguments["url"], "PullMessages")] == ("1.2", "digest", "soap")
        cache = {(arguments["url"], "SetSynchronizationPoint"): ("1.2", "digest", "soap")}
        with pytest.raises(events.OnvifTransientTransportError):
            await events._call_event_operation(client, **{**arguments, "transport_cache": cache})
        assert calls[-1] == "digest" and len(calls) == 4
    asyncio.run(scenario())


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, 301])
def test_window_validation_rejects_invalid_duration(duration):
    with pytest.raises(ValidationError):
        operators.OnvifStateGateConfig(hold_seconds=duration)
    with pytest.raises(ValidationError):
        CameraOnvifOptimization(topic="Motion", qualified_topic="{urn:fixture}Motion", item_name="Active", hold_seconds=duration)


def test_identical_windows_preserve_shared_capture_and_motion_processing():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    registry = OperatorRegistry()
    operators.register_camera_pipeline_operators(registry)
    def pipeline(name, hold=10):
        return Pipeline(name=name, graph=build_pipeline_graph_v2(nodes=[
            {"id": "gate", "operator": "camera.onvif_state_gate", "config": {"camera_id": "fixture", "hold_seconds": hold, "stream_id": "shared-fixture"}},
            {"id": "source", "operator": "camera.source", "config": {"camera_id": "fixture", "keep_capture_when_gate_closed": True}},
            {"id": "motion", "operator": "camera.motion_gate", "config": {}},
        ], edges=[{"from": {"node": "gate", "port": "out"}, "to": {"node": "source", "port": "gate"}},
                  {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}}], graph_uid=name))
    report = PipelineGraphCompiler(registry).compile_many([pipeline("one"), pipeline("two"), pipeline("different", 30)])
    by_pipeline = [{n.node_id: n.signature for n in p.nodes} for p in report.pipelines]
    assert by_pipeline[0] == by_pipeline[1]
    assert all(by_pipeline[0][key] != by_pipeline[2][key] for key in by_pipeline[0])


def test_reordered_property_is_preserved_in_history_but_not_emitted_as_new_activation(monkeypatch):
    async def scenario():
        fake_time = SimpleNamespace(time=lambda: 1000., monotonic=lambda: 1.)
        monkeypatch.setattr(events, "time", fake_time)
        monkeypatch.setattr(operators, "time", fake_time)
        manager, _ = manager_and_transport()
        await manager.list_descriptors("controlled")
        runtime = manager._runtimes["controlled"]
        for value, timestamp in [("true", "2026-01-01T00:00:00Z"), ("false", "2026-01-01T00:00:01Z"), ("true", "2026-01-01T00:00:00Z")]:
            manager._record_messages(runtime, [events.OnvifEventMessage(0, "Motion", operation="Changed", utc_time=timestamp, data={"Active": value})])
        assert runtime.events[-1].state_accepted is False
        services = ServiceRegistry()
        async def recent(**kwargs):
            return {"available": True, "session": 0, "events": [event.as_dict() for event in runtime.events]}
        services.register("cameras.onvif_events.recent_events", recent)
        source = operators.OnvifEventSourceRuntime({}, PipelineRuntimeDependencies(services=services))
        packets = await source.produce_batch(SimpleNamespace(pipeline_name="replay", node_id="events"))
        assert [packet.payload["onvif_event"]["sequence"] for packet in packets] == [1, 2]
    asyncio.run(scenario())
