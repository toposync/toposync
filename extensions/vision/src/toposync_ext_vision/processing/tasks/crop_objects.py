from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import TransformOperatorRuntime
from toposync.runtime.pipelines.images import (
    MAIN_ARTIFACT_NAME,
    normalize_artifact_name,
    resolve_image_artifact_for_data,
)
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ...pipelines.schemas import VisionCropObjectsConfig
from ..artifact_helpers import read_frame_crop_bbox01
from ..contracts import normalize_bbox01


class VisionCropObjectsRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        *,
        operator_id: str = "vision.crop_objects",
    ) -> None:
        self._config = VisionCropObjectsConfig.model_validate(config)
        self._operator_id = str(operator_id or "").strip() or "vision.crop_objects"

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        if packet.lifecycle == Lifecycle.CLOSE and not bool(self._config.crop_close_frames):
            return [self._close_without_crop(packet)]

        selected_name, image = resolve_image_artifact_for_data(
            packet,
            input_artifact_name=self._config.input_artifact_name,
        )
        if image is None or isinstance(image, (bytes, bytearray, memoryview)):
            return []

        bbox_result = _read_annotation_bbox01(packet, bbox_field=self._config.bbox_field)
        if bbox_result is None:
            return []
        bbox01, bbox_source = bbox_result

        bbox01_input = bbox01
        bbox01_selected = bbox01_input

        crop_bbox01 = read_frame_crop_bbox01(packet, selected_artifact_name=selected_name)
        if crop_bbox01 is not None:
            reproj = _project_stream_bbox01_to_crop(bbox01_selected, crop_bbox01)
            if reproj is None:
                return []
            bbox01_selected = reproj
            bbox_source = f"{bbox_source}|reproject:frame_crop"

        frame_warp = _read_frame_warp(packet, selected_artifact_name=selected_name)
        if frame_warp is not None:
            reproj = _project_stream_bbox01_to_warp(bbox01_selected, frame_warp)
            if reproj is None:
                return []
            bbox01_selected = reproj
            bbox_source = f"{bbox_source}|reproject:frame_warp"

        bbox01_used = _expand_bbox01(bbox01_selected, padding_ratio=float(self._config.padding_ratio))
        crop = _crop_bbox01(
            image=image,
            bbox01=bbox01_used,
            min_crop_size_px=int(self._config.min_crop_size_px),
        )
        if crop is None:
            return []

        output_artifact_name = self._config.output_artifact_name
        out = packet.with_artifact(
            Artifact(
                name=output_artifact_name,
                data=crop,
                mime_type="image/raw",
                metadata={
                    "source": self._operator_id,
                    "source_artifact_name": selected_name or MAIN_ARTIFACT_NAME,
                    "bbox01": list(bbox01_used),
                    "bbox01_original": list(bbox01_input),
                    "bbox01_selected": list(bbox01_selected),
                    "bbox_source": bbox_source,
                    "padding_ratio": float(self._config.padding_ratio),
                },
            )
        )
        payload = dict(out.payload)
        if output_artifact_name == MAIN_ARTIFACT_NAME:
            shape = getattr(crop, "shape", None)
            if shape and len(shape) >= 2:
                try:
                    payload["frame_height"] = int(shape[0])
                    payload["frame_width"] = int(shape[1])
                except Exception:
                    pass
        return [replace(out, payload=payload)]

    def _close_without_crop(self, packet: Packet) -> Packet:
        return replace(packet, artifacts={})


def _read_annotation_bbox01(
    packet: Packet,
    *,
    bbox_field: str,
) -> tuple[tuple[float, float, float, float], str] | None:
    field = str(bbox_field or "").strip()
    if field:
        bbox01 = _normalize_raw_bbox01(_deep_get(packet.payload, field))
        if bbox01 is not None:
            return bbox01, f"payload:{field}"

    subject = packet.payload.get("subject")
    if isinstance(subject, dict):
        bbox01 = _normalize_raw_bbox01(subject.get("bbox01"))
        if bbox01 is not None:
            return bbox01, "payload:subject.bbox01"

    vision = packet.payload.get("vision")
    if isinstance(vision, dict):
        for key in ("tracks", "detections", "segmentations"):
            raw_items = vision.get(key)
            if not isinstance(raw_items, list):
                continue
            for index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    continue
                bbox01 = _normalize_raw_bbox01(raw_item.get("bbox01"))
                if bbox01 is not None:
                    return bbox01, f"payload:vision.{key}[{index}].bbox01"
    return None


def _deep_get(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            return None
        if isinstance(current, dict):
            current = current.get(key)
            continue
        return None
    return current


def _normalize_raw_bbox01(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        values = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except Exception:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return normalize_bbox01(values)


def _payload_transform_targets_artifact(raw: Any, *, selected_artifact_name: str | None) -> bool:
    if not isinstance(raw, dict):
        return False
    target_name = normalize_artifact_name(raw.get("output_artifact_name"), default="")
    selected_name = normalize_artifact_name(selected_artifact_name, default=MAIN_ARTIFACT_NAME)
    return bool(target_name and selected_name and target_name == selected_name)


def _read_frame_warp(packet: Packet, *, selected_artifact_name: str | None) -> dict[str, Any] | None:
    warp = packet.payload.get("frame_warp")
    if not _payload_transform_targets_artifact(warp, selected_artifact_name=selected_artifact_name):
        return None
    if str(warp.get("kind", "")).strip().lower() != "perspective":
        return None

    raw = warp.get("homography")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    homography: list[list[float]] = []
    try:
        for row in raw:
            if not isinstance(row, list) or len(row) != 3:
                return None
            homography.append([float(row[0]), float(row[1]), float(row[2])])
    except Exception:
        return None

    try:
        source_frame_width = int(warp.get("source_frame_width"))
        source_frame_height = int(warp.get("source_frame_height"))
        dest_frame_width = int(warp.get("dest_frame_width"))
        dest_frame_height = int(warp.get("dest_frame_height"))
    except Exception:
        return None
    if source_frame_width <= 1 or source_frame_height <= 1 or dest_frame_width <= 1 or dest_frame_height <= 1:
        return None

    return {
        "homography": homography,
        "source_frame_width": source_frame_width,
        "source_frame_height": source_frame_height,
        "dest_frame_width": dest_frame_width,
        "dest_frame_height": dest_frame_height,
    }


def _project_stream_bbox01_to_crop(
    bbox01: tuple[float, float, float, float],
    crop_bbox01: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    x1, y1, x2, y2 = [float(v) for v in bbox01]
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    cw = float(cx2) - float(cx1)
    ch = float(cy2) - float(cy1)
    if cw <= 1e-12 or ch <= 1e-12:
        return None
    return normalize_bbox01(
        (
            (x1 - cx1) / cw,
            (y1 - cy1) / ch,
            (x2 - cx1) / cw,
            (y2 - cy1) / ch,
        )
    )


def _project_stream_bbox01_to_warp(
    bbox01: tuple[float, float, float, float],
    warp: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    raw = warp.get("homography")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    try:
        homography = np.asarray(raw, dtype=np.float32).reshape(3, 3)
    except Exception:
        return None

    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    x1, y1, x2, y2 = [float(v) for v in bbox01]
    denom_sx = float(src_w - 1)
    denom_sy = float(src_h - 1)
    denom_dx = float(dst_w - 1)
    denom_dy = float(dst_h - 1)
    if denom_sx <= 1e-6 or denom_sy <= 1e-6 or denom_dx <= 1e-6 or denom_dy <= 1e-6:
        return None

    corners_src = np.asarray(
        [
            [x1 * denom_sx, y1 * denom_sy, 1.0],
            [x2 * denom_sx, y1 * denom_sy, 1.0],
            [x2 * denom_sx, y2 * denom_sy, 1.0],
            [x1 * denom_sx, y2 * denom_sy, 1.0],
        ],
        dtype=np.float32,
    )
    dst_hom = corners_src @ homography.T
    weights = dst_hom[:, 2:3]
    if not np.isfinite(dst_hom).all() or not np.isfinite(weights).all():
        return None
    valid = np.abs(weights) > 1e-9
    if not bool(valid.all()):
        return None
    dst_xy = dst_hom[:, 0:2] / weights
    if not np.isfinite(dst_xy).all():
        return None

    xs = dst_xy[:, 0] / denom_dx
    ys = dst_xy[:, 1] / denom_dy
    return normalize_bbox01(
        (
            float(np.min(xs)),
            float(np.min(ys)),
            float(np.max(xs)),
            float(np.max(ys)),
        )
    )


def _expand_bbox01(
    bbox01: tuple[float, float, float, float],
    *,
    padding_ratio: float,
) -> tuple[float, float, float, float]:
    ratio = float(padding_ratio)
    if ratio <= 0.0:
        return bbox01
    x1, y1, x2, y2 = bbox01
    width = max(0.0, float(x2) - float(x1))
    height = max(0.0, float(y2) - float(y1))
    return normalize_bbox01(
        (
            x1 - (width * ratio),
            y1 - (height * ratio),
            x2 + (width * ratio),
            y2 + (height * ratio),
        )
    )


def _crop_bbox01(
    *,
    image: Any,
    bbox01: tuple[float, float, float, float],
    min_crop_size_px: int,
) -> Any | None:
    shape = getattr(image, "shape", None)
    if not shape or len(shape) < 2:
        return None
    try:
        height = int(shape[0])
        width = int(shape[1])
    except Exception:
        return None
    if width <= 1 or height <= 1:
        return None

    x1, y1, x2, y2 = bbox01
    px1 = max(0, min(width - 1, int(float(x1) * width)))
    py1 = max(0, min(height - 1, int(float(y1) * height)))
    px2 = max(px1 + 1, min(width, int(math.ceil(float(x2) * width))))
    py2 = max(py1 + 1, min(height, int(math.ceil(float(y2) * height))))
    if (px2 - px1) < int(min_crop_size_px) or (py2 - py1) < int(min_crop_size_px):
        return None

    try:
        crop = image[py1:py2, px1:px2]
    except Exception:
        return None
    try:
        return crop.copy()
    except Exception:
        return crop
