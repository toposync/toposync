"""Transport queues must route before applying per-destination compaction."""

import asyncio

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed import build_distributed_graphs
from toposync.runtime.pipelines.runtime import DropPolicy, QueueOperationStatus


def origin_runtime():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    graph = {
        "schema_version": 2,
        "uid": "two-notification-targets",
        "nodes": [
            {"uid": "source", "id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
            *[
                {"uid": target, "id": target, "operator": "core.notify", "config": {}}
                for target in ("notify_a", "notify_b")
            ],
        ],
        "edges": [
            {
                "uid": f"to-{target}",
                "from": {"node": "source", "port": "out"},
                "to": {"node": target, "port": "in"},
                "queue": {"max_items": 1, "drop_policy": "keyed_latest_only"},
            }
            for target in ("notify_a", "notify_b")
        ],
    }
    partition = build_distributed_graphs(Pipeline(name="target_queue", graph=graph), registry)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="target_queue", graph=partition.origin_graph)
    )
    # Build the actual channels and filters without starting sources, sinks or services.
    return PipelineRuntime(compiled=compiled, registry=registry)


def packet_for(target, lifecycle=Lifecycle.UPDATE):
    return Packet.create(
        stream_id="subject:one",
        lifecycle=lifecycle,
        payload={"subject": {"id": "subject:one"}},
        metadata={"dist_target": {"node_id": target, "port": "in"}},
    )


def prefilter(runtime, target):
    node = next(
        node for node in runtime.compiled.nodes
        if node.operator_id == "dist.target_filter" and node.normalized_config["target_node_id"] == target
    )
    context = runtime._context_by_node[node.node_id]
    return context.inputs["in"], runtime._runtime_by_node[node.node_id], context


@pytest.mark.parametrize("first_target", ["notify_a", "notify_b"])
def test_mixed_target_backlog_routes_each_subject_update_without_replacement(first_target):
    async def scenario():
        runtime = origin_runtime()
        targets = (first_target, "notify_b" if first_target == "notify_a" else "notify_a")
        packets = [packet_for(target) for target in targets]
        received = {target: [] for target in targets}
        try:
            # Every prefilter sees the multiplexed inbox output, not just its own target.
            for target in targets:
                channel, target_filter, context = prefilter(runtime, target)
                assert (await channel.put(packets[0])).accepted
                pending = asyncio.create_task(channel.put(packets[1], cancel_event=runtime._cancel_event))
                try:
                    await asyncio.sleep(0)
                    first = await channel.get(timeout_s=0.2)
                    assert first.accepted
                    received[target].extend(await target_filter.process_packet(first.item, context))
                    assert (await asyncio.wait_for(pending, 0.5)).accepted
                    if channel.depth:
                        second = await channel.get(timeout_s=0.2)
                        received[target].extend(await target_filter.process_packet(second.item, context))
                    assert channel.depth == 0
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            assert {target: [packet.packet_id for packet in output] for target, output in received.items()} == {
                target: [packet.packet_id for packet in packets if packet.metadata["dist_target"]["node_id"] == target]
                for target in targets
            }
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("lifecycle", [Lifecycle.UPDATE, Lifecycle.CLOSE])
def test_full_prefilter_queue_backpressures_and_cancellation_releases_writer(lifecycle):
    async def scenario():
        runtime = origin_runtime()
        channel, _, _ = prefilter(runtime, "notify_a")
        first = packet_for("notify_a")
        assert (await channel.put(first)).accepted
        cancel = asyncio.Event()
        pending = asyncio.create_task(channel.put(packet_for("notify_b", lifecycle), cancel_event=cancel))
        try:
            await asyncio.sleep(0)
            assert not pending.done(), "The full transport queue must wait, not replace another target's packet"
            assert channel.depth == channel.maxsize == 1
            cancel.set()
            result = await asyncio.wait_for(pending, 0.5)
            assert result.status == QueueOperationStatus.CANCELED
            remaining = await channel.get(timeout_s=0.2)
            assert remaining.item.packet_id == first.packet_id
            assert channel.depth == 0
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await runtime.stop()

    asyncio.run(scenario())


def test_transport_blocks_before_filter_but_user_compaction_remains_after_routing():
    runtime = origin_runtime()
    for edge in runtime.compiled.edges:
        if edge.source_node_id == "inbox":
            assert edge.channel_drop_policy == DropPolicy.BLOCK
            assert edge.queue_key_policy == "none"
        else:
            assert edge.channel_drop_policy == DropPolicy.KEYED_LATEST_ONLY
        assert edge.channel_maxsize == 1


def test_prefilter_update_timeout_is_reported_without_replacing_other_target():
    async def scenario():
        runtime = origin_runtime()
        context = runtime._context_by_node["inbox"]
        first = packet_for("notify_a")
        try:
            assert await context.emit(first) == 2
            # BLOCK preserves queued packets, but does not promise lossless UPDATE
            # delivery when a destination stops consuming beyond emit's timeout.
            assert await context.emit(packet_for("notify_b"), timeout_s=0.01) == 0
            assert context.metrics.timeout_count == 2
            for target in ("notify_a", "notify_b"):
                channel, _, _ = prefilter(runtime, target)
                assert channel.depth == channel.maxsize == 1
                assert (await channel.get(timeout_s=0.2)).item.packet_id == first.packet_id
        finally:
            await runtime.stop()

    asyncio.run(scenario())
