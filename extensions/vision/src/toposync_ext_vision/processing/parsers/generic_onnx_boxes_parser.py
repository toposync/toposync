from __future__ import annotations

import numpy as np

from typing import Any

from ...registry.manifests import ModelManifest
from ..contracts import DetectionObject, normalize_bbox01


def select_manifest_labels(manifest: ModelManifest) -> list[str]:
    return list(manifest.classes.resolved_labels())


def _safe_bbox01_from_source_pixels(
    bbox: tuple[float, float, float, float],
    *,
    source_width: int,
    source_height: int,
) -> tuple[float, float, float, float]:
    width = max(1, int(source_width))
    height = max(1, int(source_height))
    x1, y1, x2, y2 = bbox
    return normalize_bbox01((x1 / width, y1 / height, x2 / width, y2 / height))


def map_bbox_pixels_to_source_bbox01(
    bbox: tuple[float, float, float, float],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,
) -> tuple[float, float, float, float]:
    meta = preprocess_meta if isinstance(preprocess_meta, dict) else {}
    source_width = int(meta.get("source_width", 0) or 0)
    source_height = int(meta.get("source_height", 0) or 0)
    if source_width <= 0 or source_height <= 0:
        return _safe_bbox01_from_source_pixels(
            bbox,
            source_width=int(manifest.input.width),
            source_height=int(manifest.input.height),
        )

    scale_x = float(meta.get("scale_x", 1.0) or 1.0)
    scale_y = float(meta.get("scale_y", 1.0) or 1.0)
    offset_x = float(meta.get("offset_x", 0.0) or 0.0)
    offset_y = float(meta.get("offset_y", 0.0) or 0.0)
    x1, y1, x2, y2 = bbox
    if abs(scale_x) < 1e-9 or abs(scale_y) < 1e-9:
        return _safe_bbox01_from_source_pixels(
            bbox,
            source_width=source_width,
            source_height=source_height,
        )
    return _safe_bbox01_from_source_pixels(
        (
            (x1 - offset_x) / scale_x,
            (y1 - offset_y) / scale_y,
            (x2 - offset_x) / scale_x,
            (y2 - offset_y) / scale_y,
        ),
        source_width=source_width,
        source_height=source_height,
    )


def _select_output_tensor(
    outputs_by_name: dict[str, np.ndarray],
    manifest: ModelManifest,
) -> np.ndarray:
    preferred = str(manifest.postprocess.output_name or "").strip()
    if preferred and preferred in outputs_by_name:
        return np.asarray(outputs_by_name[preferred])
    if len(outputs_by_name) == 1:
        return np.asarray(next(iter(outputs_by_name.values())))
    for candidate in ("boxes", "detections", "output"):
        if candidate in outputs_by_name:
            return np.asarray(outputs_by_name[candidate])
    return np.asarray(next(iter(outputs_by_name.values())))


def _reshape_rows(tensor: np.ndarray) -> np.ndarray:
    array = np.asarray(tensor, dtype=np.float32)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 2:
        return array
    if array.ndim == 1 and array.size >= 6 and array.size % 6 == 0:
        return array.reshape(-1, 6)
    raise ValueError(f"Unsupported detection output shape: {tuple(array.shape)}")


def _normalize_output_bbox(
    bbox: tuple[float, float, float, float],
    manifest: ModelManifest,
    *,
    preprocess_meta: dict[str, Any] | None = None,
) -> tuple[float, float, float, float]:
    if manifest.postprocess.box_format == "xyxy_pixels":
        return map_bbox_pixels_to_source_bbox01(
            bbox,
            manifest=manifest,
            preprocess_meta=preprocess_meta,
        )
    return normalize_bbox01(bbox)


def parse_generic_onnx_boxes(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,
    categories: set[str] | None = None,
) -> list[DetectionObject]:
    tensor = _select_output_tensor(outputs_by_name, manifest)
    rows = _reshape_rows(tensor)
    labels = select_manifest_labels(manifest)
    detections: list[DetectionObject] = []
    for row in rows:
        if row.shape[0] < 6:
            continue
        x1, y1, x2, y2 = [float(row[index]) for index in range(4)]
        score = float(row[4])
        label_id = int(row[5])
        label = labels[label_id] if 0 <= label_id < len(labels) else f"class_{label_id}"
        if categories and label not in categories:
            continue
        detections.append(
            DetectionObject(
                label=label,
                label_id=label_id,
                score=score,
                bbox01=_normalize_output_bbox(
                    (x1, y1, x2, y2),
                    manifest,
                    preprocess_meta=preprocess_meta,
                ),
                model_id=manifest.model_id,
                metadata={"parser": "generic_onnx_boxes"},
            )
        )
    return detections
