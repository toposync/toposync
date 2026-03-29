from __future__ import annotations

import os
import platform
import statistics
import time
from importlib import metadata
from typing import Any

import numpy as np

from ...registry.manifests import ModelManifest
from ..contracts import DetectionObject, ImageClassificationResult, SegmentationInstance
from ..parsers import (
    parse_generic_onnx_boxes,
    parse_image_classification_logits,
    parse_rfdetr_outputs,
    parse_rtmdet_ins_outputs,
    parse_rtmdet_outputs,
)


def _import_onnxruntime():
    try:
        import onnxruntime as ort  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "onnxruntime is not installed. Install the first-party vision runtime dependencies."
        ) from exc
    return ort


def available_onnxruntime_execution_providers() -> list[str]:
    try:
        ort = _import_onnxruntime()
        return list(ort.get_available_providers())
    except Exception:
        return []


def _configured_onnxruntime_execution_providers() -> list[str] | None:
    raw = str(os.getenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS") or "").strip()
    if not raw:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        provider = str(item or "").strip()
        if not provider or provider in seen:
            continue
        out.append(provider)
        seen.add(provider)
    return out or None


def _installed_onnxruntime_runtime_variant() -> str:
    package_names: list[str] = []
    for package_name in ("onnxruntime-directml", "onnxruntime-gpu", "onnxruntime"):
        try:
            metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
        package_names.append(package_name)
    if "onnxruntime-directml" in package_names and platform.system().strip().lower() == "windows":
        return "directml"
    if "onnxruntime-gpu" in package_names:
        return "cuda"
    if "onnxruntime" in package_names:
        return "cpu"
    return "unknown"


def _default_onnxruntime_execution_providers() -> list[str]:
    variant = _installed_onnxruntime_runtime_variant()
    if variant == "directml":
        return [
            "DmlExecutionProvider",
            "CPUExecutionProvider",
        ]
    if variant == "cuda":
        return [
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
    return [
        "CPUExecutionProvider",
        "DmlExecutionProvider",
        "OpenVINOExecutionProvider",
        "CoreMLExecutionProvider",
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
    ]


def resolve_onnxruntime_execution_providers(preferred: list[str] | None = None) -> list[str]:
    available = available_onnxruntime_execution_providers()
    if not available:
        return []
    ordered = preferred or _configured_onnxruntime_execution_providers() or _default_onnxruntime_execution_providers()
    selected = [provider for provider in ordered if provider in available]
    if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in selected:
        selected.append("CPUExecutionProvider")
    return selected or available


def _resize_image_hwc(image: np.ndarray, *, width: int, height: int) -> np.ndarray:
    src_h, src_w = image.shape[:2]
    if src_h == height and src_w == width:
        return image
    y_index = np.clip(
        np.round(np.linspace(0, max(0, src_h - 1), num=height)).astype(np.int64),
        0,
        max(0, src_h - 1),
    )
    x_index = np.clip(
        np.round(np.linspace(0, max(0, src_w - 1), num=width)).astype(np.int64),
        0,
        max(0, src_w - 1),
    )
    return image[y_index][:, x_index]


def _frame_to_hwc_array(frame: Any) -> np.ndarray:
    array = np.asarray(frame)
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    if array.ndim != 3:
        raise ValueError(f"Expected HWC frame, got shape {tuple(array.shape)}")
    if array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    elif array.shape[2] >= 4:
        array = array[:, :, :3]
    return np.asarray(array, dtype=np.float32)


def _normalize_image(array: np.ndarray, manifest: ModelManifest) -> np.ndarray:
    rescale_factor = float(getattr(manifest.input, "rescale_factor", 1.0) or 1.0)
    if rescale_factor != 1.0:
        array = np.asarray(array, dtype=np.float32) * rescale_factor
    channels = array.shape[2]
    mean_values = manifest.input.normalization.mean or [0.0] * channels
    std_values = manifest.input.normalization.std or [1.0] * channels
    mean = np.asarray(mean_values, dtype=np.float32).reshape(1, 1, channels)
    std = np.asarray(std_values, dtype=np.float32).reshape(1, 1, channels)
    std = np.where(np.abs(std) < 1e-6, 1.0, std)
    return (array - mean) / std


def _normalize_color_order(image: np.ndarray, manifest: ModelManifest) -> np.ndarray:
    color_order = str(manifest.input.color_order or "rgb").strip().lower()
    if color_order == "rgb":
        return image[:, :, ::-1]
    if color_order == "bgr":
        return image
    raise ValueError(f"Unsupported ONNX input color_order: {manifest.input.color_order}")


def _resize_image_for_manifest(
    image: np.ndarray,
    manifest: ModelManifest,
) -> tuple[np.ndarray, dict[str, Any]]:
    source_height, source_width = image.shape[:2]
    target_width = max(1, int(manifest.input.width))
    target_height = max(1, int(manifest.input.height))
    resize_mode = str(manifest.input.resize_mode or "stretch").strip().lower()
    pad_value = float(manifest.input.pad_value)

    if resize_mode == "letterbox":
        scale = min(target_width / max(1, source_width), target_height / max(1, source_height))
        resized_width = max(1, int(round(source_width * scale)))
        resized_height = max(1, int(round(source_height * scale)))
        resized = _resize_image_hwc(image, width=resized_width, height=resized_height)
        canvas = np.full((target_height, target_width, resized.shape[2]), pad_value, dtype=np.float32)
        offset_x = max(0, (target_width - resized_width) // 2)
        offset_y = max(0, (target_height - resized_height) // 2)
        canvas[offset_y : offset_y + resized_height, offset_x : offset_x + resized_width] = resized
        return canvas, {
            "source_width": int(source_width),
            "source_height": int(source_height),
            "input_width": int(target_width),
            "input_height": int(target_height),
            "resized_width": int(resized_width),
            "resized_height": int(resized_height),
            "offset_x": float(offset_x),
            "offset_y": float(offset_y),
            "scale_x": float(scale),
            "scale_y": float(scale),
            "resize_mode": "letterbox",
        }

    resized = _resize_image_hwc(image, width=target_width, height=target_height)
    return resized, {
        "source_width": int(source_width),
        "source_height": int(source_height),
        "input_width": int(target_width),
        "input_height": int(target_height),
        "resized_width": int(target_width),
        "resized_height": int(target_height),
        "offset_x": 0.0,
        "offset_y": 0.0,
        "scale_x": float(target_width / max(1, source_width)),
        "scale_y": float(target_height / max(1, source_height)),
        "resize_mode": "stretch",
    }


def prepare_onnx_input(frame: Any, manifest: ModelManifest) -> tuple[np.ndarray, dict[str, Any]]:
    image = _frame_to_hwc_array(frame)
    image = _normalize_color_order(image, manifest)
    image, preprocess_meta = _resize_image_for_manifest(image, manifest)
    image = _normalize_image(image, manifest)
    layout = str(manifest.input.layout or "nchw").strip().lower()
    if layout == "nchw":
        return np.transpose(image, (2, 0, 1))[None, ...].astype(np.float32), preprocess_meta
    if layout == "nhwc":
        return image[None, ...].astype(np.float32), preprocess_meta
    raise ValueError(f"Unsupported ONNX input layout: {manifest.input.layout}")


def build_benchmark_input(manifest: ModelManifest) -> np.ndarray:
    height = max(1, int(manifest.input.height))
    width = max(1, int(manifest.input.width))
    return np.zeros((height, width, 3), dtype=np.float32)


class _OnnxRuntimeSessionBackend:
    backend_id = "onnxruntime"

    def __init__(
        self,
        manifest: ModelManifest,
        *,
        task: str,
        supported_postprocess: set[str],
    ) -> None:
        if manifest.runtime != "onnxruntime":
            raise RuntimeError(
                f"{self.__class__.__name__} only supports runtime=onnxruntime, got {manifest.runtime!r}"
            )
        if manifest.task != task:
            raise RuntimeError(
                f"{self.__class__.__name__} only supports task={task!r}, got {manifest.task!r}"
            )
        adapter_family = manifest.resolved_adapter_family()
        if adapter_family not in supported_postprocess:
            raise RuntimeError(
                f"Unsupported ONNX {task} parser for this phase: {adapter_family or manifest.postprocess.type!r}"
            )

        ort = _import_onnxruntime()
        model_path = manifest.resolve_artifact_path()
        if not model_path.is_file():
            raise FileNotFoundError(f"ONNX model artifact not found: {model_path}")

        self._manifest = manifest
        self._ort = ort
        self._model_path = model_path
        self._providers = resolve_onnxruntime_execution_providers()
        if not self._providers:
            raise RuntimeError("No ONNX Runtime execution providers are available")
        self._session = ort.InferenceSession(str(model_path), providers=self._providers)
        session_inputs = self._session.get_inputs()
        if not session_inputs:
            raise RuntimeError("ONNX model has no inputs")
        self._input_name = str(manifest.input.tensor_name or session_inputs[0].name)

    @property
    def providers(self) -> list[str]:
        return list(self._session.get_providers())

    def _run_outputs(self, frame: Any) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        tensor, preprocess_meta = prepare_onnx_input(frame, self._manifest)
        outputs = self._session.run(None, {self._input_name: tensor})
        outputs_by_name = {
            str(meta.name or f"output_{index}"): np.asarray(value)
            for index, (meta, value) in enumerate(
                zip(self._session.get_outputs(), outputs, strict=False)
            )
        }
        return outputs_by_name, preprocess_meta

    def benchmark(
        self,
        *,
        frame: Any | None = None,
        iterations: int = 5,
        warmup_runs: int = 1,
    ) -> dict[str, Any]:
        sample = build_benchmark_input(self._manifest) if frame is None else frame
        for _ in range(max(0, int(warmup_runs))):
            self._run_outputs(sample)

        samples_ms: list[float] = []
        total_iterations = max(1, int(iterations))
        for _ in range(total_iterations):
            started_ns = time.perf_counter_ns()
            self._run_outputs(sample)
            samples_ms.append((time.perf_counter_ns() - started_ns) / 1_000_000.0)

        p95_ms = max(samples_ms)
        if len(samples_ms) > 1:
            try:
                p95_ms = statistics.quantiles(samples_ms, n=20)[-1]
            except Exception:
                p95_ms = max(samples_ms)

        result = {
            "backend_id": self.backend_id,
            "model_id": self._manifest.model_id,
            "runtime": self._manifest.runtime,
            "providers": self.providers,
            "iterations": total_iterations,
            "avg_latency_ms": float(statistics.mean(samples_ms)),
            "min_latency_ms": float(min(samples_ms)),
            "max_latency_ms": float(max(samples_ms)),
            "p95_latency_ms": float(p95_ms),
            "input_size": {
                "width": int(self._manifest.input.width),
                "height": int(self._manifest.input.height),
            },
        }
        from ..diagnostics import record_last_benchmark

        record_last_benchmark(result)
        return result


class OnnxRuntimeDetectorBackend(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest: ModelManifest) -> None:
        super().__init__(
            manifest,
            task="detection",
            supported_postprocess={"", "generic_boxes", "mmdet_rtmdet", "rfdetr_detr"},
        )

    def detect(
        self,
        frame: Any,
        *,
        categories: set[str] | None = None,
    ) -> list[DetectionObject]:
        outputs_by_name, preprocess_meta = self._run_outputs(frame)
        adapter_family = self._manifest.resolved_adapter_family()
        if adapter_family in {"", "generic_boxes"}:
            return parse_generic_onnx_boxes(
                outputs_by_name,
                manifest=self._manifest,
                preprocess_meta=preprocess_meta,
                categories=categories,
            )
        if adapter_family == "mmdet_rtmdet":
            return parse_rtmdet_outputs(
                outputs_by_name,
                manifest=self._manifest,
                preprocess_meta=preprocess_meta,
                categories=categories,
            )
        if adapter_family == "rfdetr_detr":
            return parse_rfdetr_outputs(
                outputs_by_name,
                manifest=self._manifest,
                preprocess_meta=preprocess_meta,
                categories=categories,
            )
        raise RuntimeError(f"Unsupported ONNX detection parser for this phase: {adapter_family!r}")


class OnnxRuntimeSegmentationBackend(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest: ModelManifest) -> None:
        super().__init__(
            manifest,
            task="segmentation",
            supported_postprocess={"mmdet_rtmdet_ins"},
        )

    def segment(
        self,
        frame: Any,
        *,
        detections: list[DetectionObject] | None = None,  # noqa: ARG002
        categories: set[str] | None = None,
    ) -> list[SegmentationInstance]:
        outputs_by_name, preprocess_meta = self._run_outputs(frame)
        adapter_family = self._manifest.resolved_adapter_family()
        if adapter_family == "mmdet_rtmdet_ins":
            return parse_rtmdet_ins_outputs(
                outputs_by_name,
                manifest=self._manifest,
                preprocess_meta=preprocess_meta,
                categories=categories,
            )
        raise RuntimeError(
            f"Unsupported ONNX segmentation parser for this phase: {adapter_family!r}"
        )


class OnnxRuntimeClassificationBackend(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest: ModelManifest) -> None:
        super().__init__(
            manifest,
            task="classification",
            supported_postprocess={"image_classification_logits"},
        )

    def classify(
        self,
        frame: Any,
    ) -> ImageClassificationResult:
        outputs_by_name, _preprocess_meta = self._run_outputs(frame)
        adapter_family = self._manifest.resolved_adapter_family()
        if adapter_family == "image_classification_logits":
            return parse_image_classification_logits(
                outputs_by_name,
                manifest=self._manifest,
            )
        raise RuntimeError(
            f"Unsupported ONNX classification parser for this phase: {adapter_family!r}"
        )


def build_detector_backend(manifest: ModelManifest) -> OnnxRuntimeDetectorBackend:
    if manifest.runtime == "onnxruntime":
        return OnnxRuntimeDetectorBackend(manifest)
    raise RuntimeError(f"Unsupported detector runtime: {manifest.runtime}")


def build_segmenter_backend(manifest: ModelManifest) -> OnnxRuntimeSegmentationBackend:
    if manifest.runtime == "onnxruntime":
        return OnnxRuntimeSegmentationBackend(manifest)
    raise RuntimeError(f"Unsupported segmenter runtime: {manifest.runtime}")


def build_classifier_backend(manifest: ModelManifest) -> OnnxRuntimeClassificationBackend:
    if manifest.runtime == "onnxruntime":
        return OnnxRuntimeClassificationBackend(manifest)
    raise RuntimeError(f"Unsupported classifier runtime: {manifest.runtime}")


def build_pose_backend(manifest: ModelManifest):
    if manifest.task != "pose":
        raise RuntimeError(f"build_pose_backend only supports task='pose', got {manifest.task!r}")
    if manifest.runtime != "onnxruntime":
        raise RuntimeError(f"Unsupported pose runtime: {manifest.runtime}")
    raise RuntimeError(
        "vision.pose_estimate is scaffolded, but no first-party pose backend is enabled yet."
    )
