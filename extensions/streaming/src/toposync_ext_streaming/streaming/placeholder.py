from __future__ import annotations

from threading import Lock

import numpy


_PLACEHOLDER_CACHE: dict[tuple[int, int, str], numpy.ndarray] = {}
_PLACEHOLDER_CACHE_LOCK = Lock()


def get_placeholder_frame(width: int, height: int, mode: str = "gray") -> numpy.ndarray:
    target_width = max(1, int(width))
    target_height = max(1, int(height))
    target_mode = str(mode or "gray").strip().lower() or "gray"

    if target_mode not in {"gray", "black"}:
        target_mode = "gray"

    key = (target_width, target_height, target_mode)
    with _PLACEHOLDER_CACHE_LOCK:
        cached = _PLACEHOLDER_CACHE.get(key)
        if cached is not None:
            return cached

        fill_value = 127 if target_mode == "gray" else 0
        created = numpy.full((target_height, target_width, 3), fill_value, dtype=numpy.uint8)
        _PLACEHOLDER_CACHE[key] = created
        return created


def clear_placeholder_cache() -> None:
    with _PLACEHOLDER_CACHE_LOCK:
        _PLACEHOLDER_CACHE.clear()


def placeholder_cache_size() -> int:
    with _PLACEHOLDER_CACHE_LOCK:
        return len(_PLACEHOLDER_CACHE)
