import asyncio
from dataclasses import replace

import numpy as np

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio_events import AudioOccurrenceRuntime
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime


class Context:
    pipeline_name = "audio-events"
    node_id = "notify"
    async def run_blocking(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def packet(timestamp, loud=True, source="microphone-a"):
    samples = np.full(16000, 8000 if loud else 0, dtype=np.int16)
    metadata = {"sample_rate": 16000, "capture_id": "capture-1", "source_id": source,
                "clock": {"started_at": timestamp, "basis": "synthetic"}}
    return Packet.create(stream_id=source, payload={
        "source": {"source_id": source}, "audio_capture": metadata, "media": {"ts": timestamp},
    }).with_artifact(Artifact(name="audio", data=samples, metadata=metadata))


def test_audio_occurrence_history_groups_burst_and_keeps_positive_evidence(tmp_path):
    store = NotificationStore(tmp_path / "history.sqlite3")
    writes = []
    async def upsert(**values):
        writes.append(store.upsert(**values))
    analysis = AudioFeaturesRuntime({"baseline": {"reference": "synthetic-quiet", "rms": 0.01}}, None)
    events = AudioOccurrenceRuntime({})
    sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0,
                          "historical_evidence_paths": ["audio_analysis", "audio_occurrence"]},
                         PipelineRuntimeDependencies(notifications_upsert=upsert))
    async def send(value):
        analyzed = (await analysis.process_packet(value, Context()))[0]
        emitted = await events.process_packet(analyzed, Context())
        for event in emitted:
            assert event.artifacts == {}
            await sink.process_packet(event, Context())
        return emitted
    async def scenario():
        assert await send(packet(0, False)) == []
        assert (await send(packet(1)))[0].lifecycle == Lifecycle.OPEN
        assert await send(packet(1)) == []
        assert (await send(packet(2)))[0].lifecycle == Lifecycle.UPDATE
        assert await send(packet(3, False)) == []
        closed = (await send(packet(4, False)))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["audio_occurrence"]["status"] == "returned_to_baseline"
        assert closed.payload["media"]["ts"] == 3
        assert sum(created for _, created in writes) == 1
        record = writes[-1][0]
        assert record.payload["status"] == "closed"
        saved = record.payload["data"]["historical_evidence"]["audio_analysis"]
        assert saved["windows"][0]["anomaly"]["status"] == "above_baseline"
        assert (await send(packet(5)))[0].lifecycle == Lifecycle.OPEN
        assert sum(created for _, created in writes) == 2
        assert writes[-1][0].id != record.id
        terminal = replace(packet(6), lifecycle=Lifecycle.CLOSE, artifacts={})
        assert (await send(terminal))[0].payload["audio_occurrence"]["status"] == "source_closed"
        assert await send(packet(7)) == []
        assert (await send(packet(1, source="microphone-b")))[0].lifecycle == Lifecycle.OPEN
        assert sum(created for _, created in writes) == 3
    asyncio.run(scenario())


def test_audio_occurrence_requires_baseline_and_closes_on_gap_or_changed_reference():
    async def scenario():
        events = AudioOccurrenceRuntime({})
        no_baseline = AudioFeaturesRuntime({}, None)
        assert await events.process_packet((await no_baseline.process_packet(packet(0), Context()))[0], Context()) == []
        analysis = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": 0.01}}, None)
        async def send(timestamp):
            analyzed = (await analysis.process_packet(packet(timestamp), Context()))[0]
            return await events.process_packet(analyzed, Context())
        assert (await send(1))[0].lifecycle == Lifecycle.OPEN
        gap = await send(9)
        assert [value.lifecycle for value in gap] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert gap[0].payload["audio_occurrence"]["status"] == "evidence_gap"
        assert gap[0].payload["media"]["ts"] == 2
        analysis.config.baseline.reference = "different-calibration"
        changed = await send(10)
        assert [value.lifecycle for value in changed] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert changed[0].payload["audio_occurrence"]["status"] == "baseline_changed"
        for state in events.states.values():
            assert state.packet.artifacts == {}
    asyncio.run(scenario())


def test_audio_occurrence_idle_timeout_preserves_last_positive_time(monkeypatch):
    async def scenario():
        analysis = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": 0.01}}, None)
        events = AudioOccurrenceRuntime({})
        analyzed = (await analysis.process_packet(packet(1), Context()))[0]
        await events.process_packet(analyzed, Context())
        state = next(iter(events.states.values()))
        monkeypatch.setattr("toposync_ext_cameras.audio_events.time.monotonic", lambda: state.last_seen + 6)
        outputs = []
        class Idle:
            def is_cancelled(self):
                return bool(outputs)
            async def read(self, **kwargs):
                return None
            async def emit(self, packet, **kwargs):
                outputs.append(packet)
        await events.run(Idle())
        assert len(outputs) == 1
        assert outputs[0].lifecycle == Lifecycle.CLOSE
        assert outputs[0].payload["audio_occurrence"]["status"] == "evidence_timeout"
        assert outputs[0].payload["media"]["ts"] == 2
        assert state.packet is None
    asyncio.run(scenario())
