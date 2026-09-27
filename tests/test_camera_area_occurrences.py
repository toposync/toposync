import asyncio
import time
from dataclasses import replace

from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime


AREA = {"name": "piscina", "points": [{"x": 0, "z": 0}, {"x": 2, "z": 0}, {"x": 2, "z": 2}, {"x": 0, "z": 2}]}


def observation(timestamp, x=1, subject="cat-1", generation=0):
    return Packet.create(stream_id=f"track:{subject}", payload={
        "source_stream_id": "camera:one", "subject": {"id": subject, "category": "cat", "type": "track"},
        "world": {"x": x, "z": 1}, "frame_ts": timestamp,
        "capture_evidence": {"capture_instance": "decoder", "generation": generation,
                             "sequence": int(timestamp * 100) + 1, "published_at": time.time()},
    })


def test_area_presence_duration_exit_replay_and_independent_subjects():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA})
        assert await runtime.process_packet(observation(0, x=3), None) == []
        assert await runtime.process_packet(observation(1), None) == []
        first = (await runtime.process_packet(observation(2), None))[0]
        assert first.lifecycle == Lifecycle.OPEN
        update = (await runtime.process_packet(observation(3), None))[0]
        assert update.stream_id == first.stream_id
        assert update.payload["area_occurrence"]["duration_seconds"] == 2
        assert await runtime.process_packet(observation(3), None) == []
        assert await runtime.process_packet(observation(3, subject="cat-2"), None) == []
        second = (await runtime.process_packet(observation(4, subject="cat-2"), None))[0]
        assert second.stream_id != first.stream_id
        closed = (await runtime.process_packet(observation(4, x=3), None))[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        assert closed.payload["area_occurrence"]["duration_seconds"] == 2
        assert await runtime.process_packet(observation(5), None) == []
        reopened = (await runtime.process_packet(observation(6), None))[0]
        assert reopened.stream_id != first.stream_id

    asyncio.run(scenario())


def test_area_entry_needs_observed_crossing_and_missing_evidence_breaks_continuity():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "mode": "entry", "minimum_duration_seconds": 0})
        assert await runtime.process_packet(observation(0), None) == []
        assert await runtime.process_packet(observation(1), None) == []
        assert await runtime.process_packet(observation(2, x=3), None) == []
        opened = (await runtime.process_packet(observation(3), None))[0]
        missing = observation(4)
        missing.payload.pop("world")
        closed = (await runtime.process_packet(missing, None))[0]
        assert closed.stream_id == opened.stream_id
        assert closed.payload["area_occurrence"]["status"] == "unconfirmed"
        assert await runtime.process_packet(observation(5), None) == []
        assert await runtime.process_packet(observation(6, x=float("nan")), None) == []
        assert await runtime.process_packet(observation(7, x=True), None) == []

    asyncio.run(scenario())


def test_area_offline_closes_at_last_observation():
    async def scenario():
        class Context:
            incoming = observation(0)

            def __init__(self):
                self.emitted = []

            def is_cancelled(self):
                return len(self.emitted) == 2

            async def read(self, **kwargs):
                if self.incoming is not None:
                    packet, self.incoming = self.incoming, None
                    return packet
                await asyncio.sleep(0.02)

            async def emit(self, packet, **kwargs):
                self.emitted.append(packet)

        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "evidence_timeout_seconds": 0.01})
        context = Context()
        await asyncio.wait_for(runtime.run(context), timeout=1)
        assert context.emitted[-1].lifecycle == Lifecycle.CLOSE
        assert context.emitted[-1].payload["area_occurrence"]["status"] == "evidence_timeout"
        assert context.emitted[-1].payload["area_occurrence"]["duration_seconds"] == 0

    asyncio.run(scenario())


def test_area_reset_closes_old_visit_and_subject_capacity_is_bounded():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "maximum_subjects": 2})
        old = observation(100)
        opened = (await runtime.process_packet(old, None))[0]
        outputs = await runtime.process_packet(observation(0, generation=1), None)
        assert [item.lifecycle for item in outputs] == [Lifecycle.CLOSE, Lifecycle.OPEN]
        assert outputs[0].stream_id == opened.stream_id
        assert outputs[1].stream_id != opened.stream_id
        assert await runtime.process_packet(old, None) == []
        assert await runtime.process_packet(replace(old, lifecycle=Lifecycle.CLOSE), None) == []
        for index in range(1, 5):
            outputs = await runtime.process_packet(observation(index, subject=f"other-{index}"), None)
            assert len(runtime.states) <= 2
            if index > 1:
                assert outputs[0].payload["area_occurrence"]["status"] == "subject_capacity"

    asyncio.run(scenario())


def test_terminal_subject_and_stale_flood_cannot_restart_or_evict_presence():
    async def scenario():
        runtime = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0, "maximum_subjects": 1})
        opened = (await runtime.process_packet(observation(0), None))[0]
        stale = observation(1, subject="stale")
        stale.payload["capture_evidence"]["published_at"] -= 60
        assert await runtime.process_packet(stale, None) == []
        assert len(runtime.states) == 1
        stale.payload["subject"]["id"] = "cat-1"
        assert await runtime.process_packet(stale, None) == []
        assert next(iter(runtime.states.values())).opened
        closed = (await runtime.process_packet(replace(observation(2), lifecycle=Lifecycle.CLOSE), None))[0]
        assert closed.stream_id == opened.stream_id
        assert await runtime.process_packet(observation(3), None) == []
        assert not runtime.states

    asyncio.run(scenario())
