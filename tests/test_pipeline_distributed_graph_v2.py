from copy import deepcopy
from itertools import pairwise

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.distributed import build_distributed_graphs
from toposync.runtime.pipelines.graph_schema_v2 import PipelineGraphV2Spec
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_cameras.pipelines.person_ground import register_person_ground_operator
from toposync_ext_cameras.pipelines.pointing import register_pointing_operator
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators


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


def test_human_observation_uses_the_origin_map_with_remote_pose_and_tracking():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_vision_pipeline_operators(registry)
    register_person_ground_operator(registry)
    register_pointing_operator(registry)
    operators = {
        "source": "core.demo_frame_sequence_source",
        "detect": "vision.detect",
        "pose": "vision.pose_estimate",
        "track": "vision.track",
        "ground": "camera.person_ground_estimate",
        "gestures": "vision.gesture_recognize",
        "pointing": "camera.pointing_target",
        "filter": "core.filter",
        "notify": "core.notify",
    }
    graph = build_pipeline_graph_v2(
        graph_uid="remote_human",
        nodes=[{"id": name, "operator": operator, "config": {"emit_mode": "annotate"} if name == "detect" else {}}
               for name, operator in operators.items()],
        edges=[{"from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"}}
               for source, target in pairwise(operators)],
    )
    pipeline = Pipeline(name="remote_human", graph=graph)
    compiler = PipelineGraphCompiler(registry)
    compiler.compile_pipeline(pipeline)
    partitions = build_distributed_graphs(pipeline, registry)
    assert [(edge["from"]["node"], edge["to"]["node"]) for edge in partitions.cross_edges] == [
        ("track", "ground"),
    ]
    for key, expected in (
        ("processing_graph", {"source", "detect", "pose", "track"}),
        ("origin_graph", {"ground", "gestures", "pointing", "filter", "notify"}),
    ):
        partition = getattr(partitions, key)
        compiler.compile_pipeline(Pipeline(name=key, graph=partition))
        assert {node["id"] for node in partition["nodes"] if node["id"] in operators} == expected
