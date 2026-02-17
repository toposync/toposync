from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    TransformOperatorRuntime,
    register_builtin_operators,
)


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _HeavyRuntime(TransformOperatorRuntime):
    def __init__(self, counters: dict[str, Any], lock: threading.Lock) -> None:
        self._counters = counters
        self._lock = lock

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        def _sync_work() -> None:
            with self._lock:
                self._counters["active"] = int(self._counters.get("active", 0)) + 1
                active = int(self._counters["active"])
                self._counters["max_active"] = max(int(self._counters.get("max_active", 0)), active)
            time.sleep(0.03)
            with self._lock:
                self._counters["active"] = int(self._counters.get("active", 0)) - 1

        await context.run_blocking(_sync_work)
        return [packet]


def test_execution_scheduler_respects_max_concurrency_across_pipelines() -> None:
    async def scenario() -> None:
        registry = OperatorRegistry()
        register_builtin_operators(registry)

        counters: dict[str, Any] = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        registry.register_operator(
            operator_id="test.heavy",
            config_model=_EmptyConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            defaults={},
            capabilities=["heavy_compute"],
            execution_mode="thread_pool",
            max_concurrency=1,
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _HeavyRuntime(counters, lock),
        )

        def _graph(name: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "nodes": [
                    {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 120.0, "stream_id": f"synthetic:{name}"}},
                    {"id": "heavy", "operator": "test.heavy", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {"from": {"node": "source", "port": "out"}, "to": {"node": "heavy", "port": "in"}, "maxsize": 1, "drop_policy": "latest_only"},
                    {"from": {"node": "heavy", "port": "out"}, "to": {"node": "sink", "port": "in"}, "maxsize": 1, "drop_policy": "drop_oldest"},
                ],
            }

        compiler = PipelineGraphCompiler(registry)
        p1 = Pipeline(name="pipeline_one", type="final", graph=_graph("one"))
        p2 = Pipeline(name="pipeline_two", type="final", graph=_graph("two"))
        compiled1 = compiler.compile_pipeline(p1)
        compiled2 = compiler.compile_pipeline(p2)

        deps = PipelineRuntimeDependencies()
        runtime1 = PipelineRuntime(compiled=compiled1, registry=registry, dependencies=deps)
        runtime2 = PipelineRuntime(compiled=compiled2, registry=registry, dependencies=deps)

        await runtime1.start()
        await runtime2.start()
        await asyncio.sleep(0.35)
        await runtime1.stop()
        await runtime2.stop()

        assert int(counters.get("max_active", 0)) <= 1

    asyncio.run(scenario())

