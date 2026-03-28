from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from toposync_ext_vision.processing import get_last_benchmark
import toposync_ext_vision.processing.runtime_backends.onnxruntime_backend as ort_backend_mod
from toposync_ext_vision.processing.runtime_backends import (
    OnnxRuntimeDetectorBackend,
    available_onnxruntime_execution_providers,
    resolve_onnxruntime_execution_providers,
)
from toposync_ext_vision.registry import ModelManifest, build_default_model_registry


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
            "confidence_threshold_default": 0.4,
            "iou_threshold_default": 0.6,
        },
        "classes": {"source": "test", "labels": ["person"]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_onnxruntime_backend_runs_on_cpu_and_benchmarks(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    model_path = _write_constant_detection_model(tmp_path / "constant_detector.onnx")
    manifest_path = _write_manifest(tmp_path / "constant_detector.json", model_path)
    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("constant.detector")

    backend = OnnxRuntimeDetectorBackend(manifest)
    providers = available_onnxruntime_execution_providers()
    assert "CPUExecutionProvider" in providers
    assert "CPUExecutionProvider" in backend.providers

    frame = np.zeros((4, 4, 3), dtype=np.float32)
    detections = backend.detect(frame)
    assert len(detections) == 1
    assert detections[0].label == "person"
    assert detections[0].bbox01 == pytest.approx((0.1, 0.2, 0.4, 0.8), abs=1e-6)
    assert detections[0].score == pytest.approx(0.95, abs=1e-6)

    benchmark = backend.benchmark(frame=frame, iterations=3, warmup_runs=1)
    assert benchmark["model_id"] == "constant.detector"
    assert benchmark["iterations"] == 3
    assert benchmark["avg_latency_ms"] >= 0.0
    assert benchmark["p95_latency_ms"] >= 0.0
    assert get_last_benchmark() is not None

    loaded_manifest = ModelManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert loaded_manifest.model_id == "constant.detector"


def test_resolve_onnxruntime_execution_providers_defaults_to_cpu_first(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", raising=False)
    monkeypatch.setattr(ort_backend_mod, "_installed_onnxruntime_runtime_variant", lambda: "cpu")
    monkeypatch.setattr(
        ort_backend_mod,
        "available_onnxruntime_execution_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider", "OpenVINOExecutionProvider"],
    )

    assert resolve_onnxruntime_execution_providers() == [
        "CPUExecutionProvider",
        "OpenVINOExecutionProvider",
        "CUDAExecutionProvider",
    ]


def test_resolve_onnxruntime_execution_providers_prefers_directml_for_directml_bundle(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", raising=False)
    monkeypatch.setattr(ort_backend_mod, "_installed_onnxruntime_runtime_variant", lambda: "directml")
    monkeypatch.setattr(
        ort_backend_mod,
        "available_onnxruntime_execution_providers",
        lambda: ["DmlExecutionProvider", "CPUExecutionProvider"],
    )

    assert resolve_onnxruntime_execution_providers() == [
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_resolve_onnxruntime_execution_providers_prefers_cuda_for_gpu_bundle(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", raising=False)
    monkeypatch.setattr(ort_backend_mod, "_installed_onnxruntime_runtime_variant", lambda: "cuda")
    monkeypatch.setattr(
        ort_backend_mod,
        "available_onnxruntime_execution_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider", "TensorrtExecutionProvider"],
    )

    assert resolve_onnxruntime_execution_providers() == [
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_resolve_onnxruntime_execution_providers_honors_env_override(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", "CUDAExecutionProvider,CPUExecutionProvider")
    monkeypatch.setattr(ort_backend_mod, "_installed_onnxruntime_runtime_variant", lambda: "cpu")
    monkeypatch.setattr(
        ort_backend_mod,
        "available_onnxruntime_execution_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider", "OpenVINOExecutionProvider"],
    )

    assert resolve_onnxruntime_execution_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
