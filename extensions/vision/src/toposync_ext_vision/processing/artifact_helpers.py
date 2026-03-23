from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.images import resolve_image_artifact_name
from toposync.runtime.pipelines.runtime import Packet

from .contracts import clamp01, normalize_bbox01


def read_frame_crop_bbox01(packet: Packet) -> tuple[float, float, float, float] | None:
    crop = packet.payload.get("frame_crop")
    if not isinstance(crop, dict):
        return None
    apply_to_stream = crop.get("set_stream_frame")
    if apply_to_stream is None:
        apply_to_stream = crop.get("set_payload_frame")
    if apply_to_stream is False:
        return None
    raw = crop.get("bbox01")
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        values = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except Exception:
        return None
    return normalize_bbox01(values)


def read_frame_warp(packet: Packet) -> dict[str, Any] | None:
    warp = packet.payload.get("frame_warp")
    if not isinstance(warp, dict):
        return None
    apply_to_stream = warp.get("set_stream_frame")
    if apply_to_stream is None:
        apply_to_stream = warp.get("set_payload_frame")
    if apply_to_stream is False:
        return None
    if str(warp.get("kind", "")).strip().lower() != "perspective":
        return None

    raw_inv = warp.get("homography_inv")
    if not isinstance(raw_inv, list) or len(raw_inv) != 3:
        return None
    inv: list[list[float]] = []
    try:
        for row in raw_inv:
            if not isinstance(row, list) or len(row) != 3:
                return None
            inv.append([float(row[0]), float(row[1]), float(row[2])])
    except Exception:
        return None

    try:
        src_w = int(warp.get("source_frame_width"))
        src_h = int(warp.get("source_frame_height"))
        dst_w = int(warp.get("dest_frame_width"))
        dst_h = int(warp.get("dest_frame_height"))
    except Exception:
        return None
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    return {
        "homography_inv": inv,
        "source_frame_width": src_w,
        "source_frame_height": src_h,
        "dest_frame_width": dst_w,
        "dest_frame_height": dst_h,
    }


def uncrop_bbox01(
    bbox01: tuple[float, float, float, float],
    crop_bbox01: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(v) for v in bbox01]
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    cw = max(0.0, cx2 - cx1)
    ch = max(0.0, cy2 - cy1)
    return (
        cx1 + (x1 * cw),
        cy1 + (y1 * ch),
        cx1 + (x2 * cw),
        cy1 + (y2 * ch),
    )


def unwarp_bbox01(
    bbox01: tuple[float, float, float, float],
    warp: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    inv = warp.get("homography_inv")
    if not isinstance(inv, list) or len(inv) != 3:
        return None
    try:
        h_inv = np.asarray(inv, dtype=np.float32).reshape(3, 3)
    except Exception:
        return None

    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    if dst_w <= 1 or dst_h <= 1 or src_w <= 1 or src_h <= 1:
        return None

    x1, y1, x2, y2 = [float(v) for v in bbox01]
    denom_dx = float(dst_w - 1)
    denom_dy = float(dst_h - 1)
    denom_sx = float(src_w - 1)
    denom_sy = float(src_h - 1)
    if denom_dx <= 1e-6 or denom_dy <= 1e-6 or denom_sx <= 1e-6 or denom_sy <= 1e-6:
        return None

    corners_dst = np.asarray(
        [
            [x1 * denom_dx, y1 * denom_dy, 1.0],
            [x2 * denom_dx, y1 * denom_dy, 1.0],
            [x2 * denom_dx, y2 * denom_dy, 1.0],
            [x1 * denom_dx, y2 * denom_dy, 1.0],
        ],
        dtype=np.float32,
    )
    src_hom = corners_dst @ h_inv.T
    weights = src_hom[:, 2:3]
    if not np.isfinite(src_hom).all() or not np.isfinite(weights).all():
        return None
    valid = np.abs(weights) > 1e-9
    if not bool(valid.all()):
        return None
    src_xy = src_hom[:, 0:2] / weights
    if not np.isfinite(src_xy).all():
        return None

    xs = src_xy[:, 0] / denom_sx
    ys = src_xy[:, 1] / denom_sy
    return (
        float(np.min(xs)),
        float(np.min(ys)),
        float(np.max(xs)),
        float(np.max(ys)),
    )


def uncrop_keypoints_to_stream_space(
    keypoints: list[tuple[float, float, float]],
    crop_bbox01: tuple[float, float, float, float],
) -> list[tuple[float, float, float]]:
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    crop_width = max(0.0, cx2 - cx1)
    crop_height = max(0.0, cy2 - cy1)
    out: list[tuple[float, float, float]] = []
    for x, y, score in keypoints:
        out.append(
            (
                clamp01(cx1 + (float(x) * crop_width)),
                clamp01(cy1 + (float(y) * crop_height)),
                clamp01(score),
            )
        )
    return out


def unwarp_keypoints_to_stream_space(
    keypoints: list[tuple[float, float, float]],
    warp: dict[str, Any],
) -> list[tuple[float, float, float]] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None

    inv = warp.get("homography_inv")
    if not isinstance(inv, list) or len(inv) != 3:
        return None
    try:
        h_inv = np.asarray(inv, dtype=np.float32).reshape(3, 3)
    except Exception:
        return None

    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    if dst_w <= 1 or dst_h <= 1 or src_w <= 1 or src_h <= 1:
        return None

    denom_dx = float(dst_w - 1)
    denom_dy = float(dst_h - 1)
    denom_sx = float(src_w - 1)
    denom_sy = float(src_h - 1)
    if denom_dx <= 1e-6 or denom_dy <= 1e-6 or denom_sx <= 1e-6 or denom_sy <= 1e-6:
        return None

    out: list[tuple[float, float, float]] = []
    for x, y, score in keypoints:
        dest = np.asarray([[float(x) * denom_dx, float(y) * denom_dy, 1.0]], dtype=np.float32)
        source_hom = dest @ h_inv.T
        weight = float(source_hom[0, 2])
        if not np.isfinite(source_hom).all() or not np.isfinite(weight) or abs(weight) <= 1e-9:
            return None
        source_xy = source_hom[0, 0:2] / weight
        if not np.isfinite(source_xy).all():
            return None
        out.append(
            (
                clamp01(float(source_xy[0]) / denom_sx),
                clamp01(float(source_xy[1]) / denom_sy),
                clamp01(score),
            )
        )
    return out


def project_detection_bbox_to_stream_space(
    bbox01: tuple[float, float, float, float],
    packet: Packet,
) -> tuple[float, float, float, float] | None:
    bbox = tuple(float(v) for v in bbox01)
    warp = read_frame_warp(packet)
    if warp is not None:
        unwarped = unwarp_bbox01(bbox, warp)
        if unwarped is None:
            return None
        bbox = unwarped
    crop_bbox01 = read_frame_crop_bbox01(packet)
    if crop_bbox01 is not None:
        bbox = uncrop_bbox01(bbox, crop_bbox01)
    return normalize_bbox01(bbox)


def project_keypoints_to_stream_space(
    keypoints: list[tuple[float, float, float]] | None,
    packet: Packet,
) -> list[tuple[float, float, float]] | None:
    if not keypoints:
        return None
    out = [(float(x), float(y), float(score)) for x, y, score in keypoints]
    warp = read_frame_warp(packet)
    if warp is not None:
        unwarped = unwarp_keypoints_to_stream_space(out, warp)
        if unwarped is None:
            return None
        out = unwarped
    crop_bbox01 = read_frame_crop_bbox01(packet)
    if crop_bbox01 is not None:
        out = uncrop_keypoints_to_stream_space(out, crop_bbox01)
    return [(clamp01(x), clamp01(y), clamp01(score)) for x, y, score in out]


def _resize_mask_nearest(mask: Any, *, width: int, height: int):  # noqa: ANN202
    import numpy as np  # type: ignore

    array = np.asarray(mask)
    if array.ndim == 3 and int(array.shape[2]) == 1:
        array = array[:, :, 0]
    if array.ndim != 2:
        raise ValueError(f"Expected a 2D mask array, got shape {tuple(array.shape)}")
    if array.dtype != np.uint8:
        array = np.where(array > 0, 255, 0).astype(np.uint8)
    src_h, src_w = array.shape[:2]
    if src_h == height and src_w == width:
        return array
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
    return array[y_index][:, x_index]


def _resolve_original_frame_shape(packet: Packet) -> tuple[int, int] | None:
    artifact = packet.artifacts.get("frame_original")
    shape = getattr(getattr(artifact, "data", None), "shape", None)
    if shape and len(shape) >= 2:
        try:
            return int(shape[0]), int(shape[1])
        except Exception:
            return None
    warp = read_frame_warp(packet)
    if warp is not None:
        return int(warp.get("source_frame_height", 0)), int(warp.get("source_frame_width", 0))
    return None


def _mask_selected_from_treated_frame(packet: Packet, *, selected_artifact_name: str | None) -> bool:
    if not selected_artifact_name:
        return True
    resolved = resolve_image_artifact_name(packet, "treated")
    treated_name = resolved[1] if resolved is not None else "frame"
    return str(selected_artifact_name or "").strip() == str(treated_name or "").strip()


def unwarp_mask_to_stream_space(mask: Any, warp: dict[str, Any]):  # noqa: ANN202
    import numpy as np  # type: ignore

    inv = warp.get("homography_inv")
    if not isinstance(inv, list) or len(inv) != 3:
        return None
    try:
        h_inv = np.asarray(inv, dtype=np.float32).reshape(3, 3)
        h = np.linalg.inv(h_inv)
    except Exception:
        return None

    src_w = int(warp.get("source_frame_width", 0))
    src_h = int(warp.get("source_frame_height", 0))
    dst_w = int(warp.get("dest_frame_width", 0))
    dst_h = int(warp.get("dest_frame_height", 0))
    if src_w <= 1 or src_h <= 1 or dst_w <= 1 or dst_h <= 1:
        return None

    resized_mask = _resize_mask_nearest(mask, width=dst_w, height=dst_h)
    ys, xs = np.indices((src_h, src_w), dtype=np.float32)
    src_points = np.stack([xs, ys, np.ones_like(xs)], axis=-1).reshape(-1, 3)
    dest_hom = src_points @ h.T
    weights = dest_hom[:, 2:3]
    valid = np.abs(weights[:, 0]) > 1e-9
    if not bool(valid.any()):
        return None
    dest_xy = np.zeros((src_points.shape[0], 2), dtype=np.float32)
    dest_xy[valid] = dest_hom[valid, 0:2] / weights[valid]
    dest_x = np.round(dest_xy[:, 0]).astype(np.int64)
    dest_y = np.round(dest_xy[:, 1]).astype(np.int64)

    output = np.zeros((src_h, src_w), dtype=np.uint8)
    inside = (
        valid
        & (dest_x >= 0)
        & (dest_x < dst_w)
        & (dest_y >= 0)
        & (dest_y < dst_h)
    )
    flat = output.reshape(-1)
    flat[inside] = resized_mask[dest_y[inside], dest_x[inside]]
    return output


def uncrop_mask_to_stream_space(
    mask: Any,
    crop_bbox01: tuple[float, float, float, float],
    *,
    source_width: int,
    source_height: int,
):  # noqa: ANN202
    import numpy as np  # type: ignore

    if source_width <= 1 or source_height <= 1:
        return None
    cx1, cy1, cx2, cy2 = [float(v) for v in crop_bbox01]
    left = max(0, min(source_width, int(round(cx1 * source_width))))
    top = max(0, min(source_height, int(round(cy1 * source_height))))
    right = max(left + 1, min(source_width, int(round(cx2 * source_width))))
    bottom = max(top + 1, min(source_height, int(round(cy2 * source_height))))
    target_width = max(1, right - left)
    target_height = max(1, bottom - top)

    resized_mask = _resize_mask_nearest(mask, width=target_width, height=target_height)
    canvas = np.zeros((source_height, source_width), dtype=np.uint8)
    canvas[top:bottom, left:right] = resized_mask[: bottom - top, : right - left]
    return canvas


def project_mask_to_stream_space(
    mask: Any,
    packet: Packet,
    *,
    selected_artifact_name: str | None = None,
):  # noqa: ANN202
    import numpy as np  # type: ignore

    array = np.asarray(mask)
    if array.ndim == 3 and int(array.shape[2]) == 1:
        array = array[:, :, 0]
    if array.ndim != 2:
        raise ValueError(f"Expected a 2D mask array, got shape {tuple(array.shape)}")
    array = np.where(array > 0, 255, 0).astype(np.uint8)

    if not _mask_selected_from_treated_frame(packet, selected_artifact_name=selected_artifact_name):
        return array

    warp = read_frame_warp(packet)
    if warp is not None:
        unwarped = unwarp_mask_to_stream_space(array, warp)
        if unwarped is None:
            return None
        array = unwarped

    crop_bbox01 = read_frame_crop_bbox01(packet)
    source_shape = _resolve_original_frame_shape(packet)
    if crop_bbox01 is not None and source_shape is not None:
        source_height, source_width = source_shape
        uncropped = uncrop_mask_to_stream_space(
            array,
            crop_bbox01,
            source_width=source_width,
            source_height=source_height,
        )
        if uncropped is None:
            return None
        array = uncropped
    return array
