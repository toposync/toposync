import asyncio
from dataclasses import replace

import numpy as np

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.audio_correlation import AudioCorrelationRuntime
from toposync_ext_cameras.audio_groups import AudioGroupRuntime
from toposync_ext_cameras.audio_pipeline import AudioFeaturesRuntime


class Context:
    pipeline_name = "paired-audio"
    node_id = "notify"
    async def run_blocking(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def packet(source, second=10, *, seed=1, basis="capture", capture="session", media_offset=0):
    values = np.random.default_rng(seed).normal(size=16000)
    envelope = np.interp(np.arange(16000), np.linspace(0, 15999, 20),
                         np.random.default_rng(seed + 100).uniform(.02, .6, 20))
    samples = (np.clip(values * envelope, -.9, .9) * 32767).astype(np.int16)
    metadata = {"source_id": source, "capture_id": capture, "sample_rate": 16000,
                "availability": "available", "clock": {"basis": basis, "started_at": second,
                "uncertainty_seconds": .01, "synchronization_reference": "calibrated-pair"}}
    return Packet.create(stream_id=source, payload={
        "camera_id": source, "source": {"source_id": source},
        "media": {"ts": second + media_offset}, "audio_capture": metadata,
    }).with_artifact(Artifact(name="audio", data=samples, metadata=metadata, private=True))


def test_group_updates_one_notification_until_both_sources_close(tmp_path):
    async def scenario():
        store = NotificationStore(tmp_path / "notifications.sqlite3")
        writes = []
        async def upsert(**values):
            writes.append(store.upsert(**values))
        sink = NotifyRuntime({"dedupe_by_occurrence": True, "update_interval_seconds": 0,
                              "historical_evidence_paths": ["audio_group", "audio_occurrence"]},
                             PipelineRuntimeDependencies(notifications_upsert=upsert))
        features = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": .01}}, None)
        correlator = AudioCorrelationRuntime({})
        groups = AudioGroupRuntime({})
        async def send(value, port):
            analyzed = (await features.process_packet(value, Context()))[0]
            matched = await correlator.process_packet(analyzed, Context(), port)
            events = await groups.process_packet(matched, Context())
            for event in events:
                assert event.artifacts == {}
                await sink.process_packet(event, Context())
            return events

        first = (await send(packet("a"), "first"))[0]
        second = (await send(packet("b", media_offset=100), "second"))[0]
        assert [first.lifecycle, second.lifecycle] == [Lifecycle.OPEN, Lifecycle.UPDATE]
        assert first.payload["subject"]["id"] == second.payload["subject"]["id"]
        assert second.payload["camera_id"] == "a"
        assert len(second.payload["audio_group"]["members"]) == 2
        await send(packet("a", 11), "first")
        await send(packet("b", 11, media_offset=100), "second")
        assert sum(created for _, created in writes) == 1
        closing = replace(packet("a", 12), lifecycle=Lifecycle.CLOSE, artifacts={})
        output = (await send(closing, "first"))[0]
        assert output.lifecycle == Lifecycle.UPDATE
        assert writes[-1][0].payload["status"] != "closed"
        closing = replace(packet("b", 12, media_offset=100), lifecycle=Lifecycle.CLOSE, artifacts={})
        output = (await send(closing, "second"))[0]
        assert output.lifecycle == Lifecycle.CLOSE
        assert writes[-1][0].payload["status"] == "closed"
        assert sum(created for _, created in writes) == 1
        assert output.payload["media"]["ts"] == 12
        assert output.payload["subject"]["started_ts"] == 10
        saved = writes[-1][0].payload["data"]["historical_evidence"]["audio_group"]
        assert not any(member["active"] for member in saved["members"])
        assert {member["camera_id"] for member in saved["members"]} == {"a", "b"}
        assert not groups.groups and not groups.members
        new = (await send(packet("a", 13, capture="new-session"), "first"))[0]
        assert new.payload["subject"]["id"] != first.payload["subject"]["id"]
        assert sum(created for _, created in writes) == 2
        await groups.shutdown()
        assert not groups.occurrences.states
    asyncio.run(scenario())


def test_unmatched_sources_and_active_divergence_remain_separate():
    async def scenario(basis):
        features = AudioFeaturesRuntime({"baseline": {"reference": "quiet", "rms": .01}}, None)
        correlator = AudioCorrelationRuntime({})
        groups = AudioGroupRuntime({})
        async def send(source, port, second=10, seed=1):
            analyzed = (await features.process_packet(packet(source, second, seed=seed, basis=basis), Context()))[0]
            matched = await correlator.process_packet(analyzed, Context(), port)
            return await groups.process_packet(matched, Context())
        first = (await send("a", "first"))[0]
        second = (await send("b", "second"))[0]
        if basis == "receipt":
            assert first.payload["subject"]["id"] != second.payload["subject"]["id"]
            return
        await send("a", "first", 11)
        divergent = await send("b", "second", 11, seed=33)
        assert [event.lifecycle for event in divergent] == [Lifecycle.UPDATE, Lifecycle.OPEN]
        assert divergent[0].payload["subject"]["id"] == first.payload["subject"]["id"]
        assert divergent[1].payload["subject"]["id"] != first.payload["subject"]["id"]
        assert len(groups.groups) == 2
        assert divergent[1].payload["subject"]["started_ts"] == 11
        for state in groups.occurrences.states.values():
            state.last_seen = 0
        expired = groups._group_events(groups.occurrences.expire_events())
        assert len(expired) == 2 and all(event.lifecycle == Lifecycle.CLOSE for event in expired)
        assert not groups.groups and not groups.members
    asyncio.run(scenario("capture"))
    asyncio.run(scenario("receipt"))
