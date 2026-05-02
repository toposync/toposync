from __future__ import annotations

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_contract_alerts_when_required_payload_keys_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_payload_keys",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                {"id": "seg", "operator": "camera.object_crop", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "seg", "port": "in"}},
                {"from": {"node": "seg", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_payload_keys"
        and alert.node_id == "seg"
        and "object_bbox01" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 5.0}},
                {"id": "gate", "operator": "camera.motion_gate", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "gate", "port": "in"}},
                {"from": {"node": "gate", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "gate"
        and "frame_original" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_adaptive_motion_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts_adaptive_motion",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 5.0}},
                {"id": "motion", "operator": "camera.motion_bgsub_adaptive", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}},
                {"from": {"node": "motion", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "motion"
        and "frame_original" in alert.message
        for alert in alerts
    )


def test_contract_alerts_when_sample_motion_required_artifacts_are_missing() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_missing_artifacts_sample_motion",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.synthetic_source", "config": {"rate_hz": 5.0}},
                {"id": "motion", "operator": "camera.motion_sample_bg", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}},
                {"from": {"node": "motion", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_artifacts"
        and alert.node_id == "motion"
        and "frame_original" in alert.message
        for alert in alerts
    )
