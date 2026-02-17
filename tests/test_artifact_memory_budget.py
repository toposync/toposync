from __future__ import annotations

import asyncio

from toposync.runtime.pipelines import (
    Artifact,
    ArtifactMemoryCounter,
    BoundedChannel,
    DropPolicy,
    Lifecycle,
    Packet,
)


def test_bounded_channel_drops_updates_when_pipeline_artifact_budget_is_exceeded() -> None:
    async def scenario() -> None:
        counter = ArtifactMemoryCounter(limit_bytes=10)
        channel: BoundedChannel[Packet] = BoundedChannel(
            name="test",
            maxsize=10,
            drop_policy=DropPolicy.DROP_OLDEST,
            pipeline_artifact_counter=counter,
        )

        p1 = Packet.create(
            stream_id="s",
            lifecycle=Lifecycle.UPDATE,
            artifacts={"a": Artifact(name="a", data=b"x" * 8)},
        )
        p2 = Packet.create(
            stream_id="s",
            lifecycle=Lifecycle.UPDATE,
            artifacts={"a": Artifact(name="a", data=b"y" * 8)},
        )

        assert (await channel.put(p1, timeout_s=0.0, cancel_event=None)).accepted
        assert (await channel.put(p2, timeout_s=0.0, cancel_event=None)).accepted
        assert channel.depth == 1
        assert counter.current_bytes <= 10

        got = await channel.get(timeout_s=0.0, cancel_event=None)
        assert got.accepted and got.item is not None
        assert got.item.packet_id == p2.packet_id

    asyncio.run(scenario())


def test_per_packet_artifact_budget_evicts_non_frame_artifacts() -> None:
    async def scenario() -> None:
        channel: BoundedChannel[Packet] = BoundedChannel(
            name="test",
            maxsize=10,
            drop_policy=DropPolicy.DROP_OLDEST,
            artifact_max_bytes_per_packet=10,
        )

        packet = Packet.create(
            stream_id="s",
            lifecycle=Lifecycle.UPDATE,
            artifacts={
                "a": Artifact(name="a", data=b"x" * 8),
                "b": Artifact(name="b", data=b"y" * 8),
            },
        )

        assert (await channel.put(packet, timeout_s=0.0, cancel_event=None)).accepted
        got = await channel.get(timeout_s=0.0, cancel_event=None)
        assert got.accepted and got.item is not None

        # Deterministic eviction: 'a' is dropped first (same size, sorted by name).
        assert got.item.artifacts["a"].data is None
        assert got.item.artifacts["b"].data is not None

    asyncio.run(scenario())


def test_global_artifact_budget_applies_across_channels() -> None:
    async def scenario() -> None:
        global_counter = ArtifactMemoryCounter(limit_bytes=10)
        channel_a: BoundedChannel[Packet] = BoundedChannel(
            name="a",
            maxsize=10,
            drop_policy=DropPolicy.DROP_OLDEST,
            global_artifact_counter=global_counter,
        )
        channel_b: BoundedChannel[Packet] = BoundedChannel(
            name="b",
            maxsize=10,
            drop_policy=DropPolicy.DROP_OLDEST,
            global_artifact_counter=global_counter,
        )

        p1 = Packet.create(stream_id="s", lifecycle=Lifecycle.UPDATE, artifacts={"a": Artifact(name="a", data=b"x" * 8)})
        p2 = Packet.create(stream_id="t", lifecycle=Lifecycle.UPDATE, artifacts={"a": Artifact(name="a", data=b"y" * 8)})

        assert (await channel_a.put(p1, timeout_s=0.0, cancel_event=None)).accepted
        # No droppable items in channel_b to free budget, so the newest UPDATE is dropped.
        assert not (await channel_b.put(p2, timeout_s=0.0, cancel_event=None)).accepted

    asyncio.run(scenario())

