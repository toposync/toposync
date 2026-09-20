"""Image geometry for hand crops; no model or physical ground truth."""

import numpy as np
import pytest


def implementation():
    from toposync_ext_vision.processing.runtime_backends import mediapipe_hands

    return mediapipe_hands


def palm(*, odd=False):
    right = 121 if odd else 120
    points = [(100, 120), (80, 100), (100, 80), (110, 90),
              (right, 100), (80, 110), (85, 115)]
    return np.array([80, 80, right, 120, *np.array(points).ravel(), .95], dtype=float)


def test_upright_crop_rgb_mapping_and_input_immutability():
    image = np.full((240, 240, 3), [10, 20, 30], np.uint8)
    proposal = palm()
    original = proposal.copy()
    tensor, inverse = implementation().prepare_hand_crop(image, proposal)
    assert tensor.shape == (1, 224, 224, 3) and tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    np.testing.assert_allclose(tensor[0, 112, 112], np.array([30, 20, 10]) / 255)
    np.testing.assert_allclose(inverse, [[120 / 224, 0, 40], [0, 120 / 224, 24], [0, 0, 1]], atol=1e-10)
    np.testing.assert_array_equal(proposal, original)
    assert np.all(image == np.array([10, 20, 30]))


def test_odd_padding_uses_actual_top_padding_not_fractional_box_centre():
    _, inverse = implementation().prepare_hand_crop(np.zeros((240, 240, 3), np.uint8), palm(odd=True))
    np.testing.assert_allclose(inverse, [[123 / 224, 0, 39], [0, 123 / 224, 23], [0, 0, 1]], atol=1e-10)


@pytest.mark.parametrize("angle", [90, 180, -90])
def test_rotated_palm_base_becomes_vertical_without_mirroring(angle):
    import cv2

    proposal = palm()
    rotation = cv2.getRotationMatrix2D((100, 100), angle, 1)
    points = np.c_[proposal[4:18].reshape(7, 2), np.ones(7)] @ rotation.T
    proposal[4:18] = points.ravel()
    _, inverse = implementation().prepare_hand_crop(np.zeros((240, 240, 3), np.uint8), proposal)
    mapped = np.c_[points, np.ones(7)] @ np.linalg.inv(inverse).T
    assert mapped[0, 0] == pytest.approx(mapped[2, 0], abs=1e-8)
    assert mapped[0, 1] > mapped[2, 1]
    assert np.linalg.det(inverse[:2, :2]) > 0


def test_partial_border_crop_retains_explicit_inverse_and_unclipped_predictions():
    proposal = palm()
    proposal[:18] -= np.tile([85, 85], 9)
    _, inverse = implementation().prepare_hand_crop(np.zeros((100, 100, 3), np.uint8), proposal)
    assert np.isfinite(inverse).all()
    raw = np.tile([-1000., -1000., 0.], 21).reshape(1, 63)
    points = implementation().decode_hand_landmarks(raw, inverse)
    assert len(points) == 21 and all(point.position[0] < 0 for point in points)


@pytest.mark.parametrize("kind", ["empty_box", "outside", "coincident_axis", "nonfinite", "wrong_shape"])
def test_invalid_palm_crop_fails_before_opencv(kind):
    proposal = palm()
    if kind == "empty_box":
        proposal[2] = proposal[0]
    elif kind == "outside":
        proposal[:18] += 10000
    elif kind == "coincident_axis":
        proposal[8:10] = proposal[4:6]
    elif kind == "nonfinite":
        proposal[4] = np.nan
    elif kind == "wrong_shape":
        proposal = proposal[:4]
    with pytest.raises(ValueError):
        implementation().prepare_hand_crop(np.zeros((100, 100, 3), np.uint8), proposal)


def test_decoder_preserves_slots_names_absence_and_has_no_per_joint_confidence():
    raw = np.arange(63, dtype=np.float32).reshape(1, 63)
    raw[0, 3], raw[0, 7] = np.nan, np.inf
    original = raw.copy()
    transform = np.array([[2, 0, -10], [0, 3, 20], [0, 0, 1]])
    points = implementation().decode_hand_landmarks(raw, transform)
    assert len(points) == 21
    assert [p.index for p in points] == list(range(21))
    assert [p.name for p in points[:5]] == ['wrist', 'thumb_cmc', 'thumb_mcp', 'thumb_ip', 'thumb_tip']
    assert points[5].name == 'index_mcp' and points[20].name == 'pinky_tip'
    assert points[0].position == (-10, 23)
    assert points[1].position is None and points[2].position is None
    assert points[3].position == (8, 50)
    assert all(p.model_score is None and p.visibility == 'unknown' and p.provenance == 'image_estimate' for p in points)
    np.testing.assert_array_equal(raw, original)


@pytest.mark.parametrize("shape", [(1, 60), (21, 3), (2, 63)])
def test_wrong_landmark_tensor_does_not_change_skeleton_order(shape):
    with pytest.raises(ValueError, match="shape"):
        implementation().decode_hand_landmarks(np.zeros(shape), np.eye(3))


@pytest.mark.parametrize("matrix", [np.zeros((3, 3)), np.full((3, 3), np.nan), np.eye(2),
                                   np.array([[1, 0, 0], [0, 1, 0], [.1, 0, 1]])])
def test_decoder_rejects_invalid_inverse(matrix):
    with pytest.raises(ValueError, match="transform"):
        implementation().decode_hand_landmarks(np.zeros((1, 63)), matrix)
