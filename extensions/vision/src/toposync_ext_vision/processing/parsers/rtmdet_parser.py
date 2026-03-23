from __future__ import annotations

from typing import Any

import numpy as np

from ...registry.manifests import ModelManifest
from ..contracts import DetectionObject
from .generic_onnx_boxes_parser import map_bbox_pixels_to_source_bbox01, select_manifest_labels


def _select_output(
    outputs_by_name: dict[str, np.ndarray],
    *,
    preferred: str,
    fallback_names: tuple[str, ...],
    min_last_dim: int | None = None,
) -> np.ndarray | None:
    chosen = str(preferred or "").strip()
    if chosen and chosen in outputs_by_name:
        return np.asarray(outputs_by_name[chosen])
    for name in fallback_names:
        if name in outputs_by_name:
            return np.asarray(outputs_by_name[name])
    for value in outputs_by_name.values():
        array = np.asarray(value)
        if min_last_dim is not None and array.ndim >= 2 and int(array.shape[-1]) >= min_last_dim:
            return array
    if len(outputs_by_name) == 1:
        return np.asarray(next(iter(outputs_by_name.values())))
    return None


def _reshape_dets(tensor: np.ndarray) -> np.ndarray:
    array = np.asarray(tensor, dtype=np.float32)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2 or array.shape[1] < 5:
        raise ValueError(f"Unsupported RTMDet detection tensor shape: {tuple(array.shape)}")
    return array


def _reshape_labels(tensor: np.ndarray | None, *, expected_rows: int) -> np.ndarray | None:
    if tensor is None:
        return None
    array = np.asarray(tensor)
    if array.ndim == 2 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 1:
        raise ValueError(f"Unsupported RTMDet label tensor shape: {tuple(array.shape)}")
    if int(array.shape[0]) != int(expected_rows):
        raise ValueError(
            f"RTMDet dets/labels row mismatch: expected {expected_rows}, got {int(array.shape[0])}"
        )
    return array.astype(np.int64, copy=False)


def parse_rtmdet_outputs(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,
    categories: set[str] | None = None,
) -> list[DetectionObject]:
    det_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.output_name,
        fallback_names=("dets", "boxes", "bbox", "bboxes"),
        min_last_dim=5,
    )
    if det_tensor is None:
        raise ValueError("RTMDet parser could not find a detection tensor")
    det_rows = _reshape_dets(det_tensor)

    label_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.label_output_name,
        fallback_names=("labels", "label_ids", "classes"),
    )
    label_rows = _reshape_labels(label_tensor, expected_rows=int(det_rows.shape[0])) if label_tensor is not None else None
    labels = select_manifest_labels(manifest)

    detections: list[DetectionObject] = []
    for index, row in enumerate(det_rows):
        score = float(row[4])
        if score <= 0.0:
            continue
        if label_rows is not None:
            label_id = int(label_rows[index])
        elif row.shape[0] >= 6:
            label_id = int(row[5])
        else:
            label_id = -1
        label = labels[label_id] if 0 <= label_id < len(labels) else f"class_{label_id}"
        if categories and label not in categories:
            continue
        bbox01 = map_bbox_pixels_to_source_bbox01(
            (float(row[0]), float(row[1]), float(row[2]), float(row[3])),
            manifest=manifest,
            preprocess_meta=preprocess_meta,
        )
        detections.append(
            DetectionObject(
                label=label,
                label_id=label_id,
                score=score,
                bbox01=bbox01,
                model_id=manifest.model_id,
                metadata={"parser": "mmdet_rtmdet"},
            )
        )
    return detections
