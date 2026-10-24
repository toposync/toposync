from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.demands import DemandRequest, FlowResultBinding, ResourceDemandRegistry
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
import asyncio

from toposync.runtime.pipelines import Packet, PipelineRuntimeDependencies, SourceOperatorRuntime, SinkRuntime
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime


def test_saved_flow_renews_restarts_and_stops_without_automatic_or_a_browser():
    operators = OperatorRegistry()
    register_builtin_operators(operators)
    compiler = PipelineGraphCompiler(operators)
    now, permission = [0], [True]
    registry = ResourceDemandRegistry(lambda *_: permission[0], compiler=compiler, clock=lambda: now[0])
    producer = Pipeline(name="source", enabled=False, graph=build_pipeline_graph_v2(
        graph_uid="source", nodes=[{"id": "source", "operator": "core.synthetic_source"}], edges=[],
        meta={"outputs": [{"key": "test.result", "node_uid": "source", "production_scope": "test",
                           "contract": {"type": "packet", "version": 1, "payload_keys": [], "artifacts": []}}]},
    ))
    registry.bind([producer])
    _, output, revision = registry.outputs["test.result"]
    graph = build_pipeline_graph_v2(graph_uid="manual", nodes=[{
        "id": "input", "operator": "core.result_input",
        "config": {"key": output.key, "contract": output.contract.model_dump()},
    }], edges=[])
    graph.setdefault("meta", {})["result_consumer"] = FlowResultBinding(
        owner="owner", node_uid=graph["nodes"][0]["uid"], request=DemandRequest(
            consumer_id="manual", purpose="manual", result_key=output.key,
            contract=output.contract, production_revision=revision, lease_seconds=30,
        ),
    ).model_dump(mode="json")
    flow = Pipeline(name="manual", graph=graph)
    [bound] = registry.synchronize_flows([producer, flow])
    identifier = registry.flow_leases[flow.name]
    assert bound.graph["nodes"][0]["config"]["demand_id"] == identifier
    assert "demand_id" not in flow.graph["nodes"][0]["config"]
    now[0] = 20
    registry.synchronize_flows([producer, flow])
    assert registry.flow_leases[flow.name] == identifier
    assert registry.demands[identifier].expires_at == 50
    assert registry.synchronize_flows([producer, flow.model_copy(update={"enabled": False})]) == []
    assert not registry.demands
    assert registry.flow_status[flow.name]["state"] == "paused"
    registry.synchronize_flows([producer, flow])
    assert registry.flow_leases[flow.name] != identifier
    registry.synchronize_flows([producer])
    assert not registry.demands and not registry.flow_status
    # A restart reconstructs authorization from the saved request, never a saved lease ID.
    restarted = ResourceDemandRegistry(lambda *_: permission[0], compiler=compiler, clock=lambda: now[0])
    restarted.bind([producer, flow])
    assert len(restarted.synchronize_flows([producer, flow])) == 1
    producer.graph["meta"]["outputs"][0]["production_scope"] = "changed"
    restarted.bind([producer, flow])
    assert restarted.outputs[output.key][2] != revision
    assert restarted.synchronize_flows([producer, flow]) == []
    assert not restarted.demands
    producer.graph["meta"]["outputs"][0]["production_scope"] = "test"
    restarted = ResourceDemandRegistry(lambda *_: permission[0], compiler=compiler, clock=lambda: now[0])
    restarted.bind([producer, flow])
    assert len(restarted.synchronize_flows([producer, flow])) == 1
    permission[0] = False
    assert restarted.synchronize_flows([producer, flow]) == []
    assert not restarted.demands and restarted.flow_status[flow.name]["state"] == "blocked"
    events = len(restarted.events)
    for _ in range(3):
        assert restarted.synchronize_flows([producer, flow]) == []
    assert len(restarted.events) == events
    permission[0] = True
    assert len(restarted.synchronize_flows([producer, flow])) == 1
    assert restarted.flow_status[flow.name]["state"] == "waiting"


def test_saved_event_flow_preserves_burst_subjects_and_their_closures():
    async def scenario():
        operators = OperatorRegistry()
        register_builtin_operators(operators)
        received = []

        class Source(SourceOperatorRuntime):
            async def run(self, context):
                for index in range(40):
                    for lifecycle in (Lifecycle.OPEN, Lifecycle.CLOSE):
                        await context.emit(Packet.create(stream_id="fixture", lifecycle=lifecycle,
                                                        payload={"subject": {"id": str(index)}}))
                await context.cancel_event.wait()

        class Sink(SinkRuntime):
            async def process_packet(self, packet, context):
                received.append((packet.payload['subject']['id'], packet.lifecycle))
                return []

        operators.register_operator(operator_id="test.burst", inputs=[], outputs=[{"name": "out"}],
                                    produces_payload_keys=["subject"], runtime_factory=lambda *_: Source())
        operators.register_operator(operator_id="test.receipt", inputs=[{"name": "in", "required": True}],
                                    outputs=[], runtime_factory=lambda *_: Sink())
        compiler = PipelineGraphCompiler(operators)
        registry = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        producer = Pipeline(name="source", enabled=False, graph=build_pipeline_graph_v2(
            graph_uid="source", nodes=[{"id": "source", "operator": "test.burst"}], edges=[],
            meta={"outputs": [{"key": "test.events", "node_uid": "source", "production_scope": "test",
                               "contract": {"type": "packet", "version": 1, "payload_keys": ["subject"], "artifacts": []}}]},
        ))
        registry.bind([producer])
        _, output, revision = registry.outputs["test.events"]
        graph = build_pipeline_graph_v2(graph_uid="manual", nodes=[
            {"id": "input", "operator": "core.result_input", "config": {"key": output.key, "contract": output.contract.model_dump()}},
            {"id": "sink", "operator": "test.receipt"},
        ], edges=[{"from": {"node": "input", "port": "out"}, "to": {"node": "sink", "port": "in"},
                   "queue": {"max_items": 64, "drop_policy": "block"},
                   "backpressure": {"mode": "block"}, "lifecycle": {"compact_updates": False}}])
        graph.setdefault("meta", {})["result_consumer"] = FlowResultBinding(
            owner="owner", node_uid=graph["nodes"][0]["uid"], request=DemandRequest(
                consumer_id="manual", purpose="manual", result_key=output.key, contract=output.contract,
                production_revision=revision, payload_keys=["subject"], lease_seconds=30,
            ),
        ).model_dump(mode="json")
        flow = Pipeline(name="manual", graph=graph)
        bound = registry.synchronize_flows([producer, flow])
        runtime = PipelineBundleRuntime(compiler.compile_many(bound, catalog=[producer, *bound]), operators,
                                        PipelineRuntimeDependencies(demand_registry=registry))
        try:
            await runtime.start()
            for _ in range(200):
                if len(received) == 80:
                    break
                await asyncio.sleep(.005)
            assert received == [(str(i), phase) for i in range(40) for phase in (Lifecycle.OPEN, Lifecycle.CLOSE)]
        finally:
            registry.synchronize_flows([])
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 3))
