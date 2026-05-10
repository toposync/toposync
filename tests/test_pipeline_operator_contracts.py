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
                {"id": "crop", "operator": "vision.crop_objects", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "crop", "port": "in"}},
                {"from": {"node": "crop", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert any(
        alert.code == "missing_required_payload_keys"
        and alert.node_id == "crop"
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
        and "main" in alert.message
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
        and "main" in alert.message
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
        and "main" in alert.message
        for alert in alerts
    )


def test_contract_tracks_explicit_custom_artifact_names() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="contract_custom_artifact_names",
        graph={
            "schema_version": 1,
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
                {"from": {"node": "source", "port": "out"}, "to": {"node": "crop", "port": "in"}},
                {"from": {"node": "crop", "port": "out"}, "to": {"node": "adjust", "port": "in"}},
            ],
        },
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
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                {
                    "id": "adjust",
                    "operator": "camera.image_adjust",
                    "config": {"input_artifact_name": "debug_crop"},
                },
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "adjust", "port": "in"}},
            ],
        },
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


def test_compile_rejects_detect_events_before_tracking() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="detect_events_before_tracking",
        graph={
            "schema_version": 1,
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
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
                {"from": {"node": "detect", "port": "out"}, "to": {"node": "track", "port": "in"}},
                {"from": {"node": "track", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )

    with pytest.raises(GraphCompileError, match="emit_mode='annotate'"):
        PipelineGraphCompiler(registry).compile_pipeline(pipeline)
