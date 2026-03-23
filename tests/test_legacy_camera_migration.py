from __future__ import annotations

from pathlib import Path

from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.migration_legacy_cameras import (
    build_pipeline_from_legacy_camera_rule,
    extract_legacy_camera_rules,
)
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators


def test_legacy_camera_migration_supports_motion_and_object_triggers(tmp_path: Path) -> None:
    _ = tmp_path

    settings = {
        "extensions": {
            "com.toposync.cameras": {
                "cameras": [
                    {
                        "id": "camera-main",
                        "name": "Front door",
                        "processing_server_id": "local",
                        "enabled": True,
                        "detections": [
                            {"id": "rule-motion", "trigger": {"kind": "motion"}},
                            {"id": "rule-object", "trigger": {"kind": "object", "category": "cat"}},
                        ],
                    },
                ],
            },
        },
    }

    rules = extract_legacy_camera_rules(settings)
    assert {r.rule_id for r in rules} == {"rule-motion", "rule-object"}

    existing: set[str] = set()
    pipelines = [build_pipeline_from_legacy_camera_rule(rule, existing_names=existing) for rule in rules]
    assert all(p is not None for p in pipelines)
    motion_pipeline = next(
        p
        for p in pipelines
        if p
        and any(
            isinstance(node, dict) and node.get("operator") == "core.lifecycle_from_boolean"
            for node in (p.graph.get("nodes") or [])
        )
    )
    object_pipeline = next(
        p
        for p in pipelines
        if p
        and any(
            isinstance(node, dict) and node.get("operator") == "vision.track"
            for node in (p.graph.get("nodes") or [])
        )
    )

    motion_nodes = {n.get("id"): n for n in motion_pipeline.graph.get("nodes", []) if isinstance(n, dict)}
    assert motion_nodes.get("motion", {}).get("operator") == "camera.motion_gate"
    assert motion_nodes.get("motion", {}).get("config", {}).get("emit_when_idle") is True
    assert motion_nodes.get("lifecycle", {}).get("operator") == "core.lifecycle_from_boolean"

    object_nodes = {n.get("id"): n for n in object_pipeline.graph.get("nodes", []) if isinstance(n, dict)}
    assert object_nodes.get("detect", {}).get("operator") == "vision.detect"
    assert object_nodes.get("track", {}).get("operator") == "vision.track"

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    compiler = PipelineGraphCompiler(registry)
    compiler.compile_pipeline(motion_pipeline)
    compiler.compile_pipeline(object_pipeline)
