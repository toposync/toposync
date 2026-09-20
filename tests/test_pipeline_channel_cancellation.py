from __future__ import annotations

import asyncio

import pytest

from toposync.runtime.pipelines.runtime import (
    Artifact,
    BoundedChannel,
    DropPolicy,
    Lifecycle,
    Packet,
    QueueOperationStatus,
)


def packet(phase=Lifecycle.OPEN):
    return Packet.create(stream_id="cancel-test", lifecycle=phase).with_artifact(
        Artifact(name="private", data=b"x" * 16, private=True)
    )


@pytest.mark.parametrize("with_timeout", [False, True])
@pytest.mark.parametrize("after_arrival", [False, True])
def test_canceled_reader_cannot_steal_next_lifecycle_or_keep_reserved_bytes(
    with_timeout, after_arrival
):
    async def scenario():
        channel = BoundedChannel(name="cancel-test", maxsize=2, drop_policy=DropPolicy.BLOCK)
        cancel = asyncio.Event() if with_timeout else None
        existing = set(asyncio.all_tasks())
        reader = asyncio.create_task(
            channel.get(timeout_s=60 if with_timeout else None, cancel_event=cancel)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        first = packet()
        if after_arrival:
            await channel.put(first)
            # Give the auxiliary waiter a turn, but cancel before delivery resumes.
            await asyncio.sleep(0)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        for _ in range(3):
            await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == existing
        if not after_arrival:
            await channel.put(first)
        second = packet(Lifecycle.CLOSE)
        await channel.put(second)
        assert channel.depth == 2
        assert channel.metrics_snapshot().artifact_bytes_current == 32
        assert (await channel.get(timeout_s=0)).item is first
        assert (await channel.get(timeout_s=0)).item is second
        metrics = channel.metrics_snapshot()
        assert metrics.artifact_bytes_current == 0
        assert metrics.artifact_bytes_delivered == 32
        assert metrics.get_accepted == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_first", [False, True])
def test_item_wins_simultaneous_cancellation_event(cancel_first):
    async def scenario():
        channel = BoundedChannel(name="tie", maxsize=1)
        event = asyncio.Event()
        reader = asyncio.create_task(channel.get(timeout_s=1, cancel_event=event))
        for _ in range(3):
            await asyncio.sleep(0)
        value = packet()
        if cancel_first:
            event.set()
        await channel.put(value)
        event.set()
        result = await reader
        assert result.status == QueueOperationStatus.ACCEPTED and result.item is value
        assert channel.metrics_snapshot().artifact_bytes_current == 0

    asyncio.run(scenario())


def test_multiple_consumers_compete_without_losing_wakeup_or_order():
    async def scenario():
        channel = BoundedChannel(name="competing", maxsize=1, drop_policy=DropPolicy.BLOCK)
        readers = [asyncio.create_task(channel.get(timeout_s=1)) for _ in range(3)]
        await asyncio.sleep(0)
        for index in range(3):
            assert (await channel.put(index)).status == QueueOperationStatus.ACCEPTED
            await asyncio.sleep(0)
        results = await asyncio.gather(*readers)
        assert sorted(item.item for item in results) == [0, 1, 2]
        assert channel.depth == 0

    asyncio.run(scenario())
