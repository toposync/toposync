from __future__ import annotations

from typing import Any

import numpy as np

from ...registry.builtin_data import resolve_coco91_category_label
from ...registry.manifests import ModelManifest
from ..contracts import DetectionObject, normalize_bbox01
from .generic_onnx_boxes_parser import select_manifest_labels


def _select_output(outputs_by_name: dict[str, np.ndarray], preferred: str, *fallbacks: str) -> np.ndarray:
    clean = str(preferred or "").strip()
    if clean and clean in outputs_by_name:
        return np.asarray(outputs_by_name[clean], dtype=np.float32)
    for candidate in fallbacks:
        if candidate in outputs_by_name:
            return np.asarray(outputs_by_name[candidate], dtype=np.float32)
    return np.asarray(next(iter(outputs_by_name.values())), dtype=np.float32)


def _reshape_dets(array: np.ndarray) -> np.ndarray:
    value = np.asarray(array, dtype=np.float32)
    if value.ndim == 3 and value.shape[0] == 1:
        value = value[0]
    if value.ndim != 2 or value.shape[1] != 4:
        raise ValueError(f"Unsupported RF-DETR det tensor shape: {tuple(value.shape)}")
    return value


def _reshape_logits(array: np.ndarray, *, expected_rows: int) -> np.ndarray:
    value = np.asarray(array, dtype=np.float32)
    if value.ndim == 3 and value.shape[0] == 1:
        value = value[0]
    if value.ndim != 2:
        raise ValueError(f"Unsupported RF-DETR logits tensor shape: {tuple(value.shape)}")
    if int(value.shape[0]) != int(expected_rows):
        raise ValueError(
            f"RF-DETR dets/logits row mismatch: expected {expected_rows}, got {int(value.shape[0])}"
        )
    return value


def _sigmoid(array: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(array, dtype=np.float32), -80.0, 80.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _box_cxcywh_to_xyxy01(row: np.ndarray) -> tuple[float, float, float, float]:
    cx, cy, width, height = [float(value) for value in row[:4]]
    half_w = width / 2.0
    half_h = height / 2.0
    return normalize_bbox01((cx - half_w, cy - half_h, cx + half_w, cy + half_h))


def _normalize_rfdetr_bbox01(row: np.ndarray, manifest: ModelManifest) -> tuple[float, float, float, float]:
    box_format = str(manifest.postprocess.box_format or "cxcywh01").strip().lower()
    if box_format == "cxcywh01":
        return _box_cxcywh_to_xyxy01(row)
    return normalize_bbox01(tuple(float(value) for value in row[:4]))


def _select_label(label_id: int, *, manifest: ModelManifest, num_classes: int) -> str:
    if num_classes == 91 and str(manifest.classes.source or "").strip().lower() == "coco80":
        return resolve_coco91_category_label(label_id)
    labels = select_manifest_labels(manifest)
    return labels[label_id] if 0 <= label_id < len(labels) else f"class_{label_id}"


def parse_rfdetr_outputs(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,  # noqa: ARG001
    categories: set[str] | None = None,
) -> list[DetectionObject]:
    dets = _reshape_dets(_select_output(outputs_by_name, manifest.postprocess.output_name, "dets", "pred_boxes"))
    logits = _reshape_logits(
        _select_output(outputs_by_name, manifest.postprocess.label_output_name, "labels", "pred_logits"),
        expected_rows=int(dets.shape[0]),
    )
    probabilities = _sigmoid(logits)
    if probabilities.size == 0:
        return []

    num_queries, num_classes = probabilities.shape
    top_k = min(num_queries, probabilities.size)
    flat_scores = probabilities.reshape(-1)
    top_indices = np.argpartition(flat_scores, -top_k)[-top_k:]
    ordered_indices = top_indices[np.argsort(flat_scores[top_indices])[::-1]]

    detections: list[DetectionObject] = []
    for flat_index in ordered_indices:
        query_index = int(flat_index // max(1, num_classes))
        label_id = int(flat_index % max(1, num_classes))
        score = float(flat_scores[int(flat_index)])
        label = _select_label(label_id, manifest=manifest, num_classes=num_classes)
        if not label:
            continue
        if categories and label not in categories:
            continue
        detections.append(
            DetectionObject(
                label=label,
                label_id=label_id,
                score=score,
                bbox01=_normalize_rfdetr_bbox01(dets[query_index], manifest),
                model_id=manifest.model_id,
                metadata={"parser": "rfdetr_detr"},
            )
        )
    return detections
