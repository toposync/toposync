from __future__ import annotations

import pytest

from toposync_ext_streaming.wizard.pipeline_builder import (
    STREAMING_WIZARD_PRESETS,
    build_streaming_wizard_graph,
    suggested_streaming_wizard_pipeline_name,
)


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


def test_motion_preset_has_fps_reducer_even_without_optional_parameters() -> None:
    graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="motion_gate_stream",
        optional_parameters=None,
    )
    operators = _operator_ids(graph)
    assert "core.fps_reducer" in operators


def test_wizard_graph_defaults_detection_to_filter_and_tracking_to_events() -> None:
    detection_graph = build_streaming_wizard_graph(
        transmission_id="transmission_main",
        camera_id="camera_a",
        preset_id="detection_stream",
        optional_parameters=None,
    )
    assert _operator_config(detection_graph, operator_id="vision.detect").get("emit_mode") == "filter"
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
