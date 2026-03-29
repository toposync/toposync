from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Literal

import numpy as np
import onnx

from .manifests import ModelManifest, ModelRegistryError
from .model_store import import_custom_manifest


SupportedCustomOnnxTask = Literal["classification", "detection"]


def _default_data_dir() -> Path:
    raw = str(os.getenv("TOPOSYNC_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / ".toposync-data").resolve()


def default_custom_onnx_artifacts_dir(*, data_dir: str | Path | None = None) -> Path:
    base = Path(data_dir).expanduser().resolve() if data_dir is not None else _default_data_dir()
    return base / "vision-model-artifacts" / "custom"


def default_custom_onnx_staging_dir(*, data_dir: str | Path | None = None) -> Path:
    return default_custom_onnx_artifacts_dir(data_dir=data_dir) / "_staging"


def _safe_filename(value: str, *, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = Path(text).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return safe or fallback


def _slug_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return token or "model"


def _copy_stream(source: BinaryIO, target_path: Path) -> int:
    written = 0
    with target_path.open("wb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            written += len(chunk)
            handle.write(chunk)
    return written


def _tensor_dtype_name(elem_type: int) -> str:
    try:
        return str(onnx.TensorProto.DataType.Name(int(elem_type))).lower()
    except Exception:
        return str(int(elem_type))


def _value_info_shape(value_info: Any) -> list[int | str | None]:
    tensor_type = getattr(getattr(value_info, "type", None), "tensor_type", None)
    shape = getattr(tensor_type, "shape", None)
    dims = getattr(shape, "dim", []) if shape is not None else []
    out: list[int | str | None] = []
    for dim in dims:
        dim_value = int(getattr(dim, "dim_value", 0) or 0)
        dim_param = str(getattr(dim, "dim_param", "") or "").strip()
        if dim_value > 0:
            out.append(dim_value)
        elif dim_param:
            out.append(dim_param)
        else:
            out.append(None)
    return out


def _tensor_summary(value_info: Any) -> dict[str, Any]:
    tensor_type = getattr(getattr(value_info, "type", None), "tensor_type", None)
    dims = _value_info_shape(value_info)
    return {
        "name": str(getattr(value_info, "name", "") or "").strip(),
        "dtype": _tensor_dtype_name(int(getattr(tensor_type, "elem_type", 0) or 0)),
        "shape": dims,
        "rank": len(dims),
    }


def _guess_input_defaults(input_tensors: list[dict[str, Any]]) -> dict[str, Any]:
    primary = input_tensors[0] if input_tensors else {"name": "", "shape": []}
    shape = list(primary.get("shape") or [])
    layout = "nchw"
    width = 640
    height = 640
    channels = 3
    if len(shape) >= 4:
        if isinstance(shape[1], int) and int(shape[1]) in {1, 3, 4}:
            layout = "nchw"
            channels = int(shape[1])
            if isinstance(shape[2], int) and int(shape[2]) > 0:
                height = int(shape[2])
            if isinstance(shape[3], int) and int(shape[3]) > 0:
                width = int(shape[3])
        elif isinstance(shape[3], int) and int(shape[3]) in {1, 3, 4}:
            layout = "nhwc"
            channels = int(shape[3])
            if isinstance(shape[1], int) and int(shape[1]) > 0:
                height = int(shape[1])
            if isinstance(shape[2], int) and int(shape[2]) > 0:
                width = int(shape[2])
    return {
        "tensor_name": str(primary.get("name") or "").strip(),
        "layout": layout,
        "width": width,
        "height": height,
        "channels": channels,
        "color_order": "rgb",
        "resize_mode": "stretch",
        "rescale_factor": 1.0,
        "normalization_mean": [0.0] * max(1, channels),
        "normalization_std": [1.0] * max(1, channels),
    }


def _suggest_tasks(
    input_defaults: dict[str, Any],
    output_tensors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    primary_output = output_tensors[0] if output_tensors else {"name": "", "shape": []}
    shape = list(primary_output.get("shape") or [])
    rank = int(primary_output.get("rank") or len(shape))
    last_dim = shape[-1] if shape else None

    if rank in {1, 2} and isinstance(last_dim, int) and int(last_dim) >= 2:
        suggestions.append(
            {
                "task": "classification",
                "adapter_family": "image_classification_logits",
                "label": "Image classification",
                "reason": "The primary output looks like a ranked logits/probabilities vector.",
                "confidence": "high",
                "defaults": {
                    **input_defaults,
                    "output_name": str(primary_output.get("name") or "").strip(),
                    "box_format": "xyxy01",
                    "labels_count_hint": int(last_dim),
                },
            }
        )

    if rank in {2, 3} and isinstance(last_dim, int) and int(last_dim) >= 6:
        suggestions.append(
            {
                "task": "detection",
                "adapter_family": "generic_boxes",
                "label": "Object detection",
                "reason": "The primary output looks like rows of x1, y1, x2, y2, score, class_id.",
                "confidence": "high",
                "defaults": {
                    **input_defaults,
                    "output_name": str(primary_output.get("name") or "").strip(),
                    "box_format": "xyxy01",
                    "labels_count_hint": 0,
                },
            }
        )

    if suggestions:
        return suggestions

    return [
        {
            "task": "classification",
            "adapter_family": "image_classification_logits",
            "label": "Image classification",
            "reason": "No strong heuristic matched. This option works for logits/probabilities outputs.",
            "confidence": "low",
            "defaults": {
                **input_defaults,
                "output_name": str(primary_output.get("name") or "").strip(),
                "box_format": "xyxy01",
                "labels_count_hint": int(last_dim) if isinstance(last_dim, int) and int(last_dim) > 0 else 0,
            },
        },
        {
            "task": "detection",
            "adapter_family": "generic_boxes",
            "label": "Object detection",
            "reason": "Use this when the ONNX returns rows of boxes + score + class id.",
            "confidence": "low",
            "defaults": {
                **input_defaults,
                "output_name": str(primary_output.get("name") or "").strip(),
                "box_format": "xyxy01",
                "labels_count_hint": 0,
            },
        },
    ]


def inspect_custom_onnx_artifact(
    *,
    artifact_path: str,
    uploaded_filename: str = "",
) -> dict[str, Any]:
    path = Path(str(artifact_path or "")).expanduser().resolve()
    if not path.is_file():
        raise ModelRegistryError(f"ONNX artifact not found: {path}")
    if path.suffix.lower() != ".onnx":
        raise ModelRegistryError("The custom ONNX wizard only accepts .onnx files")

    model = onnx.load(str(path), load_external_data=False)
    graph = model.graph
    input_tensors = [_tensor_summary(item) for item in list(graph.input or [])]
    output_tensors = [_tensor_summary(item) for item in list(graph.output or [])]
    if not input_tensors:
        raise ModelRegistryError("ONNX model has no inputs")
    if not output_tensors:
        raise ModelRegistryError("ONNX model has no outputs")

    input_defaults = _guess_input_defaults(input_tensors)
    task_suggestions = _suggest_tasks(input_defaults, output_tensors)
    display_name = Path(uploaded_filename or path.name).stem.replace("_", " ").replace("-", " ").strip().title()
    display_name = display_name or "Custom ONNX Model"
    return {
        "artifact_path": str(path),
        "uploaded_filename": str(uploaded_filename or path.name).strip() or path.name,
        "file_size_bytes": int(path.stat().st_size),
        "suggested_display_name": display_name,
        "input_tensors": input_tensors,
        "output_tensors": output_tensors,
        "task_suggestions": task_suggestions,
        "supported_task_adapters": [
            {
                "task": "classification",
                "adapter_family": "image_classification_logits",
                "label": "Image classification",
            },
            {
                "task": "detection",
                "adapter_family": "generic_boxes",
                "label": "Object detection",
            },
        ],
    }


def stage_custom_onnx_upload(
    *,
    stream: BinaryIO,
    filename: str,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    staging_dir = default_custom_onnx_staging_dir(data_dir=data_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename, fallback="custom-model.onnx")
    if not safe_name.lower().endswith(".onnx"):
        raise ModelRegistryError("The custom ONNX wizard only accepts .onnx files")
    staged_path = staging_dir / f"{uuid.uuid4().hex}_{safe_name}"
    _copy_stream(stream, staged_path)
    return inspect_custom_onnx_artifact(
        artifact_path=str(staged_path),
        uploaded_filename=safe_name,
    )


def _normalize_float_list(values: list[float] | None, *, fallback: list[float]) -> list[float]:
    parsed: list[float] = []
    for item in list(values or []):
        try:
            parsed.append(float(item))
        except Exception:
            continue
    return parsed or list(fallback)


def _normalize_labels(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        label = str(raw or "").strip().lower()
        if not label or label in seen:
            continue
        out.append(label)
        seen.add(label)
    return out


def build_custom_onnx_manifest_payload(
    *,
    artifact_path: str,
    display_name: str,
    task: SupportedCustomOnnxTask,
    adapter_family: str,
    model_id: str = "",
    uploaded_filename: str = "",
    tensor_name: str = "",
    width: int = 640,
    height: int = 640,
    layout: str = "nchw",
    color_order: str = "rgb",
    resize_mode: str = "stretch",
    rescale_factor: float = 1.0,
    normalization_mean: list[float] | None = None,
    normalization_std: list[float] | None = None,
    output_name: str = "",
    box_format: str = "xyxy01",
    class_labels: list[str] | None = None,
    source_url: str = "",
    acquisition_mode: str = "guided_upload",
    artifact_source: str = "onnx_ready",
    guide_url: str = "",
    export_guide_url: str = "",
    checkpoint_url: str = "",
    builder_backend: str = "",
    supported_platforms: list[str] | None = None,
    explicit_consent_required: bool = False,
    provenance_origin: str = "custom_onnx_wizard",
    provenance_source_ref: str = "",
    code_license: str = "",
    weights_license: str = "",
    commercial_use_status: str = "",
    redistribution_allowed: bool = False,
    official_build_allowed: bool = False,
    notes: list[str] | None = None,
    imported_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_task = str(task or "").strip().lower()
    clean_adapter = str(adapter_family or "").strip().lower()
    if clean_task not in {"classification", "detection"}:
        raise ModelRegistryError(f"Unsupported custom ONNX task: {clean_task}")
    if clean_task == "classification" and clean_adapter != "image_classification_logits":
        raise ModelRegistryError(f"Unsupported classification adapter family: {clean_adapter}")
    if clean_task == "detection" and clean_adapter != "generic_boxes":
        raise ModelRegistryError(f"Unsupported detection adapter family: {clean_adapter}")

    name = str(display_name or "").strip()
    if not name:
        raise ModelRegistryError("display_name is required")
    resolved_model_id = str(model_id or "").strip().lower() or f"custom_{clean_task}_{_slug_token(name)}"
    channels = 3
    mean = _normalize_float_list(normalization_mean, fallback=[0.0] * channels)
    std = _normalize_float_list(normalization_std, fallback=[1.0] * channels)
    labels = _normalize_labels(class_labels)

    payload: dict[str, Any] = {
        "model_id": resolved_model_id,
        "display_name": name,
        "task": clean_task,
        "runtime": "onnxruntime",
        "artifact_format": "onnx",
        "artifact_path": str(Path(artifact_path).expanduser().resolve()),
        "input": {
            "width": max(1, int(width)),
            "height": max(1, int(height)),
            "layout": str(layout or "nchw").strip().lower() or "nchw",
            "color_order": str(color_order or "rgb").strip().lower() or "rgb",
            "resize_mode": str(resize_mode or "stretch").strip().lower() or "stretch",
            "rescale_factor": float(rescale_factor),
            "tensor_name": str(tensor_name or "").strip(),
            "normalization": {
                "mean": mean,
                "std": std,
            },
        },
        "postprocess": {
            "adapter_family": clean_adapter,
            "output_name": str(output_name or "").strip(),
            "box_format": "xyxy01" if clean_task == "classification" else str(box_format or "xyxy01").strip().lower(),
        },
        "classes": {
            "source": "custom",
            "labels": labels,
        },
        "license": {
            "code_license": str(code_license or "").strip(),
            "weights_license": str(weights_license or "").strip(),
            "commercial_use_status": str(commercial_use_status or "").strip(),
            "redistribution_allowed": bool(redistribution_allowed),
            "official_build_allowed": bool(official_build_allowed),
        },
        "acquisition": {
            "mode": str(acquisition_mode or "guided_upload").strip() or "guided_upload",
            "artifact_source": str(artifact_source or "onnx_ready").strip() or "onnx_ready",
            "guide_url": str(guide_url or "").strip(),
            "export_guide_url": str(export_guide_url or "").strip(),
            "source_url": str(source_url or "").strip(),
            "checkpoint_url": str(checkpoint_url or "").strip(),
            "builder_backend": str(builder_backend or "").strip(),
            "supported_platforms": [
                str(item or "").strip().lower()
                for item in list(supported_platforms or [])
                if str(item or "").strip()
            ],
            "explicit_consent_required": bool(explicit_consent_required),
        },
        "provenance": {
            "origin": str(provenance_origin or "").strip() or "custom_onnx_wizard",
            "source_url": str(source_url or "").strip(),
            "source_ref": str(provenance_source_ref or "").strip(),
            "source_file": str(uploaded_filename or Path(artifact_path).name).strip(),
            "imported_via": "custom_onnx_wizard",
            "imported_by": dict(imported_by or {}),
        },
        "notes": [str(item or "").strip() for item in list(notes or ["Generated by the TopoSync custom ONNX wizard."]) if str(item or "").strip()],
    }
    return payload


def _finalize_custom_artifact(
    *,
    source_path: Path,
    model_id: str,
    data_dir: str | Path | None = None,
) -> Path:
    target_dir = default_custom_onnx_artifacts_dir(data_dir=data_dir) / model_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{model_id}.onnx"
    if source_path.resolve() != target_path.resolve():
        shutil.copy2(source_path, target_path)
        staging_dir = default_custom_onnx_staging_dir(data_dir=data_dir)
        try:
            if staging_dir in source_path.resolve().parents:
                source_path.unlink(missing_ok=True)
        except Exception:
            pass
    return target_path


def import_custom_onnx_model(
    *,
    artifact_path: str,
    display_name: str,
    task: SupportedCustomOnnxTask,
    adapter_family: str,
    model_id: str = "",
    uploaded_filename: str = "",
    tensor_name: str = "",
    width: int = 640,
    height: int = 640,
    layout: str = "nchw",
    color_order: str = "rgb",
    resize_mode: str = "stretch",
    rescale_factor: float = 1.0,
    normalization_mean: list[float] | None = None,
    normalization_std: list[float] | None = None,
    output_name: str = "",
    box_format: str = "xyxy01",
    class_labels: list[str] | None = None,
    source_url: str = "",
    acquisition_mode: str = "guided_upload",
    artifact_source: str = "onnx_ready",
    guide_url: str = "",
    export_guide_url: str = "",
    checkpoint_url: str = "",
    builder_backend: str = "",
    supported_platforms: list[str] | None = None,
    explicit_consent_required: bool = False,
    provenance_origin: str = "custom_onnx_wizard",
    provenance_source_ref: str = "",
    code_license: str = "",
    weights_license: str = "",
    commercial_use_status: str = "",
    redistribution_allowed: bool = False,
    official_build_allowed: bool = False,
    notes: list[str] | None = None,
    replace_existing: bool = False,
    imported_by: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    draft = build_custom_onnx_manifest_payload(
        artifact_path=artifact_path,
        display_name=display_name,
        task=task,
        adapter_family=adapter_family,
        model_id=model_id,
        uploaded_filename=uploaded_filename,
        tensor_name=tensor_name,
        width=width,
        height=height,
        layout=layout,
        color_order=color_order,
        resize_mode=resize_mode,
        rescale_factor=rescale_factor,
        normalization_mean=normalization_mean,
        normalization_std=normalization_std,
        output_name=output_name,
        box_format=box_format,
        class_labels=class_labels,
        source_url=source_url,
        acquisition_mode=acquisition_mode,
        artifact_source=artifact_source,
        guide_url=guide_url,
        export_guide_url=export_guide_url,
        checkpoint_url=checkpoint_url,
        builder_backend=builder_backend,
        supported_platforms=supported_platforms,
        explicit_consent_required=explicit_consent_required,
        provenance_origin=provenance_origin,
        provenance_source_ref=provenance_source_ref,
        code_license=code_license,
        weights_license=weights_license,
        commercial_use_status=commercial_use_status,
        redistribution_allowed=redistribution_allowed,
        official_build_allowed=official_build_allowed,
        notes=notes,
        imported_by=imported_by,
    )
    source_path = Path(str(artifact_path or "")).expanduser().resolve()
    if not source_path.is_file():
        raise ModelRegistryError(f"ONNX artifact not found: {source_path}")
    final_artifact_path = _finalize_custom_artifact(
        source_path=source_path,
        model_id=str(draft["model_id"]),
        data_dir=data_dir,
    )
    draft["artifact_path"] = str(final_artifact_path)
    return import_custom_manifest(
        manifest_text=json.dumps(draft),
        data_dir=data_dir,
        replace_existing=bool(replace_existing),
        imported_by=dict(imported_by or {}),
        imported_via="custom_onnx_wizard",
    )


def _load_preview_image(data: bytes) -> np.ndarray:
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Image preview for the custom ONNX wizard requires Pillow") from exc
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(image, dtype=np.float32)


def preview_custom_onnx_model(
    *,
    image_bytes: bytes,
    artifact_path: str,
    display_name: str,
    task: SupportedCustomOnnxTask,
    adapter_family: str,
    uploaded_filename: str = "",
    tensor_name: str = "",
    width: int = 640,
    height: int = 640,
    layout: str = "nchw",
    color_order: str = "rgb",
    resize_mode: str = "stretch",
    rescale_factor: float = 1.0,
    normalization_mean: list[float] | None = None,
    normalization_std: list[float] | None = None,
    output_name: str = "",
    box_format: str = "xyxy01",
    class_labels: list[str] | None = None,
    source_url: str = "",
) -> dict[str, Any]:
    draft = build_custom_onnx_manifest_payload(
        artifact_path=artifact_path,
        display_name=display_name,
        task=task,
        adapter_family=adapter_family,
        uploaded_filename=uploaded_filename,
        tensor_name=tensor_name,
        width=width,
        height=height,
        layout=layout,
        color_order=color_order,
        resize_mode=resize_mode,
        rescale_factor=rescale_factor,
        normalization_mean=normalization_mean,
        normalization_std=normalization_std,
        output_name=output_name,
        box_format=box_format,
        class_labels=class_labels,
        source_url=source_url,
    )
    manifest = ModelManifest.model_validate(draft)
    frame = _load_preview_image(image_bytes)

    if task == "classification":
        from ..processing.runtime_backends import build_classifier_backend

        result = build_classifier_backend(manifest).classify(frame)
        return {
            "task": "classification",
            "summary": {
                "top_label": result.top_label.label if result.top_label is not None else None,
                "top_score": float(result.top_label.score) if result.top_label is not None else 0.0,
                "labels": [
                    {"label": item.label, "label_id": item.label_id, "score": float(item.score)}
                    for item in result.labels[:5]
                ],
            },
        }

    if task == "detection":
        from ..processing.runtime_backends import build_detector_backend

        detections = build_detector_backend(manifest).detect(frame)
        return {
            "task": "detection",
            "summary": {
                "count": len(detections),
                "detections": [
                    {
                        "label": item.label,
                        "label_id": item.label_id,
                        "score": float(item.score),
                        "bbox01": [float(value) for value in item.bbox01],
                    }
                    for item in detections[:5]
                ],
            },
        }

    raise ModelRegistryError(f"Unsupported preview task: {task}")
