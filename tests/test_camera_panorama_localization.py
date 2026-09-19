from __future__ import annotations

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.processing.panorama_localization import (
    PanoramaLocalizer,
    fit_frame_rotation,
    frame_identity,
)


def _correspondences():
    lens = {
        "width": 960,
        "height": 540,
        "fx": 640,
        "fy": 640,
        "cx": 479.5,
        "cy": 269.5,
        "distortion": [-0.15, 0.03, 0, 0, 0],
    }
    matrix = np.array([[640.0, 0, 479.5], [0, 640, 269.5], [0, 0, 1]])
    rays = np.column_stack(
        (np.random.default_rng(775).uniform([-0.6, -0.33], [0.6, 0.33], (500, 2)), np.ones(500))
    )
    rotation, _ = cv2.Rodrigues(np.array([0.018, -0.055, 0.012]))
    current, _ = cv2.projectPoints(
        rays, np.zeros(3), np.zeros(3), matrix, np.array(lens["distortion"])
    )
    reference, _ = cv2.projectPoints(
        rays @ rotation.T, np.zeros(3), np.zeros(3), matrix, np.array(lens["distortion"])
    )
    return lens, current.reshape(-1, 2), reference.reshape(-1, 2), rotation


def test_rotation_has_independent_pixel_oracle_and_held_out_validation():
    lens, current, reference, expected = _correspondences()
    result = fit_frame_rotation(current, reference, lens, np.eye(3))
    assert result is not None
    assert np.allclose(result["rotation_matrix"], expected, atol=1e-6)
    assert result["validation_p95_pixels"] < 1e-5
    assert result["validation_matches"] == 100


@pytest.mark.parametrize("failure", ["holdout", "local", "zoom"])
def test_rotation_refuses_bad_validation_local_support_and_changed_optics(failure):
    lens, current, reference, _ = _correspondences()
    if failure == "holdout":
        reference[::5] = reference[np.random.default_rng(433).permutation(500)[:100]]
    elif failure == "local":
        current = current * 0.1 + [420, 230]
        reference = reference * 0.1 + [420, 230]
    else:
        current = (current - [479.5, 269.5]) * 1.12 + [479.5, 269.5]
    assert fit_frame_rotation(current, reference, lens, np.eye(3)) is None


def test_presentation_rotation_is_applied_once():
    lens, current, reference, rotation = _correspondences()
    presentation, _ = cv2.Rodrigues(np.array([0.2, -0.3, 0.1]))
    result = fit_frame_rotation(current, reference, lens, presentation)
    assert result is not None
    assert np.allclose(result["rotation_matrix"], presentation @ rotation, atol=1e-6)


def test_localizer_binds_cache_to_frame_bytes_and_never_reuses_last_pose(tmp_path):
    image = np.random.default_rng(500).integers(0, 256, (540, 960), np.uint8)
    image = cv2.GaussianBlur(image, (5, 5), 1)
    path = tmp_path / "reference.png"
    assert cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    evidence = {"capture_instance": "first", "generation": 1, "sequence": 1}
    located = localizer.locate(image, evidence)
    assert located["status"] == "localized"
    assert located["capture_evidence"] == evidence
    assert localizer.locate(np.zeros_like(image), evidence)["status"] == "unlocalized"
    for sequence in range(2, 9):
        localizer.locate(np.zeros_like(image), {**evidence, "sequence": sequence})
    assert len(localizer.results) == 4
    assert localizer.locate(image, {})["reason"] == "panorama_frame_identity_missing"


def test_frame_identity_requires_capture_instance_and_integer_sequence():
    assert frame_identity({"capture_instance": "one", "generation": 0, "sequence": 1}) == (
        "one",
        0,
        1,
    )
    for invalid in (
        {},
        {"generation": 0, "sequence": 1},
        {"capture_instance": "one", "generation": 0, "sequence": True},
    ):
        assert frame_identity(invalid) is None


def test_crop_resize_provenance_localizes_optical_pixels_and_rejects_changed_binding(tmp_path):
    from types import SimpleNamespace
    from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry

    image = cv2.GaussianBlur(
        np.random.default_rng(716).integers(0, 256, (540, 960), np.uint8), (5, 5), 1
    )
    path = tmp_path / "optical.png"
    cv2.imwrite(str(path), image)
    lens, _, _, _ = _correspondences()
    localizer = PanoramaLocalizer(
        {"lens": lens, "captures": [{"id": "one", "rotation_matrix": np.eye(3).tolist()}]},
        [{"id": "one", "path": str(path)}],
    )
    evidence = {"capture_instance": "one", "generation": 1, "sequence": 3}
    artifact = SimpleNamespace(
        data=image, metadata={"image_geometry": image_geometry(960, 540, evidence)}
    )
    cropped = image[30:510, 40:920]
    cropped_geometry = transformed_geometry(
        artifact, [[1, 0, -40], [0, 1, -30], [0, 0, 1]], 880, 480
    )
    artifact = SimpleNamespace(data=cropped, metadata=cropped_geometry)
    resized = cv2.resize(cropped, (660, 360), interpolation=cv2.INTER_AREA)
    geometry = transformed_geometry(
        artifact, [[0.75, 0, -0.125], [0, 0.75, -0.125], [0, 0, 1]], 660, 360
    )["image_geometry"]
    located = localizer.locate(resized, evidence, geometry)
    assert located["status"] == "localized"
    assert np.allclose(located["rotation_matrix"], np.eye(3), atol=0.002)
    assert localizer.locate(resized, evidence)["status"] == "unlocalized"
    assert (
        localizer.locate(resized, {**evidence, "sequence": 4}, geometry)["status"] == "unlocalized"
    )
