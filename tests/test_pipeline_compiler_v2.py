from __future__ import annotations

from copy import deepcopy

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    DropPolicy,
    GraphCompileError,
    OperatorRegistry,
    PipelineGraphCompiler,
)


def _registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.video_source",
        inputs=[],
        outputs=[{"name": "out"}],
        output_modalities=["video"],
    )
    registry.register_operator(
        operator_id="test.video_filter",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        input_modalities=["video"],
        output_modalities=["video"],
    )
    registry.register_operator(
        operator_id="test.data_sink",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        input_modalities=["data"],
        share_strategy="never",
    )
    return registry


def _graph_v2() -> dict:
    return {
        "schema_version": 2,
        "uid": "graph_front",
        "revision": 1,
        "nodes": [
            {"uid": "node_source", "id": "source", "operator": "test.video_source"},
            {"uid": "node_filter", "id": "filter", "operator": "test.video_filter"},
        ],
        "edges": [
            {
                "uid": "edge_source_filter",
                "from": {"node": "source", "port": "out"},
                "to": {"node": "filter", "port": "in"},
                "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                "queue": {"max_items": 3, "drop_policy": "drop_oldest"},
            }
        ],
    }


def test_compiler_v2_preserves_uids_and_queue_contract() -> None:
    compiled = PipelineGraphCompiler(_registry()).compile_pipeline(
        Pipeline(name="graph_v2", graph=_graph_v2())
    )

    assert compiled.schema_version == 2
    assert [(node.node_id, node.uid) for node in compiled.nodes] == [
        ("source", "node_source"),
        ("filter", "node_filter"),
    ]
    assert len(compiled.edges) == 1
    edge = compiled.edges[0]
    assert edge.uid == "edge_source_filter"
    assert edge.channel_maxsize == 3
    assert edge.channel_drop_policy == DropPolicy.DROP_OLDEST


def test_compiler_v2_accepts_empty_graph_without_graph_uid_for_api_compatibility() -> None:
    compiled = PipelineGraphCompiler(_registry()).compile_pipeline(
        Pipeline(name="empty_v2", graph={"schema_version": 2, "nodes": [], "edges": []})
    )

    assert compiled.schema_version == 2
    assert compiled.nodes == ()
    assert compiled.edges == ()


def test_compiler_v2_rejects_duplicate_node_uid() -> None:
    graph = _graph_v2()
    graph["nodes"][1]["uid"] = "node_source"

    with pytest.raises(GraphCompileError, match="Duplicate node uid"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="bad", graph=graph))


def test_compiler_v2_rejects_duplicate_edge_uid() -> None:
    graph = _graph_v2()
    graph["nodes"].append({"uid": "node_filter_2", "id": "filter_2", "operator": "test.video_filter"})
    graph["edges"].append(
        {
            "uid": "edge_source_filter",
            "from": {"node": "source", "port": "out"},
            "to": {"node": "filter_2", "port": "in"},
            "traffic": {"modality": "video.frame", "semantic_class": "frame"},
        }
    )

    with pytest.raises(GraphCompileError, match="Duplicate edge uid"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="bad", graph=graph))


def test_compiler_v2_validates_modalities_against_operator_contracts() -> None:
    graph = _graph_v2()
    graph["nodes"][1] = {"uid": "node_sink", "id": "sink", "operator": "test.data_sink"}
    graph["edges"][0]["to"] = {"node": "sink", "port": "in"}

    with pytest.raises(GraphCompileError, match="modality 'video.frame' is incompatible"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="bad", graph=graph))


def test_compiler_v2_accepts_specific_modality_for_broad_operator_modality() -> None:
    compiled = PipelineGraphCompiler(_registry()).compile_pipeline(
        Pipeline(name="compatible", graph=_graph_v2())
    )

    assert compiled.edges[0].source_node_id == "source"


def test_compiler_v2_rejects_edges_that_do_not_preserve_lifecycle() -> None:
    graph = _graph_v2()
    graph["edges"][0]["lifecycle"] = {"preserve_open": False, "preserve_close": True}

    with pytest.raises(GraphCompileError, match="must preserve OPEN and CLOSE"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="bad", graph=graph))


def test_compiler_v2_keeps_dag_and_single_input_port_guards() -> None:
    cycle = {
        "schema_version": 2,
        "uid": "graph_cycle",
        "nodes": [
            {"uid": "node_a", "id": "a", "operator": "test.video_filter"},
            {"uid": "node_b", "id": "b", "operator": "test.video_filter"},
        ],
        "edges": [
            {
                "uid": "edge_a_b",
                "from": {"node": "a", "port": "out"},
                "to": {"node": "b", "port": "in"},
                "traffic": {"modality": "video.frame", "semantic_class": "frame"},
            },
            {
                "uid": "edge_b_a",
                "from": {"node": "b", "port": "out"},
                "to": {"node": "a", "port": "in"},
                "traffic": {"modality": "video.frame", "semantic_class": "frame"},
            },
        ],
    }
    with pytest.raises(GraphCompileError, match="Graph must be a DAG"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="cycle", graph=cycle))

    multi_input = _graph_v2()
    multi_input["nodes"].append({"uid": "node_source_2", "id": "source_2", "operator": "test.video_source"})
    multi_input["edges"].append(
        {
            "uid": "edge_source_2_filter",
            "from": {"node": "source_2", "port": "out"},
            "to": {"node": "filter", "port": "in"},
            "traffic": {"modality": "video.frame", "semantic_class": "frame"},
        }
    )
    with pytest.raises(GraphCompileError, match="multiple incoming edges"):
        PipelineGraphCompiler(_registry()).compile_pipeline(Pipeline(name="multi", graph=multi_input))


def test_compiler_v2_output_is_independent_of_json_order() -> None:
    graph = _graph_v2()
    reordered = deepcopy(graph)
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    reordered["edges"] = list(reversed(reordered["edges"]))

    compiler = PipelineGraphCompiler(_registry())
    compiled = compiler.compile_pipeline(Pipeline(name="ordered", graph=graph))
    compiled_reordered = compiler.compile_pipeline(Pipeline(name="ordered", graph=reordered))

    assert compiled.topological_order == compiled_reordered.topological_order
    assert [(node.node_id, node.signature) for node in compiled.nodes] == [
        (node.node_id, node.signature) for node in compiled_reordered.nodes
    ]
    assert [edge.uid for edge in compiled.edges] == [edge.uid for edge in compiled_reordered.edges]


def test_compiler_v2_shareable_signatures_ignore_uids() -> None:
    graph_one = _graph_v2()
    graph_two = _graph_v2()
    graph_two["uid"] = "graph_back"
    graph_two["nodes"][0]["uid"] = "node_source_other"
    graph_two["nodes"][0]["id"] = "source_other"
    graph_two["nodes"][1]["uid"] = "node_filter_other"
    graph_two["nodes"][1]["id"] = "filter_other"
    graph_two["edges"][0]["uid"] = "edge_source_filter_other"
    graph_two["edges"][0]["from"]["node"] = "source_other"
    graph_two["edges"][0]["to"]["node"] = "filter_other"

    report = PipelineGraphCompiler(_registry()).compile_many(
        [
            Pipeline(name="one", graph=graph_one),
            Pipeline(name="two", graph=graph_two),
        ]
    )

    assert any(len(occurrences) == 2 for occurrences in report.shared_signatures.values())
