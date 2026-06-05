from __future__ import annotations

import numpy

from toposync_ext_streaming.streaming.resize import contain_content_rect, resize_frame_contain


def test_resize_frame_contain_keeps_aspect_ratio_and_black_bars() -> None:
    source = numpy.full((100, 200, 3), 220, dtype=numpy.uint8)

    output = resize_frame_contain(source, 300, 300)

    assert output.shape == (300, 300, 3)

    # Barra superior e inferior devem ser pretas no modo contain para frame 2:1 em alvo 1:1.
    assert int(output[10, 10, 0]) == 0
    assert int(output[289, 289, 1]) == 0

    # The center should contain the resized content.
    assert int(output[150, 150, 2]) == 220


def test_resize_frame_contain_noop_when_size_matches() -> None:
    source = numpy.random.default_rng(42).integers(0, 255, size=(180, 320, 3), dtype=numpy.uint8)

    output = resize_frame_contain(source, 320, 180)

    assert output.shape == source.shape
    assert numpy.array_equal(output, source)


def test_contain_content_rect_for_portrait_source_in_landscape_target() -> None:
    rect = contain_content_rect(720, 1280, 1280, 720)

    assert rect == {
        "x": 0.34140625,
        "y": 0.0,
        "width": 0.31640625,
        "height": 1.0,
    }


def test_contain_content_rect_for_landscape_source_in_square_target() -> None:
    rect = contain_content_rect(200, 100, 300, 300)

    assert rect == {
        "x": 0.0,
        "y": 0.25,
        "width": 1.0,
        "height": 0.5,
    }


def test_contain_content_rect_full_when_aspect_matches() -> None:
    rect = contain_content_rect(320, 180, 1280, 720)

    assert rect == {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
    }
