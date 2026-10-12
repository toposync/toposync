from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines import Lifecycle, Packet, PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_core import StationaryEventRuntime
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.operators_gates import CoreFilterRuntime
from toposync.runtime.pipelines.execution import NodeRuntimeMetrics
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras import observation_window
from toposync_ext_cameras.pipelines import operators
from test_camera_source_cadence import setup_sources


@pytest.fixture
def clock_and_store(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    fake_time = SimpleNamespace(time=lambda: 1000 + clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(operators, "time", fake_time)
    monkeypatch.setattr(observation_window, "time", fake_time)
    store = observation_window.ObservationWindowStore()
    monkeypatch.setattr(observation_window, "_STORE", store)
    return clock, store


def observed(clock, *, subject_id="vehicle", camera_id="camera", source_id="wide", sequence=None):
    return Packet.create(stream_id=f"track:{subject_id}", payload={
        "source_stream_id": f"camera:{camera_id}:{source_id}",
        "source": {"device_id": camera_id, "source_id": source_id},
        "subject": {"id": subject_id, "category": "car"},
        "capture_evidence": {"capture_instance": "capture", "generation": 1,
                             "sequence": sequence or int(clock.now * 1000) + 1,
                             "published_at": 1000 + clock.now},
    })


def test_silent_onvif_does_not_close_visual_candidates_and_other_owners(clock_and_store):
    async def scenario():
        clock, store = clock_and_store
        vehicle = operators.ObservationHoldRuntime({"window_id": "front", "hold_seconds": 15})
        person = operators.ObservationHoldRuntime({"window_id": "front", "hold_seconds": 15})
        source = operators.CameraSourceRuntime({"camera_id": "camera", "source_id": "wide",
                                               "observation_window_id": "front"}, PipelineRuntimeDependencies())
        source._gate_open = False
        assert not source._capture_gate_open()
        for seconds in (0, 10, 100):
            clock.now = seconds
            packet = observed(clock)
            assert await vehicle.process_packet(packet, None) == [packet]
            assert source._capture_gate_open()  # No stationary confirmation required.
        await person.process_packet(observed(clock, subject_id="person"), None)
        await vehicle.shutdown()
        assert source._capture_gate_open()  # One branch cannot release another's demand.
        assert not store.active(("other-camera", "wide", "front"))
        assert not store.active(("camera", "other-source", "front"))
        assert not store.active(("camera", "wide", "other-window"))
        clock.now = 115.01
        assert not source._capture_gate_open()
        await person.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["duplicate", "older", "stale", "future", "missing", "old_generation"])
def test_invalid_or_replayed_evidence_cannot_extend_capture(clock_and_store, failure):
    async def scenario():
        clock, store = clock_and_store
        holder = operators.ObservationHoldRuntime({"window_id": "front", "hold_seconds": 5})
        packet = observed(clock)
        await holder.process_packet(packet, None)
        clock.now = 4
        replay = observed(clock)
        if failure == "duplicate":
            replay.payload["capture_evidence"] = dict(packet.payload["capture_evidence"])
        elif failure == "older":
            replay.payload["capture_evidence"]["published_at"] = 999
        elif failure == "stale":
            replay.payload["capture_evidence"]["published_at"] = 998
        elif failure == "future":
            replay.payload["capture_evidence"]["published_at"] = 1005
        elif failure == "missing":
            replay.payload.pop("capture_evidence")
        else:
            replay.payload["capture_evidence"]["generation"] = 0
        await holder.process_packet(replay, None)
        clock.now = 5.01
        assert not store.active(("camera", "wide", "front"))
        await holder.shutdown()
    asyncio.run(scenario())


def test_terminal_subject_and_obsolete_close_cannot_change_current_demand(clock_and_store):
    async def scenario():
        clock, store = clock_and_store
        holder = operators.ObservationHoldRuntime({"window_id": "front"})
        first = observed(clock)
        await holder.process_packet(first, None)
        clock.now = 1
        current = observed(clock)
        current.payload["capture_evidence"]["generation"] = 2
        await holder.process_packet(current, None)
        await holder.process_packet(first.with_lifecycle(Lifecycle.CLOSE), None)
        assert store.active(("camera", "wide", "front"))
        await holder.process_packet(current.with_lifecycle(Lifecycle.CLOSE), None)
        assert not store.active(("camera", "wide", "front"))
        clock.now = 2
        replay = observed(clock)
        replay.payload["capture_evidence"]["generation"] = 2
        await holder.process_packet(replay, None)
        assert not store.active(("camera", "wide", "front"))
        await holder.shutdown()
    asyncio.run(scenario())


def test_moving_approach_is_retained_before_area_and_stationary_area_takes_over(clock_and_store):
    async def scenario():
        clock, _ = clock_and_store
        movement = CoreFilterRuntime({"expression": "payload.velocity.valid != True or payload.velocity.stopped != True"})
        approach = operators.ObservationHoldRuntime({"window_id": "front"})
        area = operators.ObservationHoldRuntime({"window_id": "front"})
        source = operators.CameraSourceRuntime({"camera_id": "camera", "source_id": "wide",
                                               "observation_window_id": "front"}, PipelineRuntimeDependencies())
        source._gate_open = False
        context = SimpleNamespace(metrics=NodeRuntimeMetrics())
        for seconds in (0, 10, 20, 30):
            clock.now = seconds
            packet = observed(clock)
            packet.payload["velocity"] = {"valid": True, "stopped": False}
            if seconds == 0:
                packet = packet.with_lifecycle(Lifecycle.OPEN)
            for moving in await movement.process_packet(packet, context):
                await approach.process_packet(moving, None)
            assert source._capture_gate_open()
        # Area admission owns stationary retention; movement alone cannot renew a parked background object.
        for seconds in (31, 40, 50, 60):
            clock.now = seconds
            packet = observed(clock)
            packet.payload["velocity"] = {"valid": True, "stopped": True}
            assert await movement.process_packet(packet, context) == []
            await area.process_packet(packet, None)
            assert source._capture_gate_open()
        await area.shutdown()
        assert not source._capture_gate_open()  # The approach lease expired without new movement.
        clock.now = 61
        packet = observed(clock)
        packet.payload["velocity"] = {"valid": True, "stopped": False}
        for moving in await movement.process_packet(packet, context):
            await approach.process_packet(moving, None)
        assert source._capture_gate_open()
        await approach.shutdown()
    asyncio.run(scenario())


def test_undeclared_onvif_activity_wakes_visual_inspection_without_asserting_category(clock_and_store):
    async def scenario():
        clock, _ = clock_and_store
        services = ServiceRegistry()
        response = {"available": True, "session": 1, "events": []}

        async def snapshot(**kwargs):
            return {"available": True, "known": True, "value": False, "session": 1}

        async def recent(**kwargs):
            return response

        services.register("cameras.onvif_events.snapshot", snapshot)
        services.register("cameras.onvif_events.recent_events", recent)
        selection = {"topic": "Camera/Activity", "qualified_topic": "{urn:camera}Camera/Activity",
                     "item_name": "State", "source": {"Source": "wide"}, "key": {}}
        gate = operators.OnvifStateGateRuntime({"camera_id": "camera", "hold_seconds": 10,
                                               "activation_events": [selection]},
                                              PipelineRuntimeDependencies(services=services))
        context = SimpleNamespace(pipeline_name="inspection", node_id="gate")
        assert (await gate.produce(context)).lifecycle == Lifecycle.CLOSE
        clock.now = 1
        event = {"sequence": 1, "session": 1, "received_at_ts": 1001,
                 "topic": selection["topic"], "qualified_topic": selection["qualified_topic"],
                 "source": selection["source"], "key": {}, "data": {"State": "true"},
                 "operation": "Changed", "is_property": None, "state_accepted": False}
        response["events"] = [event]
        opened = await gate.produce(context)
        assert opened.lifecycle == Lifecycle.OPEN
        assert opened.payload["onvif"]["value"] is False  # Original property stays false.
        assert opened.payload["onvif"]["activation_hint_semantics"] == "visual_attention_only"
        assert "subject" not in opened.payload and "stationary_event" not in opened.payload
        clock.now = 2
        response["events"].append({**event, "sequence": 2, "received_at_ts": 1002,
                                   "data": {"State": "false"}})
        assert await gate.produce(context) is None  # Falling edge does not close or renew.
        clock.now = 10.9
        assert await gate.produce(context) is None
        clock.now = 11.01
        assert (await gate.produce(context)).lifecycle == Lifecycle.CLOSE
        response.update(available=False)
        gate._config.fail_open = True
        fallback = await gate.produce(context)
        assert fallback.lifecycle == Lifecycle.OPEN
        assert fallback.payload["onvif"]["reason"] == "activation_hints_unavailable"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["history", "future", "namespace", "source", "key", "item", "initialized", "session", "reordered"])
def test_activation_hint_rejects_unbound_or_non_current_events(clock_and_store, failure):
    async def scenario():
        clock, _ = clock_and_store
        services = ServiceRegistry()
        selection = {"topic": "Camera/Activity", "qualified_topic": "{urn:camera}Camera/Activity",
                     "item_name": "State", "source": {"Source": "wide"}, "key": {}}
        event = {"sequence": 1, "session": 1, "received_at_ts": 1000,
                 "topic": selection["topic"], "qualified_topic": selection["qualified_topic"],
                 "source": selection["source"], "key": {}, "data": {"State": "true"},
                 "operation": "Changed", "is_property": None}
        response = {"available": True, "session": 1, "events": [event]}
        if failure == "history":
            event["received_at_ts"] = 999
        elif failure == "future":
            event["received_at_ts"] = 1001
        elif failure == "item":
            event["data"] = {"Unrelated": "true"}
        elif failure == "namespace":
            event["qualified_topic"] = "{urn:other}Camera/Activity"
        elif failure in {"source", "key"}:
            event[failure] = {"Source": "other"}
        elif failure == "initialized":
            event["operation"] = "Initialized"
        elif failure == "session":
            event["session"] = 0
        else:
            event.update(is_property=True, state_accepted=False)

        async def recent(**kwargs):
            return response

        services.register("cameras.onvif_events.recent_events", recent)
        gate = operators.OnvifStateGateRuntime({"activation_events": [selection]}, PipelineRuntimeDependencies(services=services))
        assert await gate._resolve_activation_hints() == (False, True)
    asyncio.run(scenario())


def test_capture_stop_retains_stationary_evidence_then_suspends_and_reopens(monkeypatch, clock_and_store, tmp_path):
    async def scenario():
        _, store = clock_and_store
        clock, capture, other_sources, context = setup_sources(monkeypatch)
        monkeypatch.setattr(observation_window, "time", SimpleNamespace(monotonic=lambda: clock.now))
        fake_time = SimpleNamespace(time=lambda: 1000 + clock.now, monotonic=lambda: clock.now)
        monkeypatch.setattr("toposync.runtime.pipelines.operators_core.time", fake_time)
        monkeypatch.setattr("toposync.runtime.pipelines.packet_contract.time", fake_time)
        source = operators.CameraSourceRuntime({"camera_id": "cadence", "source_id": "wide", "fps": 4,
                                               "gate_min_observation_seconds": 5,
                                               "observation_window_id": "front"}, PipelineRuntimeDependencies())
        source._capture_service = capture
        original = capture.get_latest

        async def fresh(*args, **kwargs):
            frame = await original(*args, **kwargs)
            return replace(frame, capture_evidence={**frame.capture_evidence, "generation": 1,
                                                    "published_at": 1000 + clock.now})

        capture.get_latest = fresh
        selection = {"topic": "Camera/Activity", "qualified_topic": "{urn:camera}Camera/Activity",
                     "item_name": "State", "source": {"Source": "wide"}, "key": {}}
        hint = {"sequence": 1, "session": 1, "received_at_ts": 1000,
                "topic": selection["topic"], "qualified_topic": selection["qualified_topic"],
                "source": selection["source"], "key": {}, "data": {"State": "true"},
                "operation": "Changed", "is_property": None}
        response = {"available": True, "session": 1, "events": [hint]}
        services = ServiceRegistry()

        async def snapshot(**kwargs):
            return {"available": True, "known": True, "value": False, "session": 1}

        async def recent(**kwargs):
            return response

        services.register("cameras.onvif_events.snapshot", snapshot)
        services.register("cameras.onvif_events.recent_events", recent)
        onvif = operators.OnvifStateGateRuntime({"camera_id": "cadence", "hold_seconds": 1,
                                                "activation_events": [selection]},
                                               PipelineRuntimeDependencies(services=services))

        class Gate:
            packet = None

            async def get(self, **kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)

        gate = Gate()
        context.inputs["gate"] = gate
        holder = operators.ObservationHoldRuntime({"window_id": "front", "hold_seconds": 15})
        stationary = StationaryEventRuntime({"min_stationary_seconds": 6, "min_valid_samples": 3,
                                            "require_arrival": True})
        counts = {"detect": 0, "map": 0}
        events = []
        records = []
        notifications = NotificationStore(tmp_path / "notifications.sqlite3")

        async def persist(**values):
            result = notifications.upsert(**values)
            records.append(result)
            return result

        notify = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0,
                                "include_payload_paths": ["stationary_event"]},
                               PipelineRuntimeDependencies(notifications_upsert=persist))
        notify_context = SimpleNamespace(pipeline_name="vehicle_window", node_id="notify")

        async def process_frame():
            gate.packet = await onvif.produce(context)
            frame = await source.produce(context)
            if frame is None:
                return
            # Controlled detection/mapping annotations; downstream operators are real.
            counts["detect"] += 1
            counts["map"] += 1
            payload = {**frame.payload, "source_stream_id": frame.stream_id,
                       "subject": {"id": "vehicle", "category": "car"},
                       "world": {"x": 1, "z": 1},
                       "velocity": {"valid": True, "stopped": clock.now > 0,
                                    "speed_mps": 0 if clock.now > 0 else 2}}
            packet = replace(frame, payload=payload)
            await holder.process_packet(packet, None)
            outputs = await stationary.process_packet(packet, None)
            events.extend(outputs)
            for event in outputs:
                await notify.process_packet(event, notify_context)

        await process_frame()
        response["events"].append({**hint, "sequence": 2, "received_at_ts": 1001,
                                   "data": {"State": "false"}})
        for seconds in range(1, 21):
            clock.now = seconds
            await process_frame()
        assert source._gate_open is False and source._visual_observation_retained()
        assert counts == {"detect": 21, "map": 21}
        opened = next(packet for packet in events if packet.lifecycle == Lifecycle.OPEN)
        assert opened.payload["stationary_event"]["last_evidence_at"] >= 1007
        assert any(packet.lifecycle == Lifecycle.UPDATE for packet in events)
        # Fresh absence ceases retention; idle expiry closes with the real evidence reason.
        clock.now = 36
        await process_frame()
        assert counts == {"detect": 21, "map": 21}
        assert capture.released == [source._capture_owner_id]
        assert not store.active(("cadence", "wide", "front"))
        [closed] = await stationary.on_idle(None)
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["stationary_event"]["reason"] == "evidence_timeout"
        assert closed.payload["stationary_event"]["last_evidence_at"] == events[-1].payload["stationary_event"]["last_evidence_at"]
        await notify.process_packet(closed, notify_context)
        assert sum(created for _, created in records) == 1
        assert len({record.id for record, _ in records}) == 1
        history = records[-1][0].payload
        assert history["lifecycle"] == "close"
        assert history["data"]["stationary_event"]["confirmed"] is True
        assert history["data"]["stationary_event"]["stationary_seconds"] >= 18
        for seconds in (37, 38, 39):
            clock.now = seconds
            await process_frame()
        assert counts == {"detect": 21, "map": 21}
        clock.now = 40
        response["events"].append({**hint, "sequence": 3, "received_at_ts": 1040})
        await process_frame()
        assert counts == {"detect": 22, "map": 22}
        await holder.shutdown()
        await notify.shutdown()
        await source.shutdown()
        for other in other_sources:
            await other.shutdown()
    asyncio.run(scenario())
