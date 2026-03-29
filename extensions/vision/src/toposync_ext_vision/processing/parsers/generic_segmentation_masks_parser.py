from __future__ import annotations

from typing import Any

import numpy as np

from ...registry.manifests import ModelManifest
from ..contracts import SegmentationInstance, normalize_bbox01
from .generic_onnx_boxes_parser import map_bbox_pixels_to_source_bbox01, select_manifest_labels
from .rtmdet_ins_parser import _project_mask_to_source, _reshape_masks


def _select_output(
    outputs_by_name: dict[str, np.ndarray],
    *,
    preferred: str,
    fallback_names: tuple[str, ...],
    min_last_dim: int = 0,
    fallback_to_any: bool = True,
) -> np.ndarray | None:
    preferred_name = str(preferred or "").strip()
    if preferred_name and preferred_name in outputs_by_name:
        return np.asarray(outputs_by_name[preferred_name])
    for candidate in fallback_names:
        if candidate not in outputs_by_name:
            continue
        tensor = np.asarray(outputs_by_name[candidate])
        if min_last_dim > 0 and tensor.ndim > 0 and int(tensor.shape[-1] or 0) < min_last_dim:
            continue
        return tensor
    if fallback_to_any:
        for value in outputs_by_name.values():
            tensor = np.asarray(value)
            if min_last_dim > 0 and tensor.ndim > 0 and int(tensor.shape[-1] or 0) < min_last_dim:
                continue
            return tensor
    return None


def _reshape_rows(tensor: np.ndarray | None) -> np.ndarray:
    if tensor is None:
        raise ValueError("Generic segmentation parser could not find a detection tensor")
    array = np.asarray(tensor, dtype=np.float32)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 2:
        return array
    raise ValueError(f"Unsupported generic segmentation detection tensor shape: {tuple(array.shape)}")


def _reshape_labels(tensor: np.ndarray | None, *, expected_rows: int) -> np.ndarray | None:
    if tensor is None:
        return None
    array = np.asarray(tensor)
    if array.ndim == 2 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 2 and array.shape[1] == 1:
        array = array[:, 0]
    if array.ndim != 1:
        raise ValueError(f"Unsupported generic segmentation label tensor shape: {tuple(array.shape)}")
    if int(array.shape[0]) != int(expected_rows):
        raise ValueError(
            f"Generic segmentation dets/labels row mismatch: expected {expected_rows}, got {int(array.shape[0])}"
        )
    return np.asarray(array, dtype=np.int64)


def _normalize_output_bbox(
    bbox: tuple[float, float, float, float],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None,
) -> tuple[float, float, float, float]:
    if manifest.postprocess.box_format == "xyxy_pixels":
        return map_bbox_pixels_to_source_bbox01(
            bbox,
            manifest=manifest,
            preprocess_meta=preprocess_meta,
        )
    return normalize_bbox01(bbox)


def parse_generic_segmentation_masks(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,
    categories: set[str] | None = None,
) -> list[SegmentationInstance]:
    det_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.output_name,
        fallback_names=("dets", "boxes", "bbox", "bboxes", "detections"),
        min_last_dim=5,
    )
    det_rows = _reshape_rows(det_tensor)

    label_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.label_output_name,
        fallback_names=("labels", "label_ids", "classes"),
        fallback_to_any=False,
    )
    label_rows = _reshape_labels(label_tensor, expected_rows=int(det_rows.shape[0])) if label_tensor is not None else None

    mask_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.mask_output_name,
        fallback_names=("masks", "mask", "segm", "segms"),
        fallback_to_any=False,
    )
    mask_rows = _reshape_masks(mask_tensor, expected_rows=int(det_rows.shape[0]))
    labels = select_manifest_labels(manifest)

    instances: list[SegmentationInstance] = []
    for index, row in enumerate(det_rows):
        if row.shape[0] < 5:
            continue
        score = float(row[4])
        if score <= 0.0:
            continue
        if label_rows is not None:
            label_id = int(label_rows[index])
        elif row.shape[0] >= 6:
            label_id = int(row[5])
        else:
            label_id = index
        label = labels[label_id] if 0 <= label_id < len(labels) else f"class_{label_id}"
        if categories and label not in categories:
            continue
        bbox_values = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        source_mask = _project_mask_to_source(
            mask_rows[index],
            manifest=manifest,
            preprocess_meta=preprocess_meta,
            bbox_pixels=bbox_values,
        )
        instances.append(
            SegmentationInstance(
                label=label,
                label_id=label_id,
                score=score,
                bbox01=_normalize_output_bbox(
                    bbox_values,
                    manifest=manifest,
                    preprocess_meta=preprocess_meta,
                ),
                mask_artifact_name=f"mask_{index}",
                model_id=manifest.model_id,
                metadata={
                    "parser": "generic_segmentation_masks",
                    "_mask": source_mask,
                },
            )
        )
    return instances
