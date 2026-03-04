from __future__ import annotations

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_best_frame_selector_is_not_marked_unused_when_store_images_uses_fallback_string() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="best_frame_store_images_fallback_usage",
        type="final",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                {"id": "detect", "operator": "vision.object_detection_yolo", "config": {"emit_mode": "annotate"}},
                {"id": "bf", "operator": "camera.best_frame_selector", "config": {}},
                {"id": "store", "operator": "core.store_images", "config": {"image_with_fallback": "best_frame,treated,original"}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "detect", "port": "in"}},
                {"from": {"node": "detect", "port": "out"}, "to": {"node": "bf", "port": "in"}},
                {"from": {"node": "bf", "port": "out"}, "to": {"node": "store", "port": "in"}},
                {"from": {"node": "store", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert not any(alert.code == "best_frame_unused" and alert.node_id == "bf" for alert in alerts)


def test_best_frame_selector_default_inputs_do_not_trigger_missing_inputs_alert() -> None:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)

    pipeline = Pipeline(
        name="best_frame_default_inputs_no_missing_alert",
        type="final",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
                {"id": "bf", "operator": "camera.best_frame_selector", "config": {}},
                {"id": "sink", "operator": "core.sink", "config": {}},
            ],
            "edges": [
                {"from": {"node": "source", "port": "out"}, "to": {"node": "bf", "port": "in"}},
                {"from": {"node": "bf", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        },
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)

    assert not any(alert.code == "best_frame_missing_inputs" and alert.node_id == "bf" for alert in alerts)

