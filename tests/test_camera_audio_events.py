import asyncio
from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio_events import AudioOccurrenceRuntime
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime
from toposync_ext_cameras.audio_clips import AudioClipStore


class Context:
    pipeline_name = "audio-events"
    node_id = "notify"
    async def run_blocking(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def test_audio_retention_is_explicit_and_stable_for_the_occurrence(tmp_path):
    dependencies = SimpleNamespace(config_store=SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)))
    analysis = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": 0.01}}, None)
    async def scenario():
        original = packet(1)
        original = replace(original, payload={**original.payload, "camera_id": "camera-one"})
        analyzed = (await analysis.process_packet(original, Context()))[0]
        from toposync.runtime.pipelines.operators_distributed import _serialize_packet, _deserialize_packet
        analyzed = _deserialize_packet(_serialize_packet(analyzed))
        assert analyzed.artifacts["audio"].private
        disabled = AudioOccurrenceRuntime({}, dependencies)
        assert (await disabled.process_packet(analyzed, Context()))[0].payload["audio_occurrence"]["clip"] is None
        assert not (tmp_path / "private-camera-audio").exists()
        enabled = AudioOccurrenceRuntime({"retain_audio": True}, dependencies)
        opened = (await enabled.process_packet(analyzed, Context()))[0]
        clip = opened.payload["audio_occurrence"]["clip"]
        assert clip["status"] == "retained" and clip["duration_seconds"] == 1
        assert opened.artifacts == {}
        closed = (await enabled.process_packet(replace(analyzed, lifecycle=Lifecycle.CLOSE), Context()))[0]
        assert closed.payload["audio_occurrence"]["clip"] == clip
        assert AudioClipStore(tmp_path).read(clip["id"], authorize=lambda camera: None) is not None
    asyncio.run(scenario())


def test_audio_review_storage_stays_at_origin_and_requires_private_transport():
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, register_builtin_operators
    from toposync.runtime.pipelines.distributed.plan import build_distributed_graphs, required_transport_capabilities
    from toposync_ext_cameras.audio_events import build_audio_recipe
    from toposync_ext_cameras.audio_pipeline import register_audio_operator
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_audio_operator(registry)
    pipeline = Pipeline(name="audio", enabled=True, graph=build_audio_recipe(
        graph_uid="audio", camera_id="camera", source_id="source", baseline={"reference": "quiet", "rms": 0.01}, retain_audio=True))
    graphs = build_distributed_graphs(pipeline, registry)
    assert "occurrence" in {node["id"] for node in graphs.origin_graph["nodes"]}
    assert required_transport_capabilities([pipeline], registry) == {"private_artifacts_v1"}


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
