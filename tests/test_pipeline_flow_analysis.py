from __future__ import annotations

from typing import Any

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler
from toposync.runtime.pipelines.flow_analysis import analyze_pipeline_flow
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline


def _registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.data_source",
        inputs=[],
        outputs=[{"name": "out"}],
        output_modalities=["data"],
    )
    registry.register_operator(
        operator_id="test.video_source",
        inputs=[],
        outputs=[{"name": "out"}],
        produces_artifacts=["main"],
        output_modalities=["video"],
        default_output_policy={
            "traffic": {"modality": "video.frame", "semantic_class": "frame", "continuous": True},
            "queue": {"max_items": 1, "drop_policy": "latest_only"},
        },
    )
    registry.register_operator(
        operator_id="vision.detect",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["vision", "heavy_compute"],
        requires_artifacts=["main"],
        resource_kind="vision_model",
        output_modalities=["video"],
        pressure_behavior="skip_before_compute",
    )
    registry.register_operator(
        operator_id="test.transform",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        input_modalities=["video"],
        output_modalities=["video"],
    )
    registry.register_operator(
        operator_id="test.video_sink",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["sink", "realtime"],
        input_modalities=["video"],
        state_kind="external_side_effect",
        pressure_behavior="reduce_source_rate",
    )
    registry.register_operator(
        operator_id="test.debounce",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["rate_control"],
        state_kind="stateful_per_subject",
    )
    registry.register_operator(
        operator_id="test.artifact_producer",
        inputs=[],
        outputs=[{"name": "out"}],
        produces_artifacts=["main"],
    )
    registry.register_operator(
        operator_id="test.artifact_consumer",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        requires_artifacts=["main"],
    )
    registry.register_operator(
        operator_id="test.notify",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        state_kind="external_side_effect",
        pressure_behavior="block",
        share_strategy="never",
    )
    return registry


def _edge(
    source: str,
    target: str,
    *,
    maxsize: int | None = None,
    drop_policy: str | None = None,
) -> dict:
    edge: dict = {
        "from": {"node": source, "port": "out"},
        "to": {"node": target, "port": "in"},
    }
    if maxsize is not None:
        edge["maxsize"] = maxsize
    if drop_policy is not None:
        edge["drop_policy"] = drop_policy
    return edge


def _compile(graph: dict) -> tuple[OperatorRegistry, object]:
    registry = _registry()
    operators_by_node_id = {str(node["id"]): str(node["operator"]) for node in graph.get("nodes", [])}

    def edge_traffic(edge: dict) -> dict[str, Any]:
        source_operator_id = operators_by_node_id.get(str(edge["from"]["node"]), "")
        registered = registry.get(source_operator_id)
        output_modalities = registered.definition.output_modalities if registered is not None else []
        if any(str(item).startswith("video") for item in output_modalities):
            return {"modality": "video.frame", "semantic_class": "frame", "continuous": True}
        return {"modality": "data.record", "semantic_class": "data", "continuous": False}

    normalized = {
        **graph,
        "schema_version": 2,
        "uid": graph.get("uid", "graph"),
        "nodes": [{**node, "uid": node.get("uid", node["id"])} for node in graph.get("nodes", [])],
        "edges": [
            {
                "uid": edge.get("uid", f"{edge['from']['node']}.{edge['from']['port']}->{edge['to']['node']}.{edge['to']['port']}"),
                "from": edge["from"],
                "to": edge["to"],
                "queue": {
                    "max_items": edge.get("maxsize", 1),
                    "drop_policy": edge.get("drop_policy", "latest_only"),
                },
                "traffic": edge_traffic(edge),
            }
            for edge in graph.get("edges", [])
        ],
    }
    compiled = PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="flow", graph=normalized))
    return registry, compiled


def _codes(alerts: list[Any]) -> set[str]:
    return {alert.code for alert in alerts}


def test_flow_analysis_warns_about_duplicate_heavy_ai_with_different_categories() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {
                    "id": "detect_person",
                    "operator": "vision.detect",
                    "config": {"model_id": "yolo", "categories": ["person"]},
                },
                {
                    "id": "detect_vehicle",
                    "operator": "vision.detect",
                    "config": {"model_id": "yolo", "categories": ["car", "motorcycle"]},
                },
            ],
            "edges": [
                _edge("source", "detect_person"),
                _edge("source", "detect_vehicle"),
            ],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "duplicate_heavy_ai" in _codes(alerts)


def test_flow_analysis_accepts_single_combined_detection_before_branches() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {
                    "id": "detect",
                    "operator": "vision.detect",
                    "config": {"model_id": "yolo", "categories": ["person", "car", "motorcycle"]},
                },
                {"id": "person_branch", "operator": "test.transform"},
                {"id": "vehicle_branch", "operator": "test.transform"},
            ],
            "edges": [
                _edge("source", "detect"),
                _edge("detect", "person_branch"),
                _edge("detect", "vehicle_branch"),
            ],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "duplicate_heavy_ai" not in _codes(alerts)


def test_flow_analysis_warns_about_heavy_edge_backlog_policy() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {"id": "detect", "operator": "vision.detect", "config": {"model_id": "yolo"}},
            ],
            "edges": [_edge("source", "detect", maxsize=16, drop_policy="drop_oldest")],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "heavy_edge_policy" in _codes(alerts)


def test_flow_analysis_warns_about_artifact_fanout() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "producer", "operator": "test.artifact_producer"},
                {"id": "consumer_a", "operator": "test.artifact_consumer"},
                {"id": "consumer_b", "operator": "test.artifact_consumer"},
            ],
            "edges": [
                _edge("producer", "consumer_a"),
                _edge("producer", "consumer_b"),
            ],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "artifact_fanout" in _codes(alerts)


def test_flow_analysis_warns_about_lossy_edge_before_blocking_side_effect() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.data_source"},
                {"id": "notify", "operator": "test.notify"},
            ],
            "edges": [_edge("source", "notify", drop_policy="drop_oldest")],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "side_effect_lossy_edge" in _codes(alerts)


def test_flow_analysis_warns_about_continuous_stream_to_sparse_operator() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {"id": "debounce", "operator": "test.debounce"},
            ],
            "edges": [_edge("source", "debounce")],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "continuous_stream_to_sparse_operator" in _codes(alerts)


def test_flow_analysis_accepts_continuous_stream_to_video_sink() -> None:
    registry, compiled = _compile(
        {
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {"id": "publish", "operator": "test.video_sink"},
            ],
            "edges": [_edge("source", "publish")],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "continuous_stream_to_sparse_operator" not in _codes(alerts)


def test_flow_analysis_warns_about_blocking_policy_on_continuous_video() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {"id": "transform", "operator": "test.transform"},
            ],
            "edges": [_edge("source", "transform", drop_policy="block")],
        }
    )

    alerts = analyze_pipeline_flow(pipeline=compiled, registry=registry)

    assert "edge_policy_mismatch" in _codes(alerts)


def test_flow_analysis_is_included_in_compiled_pipeline_recommendations() -> None:
    registry, compiled = _compile(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.video_source"},
                {"id": "detect", "operator": "vision.detect", "config": {"model_id": "yolo"}},
            ],
            "edges": [_edge("source", "detect", maxsize=8, drop_policy="drop_newest")],
        }
    )

    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert "heavy_edge_policy" in _codes(alerts)
