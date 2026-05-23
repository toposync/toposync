from __future__ import annotations

import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_streaming.wizard.pipeline_builder import (
    STREAMING_WIZARD_PRESETS,
    build_streaming_wizard_graph,
    suggested_streaming_wizard_pipeline_name,
)
from toposync_ext_streaming.pipelines import register_streaming_pipeline_operators
from toposync_ext_vision.pipelines import register_vision_pipeline_operators


def _operator_ids(graph: dict) -> list[str]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        out.append(str(node.get("operator") or ""))
    return out


def _stream_config(graph: dict) -> dict:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") == "stream.publish_video":
            return node.get("config") if isinstance(node.get("config"), dict) else {}
    return {}


def _operator_config(graph: dict, *, operator_id: str) -> dict:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("operator") or "") != operator_id:
            continue
        config = node.get("config")
        return config if isinstance(config, dict) else {}
    return {}


def _streaming_meta(graph: dict) -> dict:
    meta = graph.get("meta") if isinstance(graph.get("meta"), dict) else {}
    streaming = meta.get("streaming") if isinstance(meta.get("streaming"), dict) else {}
    return streaming


def _upstream_operator_ids_to_stream(graph: dict) -> set[str]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    operator_by_node: dict[str, str] = {}
    incoming: dict[str, list[str]] = {}

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        operator_by_node[node_id] = str(node.get("operator") or "")

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        source_node = str(source.get("node") or "")
        target_node = str(target.get("node") or "")
        incoming.setdefault(target_node, []).append(source_node)

    upstream: set[str] = set()
    stack = ["stream"]
    while stack:
        current = stack.pop()
        for source_node in incoming.get(current, []):
            if source_node in upstream:
                continue
            upstream.add(source_node)
            stack.append(source_node)
    return {operator_by_node[node_id] for node_id in upstream if node_id in operator_by_node}


def _compile_alert_codes(graph: dict) -> set[str]:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_vision_pipeline_operators(registry)
    register_streaming_pipeline_operators(registry)
    pipeline = Pipeline(name="streaming_diagnostics", graph=graph)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)
    return {alert.code for alert in alerts}


def test_suggested_pipeline_name_starts_with_transmission_context() -> None:
    assert (
        suggested_streaming_wizard_pipeline_name(
            transmission_id="frente",
            transmission_path="frente",
            camera_id="frente",
            preset_id="simple_stream",
        )
        == "frente__stream"
    )


def test_suggested_pipeline_name_uses_meaningful_slug_before_preset() -> None:
    assert (
        suggested_streaming_wizard_pipeline_name(
            transmission_id="550e8400-e29b-41d4-a716-446655440000",
            transmission_name="Transmissão",
            transmission_path="entrada-frente",
            camera_id="camera-frente",
            preset_id="detection_stream",
        )
        == "entrada_frente__camera_frente__detection"
    )


def test_suggested_pipeline_name_skips_generic_transmission_prefix() -> None:
    assert (
        suggested_streaming_wizard_pipeline_name(
            transmission_id="550e8400-e29b-41d4-a716-446655440000",
            transmission_name="Transmissão",
            transmission_path="stream",
            camera_id="frente",
            preset_id="simple_stream",
        )
        == "frente__stream"
    )


@pytest.mark.parametrize("preset_id", STREAMING_WIZARD_PRESETS)
def test_wizard_graph_always_has_source_and_stream(preset_id: str) -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id=preset_id,  # type: ignore[arg-type]
        optional_parameters=None,
    )

    operators = _operator_ids(graph)
    assert "camera.source" in operators
    assert "stream.publish_video" in operators

    stream_config = _stream_config(graph)
    assert stream_config.get("transmission_id") == "transmission_main"
    assert stream_config.get("resize_mode") == "contain"
    assert stream_config.get("bypass_mode") == "auto"
    assert _streaming_meta(graph).get("stream_behavior") == "continuous"


def test_wizard_graph_has_expected_operator_by_preset() -> None:
    expected = {
        "simple_stream": [],
        "motion_gate_stream": ["camera.motion_gate"],
        "detection_stream": ["vision.detect"],
        "tracking_stream": ["vision.detect", "vision.track"],
        "segmentation_stream": ["vision.segment_instances"],
    }
    for preset_id, required_operators in expected.items():
        graph = build_streaming_wizard_graph(
            transmission_id="transmission_main",
            camera_id="camera_a",
            preset_id=preset_id,  # type: ignore[arg-type]
            optional_parameters=None,
        )
        operators = _operator_ids(graph)
        for operator_id in required_operators:
            assert operator_id in operators


def test_wizard_graph_adds_fps_reducer_when_fps_limit_is_set() -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="simple_stream",
        optional_parameters={
            "fps_limit": 7,
            "resize_mode": "contain",
            "bypass_mode": "auto",
            "writer_priority": 2,
        },
    )
    operators = _operator_ids(graph)
    assert "core.fps_reducer" in operators

    stream_config = _stream_config(graph)
    assert stream_config.get("writer_priority") == 2


def test_wizard_graph_can_gate_camera_source_by_stream_demand() -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        camera_source_id="sub",
        preset_id="simple_stream",
        optional_parameters={
            "demand_gate": True,
            "demand_gate_output_id": "hls_quad_grid",
            "demand_gate_quality_profile_id": "quad_grid",
        },
    )

    operators = _operator_ids(graph)
    assert "stream.demand_gate" in operators
    gate_config = _operator_config(graph, operator_id="stream.demand_gate")
    assert gate_config["transmission_id"] == "transmission_main"
    assert gate_config["output_id"] == "hls_quad_grid"
    assert gate_config["quality_profile_id"] == "quad_grid"

    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    assert {
        "from": {"node": "demand", "port": "out"},
        "to": {"node": "source", "port": "gate"},
        "maxsize": 1,
        "drop_policy": "drop_oldest",
    } in edges
    assert _streaming_meta(graph).get("demand_driven") is True

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_streaming_pipeline_operators(registry)
    PipelineGraphCompiler(registry).compile_pipeline(Pipeline(name="demand_gate_stream", graph=graph))


def test_motion_preset_has_fps_reducer_even_without_optional_parameters() -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="motion_gate_stream",
        optional_parameters=None,
    )
    operators = _operator_ids(graph)
    assert "core.fps_reducer" in operators


@pytest.mark.parametrize("preset_id", STREAMING_WIZARD_PRESETS)
def test_wizard_graph_continuous_stream_is_not_downstream_of_event_gates(preset_id: str) -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id=preset_id,  # type: ignore[arg-type]
        optional_parameters=None,
    )

    upstream = _upstream_operator_ids_to_stream(graph)
    assert "camera.motion_gate" not in upstream
    assert "vision.detect" not in upstream
    assert "vision.track" not in upstream


def test_wizard_graph_defaults_detection_to_annotate_and_tracking_to_events() -> None:
    detection_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="detection_stream",
        optional_parameters=None,
    )
    assert _operator_config(detection_graph, operator_id="vision.detect").get("emit_mode") == "annotate"
    assert _operator_config(detection_graph, operator_id="vision.detect").get("model_id") == "rfdetr_det_medium"

    tracking_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="tracking_stream",
        optional_parameters=None,
    )
    assert _operator_config(tracking_graph, operator_id="vision.detect").get("emit_mode") == "annotate"
    assert _operator_config(tracking_graph, operator_id="vision.detect").get("model_id") == "rfdetr_det_medium"
    assert _operator_config(tracking_graph, operator_id="vision.track").get("emit_mode") == "events"


def test_wizard_graph_event_gated_keeps_gate_upstream_of_stream() -> None:
    motion_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="motion_gate_stream",
        optional_parameters={"stream_behavior": "event_gated"},
    )
    assert _streaming_meta(motion_graph).get("stream_behavior") == "event_gated"
    assert "camera.motion_gate" in _upstream_operator_ids_to_stream(motion_graph)

    detection_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="detection_stream",
        optional_parameters={"stream_behavior": "event_gated"},
    )
    assert "vision.detect" in _upstream_operator_ids_to_stream(detection_graph)
    assert _operator_config(detection_graph, operator_id="vision.detect").get("emit_mode") == "filter"


def test_stream_publish_video_diagnostics_warn_for_event_gated_upstream() -> None:
    motion_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="motion_gate_stream",
        optional_parameters={"stream_behavior": "event_gated"},
    )
    assert "stream_publish_video_event_gated_motion" in _compile_alert_codes(motion_graph)

    detection_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="detection_stream",
        optional_parameters={"stream_behavior": "event_gated"},
    )
    assert "stream_publish_video_event_gated_detection" in _compile_alert_codes(detection_graph)

    tracking_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="tracking_stream",
        optional_parameters={"stream_behavior": "event_gated"},
    )
    assert "stream_publish_video_event_gated_tracking" in _compile_alert_codes(tracking_graph)


@pytest.mark.parametrize("preset_id", ["motion_gate_stream", "detection_stream", "tracking_stream"])
def test_stream_publish_video_diagnostics_do_not_warn_for_continuous_presets(preset_id: str) -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id=preset_id,  # type: ignore[arg-type]
        optional_parameters=None,
    )
    alert_codes = _compile_alert_codes(graph)
    assert not any(code.startswith("stream_publish_video_event_gated_") for code in alert_codes)


def test_wizard_graph_disables_yolo_filter_when_requested() -> None:
    detection_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="detection_stream",
        optional_parameters={"yolo_filter_enabled": False},
    )
    assert _operator_config(detection_graph, operator_id="vision.detect").get("emit_mode") == "annotate"

    tracking_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="tracking_stream",
        optional_parameters={"yolo_filter_enabled": False},
    )
    assert _operator_config(tracking_graph, operator_id="vision.detect").get("emit_mode") == "annotate"
    assert _operator_config(tracking_graph, operator_id="vision.track").get("emit_mode") == "annotate"


def test_wizard_graph_defaults_segmentation_to_rtmdet_ins_and_mask_publish() -> None:
    segmentation_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="segmentation_stream",
        optional_parameters=None,
    )

    assert _operator_config(segmentation_graph, operator_id="vision.segment_instances").get("model_id") == "rtmdet_ins_small"
    assert _operator_config(segmentation_graph, operator_id="vision.segment_instances").get("attach_mask_artifacts") is True
    assert _stream_config(segmentation_graph).get("input_artifact_name", "") == ""
