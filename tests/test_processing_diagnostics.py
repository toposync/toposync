from __future__ import annotations

import asyncio
import json
from pathlib import Path

import toposync_ext_vision.processing as vision_processing
from toposync.runtime.processing_diagnostics import collect_processing_server_diagnostics
from toposync.runtime.processing_diagnostics import collect_vision_extension_diagnostics
from toposync_ext_vision.processing.runtime_backends import OnnxRuntimeDetectorBackend
from toposync_ext_vision.registry import build_default_model_registry


def _write_constant_detection_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 4, 4])
    output_tensor = helper.make_tensor_value_info("boxes", TensorProto.FLOAT, [1, 1, 6])
    constant_boxes = helper.make_tensor(
        "constant_boxes",
        TensorProto.FLOAT,
        [1, 1, 6],
        [0.1, 0.2, 0.4, 0.8, 0.95, 0.0],
    )
    graph = helper.make_graph(
        [helper.make_node("Constant", inputs=[], outputs=["boxes"], value=constant_boxes)],
        "toposync_constant_detector",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_manifest(path: Path, model_path: Path) -> Path:
    payload = {
        "model_id": "constant.detector",
        "display_name": "Constant Detector",
        "task": "detection",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "images",
            "normalization": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        },
        "postprocess": {
            "type": "generic_boxes",
            "output_name": "boxes",
            "box_format": "xyxy01",
        },
        "classes": {"source": "test", "labels": ["person"]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_processing_diagnostics_exposes_vision_backends_models_and_benchmark(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    model_path = _write_constant_detection_model(tmp_path / "constant_detector.onnx")
    _write_manifest(tmp_path / "constant_detector.json", model_path)
    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

    registry = build_default_model_registry()
    backend = OnnxRuntimeDetectorBackend(registry.resolve_detector_manifest("constant.detector"))
    backend.benchmark(iterations=2, warmup_runs=0)

    diagnostics = asyncio.run(collect_processing_server_diagnostics())
    vision = diagnostics["vision"]
    assert any(item.get("id") == "onnxruntime" for item in vision["backends"])
    assert any("segmentation" in list(item.get("tasks") or []) for item in vision["backends"])
    assert any(item.get("id") == "simple_iou_kalman" for item in vision["trackers_available"])
    assert any(item.get("id") == "norfair" for item in vision["trackers_available"])
    assert "CPUExecutionProvider" in vision["execution_providers"]
    assert "CPUExecutionProvider" in vision["preferred_execution_providers"]
    assert isinstance(vision["runtime_upgrades"], dict)
    assert isinstance(vision["runtime_upgrades"].get("suggestions"), list)
    assert "yolo_device_recommended" not in vision
    assert any(item.get("model_id") == "constant.detector" for item in vision["models_installed"])
    assert any(isinstance(item.get("capabilities"), list) for item in vision["models_installed"])
    assert vision["model_registry_errors"] == []
    assert "detection" in vision["recommendations"]
    assert "segmentation" in vision["recommendations"]
    assert "pose" in vision["recommendations"]
    assert isinstance(vision["official_shortlists"].get("detection"), list)
    assert isinstance(vision["official_shortlists"].get("segmentation"), list)
    assert isinstance(vision["official_shortlists"].get("pose"), list)
    assert isinstance(vision["task_catalogs"].get("detection"), dict)
    assert isinstance(vision["task_catalogs"].get("segmentation"), dict)
    assert isinstance(vision["task_catalogs"].get("pose"), dict)
    assert isinstance(vision["local_builder"], dict)
    assert "supported" in vision["local_builder"]
    assert isinstance(vision["local_builder"].get("candidates"), list)
    detection_items = vision["task_catalogs"]["detection"].get("items")
    assert isinstance(detection_items, list)
    assert any(item.get("availability") in {"available", "manifest_only", "incompatible"} for item in detection_items)
    assert any(isinstance(item.get("capabilities"), list) for item in detection_items)
    assert any(item.get("acquisition_mode") in {"guided_upload", "auto_download", "local_build_assisted"} for item in detection_items)
    assert any(isinstance(item.get("acquisition"), dict) for item in detection_items)
    assert any("install_supported" in item for item in detection_items)
    assert any("install_reason" in item for item in detection_items)
    assert any("local_build_supported" in item for item in detection_items)
    assert any("local_build_reason" in item for item in detection_items)
    assert vision["task_catalogs"]["pose"].get("items") == []
    assert isinstance(vision["install_jobs"], list)
    assert isinstance(vision["last_benchmark"], dict)
    assert vision["last_benchmark"]["model_id"] == "constant.detector"
    assert "legacy_yolo" not in diagnostics["cameras"]


def test_collect_vision_extension_diagnostics_keeps_local_builder_on_failure(monkeypatch) -> None:  # noqa: ANN001
    def _raise(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(vision_processing, "collect_vision_diagnostics", _raise)
    diagnostics = collect_vision_extension_diagnostics()
    assert diagnostics["local_builder"] == {}
    assert diagnostics["install_jobs"] == []
    assert isinstance(diagnostics["runtime_upgrades"], dict)
    assert diagnostics["runtime_upgrades"]["suggestions"] == []
