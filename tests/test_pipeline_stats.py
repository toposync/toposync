from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.stats import PipelineStatsStore


def test_pipeline_stats_store_rolls_over_window() -> None:
    store = PipelineStatsStore(window_seconds=60, bucket_seconds=10)

    now = 1_000.0
    store.increment_node_output("p", "node_a", now_s=now, value=2)
    store.increment_node_output("p", "node_b", now_s=now, value=3)
    snap = store.snapshot("p", now_s=now)
    assert snap["node_outputs"]["node_a"] == 2
    assert snap["node_outputs"]["node_b"] == 3

    later = now + 75.0
    snap2 = store.snapshot("p", now_s=later)
    assert snap2["node_outputs"]["node_a"] == 0
    assert snap2["node_outputs"]["node_b"] == 0


class _FiniteSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "stream:test"
    frames: int = Field(default=10, ge=1, le=500)


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected: int = Field(default=10, ge=1, le=500)


class _FiniteSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = _FiniteSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._frames = int(parsed.frames)
        self._index = 0

    async def produce(self, _context) -> Packet | None:  # noqa: ANN001
        if self._index >= self._frames:
            return None
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"seq": self._index},
            metadata={"source": "test.finite_source"},
        )
        self._index += 1
        return packet


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], done_event: asyncio.Event) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._expected = int(parsed.expected)
        self._seen = 0
        self._done = done_event

    async def process_packet(self, _packet: Packet, _context) -> list[Packet]:  # noqa: ANN001
        self._seen += 1
        if self._seen >= self._expected:
            self._done.set()
        return []


def test_pipeline_runtime_records_step_output_counts() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        done_event = asyncio.Event()
        registry.register_operator(
            operator_id="test.finite_source",
            config_model=_FiniteSourceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            defaults=_FiniteSourceConfig().model_dump(),
            share_strategy="never",
            runtime_factory=lambda config, _deps: _FiniteSourceRuntime(config),
        )
        registry.register_operator(
            operator_id="test.collect_sink",
            config_model=_CollectSinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults=_CollectSinkConfig().model_dump(),
            share_strategy="never",
            runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, done_event),
        )

        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "test.finite_source",
                    "config": {"stream_id": "stream:test", "frames": 10},
                },
                {"id": "sink", "operator": "test.collect_sink", "config": {"expected": 10}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "maxsize": 1,
                    "drop_policy": "block",
                },
            ],
        }
        pipeline = Pipeline(name="stats_runtime_pipeline", graph=graph)
        compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)

        stats_store = PipelineStatsStore(window_seconds=24 * 60 * 60, bucket_seconds=60)
        deps = PipelineRuntimeDependencies(pipeline_stats_store=stats_store)
        runtime = PipelineRuntime(compiled=compiled, registry=registry, dependencies=deps)
        await runtime.start()
        await asyncio.wait_for(done_event.wait(), timeout=1.0)
        await runtime.stop()

        snap = stats_store.snapshot("stats_runtime_pipeline")
        assert snap["node_outputs"]["source"] == 10
        assert snap["node_outputs"]["sink"] == 10

    asyncio.run(scenario())
