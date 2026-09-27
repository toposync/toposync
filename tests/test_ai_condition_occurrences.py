from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import numpy as np
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_ai.pipelines.runtime import AiConditionFilterRuntime


class EvidenceService:
    matches = True
    calls = 0

    async def call(self, service_id, **kwargs):
        assert service_id == "ai.infer.evaluate_condition"
        self.calls += 1
        return {"matches": self.matches, "confidence": 0.9, "reason": "visible action"}


def frame(timestamp, *, visible=True):
    return Packet.create(
        stream_id="camera:cat", payload={"frame_ts": timestamp, "capture_evidence": {
            "capture_instance": "test", "generation": 0, "sequence": int(timestamp * 100) + 1,
            "published_at": time.time(),
        }},
        artifacts={"main": Artifact(name="main", data=np.zeros((8, 8, 3), dtype=np.uint8))} if visible else {},
    )


def test_occurrence_replay_positive_negative_missing_and_distinct_visits():
    async def scenario():
        service = EvidenceService()
        runtime = AiConditionFilterRuntime(
            {"condition_description": "cat visibly drinking", "emit_mode": "occurrence",
             "minimum_duration_seconds": 2, "evidence_timeout_seconds": 5},
            PipelineRuntimeDependencies(services=service),
        )
        assert await runtime.process_packet(frame(0), None) == []
        opened = (await runtime.process_packet(frame(2), None))[0]
        assert opened.lifecycle == Lifecycle.OPEN
        updated = (await runtime.process_packet(frame(3), None))[0]
        assert updated.lifecycle == Lifecycle.UPDATE
        assert opened.stream_id == updated.stream_id
        assert updated.payload["condition_occurrence"]["duration_seconds"] == 3
        assert await runtime.process_packet(frame(3), None) == []
        assert await runtime.process_packet(frame(1), None) == []
        assert service.calls == 3

        # Merely nearby: an explicit negative closes, even inside cache TTL.
        service.matches = False
        closed = (await runtime.process_packet(frame(4), None))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.stream_id == opened.stream_id
        assert closed.payload["condition_occurrence"]["duration_seconds"] == 3
        assert closed.payload["condition_occurrence"]["status"] == "not_confirmed"
        assert await runtime.process_packet(frame(5), None) == []

        service.matches = True
        assert await runtime.process_packet(frame(6), None) == []
        second = (await runtime.process_packet(frame(8), None))[0]
        assert second.stream_id != opened.stream_id
        missing = (await runtime.process_packet(frame(9, visible=False), None))[0]
        assert missing.lifecycle == Lifecycle.CLOSE
        assert missing.payload["condition_occurrence"]["status"] == "missing_evidence"
        assert missing.payload["condition_occurrence"]["duration_seconds"] == 2
        assert await runtime.process_packet(frame(10, visible=False), None) == []

    asyncio.run(scenario())


def test_occurrence_gap_does_not_count_unobserved_time():
    async def scenario():
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "emit_mode": "occurrence",
             "minimum_duration_seconds": 0, "evidence_timeout_seconds": 3},
            PipelineRuntimeDependencies(services=EvidenceService()),
        )
        first = (await runtime.process_packet(frame(0), None))[0]
        outputs = await runtime.process_packet(frame(9), None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert outputs[0].stream_id == first.stream_id
        assert outputs[0].payload["condition_occurrence"]["duration_seconds"] == 0
        assert outputs[1].stream_id != first.stream_id

    asyncio.run(scenario())


def test_filter_missing_evidence_cannot_reuse_expired_positive(monkeypatch):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr("toposync_ext_ai.pipelines.runtime.time.monotonic", lambda: clock[0])
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "reuse_last_decision_seconds": 2},
            PipelineRuntimeDependencies(services=EvidenceService()),
        )
        assert len(await runtime.process_packet(frame(1), None)) == 1
        clock[0] = 13.0
        assert await runtime.process_packet(frame(2, visible=False), None) == []

    asyncio.run(scenario())


def test_occurrence_closes_on_offline_without_extending_duration():
    async def scenario():
        class Context:
            emitted = []
            incoming = frame(1)

            def is_cancelled(self):
                return len(self.emitted) == 2

            async def read(self, **kwargs):
                if self.incoming is not None:
                    packet, self.incoming = self.incoming, None
                    return packet
                await asyncio.sleep(0.02)
                return None

            async def emit(self, packet, **kwargs):
                self.emitted.append(packet)

        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "emit_mode": "occurrence",
             "minimum_duration_seconds": 0, "evidence_timeout_seconds": 0.01},
            PipelineRuntimeDependencies(services=EvidenceService()),
        )
        context = Context()
        await asyncio.wait_for(runtime.run(context), timeout=1)
        assert [packet.lifecycle for packet in context.emitted] == [Lifecycle.OPEN, Lifecycle.CLOSE]
        assert context.emitted[0].packet_id != context.emitted[1].packet_id
        assert context.emitted[-1].payload["condition_occurrence"]["status"] == "evidence_timeout"
        assert context.emitted[-1].payload["condition_occurrence"]["duration_seconds"] == 0

    asyncio.run(scenario())


def test_occurrence_rejects_unverifiable_freshness_and_repeated_capture():
    async def scenario():
        service = EvidenceService()
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "emit_mode": "occurrence",
             "minimum_duration_seconds": 0},
            PipelineRuntimeDependencies(services=service),
        )
        opened = (await runtime.process_packet(frame(1), None))[0]
        repeated = frame(2)
        repeated.payload["capture_evidence"]["sequence"] = 101
        assert await runtime.process_packet(repeated, None) == []
        assert service.calls == 1
        missing = frame(3)
        del missing.payload["capture_evidence"]
        closed = (await runtime.process_packet(missing, None))[0]
        assert closed.stream_id == opened.stream_id
        assert closed.payload["condition_occurrence"]["status"] == "stale_evidence"
        stale = frame(4)
        stale.payload["capture_evidence"]["published_at"] -= 20
        assert await runtime.process_packet(stale, None) == []
        assert service.calls == 1

    asyncio.run(scenario())


def test_occurrence_source_reset_accepts_restarted_media_clock_and_rejects_old_epoch():
    async def scenario():
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "emit_mode": "occurrence", "minimum_duration_seconds": 0},
            PipelineRuntimeDependencies(services=EvidenceService()),
        )
        old_frame = frame(100)
        first = (await runtime.process_packet(old_frame, None))[0]
        restarted = frame(0)
        restarted.payload["capture_evidence"]["generation"] = 1
        outputs = await runtime.process_packet(restarted, None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert outputs[0].stream_id == first.stream_id
        assert outputs[0].payload["condition_occurrence"]["status"] == "source_reset"
        assert outputs[1].stream_id != first.stream_id
        assert await runtime.process_packet(old_frame, None) == []
        assert await runtime.process_packet(replace(old_frame, lifecycle=Lifecycle.CLOSE), None) == []
        delayed = frame(101)
        assert await runtime.process_packet(delayed, None) == []
        replacement = frame(0)
        replacement.payload["capture_evidence"]["capture_instance"] = "replacement"
        changed = await runtime.process_packet(replacement, None)
        assert changed[-1].lifecycle == Lifecycle.OPEN
        assert changed[-1].stream_id != outputs[-1].stream_id
        assert await runtime.process_packet(restarted, None) == []

    asyncio.run(scenario())


def test_occurrence_stream_capacity_closes_evicted_stream():
    async def scenario():
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible action", "emit_mode": "occurrence",
             "minimum_duration_seconds": 0, "maximum_occurrence_streams": 2},
            PipelineRuntimeDependencies(services=EvidenceService()),
        )
        for index in range(5):
            outputs = await runtime.process_packet(replace(frame(index), stream_id=f"camera:{index}"), None)
            assert len(runtime._occurrences) <= 2
            if index >= 2:
                assert outputs[0].lifecycle == Lifecycle.CLOSE
                assert outputs[0].payload["condition_occurrence"]["status"] == "stream_capacity"
            assert outputs[-1].lifecycle == Lifecycle.OPEN
        retained = set(runtime._occurrences)
        stale = replace(frame(100), stream_id="camera:stale")
        stale.payload["capture_evidence"]["published_at"] -= 60
        assert await runtime.process_packet(stale, None) == []
        assert set(runtime._occurrences) == retained

    asyncio.run(scenario())


def test_occurrence_occlusion_closes_with_explicit_historical_evidence():
    async def scenario():
        class Service:
            observable = True

            async def call(self, service_id, **kwargs):
                return {"observable": self.observable, "matches": self.observable, "confidence": .9}

        service = Service()
        runtime = AiConditionFilterRuntime(
            {"condition_description": "visible drinking", "emit_mode": "occurrence", "minimum_duration_seconds": 0},
            PipelineRuntimeDependencies(services=service),
        )
        opened = (await runtime.process_packet(frame(0), None))[0]
        assert opened.payload["condition_occurrence"]["current_state_verified"] is True
        positive = (await runtime.process_packet(frame(2), None))[0]
        service.observable = False
        closed = (await runtime.process_packet(frame(3), None))[0]
        data = closed.payload["condition_occurrence"]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.stream_id == opened.stream_id
        assert data["status"] == "evidence_unobservable"
        assert data["current_state_verified"] is False
        assert data["duration_seconds"] == 2
        assert "imagem anterior" in data["description"]
        assert closed.artifacts == positive.artifacts
        assert await runtime.process_packet(frame(4), None) == []

    asyncio.run(scenario())
