from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from toposync_ext_vision.processing import get_last_benchmark
import toposync_ext_vision.processing.runtime_backends.onnxruntime_backend as ort_backend_mod
from toposync_ext_vision.processing.runtime_backends import (
    OnnxRuntimeClassificationBackend,
    OnnxRuntimeDetectorBackend,
    OnnxRuntimeSegmentationBackend,
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


def _write_constant_classification_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, [1, 3, 4, 4])
    output_tensor = helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 2])
    constant_logits = helper.make_tensor("constant_logits", TensorProto.FLOAT, [1, 2], [0.1, 2.4])
    graph = helper.make_graph(
        [helper.make_node("Constant", inputs=[], outputs=["logits"], value=constant_logits)],
        "toposync_constant_classifier",
        [input_tensor],
        [output_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_constant_segmentation_model(path: Path) -> Path:
    import onnx
    from onnx import TensorProto, helper

    input_tensor = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 4, 4])
    boxes_tensor = helper.make_tensor_value_info("boxes", TensorProto.FLOAT, [1, 1, 6])
    masks_tensor = helper.make_tensor_value_info("masks", TensorProto.FLOAT, [1, 1, 4, 4])
    constant_boxes = helper.make_tensor(
        "constant_boxes",
        TensorProto.FLOAT,
        [1, 1, 6],
        [0.1, 0.2, 0.8, 0.9, 0.95, 0.0],
    )
    constant_masks = helper.make_tensor(
        "constant_masks",
        TensorProto.FLOAT,
        [1, 1, 4, 4],
        [
            0.0,
            1.0,
            1.0,
            0.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            0.0,
            1.0,
            1.0,
            0.0,
        ],
    )
    graph = helper.make_graph(
        [
            helper.make_node("Constant", inputs=[], outputs=["boxes"], value=constant_boxes),
            helper.make_node("Constant", inputs=[], outputs=["masks"], value=constant_masks),
        ],
        "toposync_constant_segmenter",
        [input_tensor],
        [boxes_tensor, masks_tensor],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return path


def _write_classification_manifest(path: Path, model_path: Path) -> Path:
    payload = {
        "model_id": "constant.classifier",
        "display_name": "Constant Classifier",
        "task": "classification",
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(model_path),
        "input": {
            "width": 4,
            "height": 4,
            "layout": "nchw",
            "color_order": "rgb",
            "tensor_name": "pixel_values",
            "normalization": {"mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        },
        "postprocess": {
            "adapter_family": "image_classification_logits",
            "output_name": "logits",
        },
        "classes": {"source": "test", "labels": ["normal", "nsfw"]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_segmentation_manifest(path: Path, model_path: Path) -> Path:
    payload = {
        "model_id": "constant.segmenter",
        "display_name": "Constant Segmenter",
        "task": "segmentation",
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
            "adapter_family": "generic_segmentation_masks",
            "output_name": "boxes",
            "mask_output_name": "masks",
            "box_format": "xyxy01",
            "mask_format": "full_frame_binary",
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


def test_prepare_onnx_input_applies_rescale_factor_before_normalization() -> None:
    manifest = ModelManifest.model_validate(
        {
            "model_id": "rescale.detector",
            "display_name": "Rescale Detector",
            "task": "detection",
            "runtime": "onnxruntime",
            "artifact_format": "onnx",
            "artifact_path": "/tmp/rescale.detector.onnx",
            "input": {
                "width": 1,
                "height": 1,
                "layout": "nchw",
                "color_order": "bgr",
                "rescale_factor": 1.0 / 255.0,
                "normalization": {"mean": [0.5, 0.5, 0.5], "std": [0.5, 0.5, 0.5]},
            },
            "postprocess": {"type": "generic_boxes"},
        }
    )

    tensor, meta = ort_backend_mod.prepare_onnx_input(np.asarray([[[255.0, 127.5, 0.0]]], dtype=np.float32), manifest)

    assert tensor.shape == (1, 3, 1, 1)
    assert meta["input_width"] == 1
    assert tensor[0, 0, 0, 0] == pytest.approx(1.0, abs=1e-6)
    assert tensor[0, 1, 0, 0] == pytest.approx(0.0, abs=1e-6)
    assert tensor[0, 2, 0, 0] == pytest.approx(-1.0, abs=1e-6)


def test_onnxruntime_classification_backend_runs_and_normalizes_logits(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    model_path = _write_constant_classification_model(tmp_path / "constant_classifier.onnx")
    _write_classification_manifest(tmp_path / "constant_classifier.json", model_path)
    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

    registry = build_default_model_registry()
    manifest = registry.resolve_classifier_manifest("constant.classifier")

    backend = OnnxRuntimeClassificationBackend(manifest)
    frame = np.zeros((4, 4, 3), dtype=np.float32)
    result = backend.classify(frame)

    assert result.top_label is not None
    assert result.top_label.label == "nsfw"
    assert result.top_label.score == pytest.approx(0.908877, abs=1e-5)
    assert [item.label for item in result.labels] == ["nsfw", "normal"]


def test_onnxruntime_segmentation_backend_runs_generic_masks(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    model_path = _write_constant_segmentation_model(tmp_path / "constant_segmenter.onnx")
    _write_segmentation_manifest(tmp_path / "constant_segmenter.json", model_path)
    monkeypatch.setenv("TOPOSYNC_VISION_MANIFESTS_DIR", str(tmp_path))

    registry = build_default_model_registry()
    manifest = registry.resolve_segmenter_manifest("constant.segmenter")

    backend = OnnxRuntimeSegmentationBackend(manifest)
    frame = np.zeros((4, 4, 3), dtype=np.float32)
    result = backend.segment(frame)

    assert len(result) == 1
    assert result[0].label == "person"
    assert result[0].mask_artifact_name == "mask_0"
    assert result[0].bbox01 == pytest.approx((0.1, 0.2, 0.8, 0.9), abs=1e-6)
    assert result[0].metadata["parser"] == "generic_segmentation_masks"


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
