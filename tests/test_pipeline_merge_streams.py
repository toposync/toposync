import asyncio
from dataclasses import replace

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    GraphCompileError,
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


def test_binary_merge_keeps_silent_and_closed_sources_independent_and_scopes_local_ids():
    async def scenario():
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        queues = {name: asyncio.Queue() for name in ["a", "b", "silent"]}
        received = []

        class Source(SourceOperatorRuntime):
            def __init__(self, name):
                self.name = name

            async def run(self, context):
                while not context.cancel_event.is_set():
                    try:
                        value = await asyncio.wait_for(queues[self.name].get(), 0.02)
                    except TimeoutError:
                        continue
                    await context.emit(value)

        class Sink(SinkRuntime):
            async def process_packet(self, packet, context):
                received.append(packet)
                return []

        for name in queues:
            registry.register_operator(
                operator_id=f"test.source_{name}",
                inputs=[],
                outputs=[{"name": "out"}],
                runtime_factory=lambda _config, _deps, name=name: Source(name),
            )
        registry.register_operator(
            operator_id="test.collect_merge",
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda _config, _deps: Sink(),
        )
        nodes = [{"id": name, "operator": f"test.source_{name}"} for name in queues]
        nodes += [
            {"id": name, "operator": "core.merge_streams"} for name in ["merge_ab", "merge_all"]
        ]
        nodes += [{"id": "sink", "operator": "test.collect_merge"}]
        pairs = [
            ("a", "merge_ab", "left"),
            ("b", "merge_ab", "right"),
            ("merge_ab", "merge_all", "left"),
            ("silent", "merge_all", "right"),
            ("merge_all", "sink", "in"),
        ]
        graph = build_pipeline_graph_v2(
            graph_uid="merge",
            nodes=nodes,
            edges=[
                {
                    "from": {"node": a, "port": "out"},
                    "to": {"node": b, "port": port},
                    "maxsize": 32,
                    "drop_policy": "block",
                }
                for a, b, port in pairs
            ],
        )
        runtime = PipelineRuntime(
            PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="merge", graph=graph)),
            registry,
            PipelineRuntimeDependencies(),
        )

        def packet(source, sequence, lifecycle=Lifecycle.UPDATE):
            return Packet.create(
                stream_id="track:1",
                packet_id=str(sequence),
                lifecycle=lifecycle,
                payload={
                    "source": {"device_id": "test-host", "source_id": source},
                    "subject": {"id": "1"},
                    "capture_evidence": {
                        "capture_instance": source,
                        "generation": 3,
                        "sequence": sequence,
                    },
                },
            )

        await runtime.start()
        try:
            await queues["a"].put(packet("a", 1, Lifecycle.OPEN))
            await queues["b"].put(packet("b", 1, Lifecycle.OPEN))
            await queues["a"].put(packet("a", 2, Lifecycle.CLOSE))
            await queues["b"].put(packet("b", 2))
            for _ in range(100):
                if len(received) == 4:
                    break
                await asyncio.sleep(0.005)
            assert len(received) == 4, "silent input and CLOSE must not block another source"
            by_source = {
                source: [p for p in received if p.payload["source"]["source_id"] == source]
                for source in ["a", "b"]
            }
            assert by_source["a"][0].stream_id != by_source["b"][0].stream_id
            assert by_source["a"][0].packet_id != by_source["b"][0].packet_id
            assert [p.lifecycle for p in by_source["a"]] == [Lifecycle.OPEN, Lifecycle.CLOSE]
            assert [p.lifecycle for p in by_source["b"]] == [Lifecycle.OPEN, Lifecycle.UPDATE]
            assert all(
                p.payload["subject"]["id"] == "1"
                and p.payload["capture_evidence"]["generation"] == 3
                for p in received
            )
            assert all(m.error_count == 0 for m in runtime.node_metrics.values())
        finally:
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
        assert not runtime._task_by_node

    asyncio.run(scenario())


def test_binary_merge_does_not_allow_multiple_edges_on_one_port():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    graph = build_pipeline_graph_v2(
        graph_uid="bad",
        nodes=[{"id": x, "operator": "core.synthetic_source"} for x in ["a", "b"]]
        + [{"id": "merge", "operator": "core.merge_streams"}],
        edges=[
            {"from": {"node": x, "port": "out"}, "to": {"node": "merge", "port": "left"}}
            for x in ["a", "b"]
        ],
    )
    with pytest.raises(GraphCompileError, match="multiple incoming edges"):
        PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="bad", graph=graph))


def test_merge_does_not_trust_a_malformed_provenance_marker():
    from toposync.runtime.pipelines.operators_merge import _qualified_packet

    packet = Packet.create(stream_id="local", metadata={"merge_origin": {"invalid": object()}})
    first = _qualified_packet(packet, "left")
    second = _qualified_packet(replace(packet, packet_id="other"), "right")
    assert first.stream_id != second.stream_id
    assert _qualified_packet(first, "next") is first
