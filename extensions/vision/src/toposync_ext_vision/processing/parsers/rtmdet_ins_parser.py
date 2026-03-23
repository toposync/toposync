from __future__ import annotations

from typing import Any

import numpy as np

from ...registry.manifests import ModelManifest
from ..contracts import SegmentationInstance
from .generic_onnx_boxes_parser import map_bbox_pixels_to_source_bbox01, select_manifest_labels
from .rtmdet_parser import _reshape_dets, _reshape_labels, _select_output


def _resize_mask_nearest(mask: np.ndarray, *, width: int, height: int) -> np.ndarray:
    src_h, src_w = mask.shape[:2]
    if src_h == height and src_w == width:
        return mask
    if height <= 0 or width <= 0:
        return np.zeros((max(0, height), max(0, width)), dtype=mask.dtype)
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
    return mask[y_index][:, x_index]


def _reshape_masks(
    tensor: np.ndarray | None,
    *,
    expected_rows: int,
) -> np.ndarray:
    if tensor is None:
        raise ValueError("RTMDet-Ins parser could not find a mask tensor")
    array = np.asarray(tensor)
    if array.ndim == 4 and array.shape[0] == 1:
        array = array[0]
    if array.ndim == 5 and array.shape[0] == 1 and array.shape[2] == 1:
        array = array[0, :, 0]
    if array.ndim != 3:
        raise ValueError(f"Unsupported RTMDet-Ins mask tensor shape: {tuple(array.shape)}")
    if int(array.shape[0]) != int(expected_rows):
        raise ValueError(
            f"RTMDet-Ins dets/masks row mismatch: expected {expected_rows}, got {int(array.shape[0])}"
        )
    return array


def _mask_threshold(mask: np.ndarray, *, manifest: ModelManifest) -> np.ndarray:
    threshold = float(manifest.postprocess.polygon_threshold)
    array = np.asarray(mask, dtype=np.float32)
    if manifest.postprocess.mask_format.endswith("_logits"):
        return np.where(array >= threshold, 255, 0).astype(np.uint8)
    if array.dtype == np.bool_:
        return np.where(array, 255, 0).astype(np.uint8)
    if array.max(initial=0.0) <= 1.0:
        return np.where(array >= threshold, 255, 0).astype(np.uint8)
    return np.where(array > 0, 255, 0).astype(np.uint8)


def _project_mask_to_source(
    mask: np.ndarray,
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None,
    bbox_pixels: tuple[float, float, float, float],
) -> np.ndarray:
    meta = preprocess_meta if isinstance(preprocess_meta, dict) else {}
    source_width = int(meta.get("source_width", 0) or 0)
    source_height = int(meta.get("source_height", 0) or 0)
    input_width = int(meta.get("input_width", 0) or manifest.input.width)
    input_height = int(meta.get("input_height", 0) or manifest.input.height)
    resized_width = int(meta.get("resized_width", 0) or input_width)
    resized_height = int(meta.get("resized_height", 0) or input_height)
    offset_x = int(round(float(meta.get("offset_x", 0.0) or 0.0)))
    offset_y = int(round(float(meta.get("offset_y", 0.0) or 0.0)))
    scale_x = float(meta.get("scale_x", 1.0) or 1.0)
    scale_y = float(meta.get("scale_y", 1.0) or 1.0)

    binary_mask = _mask_threshold(mask, manifest=manifest)
    mask_format = str(manifest.postprocess.mask_format or "full_frame_binary").strip().lower()

    if mask_format.startswith("bbox_crop"):
        x1, y1, x2, y2 = [float(value) for value in bbox_pixels]
        if manifest.postprocess.box_format != "xyxy_pixels":
            x1 *= input_width
            x2 *= input_width
            y1 *= input_height
            y2 *= input_height
        left = max(0, min(input_width, int(round(min(x1, x2)))))
        top = max(0, min(input_height, int(round(min(y1, y2)))))
        right = max(left + 1, min(input_width, int(round(max(x1, x2)))))
        bottom = max(top + 1, min(input_height, int(round(max(y1, y2)))))
        canvas = np.zeros((input_height, input_width), dtype=np.uint8)
        target_width = max(1, right - left)
        target_height = max(1, bottom - top)
        resized_crop = _resize_mask_nearest(binary_mask, width=target_width, height=target_height)
        canvas[top:bottom, left:right] = resized_crop[: bottom - top, : right - left]
        binary_mask = canvas
    else:
        binary_mask = _resize_mask_nearest(binary_mask, width=input_width, height=input_height)

    if source_width <= 0 or source_height <= 0:
        return binary_mask

    if str(meta.get("resize_mode", "") or "").strip().lower() == "letterbox":
        right = min(input_width, max(offset_x + 1, offset_x + resized_width))
        bottom = min(input_height, max(offset_y + 1, offset_y + resized_height))
        cropped = binary_mask[offset_y:bottom, offset_x:right]
    else:
        cropped = binary_mask

    if abs(scale_x) < 1e-9 or abs(scale_y) < 1e-9:
        return _resize_mask_nearest(cropped, width=source_width, height=source_height)
    return _resize_mask_nearest(cropped, width=source_width, height=source_height)


def parse_rtmdet_ins_outputs(
    outputs_by_name: dict[str, np.ndarray],
    *,
    manifest: ModelManifest,
    preprocess_meta: dict[str, Any] | None = None,
    categories: set[str] | None = None,
) -> list[SegmentationInstance]:
    det_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.output_name,
        fallback_names=("dets", "boxes", "bbox", "bboxes"),
        min_last_dim=5,
    )
    if det_tensor is None:
        raise ValueError("RTMDet-Ins parser could not find a detection tensor")
    det_rows = _reshape_dets(det_tensor)

    label_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.label_output_name,
        fallback_names=("labels", "label_ids", "classes"),
    )
    label_rows = _reshape_labels(label_tensor, expected_rows=int(det_rows.shape[0])) if label_tensor is not None else None

    mask_tensor = _select_output(
        outputs_by_name,
        preferred=manifest.postprocess.mask_output_name,
        fallback_names=("masks", "mask", "segm", "segms"),
    )
    mask_rows = _reshape_masks(mask_tensor, expected_rows=int(det_rows.shape[0]))
    labels = select_manifest_labels(manifest)

    instances: list[SegmentationInstance] = []
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

        bbox_pixels = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        source_mask = _project_mask_to_source(
            mask_rows[index],
            manifest=manifest,
            preprocess_meta=preprocess_meta,
            bbox_pixels=bbox_pixels,
        )
        instances.append(
            SegmentationInstance(
                label=label,
                label_id=label_id,
                score=score,
                bbox01=map_bbox_pixels_to_source_bbox01(
                    bbox_pixels,
                    manifest=manifest,
                    preprocess_meta=preprocess_meta,
                ),
                mask_artifact_name=f"mask_{index}",
                model_id=manifest.model_id,
                metadata={
                    "parser": "mmdet_rtmdet_ins",
                    "_mask": source_mask,
                },
            )
        )
    return instances
