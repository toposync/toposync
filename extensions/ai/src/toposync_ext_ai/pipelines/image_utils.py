from __future__ import annotations

from typing import Any


def image_size(image: Any) -> tuple[int, int] | None:
    shape = getattr(image, "shape", None)
    if shape and len(shape) >= 2:
        try:
            return int(shape[1]), int(shape[0])
        except Exception:
            return None
    size = getattr(image, "size", None)
    if isinstance(size, tuple) and len(size) >= 2:
        try:
            return int(size[0]), int(size[1])
        except Exception:
            return None
    return None


def normalize_bbox01(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x1, y1, x2, y2 = [float(value[i]) for i in range(4)]
    except Exception:
        return None
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def expand_bbox01(
    bbox01: tuple[float, float, float, float],
    *,
    padding_ratio: float,
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox01
    pad = max(0.0, float(padding_ratio or 0.0))
    if pad <= 0:
        return bbox01
    width = max(0.0, x2 - x1)
    height = max(0.0, y2 - y1)
    px = width * pad
    py = height * pad
    return (
        max(0.0, x1 - px),
        max(0.0, y1 - py),
        min(1.0, x2 + px),
        min(1.0, y2 + py),
    )


def bbox01_to_px(
    bbox01: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox01
    left = int(round(max(0.0, min(1.0, x1)) * width))
    top = int(round(max(0.0, min(1.0, y1)) * height))
    right = int(round(max(0.0, min(1.0, x2)) * width))
    bottom = int(round(max(0.0, min(1.0, y2)) * height))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    return left, top, right, bottom


def crop_bbox01(
    image: Any,
    *,
    bbox01: tuple[float, float, float, float],
    min_crop_size_px: int,
) -> Any | None:
    size = image_size(image)
    if size is None:
        return None
    width, height = size
    if width <= 1 or height <= 1:
        return None
    left, top, right, bottom = bbox01_to_px(bbox01, width=width, height=height)
    if (right - left) < int(min_crop_size_px) or (bottom - top) < int(min_crop_size_px):
        return None

    if hasattr(image, "crop") and callable(image.crop):
        return image.crop((left, top, right, bottom))

    try:
        cropped = image[top:bottom, left:right]
    except Exception:
        return None
    copy = getattr(cropped, "copy", None)
    if callable(copy):
        try:
            return copy()
        except Exception:
            return cropped
    return cropped
