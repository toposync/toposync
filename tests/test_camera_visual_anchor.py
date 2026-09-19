from __future__ import annotations

import copy

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.processing.visual_anchor import (
    create_visual_anchor,
    match_visual_anchor,
    visual_anchor_summary,
)


def _textured_image() -> np.ndarray:
    random = np.random.default_rng(20260912)
    image = random.integers(0, 40, size=(540, 960, 3), dtype=np.uint8)
    for index in range(40):
        x = 20 + (index * 83) % 880
        y = 20 + (index * 59) % 480
        color = tuple(int(value) for value in random.integers(80, 255, size=3))
        cv2.circle(image, (x, y), 5 + index % 17, color, thickness=2)
        cv2.putText(image, f"{index:02}", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    return image


def test_visual_anchor_matches_a_small_translation_without_retaining_raster() -> None:
    image = _textured_image()
    anchor = create_visual_anchor(image)

    assert anchor["created"] is True
    assert "image" not in anchor
    assert anchor["feature_count"] >= 24
    summary = visual_anchor_summary(anchor)
    assert "descriptors_f32_zlib_b64" not in summary

    matrix = np.float32([[1, 0, 8], [0, 1, -5]])
    shifted = cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]))
    result = match_visual_anchor(anchor, shifted)

    assert result["verified"] is True
    assert result["inliers"] >= 24
    assert result["overlap"] > 0.9
    assert result["displacement"] == pytest.approx(9.43, abs=3.0)


def test_visual_anchor_rejects_tampered_feature_payload() -> None:
    anchor = create_visual_anchor(_textured_image())
    tampered = copy.deepcopy(anchor)
    tampered["payload_sha256"] = "0" * 64

    assert match_visual_anchor(tampered, _textured_image()) == {
        "verified": False,
        "code": "visual_anchor_invalid",
    }


def test_visual_anchor_rejects_insufficient_texture() -> None:
    anchor = create_visual_anchor(np.zeros((240, 320, 3), dtype=np.uint8))

    assert anchor == {"created": False, "code": "insufficient_texture"}
