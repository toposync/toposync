"""Notification media intervals must not be compared with civil clocks."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Lifecycle, Packet


CONTEXT = SimpleNamespace(pipeline_name="timing", node_id="notify")


def sample(timestamp, lifecycle=Lifecycle.UPDATE, generation=1):
    payload = {"subject": {"id": "actor"}}
    if timestamp is not None:
        payload["media"] = {"ts": timestamp}
        payload["capture_evidence"] = {"capture_instance": "decoder", "generation": generation}
    return Packet.create(stream_id="fixture", lifecycle=lifecycle, payload=payload)


def sink(config=None):
    emitted = []

    async def upsert(**record):
        emitted.append(record)

    return NotifyRuntime(config or {"update_interval_seconds": 0},
                         PipelineRuntimeDependencies(notifications_upsert=upsert)), emitted


@pytest.mark.parametrize("terminal", ["close", "empty_close", "shutdown"])
def test_media_duration_keeps_zero_origin_and_latest_observed_time(terminal):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(0, Lifecycle.OPEN), CONTEXT)
        await runtime.process_packet(sample(12), CONTEXT)
        await runtime.process_packet(sample(11), CONTEXT)
        if terminal == "shutdown":
            await runtime.shutdown()
        else:
            await runtime.process_packet(sample(10 if terminal == "close" else None,
                                                Lifecycle.CLOSE), CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event["started_ts"] == 0
        assert event["ts"] == 12
        assert event["duration_seconds"] == 12
        assert event["time_basis"] == "media"
        assert event["duration_status"] == "observed"
        assert not runtime._state

    asyncio.run(scenario())


def test_throttled_sample_still_advances_terminal_duration(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("toposync.runtime.pipelines.operators_sinks.time.monotonic", lambda: clock[0])

    async def scenario():
        runtime, emitted = sink({"update_interval_seconds": 60})
        await runtime.process_packet(sample(20, Lifecycle.OPEN), CONTEXT)
        clock[0] += 0.1
        await runtime.process_packet(sample(24), CONTEXT)
        assert len(emitted) == 1
        await runtime.shutdown()
        assert emitted[-1]["payload"]["event"]["duration_seconds"] == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["epoch", "domain", "basis"])
def test_known_capture_epoch_change_cannot_be_added_to_the_previous_interval(change):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(None if change == "basis" else 10, Lifecycle.OPEN), CONTEXT)
        await runtime.process_packet(sample(12), CONTEXT)
        incoming = sample(500, generation=2 if change == "epoch" else 1)
        if change == "domain":
            incoming.payload["source"] = {"clock_domain": "other-clock"}
        await runtime.process_packet(incoming, CONTEXT)
        await runtime.shutdown()
        event = emitted[-1]["payload"]["event"]
        assert event["duration_seconds"] is None
        assert event["duration_status"] == "clock_changed"

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf")])
def test_invalid_media_values_do_not_become_media_time(invalid):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(invalid, Lifecycle.OPEN), CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event["time_basis"] == "packet_created_at"
        assert event["duration_seconds"] == 0.0
        await runtime.shutdown()

    asyncio.run(scenario())


def test_packet_creation_fallback_is_not_labeled_media():
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(None, Lifecycle.OPEN), CONTEXT)
        assert emitted[0]["payload"]["event"]["time_basis"] == "packet_created_at"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["generation", "instance", "domain"])
def test_empty_close_with_explicit_new_epoch_invalidates_duration(change):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(10, Lifecycle.OPEN), CONTEXT)
        terminal = sample(None, Lifecycle.CLOSE)
        if change == "generation":
            terminal.payload["capture_evidence"] = {"generation": 2}
        elif change == "instance":
            terminal.payload["capture_evidence"] = {"capture_instance": "another-decoder"}
        else:
            terminal.payload["source"] = {"clock_domain": "another-clock"}
        await runtime.process_packet(terminal, CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event["duration_seconds"] is None
        assert event["duration_status"] == "clock_changed"

    asyncio.run(scenario())


@pytest.mark.parametrize("initial", [Lifecycle.OPEN, Lifecycle.UPDATE])
@pytest.mark.parametrize("capture", [None, {"capture_instance": "decoder"}, {"generation": 1}])
def test_close_with_media_but_incomplete_capture_preserves_observed_interval(initial, capture):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(2.25, initial), CONTEXT)
        await runtime.process_packet(sample(21.75), CONTEXT)
        terminal = sample(22, Lifecycle.CLOSE)
        terminal.payload.pop("capture_evidence")
        if capture is not None:
            terminal.payload["capture_evidence"] = capture
        await runtime.process_packet(terminal, CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event == {"started_ts": 2.25, "ts": 21.75, "duration_seconds": 19.5,
                         "time_basis": "media", "duration_status": "observed"}
        assert not runtime._state

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["generation", "instance", "domain"])
def test_close_with_media_and_explicit_clock_conflict_still_invalidates_duration(change):
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(2.25, Lifecycle.UPDATE), CONTEXT)
        await runtime.process_packet(sample(21.75), CONTEXT)
        terminal = sample(22, Lifecycle.CLOSE)
        terminal.payload.pop("capture_evidence")
        if change == "generation":
            terminal.payload["capture_evidence"] = {"generation": 2}
        elif change == "instance":
            terminal.payload["capture_evidence"] = {"capture_instance": "another-decoder"}
        else:
            terminal.payload["source"] = {"clock_domain": "another-clock"}
        await runtime.process_packet(terminal, CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event["duration_seconds"] is None
        assert event["duration_status"] == "clock_changed"

    asyncio.run(scenario())


def test_finite_timestamps_cannot_overflow_the_json_duration():
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(-1e308, Lifecycle.OPEN), CONTEXT)
        await runtime.process_packet(sample(1e308, Lifecycle.CLOSE), CONTEXT)
        event = emitted[-1]["payload"]["event"]
        assert event["duration_seconds"] is None
        assert event["duration_status"] == "invalid_interval"
        json.dumps(event, allow_nan=False)

    asyncio.run(scenario())


def test_interval_failure_is_published_even_when_other_content_is_unchanged():
    async def scenario():
        runtime, emitted = sink()
        await runtime.process_packet(sample(-1e308, Lifecycle.OPEN), CONTEXT)
        await runtime.process_packet(sample(-0.5e308), CONTEXT)
        assert emitted[-1]["payload"]["event"]["duration_status"] == "observed"
        await runtime.process_packet(sample(1e308), CONTEXT)
        assert emitted[-1]["payload"]["event"]["duration_status"] == "invalid_interval"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("basis", [None, "media"])
def test_restart_closure_preserves_persisted_interval_without_adding_downtime(tmp_path, basis):
    async def scenario():
        notifications = NotificationsRuntime(data_dir=tmp_path)
        event = {"started_ts": 0.0, "ts": 12.0, "duration_seconds": 12.0}
        if basis:
            event.update(time_basis=basis, duration_status="observed")
        record = await notifications.upsert(type="pipelines.event", title="Timing", description="",
            payload={"source": "pipelines", "status": "open", "lifecycle": "open", "event": event},
            dedupe_key="timing:fixture")
        assert await notifications.close_open_pipeline_notifications() == 1
        closed = await notifications.get(record["id"])
        assert closed["payload"]["event"] == event
        assert closed["payload"]["status"] == "closed"

    asyncio.run(scenario())
