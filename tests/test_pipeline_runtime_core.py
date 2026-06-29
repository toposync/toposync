from __future__ import annotations

import asyncio

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    BoundedChannel,
    DropPolicy,
    KeyedBoundedChannel,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    QueueOperationStatus,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
)
from toposync.runtime.pipelines.operators_core import StationaryEventRuntime


class _OnePacketSourceRuntime(SourceOperatorRuntime):
    def __init__(self) -> None:
        self._done = False

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._done:
            return None
        self._done = True
        return Packet.create(stream_id="diagnostic")


class _FailingTransformRuntime(TransformOperatorRuntime):
    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        raise ValueError("diagnostic failure")


def test_packet_creation_and_artifact_attachment() -> None:
    packet = Packet.create(stream_id="camera_1", lifecycle=Lifecycle.OPEN, payload={"kind": "object"})
    assert packet.stream_id == "camera_1"
    assert packet.lifecycle == Lifecycle.OPEN
    assert packet.packet_id

    enriched = packet.with_artifact(Artifact(name="main", reference="files/cam/1.jpg"))
    assert "main" in enriched.artifacts
    assert enriched.artifacts["main"].reference == "files/cam/1.jpg"
    assert enriched.packet_id == packet.packet_id


def test_stationary_event_does_not_open_from_single_slow_sample() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.25,
                "min_valid_samples": 3,
            }
        )
        packet = Packet.create(
            stream_id="camera",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "frame_ts": 1.0,
                "subject": {"id": "subject-1"},
                "velocity": {"valid": True, "stopped": True, "speed_mps": 0.01},
                "world": {"x": 0.0, "z": 0.0},
            },
        )

        assert await runtime.process_packet(packet, context=None) == []

    asyncio.run(scenario())


def test_stationary_event_opens_after_short_confirmed_stop() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.25,
                "min_valid_samples": 3,
                "max_stationary_distance_m": 0.35,
            }
        )

        def make_packet(frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": True, "speed_mps": 0.01},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [
            make_packet(1.0, 0.0),
            make_packet(1.6, 0.03),
            make_packet(2.3, 0.04),
        ]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert len(outputs) == 1
        assert outputs[0].lifecycle == Lifecycle.OPEN
        stationary = outputs[0].payload.get("stationary_event")
        assert isinstance(stationary, dict)
        assert stationary.get("confirmed") is True
        assert stationary.get("sample_count") == 3
        assert stationary.get("distance_m") == 0.04

    asyncio.run(scenario())


def test_stationary_event_requires_arrival_when_configured() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": True,
                "min_stationary_seconds": 1.25,
                "min_valid_samples": 3,
            }
        )

        def make_packet(frame_ts: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": True, "speed_mps": 0.01},
                    "world": {"x": 0.0, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [make_packet(1.0), make_packet(1.7), make_packet(2.4), make_packet(3.1)]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert outputs == []

    asyncio.run(scenario())


def test_stationary_event_rejects_large_stationary_window_displacement() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.25,
                "min_valid_samples": 3,
                "max_stationary_distance_m": 0.20,
            }
        )

        def make_packet(frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": True, "speed_mps": 0.01},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [make_packet(1.0, 0.0), make_packet(1.7, 0.30), make_packet(2.4, 0.31)]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert outputs == []

    asyncio.run(scenario())


def test_stationary_event_closes_after_sustained_movement() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.0,
                "min_valid_samples": 3,
                "close_after_moving_seconds": 0.75,
            }
        )

        def make_packet(frame_ts: float, stopped: bool, speed_mps: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": stopped, "speed_mps": speed_mps},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [
            make_packet(1.0, True, 0.01, 0.0),
            make_packet(1.5, True, 0.01, 0.0),
            make_packet(2.1, True, 0.01, 0.0),
            make_packet(2.5, False, 1.0, 0.2),
            make_packet(3.3, False, 1.0, 1.0),
        ]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.CLOSE]

    asyncio.run(scenario())


def test_stationary_event_merges_short_moving_gap() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.0,
                "min_valid_samples": 3,
                "close_after_moving_seconds": 0.75,
                "merge_moving_gap_seconds": 15.0,
            }
        )

        def make_packet(frame_ts: float, stopped: bool, speed_mps: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": stopped, "speed_mps": speed_mps},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [
            make_packet(1.0, True, 0.01, 0.0),
            make_packet(1.5, True, 0.01, 0.0),
            make_packet(2.1, True, 0.01, 0.0),
            make_packet(2.5, False, 1.0, 0.2),
            make_packet(3.3, False, 1.0, 1.0),
            make_packet(11.0, True, 0.01, 1.0),
        ]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.UPDATE]
        stationary = outputs[-1].payload.get("stationary_event")
        assert isinstance(stationary, dict)
        assert stationary.get("reason") == "merged_moving_gap"

    asyncio.run(scenario())


def test_stationary_event_closes_after_merged_gap_expires() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.0,
                "min_valid_samples": 3,
                "close_after_moving_seconds": 0.75,
                "merge_moving_gap_seconds": 15.0,
            }
        )

        def make_packet(frame_ts: float, stopped: bool, speed_mps: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": stopped, "speed_mps": speed_mps},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [
            make_packet(1.0, True, 0.01, 0.0),
            make_packet(1.5, True, 0.01, 0.0),
            make_packet(2.1, True, 0.01, 0.0),
            make_packet(2.5, False, 1.0, 0.2),
            make_packet(18.4, False, 1.0, 1.0),
        ]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.CLOSE]

    asyncio.run(scenario())


def test_stationary_event_source_close_ignores_merge_gap() -> None:
    async def scenario() -> None:
        runtime = StationaryEventRuntime(
            {
                "require_arrival": False,
                "min_stationary_seconds": 1.0,
                "min_valid_samples": 3,
                "close_after_moving_seconds": 0.75,
                "merge_moving_gap_seconds": 15.0,
            }
        )

        def make_packet(
            frame_ts: float,
            stopped: bool,
            speed_mps: float,
            world_x: float,
            lifecycle: Lifecycle = Lifecycle.UPDATE,
        ) -> Packet:
            return Packet.create(
                stream_id="camera",
                lifecycle=lifecycle,
                payload={
                    "frame_ts": frame_ts,
                    "subject": {"id": "subject-1"},
                    "velocity": {"valid": True, "stopped": stopped, "speed_mps": speed_mps},
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [
            make_packet(1.0, True, 0.01, 0.0),
            make_packet(1.5, True, 0.01, 0.0),
            make_packet(2.1, True, 0.01, 0.0),
            make_packet(2.5, False, 1.0, 0.2),
            make_packet(3.3, False, 1.0, 1.0),
            make_packet(3.4, False, 1.0, 1.0, Lifecycle.CLOSE),
        ]:
            outputs.extend(await runtime.process_packet(packet, context=None))

        assert [packet.lifecycle for packet in outputs] == [Lifecycle.OPEN, Lifecycle.CLOSE]
        stationary = outputs[-1].payload.get("stationary_event")
        assert isinstance(stationary, dict)
        assert stationary.get("reason") == "source_closed"

    asyncio.run(scenario())


def test_runtime_snapshot_includes_last_node_error() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        registry.register_operator(
            operator_id="test.source",
            outputs=[{"name": "out"}],
            capabilities=["source"],
            runtime_factory=lambda _config, _deps: _OnePacketSourceRuntime(),
        )
        registry.register_operator(
            operator_id="test.fail",
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            runtime_factory=lambda _config, _deps: _FailingTransformRuntime(),
        )
        pipeline = Pipeline(
            name="runtime_error_probe",
            graph={
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "test.source", "config": {}},
                    {"id": "fail", "operator": "test.fail", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "fail", "port": "in"},
                    }
                ],
            },
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.start()
        await asyncio.sleep(0.05)
        snapshot = runtime.snapshot()
        await runtime.stop()

        node = snapshot["nodes"]["fail"]
        assert node["error_count"] == 1
        assert node["last_error"] == "ValueError: diagnostic failure"
        assert node["last_error_at"] is not None

    asyncio.run(scenario())


def test_channel_drop_oldest_keeps_recent_items() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[int](name="oldest", maxsize=2, drop_policy=DropPolicy.DROP_OLDEST)
        assert (await channel.put(1)).status == QueueOperationStatus.ACCEPTED
        assert (await channel.put(2)).status == QueueOperationStatus.ACCEPTED
        result = await channel.put(3)
        assert result.status == QueueOperationStatus.ACCEPTED
        first = await channel.get()
        second = await channel.get()
        assert first.item == 2
        assert second.item == 3
        metrics = channel.metrics_snapshot()
        assert metrics.dropped_oldest == 1
        assert metrics.max_depth_seen <= metrics.maxsize

    asyncio.run(scenario())


def test_channel_drop_newest_preserves_buffer() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[int](name="newest", maxsize=2, drop_policy=DropPolicy.DROP_NEWEST)
        assert (await channel.put(10)).status == QueueOperationStatus.ACCEPTED
        assert (await channel.put(20)).status == QueueOperationStatus.ACCEPTED
        dropped = await channel.put(30)
        assert dropped.status == QueueOperationStatus.DROPPED
        first = await channel.get()
        second = await channel.get()
        assert first.item == 10
        assert second.item == 20
        metrics = channel.metrics_snapshot()
        assert metrics.dropped_newest == 1
        assert metrics.max_depth_seen <= metrics.maxsize

    asyncio.run(scenario())


def test_channel_latest_only_keeps_last_value() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[int](name="latest", maxsize=3, drop_policy=DropPolicy.LATEST_ONLY)
        await channel.put(1)
        await channel.put(2)
        await channel.put(3)
        await channel.put(4)
        assert channel.depth == 1
        result = await channel.get()
        assert result.item == 4
        metrics = channel.metrics_snapshot()
        assert metrics.dropped_oldest >= 3
        assert metrics.max_depth_seen <= metrics.maxsize

    asyncio.run(scenario())


def test_channel_block_timeout_and_cancel() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[int](name="blocking", maxsize=1, drop_policy=DropPolicy.BLOCK)
        await channel.put(1)
        timeout_result = await channel.put(2, timeout_s=0.02)
        assert timeout_result.status == QueueOperationStatus.TIMEOUT

        cancel_event = asyncio.Event()
        put_task = asyncio.create_task(channel.put(3, timeout_s=0.5, cancel_event=cancel_event))
        await asyncio.sleep(0.02)
        cancel_event.set()
        canceled_result = await put_task
        assert canceled_result.status == QueueOperationStatus.CANCELED

        first = await channel.get(timeout_s=0.02)
        assert first.item == 1
        metrics = channel.metrics_snapshot()
        assert metrics.timed_out >= 1
        assert metrics.canceled >= 1
        assert metrics.max_depth_seen <= metrics.maxsize

    asyncio.run(scenario())


def test_channel_never_drops_open_close_packets() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="lifecycle", maxsize=1, drop_policy=DropPolicy.LATEST_ONLY)

        open_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.OPEN, payload={"kind": "event"})
        assert (await channel.put(open_packet)).status == QueueOperationStatus.ACCEPTED

        update_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.UPDATE, payload={"seq": 1})
        dropped = await channel.put(update_packet)
        assert dropped.status == QueueOperationStatus.DROPPED

        first = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert first.item.lifecycle == Lifecycle.OPEN

        close_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.CLOSE, payload={"seq": 2})
        assert (await channel.put(close_packet)).status == QueueOperationStatus.ACCEPTED
        second = await channel.get(timeout_s=0.05)
        assert second.item is not None
        assert second.item.lifecycle == Lifecycle.CLOSE

        metrics = channel.metrics_snapshot()
        assert metrics.dropped_newest >= 1

    asyncio.run(scenario())


def test_channel_latest_only_preserves_open_and_latest_update() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="latest_lifecycle", maxsize=2, drop_policy=DropPolicy.LATEST_ONLY)

        open_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.OPEN)
        update_1 = Packet.create(stream_id="stream", lifecycle=Lifecycle.UPDATE, payload={"seq": 1})
        update_2 = Packet.create(stream_id="stream", lifecycle=Lifecycle.UPDATE, payload={"seq": 2})

        await channel.put(open_packet)
        await channel.put(update_1)
        await channel.put(update_2)

        first = await channel.get(timeout_s=0.05)
        second = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert second.item is not None
        assert first.item.lifecycle == Lifecycle.OPEN
        assert second.item.payload.get("seq") == 2

    asyncio.run(scenario())


def test_channel_blocks_close_until_open_is_consumed() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="close_block", maxsize=1, drop_policy=DropPolicy.DROP_OLDEST)

        open_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.OPEN)
        close_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.CLOSE)
        await channel.put(open_packet)

        put_close = asyncio.create_task(channel.put(close_packet, timeout_s=0.01))
        await asyncio.sleep(0.02)
        assert not put_close.done()

        first = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert first.item.lifecycle == Lifecycle.OPEN

        result = await asyncio.wait_for(put_close, timeout=0.2)
        assert result.status == QueueOperationStatus.ACCEPTED

        second = await channel.get(timeout_s=0.05)
        assert second.item is not None
        assert second.item.lifecycle == Lifecycle.CLOSE

    asyncio.run(scenario())


def test_keyed_channel_drop_oldest_is_per_stream() -> None:
    async def scenario() -> None:
        channel = KeyedBoundedChannel[Packet](
            name="keyed_drop_oldest",
            maxsize=3,
            drop_policy=DropPolicy.DROP_OLDEST,
            key_fn=lambda packet: packet.stream_id,
        )

        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        await channel.put(Packet.create(stream_id="obj:B", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 2}))

        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 3}))

        packets: list[Packet] = []
        while channel.depth:
            item = await channel.get(timeout_s=0.05)
            assert item.item is not None
            packets.append(item.item)

        seen = {(p.stream_id, p.payload.get("seq")) for p in packets}
        assert ("obj:B", 1) in seen
        assert ("obj:A", 1) not in seen
        assert ("obj:A", 2) in seen
        assert ("obj:A", 3) in seen

    asyncio.run(scenario())


def test_keyed_channel_round_robin_fairness() -> None:
    async def scenario() -> None:
        channel = KeyedBoundedChannel[Packet](
            name="keyed_fair",
            maxsize=10,
            drop_policy=DropPolicy.DROP_OLDEST,
            key_fn=lambda packet: packet.stream_id,
        )

        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 2}))
        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 3}))
        await channel.put(Packet.create(stream_id="obj:B", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        await channel.put(Packet.create(stream_id="obj:B", lifecycle=Lifecycle.UPDATE, payload={"seq": 2}))

        first = await channel.get(timeout_s=0.05)
        second = await channel.get(timeout_s=0.05)
        third = await channel.get(timeout_s=0.05)
        fourth = await channel.get(timeout_s=0.05)
        fifth = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert second.item is not None
        assert third.item is not None
        assert fourth.item is not None
        assert fifth.item is not None

        assert (first.item.stream_id, first.item.payload.get("seq")) == ("obj:A", 1)
        assert (second.item.stream_id, second.item.payload.get("seq")) == ("obj:B", 1)
        assert (third.item.stream_id, third.item.payload.get("seq")) == ("obj:A", 2)
        assert (fourth.item.stream_id, fourth.item.payload.get("seq")) == ("obj:B", 2)
        assert (fifth.item.stream_id, fifth.item.payload.get("seq")) == ("obj:A", 3)

    asyncio.run(scenario())


def test_channel_drop_updates_never_drops_open_close_packets() -> None:
    async def scenario() -> None:
        channel = BoundedChannel[Packet](name="drop_updates", maxsize=1, drop_policy=DropPolicy.DROP_UPDATES)

        open_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.OPEN, payload={"kind": "event"})
        assert (await channel.put(open_packet)).status == QueueOperationStatus.ACCEPTED

        update_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.UPDATE, payload={"seq": 1})
        dropped = await channel.put(update_packet, timeout_s=0.01)
        assert dropped.status == QueueOperationStatus.DROPPED

        first = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert first.item.lifecycle == Lifecycle.OPEN

        close_packet = Packet.create(stream_id="stream", lifecycle=Lifecycle.CLOSE, payload={"seq": 2})
        assert (await channel.put(close_packet)).status == QueueOperationStatus.ACCEPTED
        second = await channel.get(timeout_s=0.05)
        assert second.item is not None
        assert second.item.lifecycle == Lifecycle.CLOSE

    asyncio.run(scenario())


def test_keyed_channel_keyed_latest_only_keeps_latest_update_per_stream() -> None:
    async def scenario() -> None:
        channel = KeyedBoundedChannel[Packet](
            name="keyed_latest_only",
            maxsize=10,
            drop_policy=DropPolicy.KEYED_LATEST_ONLY,
            key_fn=lambda packet: packet.stream_id,
        )

        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.OPEN))
        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        await channel.put(Packet.create(stream_id="obj:A", lifecycle=Lifecycle.UPDATE, payload={"seq": 2}))
        assert channel.depth == 2

        await channel.put(Packet.create(stream_id="obj:B", lifecycle=Lifecycle.UPDATE, payload={"seq": 1}))
        assert channel.depth == 3

        first = await channel.get(timeout_s=0.05)
        second = await channel.get(timeout_s=0.05)
        third = await channel.get(timeout_s=0.05)
        assert first.item is not None
        assert second.item is not None
        assert third.item is not None

        seen = {(p.stream_id, p.lifecycle.value, p.payload.get("seq")) for p in [first.item, second.item, third.item]}
        assert ("obj:A", "open", None) in seen
        assert ("obj:A", "update", 2) in seen
        assert ("obj:B", "update", 1) in seen

    asyncio.run(scenario())
