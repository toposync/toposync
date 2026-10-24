from copy import deepcopy

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    GraphCompileError,
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.shared_runtime import build_merged_pipeline_plan
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2


CONTRACT = {"type": "packet", "version": 1, "payload_keys": ["subject"], "artifacts": []}


def test_optional_media_requires_declared_negotiation_and_does_not_weaken_required_media():
    from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
    from toposync.runtime.pipelines.result_references import ResultContract
    from toposync.runtime.pipelines.runtime import Artifact, Packet

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    registry.register_operator(operator_id="test.optional", inputs=[], outputs=[{"name": "out"}],
        produces_payload_keys=["subject"], produces_artifacts=["main"])
    for required in (False, True):
        contract = ResultContract(version=1, payload_keys=["subject"],
            artifacts=["main"] if required else [], optional_artifacts=[] if required else ["main"])
        producer = Pipeline(name="optional", graph=build_pipeline_graph_v2(graph_uid="optional",
            nodes=[{"id": "source", "operator": "test.optional"}], edges=[],
            meta={"outputs": [{"key": "optional.frames", "node_uid": "source", "contract": contract.model_dump(),
                              "production_scope": "fixture"}]}))
        demands = ResourceDemandRegistry(lambda *_: True, compiler=PipelineGraphCompiler(registry))
        demands.bind([producer])
        request = DemandRequest(consumer_id="manual", purpose="manual", result_key="optional.frames", contract=contract,
            production_revision=demands.outputs["optional.frames"][2], payload_keys=["subject"], artifacts=["main"])
        demand = demands.open("owner", request)
        missing = Packet.create(stream_id="fixture", payload={"subject": {"id": "one"}})
        assert (demands.project(demand.id, missing) is None) is required
        with_image = missing.with_artifact(Artifact("main", b"synthetic"))
        assert set(demands.project(demand.id, with_image).artifacts) == {"main"}
        metadata = demands.open("owner", request.model_copy(update={"consumer_id": "metadata", "artifacts": []}))
        assert demands.project(metadata.id, with_image).artifacts == {}
        with pytest.raises(ValueError, match="exceeds"):
            demands.open("owner", request.model_copy(update={"artifacts": ["unregistered-private"]}))
        demands.release(demand.id)
        demands.release(metadata.id)


def result_fixture(runtime_factory=None):
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    registry.register_operator(
        operator_id="test.result_source",
        inputs=[],
        outputs=[{"name": "out"}],
        produces_payload_keys=["subject"],
        runtime_factory=runtime_factory,
    )
    registry.register_operator(
        operator_id="test.result_tracker",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        state_kind="stateful_per_stream",
        share_strategy="never",
        runtime_factory=runtime_factory,
    )
    graph = build_pipeline_graph_v2(
        graph_uid="stable_graph",
        nodes=[
            {"id": "source", "operator": "test.result_source"},
            {"id": "tracker", "operator": "test.result_tracker"},
        ],
        edges=[
            {"from": {"node": "source", "port": "out"}, "to": {"node": "tracker", "port": "in"}}
        ],
    )
    uid = next(n["uid"] for n in graph["nodes"] if n["id"] == "tracker")
    graph.setdefault("meta", {})["outputs"] = [
        {
            "key": "people.primary",
            "name": "Pessoas",
            "node_uid": uid,
            "port": "out",
            "contract": CONTRACT,
            "production_scope": "analysis:synthetic",
            "state_owner": "owner:synthetic",
            "state_partition": "capture_instance/stream_id",
        }
    ]
    producer = Pipeline(name="producer", enabled=False, graph=graph)

    def consumer(name):
        graph = build_pipeline_graph_v2(
            graph_uid=name,
            nodes=[
                {
                    "id": "reference",
                    "operator": "core.result_input",
                    "config": {"key": "people.primary", "contract": CONTRACT},
                }
            ],
            edges=[],
        )
        return Pipeline(name=name, graph=graph)

    return registry, producer, consumer("manual"), consumer("automatic")


def test_named_output_expands_dependencies_and_only_shares_state_with_explicit_owner():
    registry, producer, manual, automatic = result_fixture()
    compiler = PipelineGraphCompiler(registry)
    report = compiler.compile_many([manual, automatic], catalog=[producer, manual, automatic])
    plan = build_merged_pipeline_plan(report)
    trackers = [n for n in plan.merged_pipeline.nodes if n.operator_id == "test.result_tracker"]
    assert len(trackers) == 1 and trackers[0].shareable
    assert registry.get("test.result_tracker").definition.share_strategy == "never"
    assert len(plan.merged_node_occurrences[trackers[0].node_id]) == 2
    before = [n.signature for n in report.pipelines[0].nodes]
    producer.display_name = "Outro nome"
    producer.graph["meta"]["outputs"][0]["name"] = "Outro título"
    after = compiler.compile_many([manual, automatic], catalog=[producer, manual, automatic])
    assert [n.signature for n in after.pipelines[0].nodes] == before
    producer.graph["meta"]["outputs"][0].pop("state_owner")
    with pytest.raises(GraphCompileError, match="owner|partition"):
        compiler.compile_many([manual, automatic], catalog=[producer, manual, automatic])


def test_result_version_and_real_packet_contract_are_checked_before_activation():
    registry, producer, manual, _ = result_fixture()
    manual.graph["nodes"][0]["config"]["contract"] = {**CONTRACT, "version": 2}
    with pytest.raises(GraphCompileError, match="contract|version"):
        PipelineGraphCompiler(registry).compile_many([manual], catalog=[producer, manual])
    manual.graph["nodes"][0]["config"]["contract"] = deepcopy(CONTRACT)
    producer.graph["meta"]["outputs"][0]["contract"] = {
        **CONTRACT,
        "payload_keys": ["private_identity"],
    }
    manual.graph["nodes"][0]["config"]["contract"] = producer.graph["meta"]["outputs"][0][
        "contract"
    ]
    with pytest.raises(GraphCompileError, match="guarantee|contract"):
        PipelineGraphCompiler(registry).compile_many([manual], catalog=[producer, manual])


def test_output_reference_cycle_is_rejected_even_when_hidden_in_a_dependency():
    registry, producer, manual, _ = result_fixture()
    source = producer.graph["nodes"][0]
    source["operator"] = "core.result_input"
    source["config"] = {"key": "people.primary", "contract": CONTRACT}
    with pytest.raises(GraphCompileError, match="[Cc]ycl"):
        PipelineGraphCompiler(registry).compile_many([manual], catalog=[producer, manual])


def test_unbound_reference_cannot_construct_any_producer_runtime():
    from toposync.runtime.pipelines.execution import (
        PassThroughRuntime,
        PipelineRuntime,
        PipelineExecutionError,
    )

    created = []
    registry, producer, manual, _ = result_fixture(
        lambda *_: created.append(True) or PassThroughRuntime()
    )
    compiled = (
        PipelineGraphCompiler(registry)
        .compile_many([manual], catalog=[producer, manual])
        .pipelines[0]
    )
    with pytest.raises(PipelineExecutionError, match="binding"):
        PipelineRuntime(compiled, registry)
    assert not created


def test_isolated_node_ids_do_not_collide_when_names_contain_double_underscores():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    registry.register_operator(
        operator_id="test.isolated", inputs=[], outputs=[], share_strategy="never"
    )
    pipelines = [
        Pipeline(
            name=name,
            graph=build_pipeline_graph_v2(
                graph_uid=name, nodes=[{"id": node, "operator": "test.isolated"}], edges=[]
            ),
        )
        for name, node in [("a__b", "c"), ("a", "b__c")]
    ]
    plan = build_merged_pipeline_plan(PipelineGraphCompiler(registry).compile_many(pipelines))
    assert len({node.node_id for node in plan.merged_pipeline.nodes}) == 2
    assert len(set(plan.occurrence_to_merged_node_id.values())) == 2
