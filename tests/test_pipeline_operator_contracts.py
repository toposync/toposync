from __future__ import annotations

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    GraphCompileError,
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def _graph_v2(registry: OperatorRegistry, graph: dict) -> dict:
    operators_by_node_id = {
        str(node["id"]): str(node["operator"]) for node in graph.get("nodes", [])
    }

    def _edge_modality(edge: dict) -> str:
        source = registry.get(operators_by_node_id.get(str(edge["from"]["node"]), ""))
        target = registry.get(operators_by_node_id.get(str(edge["to"]["node"]), ""))
        source_modalities = source.definition.output_modalities if source is not None else []
        target_modalities = target.definition.input_modalities if target is not None else []
        raw = next(iter(source_modalities or target_modalities), "data.record")
        if raw == "video":
            return "video.frame"
        if raw == "data":
            return "data.record"
        return str(raw)

    edges: list[dict] = []
    for index, edge in enumerate(graph.get("edges", [])):
        modality = _edge_modality(edge)
        queue = dict(edge.get("queue") or {})
        queue.setdefault("max_items", edge.get("maxsize", 1))
        queue.setdefault("drop_policy", edge.get("drop_policy", "latest_only"))
        normalized_edge = {
            "uid": edge.get(
                "uid",
                f"edge_{index}_{edge['from']['node']}_{edge['to']['node']}",
            ),
            "from": dict(edge["from"]),
            "to": dict(edge["to"]),
            "traffic": edge.get(
                "traffic",
                {
                    "modality": modality,
                    "semantic_class": "frame" if modality.startswith("video") else "data",
                    "continuous": modality.startswith("video"),
                },
            ),
            "queue": queue,
        }
        for key in ("backpressure", "lifecycle", "debug"):
            if key in edge:
                normalized_edge[key] = edge[key]
        edges.append(normalized_edge)

    return {
        **graph,
        "schema_version": 2,
        "uid": graph.get("uid", "operator_contracts"),
        "nodes": [
            {**node, "uid": node.get("uid", f"node_{node['id']}")}
            for node in graph.get("nodes", [])
        ],
        "edges": edges,
    }


def test_contract_alerts_when_required_payload_keys_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_payload_keys",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "core.synthetic_source",
                        "config": {"rate_hz": 5.0},
                    },
                    {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "crop", "port": "in"},
                    },
                    {"from": {"node": "crop", "port": "out"}, "to": {"node": "sink", "port": "in"}},
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_payload_keys"
        and alert.node_id == "crop"
        and "subject" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "core.synthetic_source",
                        "config": {"rate_hz": 5.0},
                    },
                    {"id": "gate", "operator": "camera.motion_gate", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "gate", "port": "in"},
                    },
                    {"from": {"node": "gate", "port": "out"}, "to": {"node": "sink", "port": "in"}},
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "gate"
        and "main" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_adaptive_motion_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts_adaptive_motion",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "core.synthetic_source",
                        "config": {"rate_hz": 5.0},
                    },
                    {"id": "motion", "operator": "camera.motion_bgsub_adaptive", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "motion", "port": "in"},
                    },
                    {
                        "from": {"node": "motion", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "motion"
        and "main" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_sample_motion_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts_sample_motion",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "core.synthetic_source",
                        "config": {"rate_hz": 5.0},
                    },
                    {"id": "motion", "operator": "camera.motion_sample_bg", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "motion", "port": "in"},
                    },
                    {
                        "from": {"node": "motion", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "motion"
        and "main" in alert.message
        for alert in alerts
    )


def test_contract_tracks_explicit_custom_artifact_names() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_custom_artifact_names",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {
                        "id": "crop",
                        "operator": "camera.image_crop",
                        "config": {"output_artifact_name": "debug_crop"},
                    },
                    {
                        "id": "adjust",
                        "operator": "camera.image_adjust",
                        "config": {"input_artifact_name": "debug_crop"},
                    },
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "crop", "port": "in"},
                    },
                    {
                        "from": {"node": "crop", "port": "out"},
                        "to": {"node": "adjust", "port": "in"},
                    },
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert not any(
        alert.code == "missing_required_artifacts" and alert.node_id == "adjust" for alert in alerts
    )


def test_contract_does_not_fallback_to_main_for_missing_custom_input() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_custom_artifact",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {
                        "id": "adjust",
                        "operator": "camera.image_adjust",
                        "config": {"input_artifact_name": "debug_crop"},
                    },
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "adjust", "port": "in"},
                    },
                ],
            },
        ),
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "adjust"
        and "debug_crop" in alert.message
        and "main" not in alert.message
        for alert in alerts
    )


def _register_branch_contract_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="test.branch_source",
        inputs=[],
        outputs=[{"name": "out"}],
    )
    registry.register_operator(
        operator_id="test.contract_producer",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        produces_payload_keys=["world"],
        produces_artifacts=["branch_artifact"],
    )
    registry.register_operator(
        operator_id="test.configured_artifact_producer",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        produces_artifacts=["main"],
    )
    registry.register_operator(
        operator_id="test.contract_passthrough",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
    )
    registry.register_operator(
        operator_id="test.contract_merge",
        inputs=[
            {"name": "left", "required": True},
            {"name": "right", "required": True},
        ],
        outputs=[{"name": "out"}],
    )
    registry.register_operator(
        operator_id="test.contract_transform_with_frames",
        inputs=[
            {"name": "in", "required": True},
            {"name": "frames", "required": True},
        ],
        outputs=[{"name": "out"}],
    )
    registry.register_operator(
        operator_id="test.contract_consumer",
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        requires_payload_keys=["world"],
        requires_artifacts=["branch_artifact"],
    )


def test_contract_requires_values_guaranteed_on_every_branch_before_merge() -> None:
    registry = OperatorRegistry()
    _register_branch_contract_operators(registry)
    pipeline = Pipeline(
        name="contract_branch_merge",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "test.branch_source"},
                    {"id": "producer", "operator": "test.contract_producer"},
                    {"id": "passthrough", "operator": "test.contract_passthrough"},
                    {"id": "merge", "operator": "test.contract_merge"},
                    {"id": "consumer", "operator": "test.contract_consumer"},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "producer", "port": "in"},
                    },
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "passthrough", "port": "in"},
                    },
                    {
                        "from": {"node": "producer", "port": "out"},
                        "to": {"node": "merge", "port": "left"},
                    },
                    {
                        "from": {"node": "passthrough", "port": "out"},
                        "to": {"node": "merge", "port": "right"},
                    },
                    {
                        "from": {"node": "merge", "port": "out"},
                        "to": {"node": "consumer", "port": "in"},
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    payload_alert = next(
        alert
        for alert in alerts
        if alert.code == "missing_required_payload_keys" and alert.node_id == "consumer"
    )
    artifact_alert = next(
        alert
        for alert in alerts
        if alert.code == "missing_required_artifacts" and alert.node_id == "consumer"
    )
    assert payload_alert.details["missing_payload_keys"] == ["world"]
    assert artifact_alert.details["missing_artifacts"] == ["branch_artifact"]


def test_contract_accepts_values_produced_on_every_branch_before_merge() -> None:
    registry = OperatorRegistry()
    _register_branch_contract_operators(registry)
    pipeline = Pipeline(
        name="contract_branch_merge_guaranteed",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "test.branch_source"},
                    {"id": "left_producer", "operator": "test.contract_producer"},
                    {"id": "right_producer", "operator": "test.contract_producer"},
                    {"id": "merge", "operator": "test.contract_merge"},
                    {"id": "consumer", "operator": "test.contract_consumer"},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "left_producer", "port": "in"},
                    },
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "right_producer", "port": "in"},
                    },
                    {
                        "from": {"node": "left_producer", "port": "out"},
                        "to": {"node": "merge", "port": "left"},
                    },
                    {
                        "from": {"node": "right_producer", "port": "out"},
                        "to": {"node": "merge", "port": "right"},
                    },
                    {
                        "from": {"node": "merge", "port": "out"},
                        "to": {"node": "consumer", "port": "in"},
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert not any(
        alert.node_id == "consumer"
        and alert.code in {"missing_required_payload_keys", "missing_required_artifacts"}
        for alert in alerts
    )


def test_contract_preserves_primary_input_guarantees_with_auxiliary_frames() -> None:
    registry = OperatorRegistry()
    _register_branch_contract_operators(registry)
    pipeline = Pipeline(
        name="contract_primary_input_with_auxiliary_frames",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "test.branch_source"},
                    {"id": "producer", "operator": "test.contract_producer"},
                    {
                        "id": "transform",
                        "operator": "test.contract_transform_with_frames",
                    },
                    {"id": "consumer", "operator": "test.contract_consumer"},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "producer", "port": "in"},
                    },
                    {
                        "from": {"node": "producer", "port": "out"},
                        "to": {"node": "transform", "port": "in"},
                    },
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "transform", "port": "frames"},
                    },
                    {
                        "from": {"node": "transform", "port": "out"},
                        "to": {"node": "consumer", "port": "in"},
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert not any(
        alert.node_id == "consumer"
        and alert.code in {"missing_required_payload_keys", "missing_required_artifacts"}
        for alert in alerts
    )


def test_store_images_requires_artifact_guaranteed_on_every_merge_branch() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    _register_branch_contract_operators(registry)
    pipeline = Pipeline(
        name="store_images_branch_artifact_not_guaranteed",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "test.branch_source"},
                    {
                        "id": "producer",
                        "operator": "test.configured_artifact_producer",
                        "config": {"output_artifact_name": "branch_artifact"},
                    },
                    {"id": "passthrough", "operator": "test.contract_passthrough"},
                    {"id": "merge", "operator": "test.contract_merge"},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {"input_artifact_name": "branch_artifact"},
                    },
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "producer", "port": "in"},
                    },
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "passthrough", "port": "in"},
                    },
                    {
                        "from": {"node": "producer", "port": "out"},
                        "to": {"node": "merge", "port": "left"},
                    },
                    {
                        "from": {"node": "passthrough", "port": "out"},
                        "to": {"node": "merge", "port": "right"},
                    },
                    {
                        "from": {"node": "merge", "port": "out"},
                        "to": {"node": "store", "port": "in"},
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "store_images_missing_artifacts"
        and alert.node_id == "store"
        and alert.details["missing_artifacts"] == ["branch_artifact"]
        for alert in alerts
    )


def test_camera_world_contracts_use_generic_missing_payload_diagnostic() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    pipeline = Pipeline(
        name="camera_world_contracts",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {
                        "id": "source",
                        "operator": "core.synthetic_source",
                        "config": {"rate_hz": 5.0},
                    },
                    {"id": "area", "operator": "camera.area_restriction"},
                    {"id": "velocity", "operator": "camera.velocity_estimation"},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "area", "port": "in"},
                    },
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "velocity", "port": "in"},
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    generic_alert_nodes = {
        alert.node_id for alert in alerts if alert.code == "missing_required_payload_keys"
    }
    assert {"area", "velocity"} <= generic_alert_nodes
    assert not any(
        alert.code in {"area_missing_camera_mapping", "velocity_missing_camera_mapping"}
        for alert in alerts
    )


def test_compile_rejects_detect_events_before_tracking() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="detect_events_before_tracking",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {
                        "id": "detect",
                        "operator": "vision.detect",
                        "config": {"model_id": "fake.detector", "emit_mode": "events"},
                    },
                    {"id": "track", "operator": "vision.track", "config": {}},
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                    },
                    {
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "track", "port": "in"},
                    },
                    {
                        "from": {"node": "track", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                    },
                ],
            },
        ),
    )

    with pytest.raises(GraphCompileError, match="emit_mode='annotate'"):
        PipelineGraphCompiler(registry).compile_pipeline(pipeline)


def test_compile_accepts_detect_annotate_before_tracking_recipe_shape() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="detect_annotate_before_tracking",
        graph=_graph_v2(
            registry,
            {
                "schema_version": 2,
                "nodes": [
                    {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                    {
                        "id": "detect",
                        "operator": "vision.detect",
                        "config": {"model_id": "fake.detector", "emit_mode": "annotate"},
                    },
                    {
                        "id": "track",
                        "operator": "vision.track",
                        "config": {
                            "tracker_id": "byte_world",
                            "close_after_seconds": 10.0,
                            "stitch_gap_seconds": 30.0,
                        },
                    },
                    {"id": "sink", "operator": "core.sink", "config": {}},
                ],
                "edges": [
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "detect", "port": "in"},
                    },
                    {
                        "from": {"node": "detect", "port": "out"},
                        "to": {"node": "track", "port": "in"},
                        "maxsize": 64,
                        "drop_policy": "keyed_latest_only",
                    },
                    {
                        "from": {"node": "track", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                        "maxsize": 64,
                        "drop_policy": "keyed_latest_only",
                    },
                ],
            },
        ),
    )

    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert {node.node_id for node in compiled.nodes} == {"source", "detect", "track", "sink"}
    assert not any(alert.code == "detect_events_before_tracking" for alert in alerts)
    assert not any(alert.code == "split_stream_latest_only_channel" for alert in alerts)
