from __future__ import annotations

import numpy

from toposync_ext_streaming.streaming.resize import resize_frame_contain


def test_resize_frame_contain_keeps_aspect_ratio_and_black_bars() -> None:
    source = numpy.full((100, 200, 3), 220, dtype=numpy.uint8)

    output = resize_frame_contain(source, 300, 300)

    assert output.shape == (300, 300, 3)

    # Barra superior e inferior devem ser pretas no modo contain para frame 2:1 em alvo 1:1.
    assert int(output[10, 10, 0]) == 0
    assert int(output[289, 289, 1]) == 0

    # Centro deve conter o conteúdo redimensionado.
    assert int(output[150, 150, 2]) == 220


def test_resize_frame_contain_noop_when_size_matches() -> None:
    source = numpy.random.default_rng(42).integers(0, 255, size=(180, 320, 3), dtype=numpy.uint8)

    output = resize_frame_contain(source, 320, 180)

    assert output.shape == source.shape
    assert numpy.array_equal(output, source)
