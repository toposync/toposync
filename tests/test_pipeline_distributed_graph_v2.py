from copy import deepcopy

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.distributed import build_distributed_graphs
from toposync.runtime.pipelines.graph_schema_v2 import PipelineGraphV2Spec


def definition():
    return {
        "schema_version": 2, "uid": "human-observation", "revision": 7,
        "nodes": [
            {"uid": "source-uid", "id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
            {"uid": "notify-uid", "id": "notify", "operator": "core.notify", "config": {}},
        ],
        "edges": [{
            "uid": "observation-edge", "from": {"node": "source", "port": "out"},
            "to": {"node": "notify", "port": "in"},
            "traffic": {"modality": "video"},
            "queue": {"max_items": 13, "max_age_ms": 500, "drop_policy": "keyed_latest_only",
                      "key_policy": "payload_path", "key_path": "subject.id"},
            "lifecycle": {"preserve_open": True, "preserve_close": True, "compact_updates": False},
            "debug": {"sample_headers": False, "retain_last": 0, "retain_artifact_data": False,
                      "retain_artifact_refs": False},
            "backpressure": {"mode": "pause_upstream", "propagate_pressure": False},
        }],
    }


@pytest.mark.parametrize("side", ["processing_graph", "origin_graph"])
def test_distributed_v2_partitions_validate_and_compile(side):
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    graph = definition()
    before = deepcopy(graph)
    pipeline = Pipeline(name="human_observation", graph=graph)
    PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    partitions = build_distributed_graphs(pipeline, registry)
    partition = getattr(partitions, side)
    validated = PipelineGraphV2Spec.model_validate(partition)
    assert validated.revision == 7
    assert len({node.uid for node in validated.nodes}) == len(validated.nodes)
    assert len({edge.uid for edge in validated.edges}) == len(validated.edges)
    PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name=side, graph=partition))
    assert graph == before
    assert build_distributed_graphs(pipeline, registry) == partitions


@pytest.mark.parametrize("side", ["processing_graph", "origin_graph"])
def test_distributed_v2_bridge_preserves_explicit_edge_policies(side):
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    graph = definition()
    partitions = build_distributed_graphs(Pipeline(name="human_observation", graph=graph), registry)
    for edge in getattr(partitions, side)["edges"]:
        for policy in ("queue", "lifecycle", "debug", "backpressure", "traffic"):
            expected = deepcopy(graph["edges"][0][policy])
            if policy == "queue" and edge["uid"].endswith(":filter"):
                expected.update({"drop_policy": "block", "key_policy": "none", "key_path": ""})
            assert edge.get(policy) == expected
        assert "maxsize" not in edge and "drop_policy" not in edge
