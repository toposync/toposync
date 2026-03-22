from __future__ import annotations

from toposync_ext_streaming.streaming.placeholder import (
    clear_placeholder_cache,
    get_placeholder_frame,
    placeholder_cache_size,
)


def test_placeholder_cache_reuses_same_resolution_and_mode() -> None:
    clear_placeholder_cache()

    first = get_placeholder_frame(640, 360, mode="gray")
    second = get_placeholder_frame(640, 360, mode="gray")

    assert first is second
    assert placeholder_cache_size() == 1


def test_placeholder_cache_separates_modes() -> None:
    clear_placeholder_cache()

    gray = get_placeholder_frame(320, 240, mode="gray")
    black = get_placeholder_frame(320, 240, mode="black")

    assert gray is not black
    assert int(gray[0, 0, 0]) == 127
    assert int(black[0, 0, 0]) == 0
    assert placeholder_cache_size() == 2
