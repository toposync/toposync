from __future__ import annotations

import asyncio
from dataclasses import replace

from toposync.runtime.pipelines.operators_core import StreamStateSnapshotRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet


def test_stream_state_snapshot_emits_periodic_snapshots_and_never_includes_blob_data() -> None:
    async def scenario() -> None:
        runtime = StreamStateSnapshotRuntime({"interval_seconds": 1.0, "max_streams": 16})

        class _Ctx:
            node_id = "snapshot"

            def __init__(self) -> None:
                self.emitted: list[tuple[str, Packet]] = []

            async def emit(self, packet: Packet, *, port: str = "out", timeout_s: float = 0.1) -> int:  # noqa: ARG002
                self.emitted.append((port, packet))
                return 1

        ctx = _Ctx()
        artifact = Artifact(
            name="main",
            data=b"blob",
            reference="files/frame.png",
            mime_type="image/png",
            metadata={"source": "test"},
        )

        open_packet = Packet.create(stream_id="stream:1", lifecycle=Lifecycle.OPEN, payload={"seq": 0}, artifacts={"main": artifact})
        open_packet = replace(open_packet, created_at=1000.0, created_monotonic_ns=1000)
        out = await runtime.process_packet(open_packet, ctx)
        assert out == [open_packet]

        snapshots = [packet for port, packet in ctx.emitted if port == "snapshot"]
        assert len(snapshots) == 1
        assert snapshots[0].lifecycle == Lifecycle.OPEN
        assert snapshots[0].created_at == 1000.0
        assert snapshots[0].artifacts["main"].data is None
        assert snapshots[0].metadata["snapshot_state"]["update_count"] == 0

        update1 = Packet.create(stream_id="stream:1", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}, artifacts={"main": artifact})
        update1 = replace(update1, created_at=1000.4, created_monotonic_ns=1001)
        await runtime.process_packet(update1, ctx)

        snapshots = [packet for port, packet in ctx.emitted if port == "snapshot"]
        assert len(snapshots) == 1

        update2 = Packet.create(stream_id="stream:1", lifecycle=Lifecycle.UPDATE, payload={"seq": 2}, artifacts={"main": artifact})
        update2 = replace(update2, created_at=1001.4, created_monotonic_ns=1002)
        await runtime.process_packet(update2, ctx)

        snapshots = [packet for port, packet in ctx.emitted if port == "snapshot"]
        assert len(snapshots) == 2
        assert snapshots[-1].lifecycle == Lifecycle.UPDATE
        assert snapshots[-1].payload.get("seq") == 2
        assert snapshots[-1].metadata["snapshot_state"]["update_count"] == 2

        close_packet = Packet.create(stream_id="stream:1", lifecycle=Lifecycle.CLOSE, payload={"seq": 3}, artifacts={"main": artifact})
        close_packet = replace(close_packet, created_at=1002.0, created_monotonic_ns=1003)
        await runtime.process_packet(close_packet, ctx)

        snapshots = [packet for port, packet in ctx.emitted if port == "snapshot"]
        assert len(snapshots) == 3
        assert snapshots[-1].lifecycle == Lifecycle.CLOSE
        assert snapshots[-1].metadata["snapshot_state"]["duration_seconds"] >= 2.0

    asyncio.run(scenario())

