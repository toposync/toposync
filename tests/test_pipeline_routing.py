from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.operators_routing import PriorityByScheduleRuntime


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, packets: list[Packet]) -> None:
        self._packets = list(packets)
        self._index = 0

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._index >= len(self._packets):
            await context.sleep(0.01)
            return None
        packet = self._packets[self._index]
        self._index += 1
        return packet


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, collector: list[Packet]) -> None:
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.append(packet)
        return []


def _register_sequence_source(registry: OperatorRegistry, packets: list[Packet]) -> None:
    registry.register_operator(
        operator_id="test.sequence_source",
        config_model=_EmptyConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda _config, _deps: _SequenceSourceRuntime(packets),
    )


def _register_collect_sink(
    registry: OperatorRegistry,
    *,
    operator_id: str,
    collector: list[Packet],
) -> None:
    registry.register_operator(
        operator_id=operator_id,
        config_model=_EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda _config, _deps: _CollectSinkRuntime(collector),
    )


def _subject_packet(stream_id: str, lifecycle: Lifecycle, category: str = "") -> Packet:
    payload: dict[str, Any] = {}
    if category:
        payload["subject"] = {"category": category}
    return Packet.create(stream_id=stream_id, lifecycle=lifecycle, payload=payload)


def test_route_by_category_uses_static_match_other_ports_and_preserves_close() -> None:
    async def scenario() -> tuple[list[Packet], list[Packet], list[Packet]]:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        packets = [
            _subject_packet("event:person", Lifecycle.OPEN, "person"),
            _subject_packet("event:person", Lifecycle.UPDATE),
            _subject_packet("event:person", Lifecycle.CLOSE),
            _subject_packet("event:vehicle", Lifecycle.OPEN, "car"),
            _subject_packet("event:vehicle", Lifecycle.UPDATE),
            _subject_packet("event:vehicle", Lifecycle.CLOSE),
            _subject_packet("event:unknown", Lifecycle.OPEN, "dog"),
            _subject_packet("event:unknown", Lifecycle.CLOSE),
        ]
        person_packets: list[Packet] = []
        vehicle_packets: list[Packet] = []
        other_packets: list[Packet] = []
        _register_sequence_source(registry, packets)
        _register_collect_sink(registry, operator_id="test.person_sink", collector=person_packets)
        _register_collect_sink(registry, operator_id="test.vehicle_sink", collector=vehicle_packets)
        _register_collect_sink(registry, operator_id="test.other_sink", collector=other_packets)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {}},
                {
                    "id": "people",
                    "operator": "core.route_by_category",
                    "config": {"categories": ["person"]},
                },
                {
                    "id": "vehicles",
                    "operator": "core.route_by_category",
                    "config": {"categories": ["car", "truck", "motorcycle", "bicycle"]},
                },
                {"id": "person_sink", "operator": "test.person_sink", "config": {}},
                {"id": "vehicle_sink", "operator": "test.vehicle_sink", "config": {}},
                {"id": "other_sink", "operator": "test.other_sink", "config": {}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "people", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "people", "port": "match"},
                    "to": {"node": "person_sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "people", "port": "other"},
                    "to": {"node": "vehicles", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "vehicles", "port": "match"},
                    "to": {"node": "vehicle_sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "vehicles", "port": "other"},
                    "to": {"node": "other_sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="route_by_category_test", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.25)
        return person_packets, vehicle_packets, other_packets

    person_packets, vehicle_packets, other_packets = asyncio.run(scenario())

    assert [(packet.stream_id, packet.lifecycle) for packet in person_packets] == [
        ("event:person", Lifecycle.OPEN),
        ("event:person", Lifecycle.UPDATE),
        ("event:person", Lifecycle.CLOSE),
    ]
    assert [(packet.stream_id, packet.lifecycle) for packet in vehicle_packets] == [
        ("event:vehicle", Lifecycle.OPEN),
        ("event:vehicle", Lifecycle.UPDATE),
        ("event:vehicle", Lifecycle.CLOSE),
    ]
    assert [(packet.stream_id, packet.lifecycle) for packet in other_packets] == [
        ("event:unknown", Lifecycle.OPEN),
        ("event:unknown", Lifecycle.CLOSE),
    ]


def test_priority_by_schedule_annotates_payload_and_metadata() -> None:
    async def scenario(config: dict[str, Any]) -> Packet:
        runtime = PriorityByScheduleRuntime(config)
        packet = Packet.create(stream_id="event:1", lifecycle=Lifecycle.UPDATE)
        out_packets = await runtime.process_packet(packet, object())
        return out_packets[0]

    inside = asyncio.run(
        scenario({"enabled": False, "inside_priority": "high", "outside_priority": "low"})
    )
    assert inside.payload["priority"] == "high"
    assert inside.metadata["priority"] == "high"

    outside = asyncio.run(
        scenario({"enabled": True, "weekdays": [], "inside_priority": "high", "outside_priority": "medium"})
    )
    assert outside.payload["priority"] == "medium"
    assert outside.metadata["priority"] == "medium"


def test_boolean_gates_filter_lifecycle_streams_and_report_progress() -> None:
    async def scenario(operator_id: str, config: dict[str, Any]) -> tuple[list[Packet], dict[str, Any]]:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        packets = [
            Packet.create(
                stream_id="event:1",
                lifecycle=Lifecycle.OPEN,
                payload={"flags": {"a": True, "b": False}},
            ),
            Packet.create(
                stream_id="event:1",
                lifecycle=Lifecycle.UPDATE,
                payload={"flags": {"a": True, "b": False}},
            ),
            Packet.create(stream_id="event:1", lifecycle=Lifecycle.CLOSE),
        ]
        collected: list[Packet] = []
        _register_sequence_source(registry, packets)
        _register_collect_sink(registry, operator_id="test.collect_sink", collector=collected)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {}},
                {"id": "gate", "operator": operator_id, "config": config},
                {"id": "sink", "operator": "test.collect_sink", "config": {}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "gate", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "gate", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name=f"{operator_id.replace('.', '_')}_test", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        snapshot = await runtime.run_for(0.25)
        return collected, snapshot

    any_packets, any_snapshot = asyncio.run(
        scenario("core.any_gate", {"fields": ["payload.flags.a", "payload.flags.b"]})
    )
    assert [packet.lifecycle for packet in any_packets] == [
        Lifecycle.OPEN,
        Lifecycle.UPDATE,
        Lifecycle.CLOSE,
    ]
    assert any_snapshot["nodes"]["gate"]["dropped_packets"] == 0

    all_packets, all_snapshot = asyncio.run(
        scenario("core.all_gate", {"fields": ["payload.flags.a", "payload.flags.b"]})
    )
    assert all_packets == []
    assert all_snapshot["nodes"]["gate"]["dropped_packets"] >= 1

    not_packets, not_snapshot = asyncio.run(
        scenario("core.not_gate", {"field": "payload.flags.b"})
    )
    assert [packet.lifecycle for packet in not_packets] == [
        Lifecycle.OPEN,
        Lifecycle.UPDATE,
        Lifecycle.CLOSE,
    ]
    assert not_snapshot["nodes"]["gate"]["dropped_packets"] == 0


def test_core_filter_reports_filtered_progress() -> None:
    async def scenario() -> dict[str, Any]:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        packets = [
            Packet.create(stream_id="event:1", lifecycle=Lifecycle.OPEN),
            Packet.create(stream_id="event:1", lifecycle=Lifecycle.UPDATE),
            Packet.create(stream_id="event:1", lifecycle=Lifecycle.CLOSE),
        ]
        collected: list[Packet] = []
        _register_sequence_source(registry, packets)
        _register_collect_sink(registry, operator_id="test.collect_sink", collector=collected)

        graph = {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.sequence_source", "config": {}},
                {"id": "filter", "operator": "core.filter", "config": {"expression": "False"}},
                {"id": "sink", "operator": "test.collect_sink", "config": {}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "filter", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "filter", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 32,
                    "drop_policy": "drop_oldest",
                },
            ],
        }
        pipeline = Pipeline(name="core_filter_progress", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        return await runtime.run_for(0.25)

    snapshot = asyncio.run(scenario())
    assert snapshot["nodes"]["filter"]["dropped_packets"] >= 1
