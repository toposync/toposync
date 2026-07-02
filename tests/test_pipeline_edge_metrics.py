from __future__ import annotations

import asyncio

from toposync.runtime.pipelines import (
    Artifact,
    ArtifactMemoryCounter,
    BoundedChannel,
    DropPolicy,
    KeyedBoundedChannel,
    Packet,
    QueueOperationStatus,
)
from toposync.runtime.pipelines.execution import _snapshot_to_dict


def _packet(stream_id: str = "s", *, size: int = 0) -> Packet:
    artifacts = {}
    if size > 0:
        artifacts["main"] = Artifact(name="main", data=b"x" * size)
    return Packet.create(stream_id=stream_id, artifacts=artifacts)


def test_blocked_put_time_and_pressure_state_for_blocking_edge() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="edge", maxsize=1, drop_policy=DropPolicy.BLOCK)
        assert (await channel.put(_packet())).accepted

        result = await channel.put(_packet(), timeout_s=0.02)

        metrics = channel.metrics_snapshot()
        assert result.status == QueueOperationStatus.TIMEOUT
        assert metrics.blocked_put_time_ms > 0
        assert metrics.pressure_state == "blocked"
        assert metrics.pressure_cause == "put_timeout"
        assert metrics.last_event_ts is not None

    asyncio.run(scenario())


def test_waiting_get_time_and_oldest_packet_age() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="edge", maxsize=2, drop_policy=DropPolicy.DROP_OLDEST)

        empty = await channel.get(timeout_s=0.02)
        assert empty.status == QueueOperationStatus.TIMEOUT
        assert channel.metrics_snapshot().waiting_get_time_ms > 0

        assert (await channel.put(_packet())).accepted
        await asyncio.sleep(0.01)
        queued = channel.metrics_snapshot()
        assert queued.oldest_packet_age_ms > 0

        assert (await channel.get(timeout_s=0.02)).accepted
        assert channel.metrics_snapshot().oldest_packet_age_ms == 0.0

    asyncio.run(scenario())


def test_drop_reason_counts_for_drop_policies_and_artifact_budget() -> None:
    async def scenario() -> None:
        newest = BoundedChannel[Packet](name="newest", maxsize=1, drop_policy=DropPolicy.DROP_NEWEST)
        assert (await newest.put(_packet())).accepted
        assert (await newest.put(_packet())).status == QueueOperationStatus.DROPPED
        newest_metrics = newest.metrics_snapshot()
        assert newest_metrics.drop_reason_counts["drop_policy"] == 1
        assert newest_metrics.pressure_state == "dropping"

        latest = BoundedChannel[Packet](name="latest", maxsize=1, drop_policy=DropPolicy.LATEST_ONLY)
        assert (await latest.put(_packet())).accepted
        assert (await latest.put(_packet())).accepted
        latest_metrics = latest.metrics_snapshot()
        assert latest_metrics.drop_reason_counts["drop_policy"] == 1
        assert latest_metrics.dropped_total == 1

        budget = BoundedChannel[Packet](
            name="budget",
            maxsize=2,
            drop_policy=DropPolicy.DROP_NEWEST,
            pipeline_artifact_counter=ArtifactMemoryCounter(limit_bytes=4),
        )
        assert (await budget.put(_packet(size=4))).accepted
        assert (await budget.put(_packet(size=4))).status == QueueOperationStatus.DROPPED
        budget_metrics = budget.metrics_snapshot()
        assert budget_metrics.drop_reason_counts["artifact_budget"] == 1
        assert budget_metrics.artifact_bytes_dropped == 4
        assert budget_metrics.pressure_cause == "artifact_budget"

    asyncio.run(scenario())


def test_artifact_bytes_are_tracked_per_edge() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="artifacts", maxsize=2, drop_policy=DropPolicy.DROP_NEWEST)
        assert (await channel.put(_packet(size=3))).accepted
        assert (await channel.put(_packet(size=5))).accepted

        queued = channel.metrics_snapshot()
        assert queued.artifact_bytes_current == 8
        assert queued.artifact_bytes_max_seen == 8
        assert queued.artifact_bytes_accepted == 8
        assert queued.in_memory_artifact_bytes == 8

        assert (await channel.get(timeout_s=0.02)).accepted
        after_get = channel.metrics_snapshot()
        assert after_get.artifact_bytes_current == 5
        assert after_get.artifact_bytes_delivered == 3

        channel.clear()
        after_clear = channel.metrics_snapshot()
        assert after_clear.artifact_bytes_current == 0
        assert after_clear.artifact_bytes_dropped == 5
        assert after_clear.drop_reason_counts["clear"] == 1

    asyncio.run(scenario())


def test_keyed_channel_exposes_same_edge_metrics() -> None:
    async def scenario() -> None:
        channel = KeyedBoundedChannel[Packet](
            name="keyed",
            maxsize=4,
            drop_policy=DropPolicy.KEYED_LATEST_ONLY,
            key_fn=lambda packet: packet.stream_id,
        )
        assert (await channel.put(_packet("a", size=3))).accepted
        assert (await channel.put(_packet("b", size=4))).accepted
        assert channel.metrics_snapshot().active_keys == 2

        assert (await channel.put(_packet("a", size=5))).accepted
        metrics = channel.metrics_snapshot()
        assert metrics.drop_reason_counts["drop_policy"] == 1
        assert metrics.artifact_bytes_current == 9
        assert metrics.artifact_bytes_accepted == 12
        assert metrics.artifact_bytes_dropped == 3
        assert metrics.oldest_packet_age_ms >= 0.0
        assert metrics.pressure_state == "dropping"

        assert (await channel.get(timeout_s=0.02)).accepted
        assert channel.metrics_snapshot().artifact_bytes_delivered > 0

    asyncio.run(scenario())


def test_pipeline_snapshot_serializer_includes_advanced_edge_metrics() -> None:
    snapshot = BoundedChannel[Packet](
        name="edge",
        maxsize=1,
        drop_policy=DropPolicy.DROP_NEWEST,
    ).metrics_snapshot()

    payload = _snapshot_to_dict(snapshot)

    assert "blocked_put_time_ms" in payload
    assert "waiting_get_time_ms" in payload
    assert "oldest_packet_age_ms" in payload
    assert "last_event_ts" in payload
    assert "drop_reason_counts" in payload
    assert "artifact_bytes_current" in payload
    assert "pressure_state" in payload
