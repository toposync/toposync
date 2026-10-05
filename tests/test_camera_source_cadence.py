from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

from toposync.runtime.pipelines import Packet, PipelineRuntimeDependencies
from toposync_ext_cameras.capture_service import CameraCaptureFrame
from toposync_ext_cameras.pipelines import operators as camera_ops


class SharedCapture:
    """A single 25 fps capture observed by independently paced source consumers."""

    def __init__(self, clock):
        self.clock = clock
        self.frame = np.zeros((8, 8, 3), dtype=np.uint8)
        self.released = []

    async def open(self, request, _dependencies):
        return SimpleNamespace(
            lease_id=request.owner_id,
            grabber=self,
            resolved=camera_ops.ResolvedCameraSource(
                rtsp_url="rtsp://fixture", fps=request.fps or 5,
                camera_id="cadence", camera_name="Cadence", source_id="wide",
                source_name="Wide", view_id="", role="main", clock_domain="fixture",
                transport="rtsp", used_ingest=False,
            ),
        )

    async def get_latest(self, lease_id, *, min_frame_ts):
        sequence = int(round(self.clock.now * 1000)) // 40 + 1
        frame_ts = 1000 + sequence / 25
        return CameraCaptureFrame(
            lease_id=lease_id, frame=self.frame, frame_ts=frame_ts,
            width=8, height=8, fresh=frame_ts > min_frame_ts,
            capture_evidence={"capture_instance": "shared", "sequence": sequence},
        )

    async def release(self, lease_id):
        self.released.append(lease_id)


def setup_sources(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(camera_ops, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 1000 + clock.now,
    ))
    capture = SharedCapture(clock)
    sources = [camera_ops.CameraSourceRuntime(
        {"camera_id": "cadence", "fps": fps}, PipelineRuntimeDependencies(),
    ) for fps in (5, 25)]
    for source in sources:
        source._capture_service = capture
    context = SimpleNamespace(inputs={}, pipeline_name="cadence", node_id="source",
                              cancel_event=asyncio.Event())
    return clock, capture, sources, context


def test_shared_capture_preserves_each_source_cadence_and_frame_identity(monkeypatch):
    async def scenario():
        clock, _capture, sources, context = setup_sources(monkeypatch)
        emitted = [[], []]
        for milliseconds in range(1000):
            clock.now = milliseconds / 1000
            for index, source in enumerate(sources):
                packet = await source.produce(context)
                if packet is not None:
                    emitted[index].append(packet)
        assert [len(packets) for packets in emitted] == [5, 25]
        for packets in emitted:
            sequences = [p.payload["capture_evidence"]["sequence"] for p in packets]
            assert len(sequences) == len(set(sequences))
            assert all(p.payload["capture_evidence"]["capture_instance"] == "shared"
                       for p in packets)
        for source in sources:
            await source.shutdown()

    asyncio.run(scenario())


def test_gate_closes_during_cadence_wait_and_reopening_emits_fresh_frame(monkeypatch):
    async def scenario():
        clock, capture, sources, context = setup_sources(monkeypatch)
        source = sources[0]
        assert await source.produce(context) is not None
        clock.now = 0.04
        assert await source.produce(context) is None

        class Gate:
            packet = Packet.create(stream_id="gate", payload={"gate_open": False})

            async def get(self, **_kwargs):
                packet, self.packet = self.packet, None
                return SimpleNamespace(accepted=packet is not None, item=packet)

        context.inputs["gate"] = Gate()
        assert await source.produce(context) is None
        assert len(capture.released) == 1
        context.inputs.clear()
        packet = await source.produce(context)
        assert packet is not None
        assert packet.payload["capture_evidence"]["sequence"] == 2
        assert await source.produce(context) is None
        await source.shutdown()

    asyncio.run(scenario())


def test_source_does_not_catch_up_with_bursts_after_delayed_poll(monkeypatch):
    async def scenario():
        clock, _capture, sources, context = setup_sources(monkeypatch)
        source = sources[0]
        assert await source.produce(context) is not None
        clock.now = 10.0
        assert await source.produce(context) is not None
        clock.now = 10.04
        assert await source.produce(context) is None
        clock.now = 10.2
        assert await source.produce(context) is not None
        await source.shutdown()

    asyncio.run(scenario())
