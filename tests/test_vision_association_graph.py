import asyncio
import json

from pydantic import BaseModel

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    SinkRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.pipelines.inspection import PipelineInspectionStore
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync.runtime.pipelines.demands import ResourceDemandRegistry, DemandRequest
from toposync.runtime.pipelines.result_references import ResultContract
from toposync_ext_vision.association_api import AssociationRepository
from toposync_ext_vision.association_runtime import register_association_operator
from test_vision_association import config
from test_vision_association_runtime import packet


def fixture_graph(registry, received, inputs=None, cfg=None):
    cfg = cfg or config()
    if inputs is None:
        inputs = {
            camera: [
                packet(camera, stamp, track, (index) * 0.7)
                for stamp in stamps
                for index, track in enumerate(tracks)
            ]
            for camera, stamps, tracks in [
                ("a", [0, 1], [1, 2, 3]),
                ("b", [2, 3], [3, 1, 2]),
                ("c", [4, 5], [2, 3, 1]),
            ]
        }

    class SourceConfig(BaseModel):
        camera: str = ""

    class Source(SourceOperatorRuntime):
        def __init__(self, camera):
            self.camera = camera

        async def run(self, context):
            for value in inputs[self.camera]:
                await context.emit(value)
                await context.sleep(0.001)
            await context.cancel_event.wait()

    class Sink(SinkRuntime):
        def __init__(self, name):
            self.name = name

        async def process_packet(self, value, context):
            received.setdefault(self.name, []).append(value)
            return []

    registry.register_operator(
        operator_id="test.m3_post_tracker",
        description="Synthetic post-tracker fixture, no detector or camera",
        config_model=SourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        produces_payload_keys=["subject", "source", "capture_evidence", "mapping"],
        runtime_factory=lambda c, d: Source(c["camera"]),
    )
    registry.register_operator(
        operator_id="test.m3_receipt",
        config_model=SourceConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        share_strategy="never",
        runtime_factory=lambda c, d: Sink(c["camera"]),
    )
    nodes = [
        {"id": camera, "operator": "test.m3_post_tracker", "config": {"camera": camera}}
        for camera in inputs
    ]
    cameras = list(inputs)
    nodes += [
        {"id": "merge_ab", "operator": "core.merge_streams"},
        {"id": "merge_all", "operator": "core.merge_streams"},
        {"id": "association", "operator": "vision.associate_entities", "config": cfg.model_dump()},
    ]
    edges = [
        {
            "from": {"node": source, "port": "out"},
            "to": {"node": target, "port": port},
            "maxsize": 128,
        }
        for source, target, port in [
            (cameras[0], "merge_ab", "left"),
            (cameras[1], "merge_ab", "right"),
            ("merge_ab", "merge_all", "left"),
            (cameras[2], "merge_all", "right"),
            ("merge_all", "association", "in"),
        ]
    ]
    graph = build_pipeline_graph_v2(graph_uid="m3_fixture", nodes=nodes, edges=edges)
    contract = {"type": "packet", "version": 1, "payload_keys": ["association"], "artifacts": []}
    graph.setdefault("meta", {})["outputs"] = [
        {
            "key": "m3.entities",
            "node_uid": next(n["uid"] for n in graph["nodes"] if n["id"] == "association"),
            "port": "out",
            "contract": contract,
            "production_scope": "test",
            "state_owner": "association:test",
            "state_partition": "scope",
        }
    ]
    producer = Pipeline(
        name="m3_fixture",
        display_name="M3 · Synthetic post-tracker replay",
        enabled=False,
        graph=graph,
    )
    consumers = []
    for name in ["manual_one", "manual_two"]:
        consumers.append(
            Pipeline(
                name=name,
                graph=build_pipeline_graph_v2(
                    graph_uid=name,
                    nodes=[
                        {
                            "id": "result",
                            "operator": "core.result_input",
                            "config": {"key": "m3.entities", "contract": contract},
                        },
                        {"id": "sink", "operator": "test.m3_receipt", "config": {"camera": name}},
                    ],
                    edges=[
                        {
                            "from": {"node": "result", "port": "out"},
                            "to": {"node": "sink", "port": "in"},
                        }
                    ],
                ),
            )
        )
    return producer, consumers


def test_named_output_two_manual_consumers_share_one_associator_and_inspection(tmp_path):
    async def scenario():
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_association_operator(registry)
        received = {}
        producer, consumers = fixture_graph(registry, received)
        compiler = PipelineGraphCompiler(registry)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind([producer])
        leases = [
            demands.open(
                "synthetic-owner",
                DemandRequest(
                    consumer_id=name,
                    purpose="manual",
                    result_key="m3.entities",
                    contract=ResultContract(
                        type="packet", version=1, payload_keys=["association"], artifacts=[]
                    ),
                    production_revision=demands.outputs["m3.entities"][2],
                    payload_keys=["association"],
                ),
            )
            for name in ("manual_one", "manual_two")
        ]
        consumers = demands.pipelines()
        report = compiler.compile_many(consumers, catalog=[producer, *consumers])
        services = ServiceRegistry()
        repository = AssociationRepository(tmp_path)
        services.register("vision.association.store", repository.get)
        inspection = PipelineInspectionStore()
        tokens = [
            inspection.open(p.name, "test", {n.node_id: n.operator_id for n in p.nodes})
            for p in report.pipelines
        ]
        runtime = PipelineBundleRuntime(
            report,
            registry,
            PipelineRuntimeDependencies(
                services=services,
                demand_registry=demands,
                inspection_store=inspection,
                inspection_revisions={p.name: "test" for p in report.pipelines},
            ),
        )
        assert (
            len(
                [
                    n
                    for n in runtime.plan.merged_pipeline.nodes
                    if n.operator_id == "vision.associate_entities"
                ]
            )
            == 1
        )
        await runtime.start()
        try:
            for _ in range(200):
                if all(lease.delivered == 18 and lease.latest is not None for lease in leases):
                    break
                await asyncio.sleep(0.005)
            # Demand channels project the latest value; coalescing must not lose producer history.
            assert [lease.delivered for lease in leases] == [18, 18]
            assert all(lease.received >= 1 for lease in leases)
            assert leases[0].latest.payload == leases[1].latest.payload
            store = repository.get(config=config())
            assert len(store.observations) == 18 and store.revision == 18
            assert len({r["entity"] for r in store.observations.values()}) == 3
            for token in tokens:
                result = inspection.snapshot(token).model_dump(mode="json")
                assert "association_" in json.dumps(result)
                assert "identity_id" not in json.dumps(result)
        finally:
            for lease in leases:
                demands.release(lease.id)
            await runtime.stop()
            await runtime.dependencies.execution_scheduler.shutdown()
        assert repository.get(config=config()).owner is None

    asyncio.run(asyncio.wait_for(scenario(), 3))
