"""Analytic geometry and suppression tests; no learned accuracy claims."""

import numpy as np
import pytest


def geometry():
    from toposync_ext_vision.processing.runtime_backends import mediapipe_palms

    return mediapipe_palms


def outputs(boxes):
    regression = np.zeros((1, 2016, 18), np.float32)
    scores = np.full((1, 2016, 1), -100, np.float32)
    # The first two anchors share the centre (4, 4) in the 192-pixel input.
    for index, (x1, y1, x2, y2) in enumerate(boxes):
        regression[0, index, :4] = [(x1 + x2) / 2 - 4, (y1 + y2) / 2 - 4,
                                   x2 - x1, y2 - y1]
        regression[0, index, 4:] = np.tile([x1 - 4, y1 - 4], 7)
        scores[0, index, 0] = 5 - index
    return regression, scores


def test_disjoint_palms_are_not_suppressed_as_overlapping_widths():
    boxes = [(100, 0, 110, 10), (120, 0, 130, 10)]
    actual = geometry().decode_palms(*outputs(boxes), np.eye(3))
    np.testing.assert_allclose(actual[:, :4], boxes, rtol=0, atol=1e-5)


def test_duplicate_palm_keeps_only_highest_score():
    actual = geometry().decode_palms(*outputs([(10, 20, 30, 40)] * 2), np.eye(3))
    assert actual.shape == (1, 19)
    assert actual[0, -1] == pytest.approx(1 / (1 + np.exp(-5)))


def test_landmark_order_and_coordinates_outside_image_are_preserved():
    regression, scores = outputs([(-20, -10, 30, 40)])
    regression[0, 0, 4:] = np.arange(14) - 50
    matrix = np.array([[2, 0, 10], [0, 3, -20], [0, 0, 1]])
    actual = geometry().decode_palms(regression, scores, matrix)
    expected_points = (np.arange(14).reshape(7, 2) - 46) * [2, 3] + [10, -20]
    np.testing.assert_allclose(actual[0, 4:18].reshape(7, 2), expected_points, atol=1e-5)
    np.testing.assert_allclose(actual[0, :4], [-30, -50, 70, 100], atol=1e-5)


def test_low_scores_produce_empty_array_without_fabricated_landmarks():
    actual = geometry().decode_palms(*outputs([]), np.eye(3))
    assert actual.shape == (0, 19)


@pytest.mark.parametrize("field", ["regression", "scores", "matrix"])
@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_nonfinite_inference_or_geometry_fails_explicitly(field, invalid):
    regression, scores = outputs([(10, 20, 30, 40)])
    matrix = np.eye(3)
    {"regression": regression, "scores": scores, "matrix": matrix}[field].flat[0] = invalid
    with pytest.raises(ValueError, match="finite"):
        geometry().decode_palms(regression, scores, matrix)


@pytest.mark.parametrize("shape", [(1, 1, 18), (2016, 18), (2, 2016, 18)])
def test_wrong_output_shape_is_not_silently_reindexed(shape):
    with pytest.raises(ValueError, match="shape"):
        geometry().decode_palms(np.zeros(shape), np.zeros((1, 2016, 1)), np.eye(3))


def test_nonpositive_boxes_are_rejected_without_reordering_corners():
    regression, scores = outputs([(10, 20, 30, 40)])
    regression[0, 0, 2] = -5
    with pytest.raises(ValueError, match="positive"):
        geometry().decode_palms(regression, scores, np.eye(3))


@pytest.mark.parametrize("matrix", [np.zeros((3, 3)), np.eye(2),
                                   np.array([[1, 0, 0], [0, 1, 0], [.01, 0, 1]])])
def test_only_invertible_axis_aligned_letterbox_geometry_is_accepted(matrix):
    with pytest.raises(ValueError, match="transform"):
        geometry().decode_palms(*outputs([]), matrix)


@pytest.mark.parametrize("height,width,resized_height,resized_width,top,left", [
    (192, 384, 96, 192, 48, 0), (384, 192, 192, 96, 0, 48),
    (481, 641, 144, 192, 24, 0), (641, 481, 192, 144, 0, 24),
])
def test_rgb_letterbox_and_inverse_use_actual_rounded_resize(
    height, width, resized_height, resized_width, top, left,
):
    image = np.full((height, width, 3), [10, 20, 30], np.uint8)
    blob, matrix = geometry().prepare_palm_input(image)
    assert blob.shape == (1, 192, 192, 3) and blob.dtype == np.float32
    assert blob.flags.c_contiguous
    np.testing.assert_allclose(blob[0, top + 10, left + 10], np.array([30, 20, 10]) / 255)
    assert np.all(blob[0, 0, 0] == 0)
    np.testing.assert_allclose(matrix @ [left, top, 1], [0, 0, 1], atol=1e-9)
    np.testing.assert_allclose(matrix @ [left + resized_width, top + resized_height, 1],
                               [width, height, 1], atol=1e-9)


@pytest.mark.parametrize("image", [np.zeros((0, 10, 3), np.uint8), np.zeros((10, 10)),
                                  np.zeros((10, 10, 4), np.uint8),
                                  np.zeros((10, 10, 3), np.float32)])
def test_input_requires_nonempty_uint8_bgr(image):
    with pytest.raises(ValueError, match="BGR"):
        geometry().prepare_palm_input(image)


def test_square_resize_does_not_lose_a_column_from_floating_point_rounding():
    blob, matrix = geometry().prepare_palm_input(np.full((47, 47, 3), 255, np.uint8))
    assert np.all(blob == 1)
    np.testing.assert_allclose(matrix, np.diag([47 / 192, 47 / 192, 1]), atol=1e-12)


@pytest.mark.parametrize("source", ["model", "translation"])
def test_numerically_collapsed_final_boxes_do_not_reach_suppression(source):
    regression, scores = outputs([(10, 20, 30, 40)])
    matrix = np.eye(3)
    if source == "model":
        regression[0, 0, :2] = 1e20
    else:
        matrix[:2, 2] = 1e20
    with pytest.raises(ValueError, match="positive"):
        geometry().decode_palms(regression, scores, matrix)
