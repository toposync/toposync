from __future__ import annotations

import pytest

from toposync_ext_streaming.wizard.pipeline_builder import (
    STREAMING_WIZARD_PRESETS,
    build_streaming_wizard_graph,
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
        "detection_stream": ["vision.object_detection_yolo"],
        "tracking_stream": ["vision.object_tracking_yolo"],
        "segmentation_stream": ["camera.object_segmentation"],
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


def test_wizard_graph_defaults_to_yolo_filter_enabled() -> None:
    for preset_id, operator_id in (
        ("detection_stream", "vision.object_detection_yolo"),
        ("tracking_stream", "vision.object_tracking_yolo"),
    ):
        graph = build_streaming_wizard_graph(
            transmission_id="transmission_main",
            camera_id="camera_a",
            preset_id=preset_id,  # type: ignore[arg-type]
            optional_parameters=None,
        )
        config = _operator_config(graph, operator_id=operator_id)
        assert config.get("emit_mode") == "events"


def test_wizard_graph_disables_yolo_filter_when_requested() -> None:
    for preset_id, operator_id in (
        ("detection_stream", "vision.object_detection_yolo"),
        ("tracking_stream", "vision.object_tracking_yolo"),
    ):
        graph = build_streaming_wizard_graph(
            transmission_id="transmission_main",
            camera_id="camera_a",
            preset_id=preset_id,  # type: ignore[arg-type]
            optional_parameters={"yolo_filter_enabled": False},
        )
        config = _operator_config(graph, operator_id=operator_id)
        assert config.get("emit_mode") == "annotate"
