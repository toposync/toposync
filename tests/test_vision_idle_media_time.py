from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines import Lifecycle, Packet, PipelineRuntimeDependencies
from toposync_ext_vision.processing.tasks import event_assembler, group_events, tracking


@pytest.mark.parametrize("kind", ["track", "group"])
@pytest.mark.parametrize("has_media_timestamp", [False, True])
def test_idle_closure_preserves_evidence_time(monkeypatch, kind, has_media_timestamp) -> None:
    clock = [0.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 9_999.0)
    for module in (event_assembler, group_events, tracking):
        monkeypatch.setattr(module, "time", timer)

    class Context:
        async def run_blocking(self, function, /, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    async def scenario() -> None:
        if kind == "track":
            runtime = tracking.VisionTrackRuntime(
                {"close_after_seconds": 1}, PipelineRuntimeDependencies(),
            )
            payload = {"vision": {"detections": [{
                "label": "person", "label_id": 1, "model_id": "fixture",
                "score": .9, "bbox01": [.1, .1, .3, .5],
            }]}}
            flush = runtime._flush_idle_events
        else:
            runtime = group_events.VisionGroupEventsRuntime({"idle_timeout_seconds": 1})
            payload = {"subject": {"id": "person", "type": "event", "category": "person",
                                   "confidence": .9, "bbox01": [.1, .1, .3, .5]}}
            flush = runtime._flush_idle_groups
        payload.update(camera_id="camera", frame_ts=12.5)
        if has_media_timestamp:
            payload["media"] = {"ts": 12.5}
        packet = Packet.create(stream_id="source", lifecycle=Lifecycle.OPEN, payload=payload)
        try:
            opened = await runtime.process_packet(packet, Context())
            assert len(opened) == 1 and opened[0].lifecycle == Lifecycle.OPEN
            assert await flush(Context()) == []
            clock[0] = 2
            closed = await flush(Context())
            assert len(closed) == 1 and closed[0].lifecycle == Lifecycle.CLOSE
            assert closed[0].payload["frame_ts"] == 12.5
            if has_media_timestamp:
                assert closed[0].payload["media"]["ts"] == 12.5
            assert closed[0].artifacts == {}
            assert await flush(Context()) == []
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
