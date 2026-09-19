from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from toposync_ext_cameras.processing import panorama_reconstruction as reconstruction
from toposync_ext_cameras.processing.panorama_mapping import (
    image_pixel_to_ray,
    ray_to_image_pixel,
    render_panorama,
)


def test_full_rotation_preserves_legacy_identity_and_roll_inverse() -> None:
    lens = {
        "width": 101,
        "height": 81,
        "fx": 80.0,
        "fy": 81.0,
        "cx": 50.0,
        "cy": 40.0,
        "distortion": [],
    }
    for x, y in ((0, 0), (50, 40), (80, 65)):
        assert image_pixel_to_ray(x, y, lens, rotation_matrix=np.eye(3)) == pytest.approx(
            image_pixel_to_ray(x, y, lens, 0, 0)
        )
    rotation = Rotation.from_euler("xyz", [0.2, -0.3, 0.7]).as_matrix()
    for x, y in ((10, 10), (50, 40), (80, 65)):
        ray = image_pixel_to_ray(x, y, lens, rotation_matrix=rotation)
        assert ray_to_image_pixel(ray, lens, rotation_matrix=rotation) == pytest.approx(
            (x, y), abs=1e-6
        )
    image = np.full((81, 101, 3), 120, np.uint8)
    legacy = render_panorama(
        [{"id": "a", "image": image, "lens": lens, "pan_radians": 0, "tilt_radians": 0}],
        width=256,
        height=128,
    )
    identity = render_panorama(
        [{"id": "a", "image": image, "lens": lens, "rotation_matrix": np.eye(3)}],
        width=256,
        height=128,
    )
    assert np.array_equal(legacy["image"], identity["image"])
    assert np.array_equal(legacy["coverage_mask"], identity["coverage_mask"])
    for invalid in (np.diag([1, 1, -1]), np.eye(3) * 2, [[1, 0], [0, 1]], np.full((3, 3), np.nan)):
        with pytest.raises(ValueError, match="[Rr]otation"):
            image_pixel_to_ray(50, 40, lens, rotation_matrix=invalid)


def test_presentation_aligns_only_verified_horizontal_motion() -> None:
    mechanical_axis = np.array([0.35, -0.92, 0.18])
    mechanical_axis /= np.linalg.norm(mechanical_axis)
    rotations = [
        Rotation.from_rotvec(mechanical_axis * math.radians(angle)).as_matrix()
        for angle in (0, 4, 8, 12)
    ]
    features = [
        reconstruction._FeatureImage(
            str(index), Path("unused"), 960, 540,
            np.empty((0, 2)), np.empty((0, 128)), np.empty(0), [],
            {
                "row_index": 0,
                "movement": {"axis": "pan"} if index else None,
            },
        )
        for index in range(4)
    ]

    aligned, presentation = reconstruction._presentation_frame(rotations, features)

    transform = np.asarray(presentation["rotation_matrix"])
    assert transform @ mechanical_axis == pytest.approx([0, -1, 0], abs=1e-7)
    assert presentation["status"] == "verified"
    assert presentation["method"] == "verified_mechanical_pan_axis"
    assert presentation["horizontal_motion_pairs"] == 3
    assert presentation["horizontal_rotation_degrees"] == pytest.approx(12)
    assert len(aligned) == len(rotations)


def test_presentation_does_not_treat_tilt_motion_as_vertical() -> None:
    rotations = [
        Rotation.from_euler("x", angle, degrees=True).as_matrix()
        for angle in (0, 5, 10, 15)
    ]
    features = [
        reconstruction._FeatureImage(
            str(index), Path("unused"), 960, 540,
            np.empty((0, 2)), np.empty((0, 128)), np.empty(0), [],
            {"row_index": 0, "movement": {"axis": "tilt"} if index else None},
        )
        for index in range(4)
    ]

    _, presentation = reconstruction._presentation_frame(rotations, features)

    assert presentation["status"] == "unverified"
    assert presentation["horizontal_motion_pairs"] == 0


def test_strong_barrel_lens_pixel_ray_inverse_reaches_the_photographed_edge() -> None:
    lens = {
        "width": 960,
        "height": 540,
        "fx": 710.0,
        "fy": 710.0,
        "cx": 479.5,
        "cy": 269.5,
        "distortion": [-0.45, 0.165, 0, 0, 0],
    }
    rotation = Rotation.from_euler("xyz", [0.3, -0.1, 0.4]).as_matrix()
    for pixel in ((5, 5), (950, 530), (900, 10)):
        ray = image_pixel_to_ray(*pixel, lens, rotation_matrix=rotation)
        assert ray_to_image_pixel(ray, lens, rotation_matrix=rotation) == pytest.approx(
            pixel, abs=1e-5
        )


def test_validation_split_keeps_complete_multiview_tracks_together() -> None:
    keys = np.arange(100)
    pixels = np.column_stack((keys, keys * 2)).astype(float)
    edges = [
        reconstruction._Edge(0, 1, keys, keys, pixels, pixels, np.eye(3)),
        reconstruction._Edge(1, 2, keys, keys, pixels, pixels, np.eye(3)),
    ]
    first, second, a, b, check = reconstruction._split_tracks(edges)
    assert np.array_equal(check[:100], check[100:])
    assert 10 < int(check[:100].sum()) < 40
    assert len(first) == len(second) == len(a) == len(b) == len(check)


def test_repeated_structure_cannot_merge_two_features_from_the_same_photo() -> None:
    keys = np.arange(40)
    pixels = np.column_stack((keys, keys * 2)).astype(float)
    edges = [
        reconstruction._Edge(0, 1, keys, keys, pixels, pixels, np.eye(3)),
        reconstruction._Edge(1, 2, keys, keys, pixels, pixels, np.eye(3)),
        reconstruction._Edge(0, 2, keys, np.roll(keys, 1), pixels, pixels, np.eye(3)),
    ]
    first, second, _, _, _ = reconstruction._split_tracks(edges)
    assert not np.any((first == 0) & (second == 2))


def _matching_spatial_support(points_a, points_b, monkeypatch, target_keys=None):
    class Matcher:
        def knnMatch(self, first, second, k):
            return [
                [cv2.DMatch(index, index if target_keys is None else target_keys[index], 0.0),
                 cv2.DMatch(index, (index + 1) % len(second), 10.0)]
                for index in range(len(first))
            ]

    monkeypatch.setattr(reconstruction.cv2, "FlannBasedMatcher", lambda *args: Matcher())
    features = [
        reconstruction._FeatureImage(
            str(index),
            Path("unused"),
            960,
            540,
            points.astype(float),
            np.zeros((len(points), 128), np.float32),
            np.empty(len(points)),
            [],
            {},
        )
        for index, points in enumerate((points_a, points_b))
    ]
    return reconstruction._match(features, None, None)


def test_dominant_clock_and_one_distant_point_cannot_link_views(monkeypatch) -> None:
    clock = np.array([[x, y] for y in (6, 12, 18) for x in np.linspace(20, 200, 7)])
    points = np.vstack((clock, [[800, 450]]))
    assert _matching_spatial_support(points, points, monkeypatch) == []


def test_distributed_same_view_preserves_loop_closure(monkeypatch) -> None:
    points = np.array([[x, y] for y in np.linspace(60, 480, 5) for x in np.linspace(90, 870, 6)])
    assert len(_matching_spatial_support(points, points, monkeypatch)) == 1


def test_distributed_scene_with_real_displacement_remains_supported(monkeypatch) -> None:
    points = np.array([[x, y] for y in np.linspace(60, 480, 5) for x in np.linspace(90, 870, 6)])
    assert len(_matching_spatial_support(points, points + [25, 8], monkeypatch)) == 1


def test_many_features_at_one_target_cannot_satisfy_geometric_support(monkeypatch) -> None:
    points = np.array([[x, y] for y in (60, 200, 340, 480) for x in (90, 246, 402, 558, 714, 870)])
    targets = points.copy()
    targets[:5] = [[0, 0], [600, 300], [700, 350], [800, 400], [900, 500]]
    # A forward RANSAC consensus can count repeated targets as separate inliers,
    # even with broad percentile support. They are not independent scene points.
    monkeypatch.setattr(reconstruction.cv2, "findHomography", lambda a, b, *args, **kwargs:
                        (np.eye(3), np.ones((len(a), 1), dtype=np.uint8)))
    assert _matching_spatial_support(points, targets, monkeypatch, [0] * 20 + [1, 2, 3, 4]) == []


def test_ambiguous_targets_do_not_remove_other_independent_connections(monkeypatch) -> None:
    points = np.array([[x, y] for y in np.linspace(60, 480, 5) for x in np.linspace(90, 870, 6)])
    target_keys = list(range(len(points)))
    target_keys[-4:] = [0] * 4
    edges = _matching_spatial_support(points, points + [25, 8], monkeypatch, target_keys)
    assert len(edges) == 1
    assert len(edges[0].second_keys) == len(set(edges[0].second_keys))
    assert 0 not in edges[0].second_keys


def test_large_acquisition_matches_sampled_descriptors_and_finds_distant_overlap(tmp_path) -> None:
    generator = np.random.default_rng(9361)
    features = [
        reconstruction._FeatureImage(
            str(index),
            tmp_path / f"capture-{index}.jpg",
            960,
            540,
            np.empty((320, 2)),
            generator.random((320, 128), dtype=np.float32),
            np.empty(320),
            [],
            {},
        )
        for index in range(41)
    ]
    # These photographs overlap despite being far apart in acquisition order.
    # More than 300 descriptors exercises real FLANN with a strided query.
    features[30].descriptors = features[5].descriptors.copy()
    pairs = reconstruction._candidate_pairs(features)
    assert (5, 30) in pairs
    assert (12, 13) in pairs
    assert pairs == sorted(set(pairs))
    assert all(first < second for first, second in pairs)
    assert len(pairs) < len(features) * (len(features) - 1) // 2


def test_brown_outer_polynomial_branch_cannot_paint_unobserved_rays(tmp_path) -> None:
    path = tmp_path / "optical.png"
    cv2.imwrite(str(path), np.full((120, 160, 3), 150, np.uint8))
    feature = reconstruction._FeatureImage(
        "one", path, 160, 120, np.empty((0, 2)), np.empty((0, 128)), np.empty(0), [], {}
    )
    lens = {
        "width": 160,
        "height": 120,
        "fx": 200.0,
        "fy": 200.0,
        "cx": 79.5,
        "cy": 59.5,
        "distortion": [-0.43, 0.034, 0.0, 0.0, 0.0],
    }
    _, mask, _ = reconstruction._warp(feature, lens, np.eye(3), 512, 256, None)
    rows, columns = np.where(mask > 0)
    assert len(rows) > 100
    pan = ((columns + 0.5) / 512 * 2 - 1) * np.pi
    elevation = (0.5 - (rows + 0.5) / 256) * np.pi
    forward_dot = np.cos(elevation) * np.cos(pan)
    # The independently known photographed field fits within a 45-degree cone;
    # another root of the Brown polynomial must not create a distant ghost ring.
    assert np.all(forward_dot > math.cos(math.radians(45)))


def test_radial_inverse_is_checked_at_real_corners_not_its_own_failed_iterate() -> None:
    matrix = np.array([[857.0, 0, 479.5], [0, 865.0, 269.5], [0, 0, 1.0]])
    with pytest.raises(reconstruction.ReconstructionError) as error:
        reconstruction._maximum_optical_radius(matrix, np.array([-0.434, 0.034, 0, 0, 0]), 960, 540)
    assert error.value.code == "noninvertible_lens"
    distortion = np.array([-0.45, 0.165, 0, 0, 0])
    points = np.array([[0, 0], [959, 539], [479.5, 269.5], [800, 25]], dtype=float)
    rays = reconstruction._local_rays(points, matrix, distortion)
    assert reconstruction._project(rays, matrix, distortion) == pytest.approx(points, abs=1e-6)


def _photographs(tmp_path):
    width, height, focal = 400, 300, 310.0
    generator = np.random.default_rng(8923)
    texture = generator.integers(0, 256, (height, width, 3), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (3, 3), 0.6)
    for _ in range(450):
        center = tuple(map(int, generator.integers((0, 0), (width, height))))
        color = tuple(map(int, generator.integers(0, 256, 3)))
        cv2.circle(texture, center, int(generator.integers(2, 8)), color, -1)
    matrix = np.array([[focal, 0, (width - 1) / 2], [0, focal, (height - 1) / 2], [0, 0, 1.0]])
    captures = []
    for index, (pitch, yaw, roll) in enumerate(
        (
            (0, 0, 0),
            (0, 0.22, 0.03),
            (0, -0.22, -0.04),
            (0.18, -0.12, 0.02),
            (-0.18, 0.12, -0.02),
            (0.18, 0.18, 0.03),
        )
    ):
        rotation = Rotation.from_euler("xyz", (pitch, yaw, roll)).as_matrix()
        # Independent image-formation oracle: rotational homography, no renderer.
        homography = matrix @ rotation.T @ np.linalg.inv(matrix)
        image = cv2.warpPerspective(texture, homography, (width, height))
        path = tmp_path / f"capture-{index}.png"
        assert cv2.imwrite(str(path), image)
        captures.append(
            {
                "id": f"capture-{index}",
                "path": str(path),
                "pose": {"pan": None, "tilt": None, "zoom": None},
            }
        )
    return captures, focal


def test_reconstructs_without_camera_parameters_and_preserves_unknown_coverage(
    tmp_path, monkeypatch
) -> None:
    captures, focal = _photographs(tmp_path)
    monkeypatch.setattr(reconstruction, "OUTPUT_WIDTH", 512)
    monkeypatch.setattr(reconstruction, "OUTPUT_HEIGHT", 256)
    events = []
    report = reconstruction.reconstruct_panorama(
        captures, tmp_path / "result", progress=events.append
    )
    assert report["source_ids"] == [capture["id"] for capture in captures]
    assert report["positioning_status"] == "not_validated"
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["domain_status"] == "unknown_from_photographs"
    assert report["quality"]["holdout_error_pixels"]["p95"] < 3
    assert report["model"]["lens"]["fx"] == pytest.approx(focal, rel=0.10)
    assert events[-1]["stage"] == "complete"
    for relative in report["files"].values():
        assert (tmp_path / "result" / relative).is_file()
    panorama = cv2.imread(str(tmp_path / "result" / report["files"]["panorama"]))
    coverage = cv2.imread(
        str(tmp_path / "result" / report["files"]["coverage"]), cv2.IMREAD_GRAYSCALE
    )
    sources = np.load(tmp_path / "result" / report["files"]["source_indices"], allow_pickle=False)
    assert np.all(panorama[coverage == 0] == 0)
    assert np.array_equal(sources >= 0, coverage > 0)
    assert str(tmp_path) not in json.dumps(report, allow_nan=False)


def test_cancel_and_invalid_inputs_do_not_publish_results(tmp_path) -> None:
    with pytest.raises(reconstruction.ReconstructionCancelled):
        reconstruction.reconstruct_panorama([], tmp_path / "cancelled", cancelled=lambda: True)
    assert not (tmp_path / "cancelled").exists()
    with pytest.raises(reconstruction.ReconstructionError, match="two"):
        reconstruction.reconstruct_panorama([], tmp_path / "empty")
    captures = [{"id": "a", "path": "relative.png"}, {"id": "b", "path": "relative.png"}]
    with pytest.raises(reconstruction.ReconstructionError) as error:
        reconstruction.reconstruct_panorama(captures, tmp_path / "relative")
    assert error.value.code == "capture_path"


def test_uniform_images_are_not_a_valid_panorama(tmp_path) -> None:
    captures = []
    for index in range(2):
        path = tmp_path / f"flat-{index}.png"
        cv2.imwrite(str(path), np.full((120, 160, 3), 127, np.uint8))
        captures.append({"id": str(index), "path": str(path)})
    with pytest.raises(reconstruction.ReconstructionError) as error:
        reconstruction.reconstruct_panorama(captures, tmp_path / "result")
    assert error.value.code == "insufficient_overlap"
    assert not (tmp_path / "result" / "report.json").exists()


def test_reported_zoom_change_is_not_absorbed_into_a_shared_lens(tmp_path) -> None:
    captures, _ = _photographs(tmp_path)
    captures[0]["pose"]["zoom"] = 0.1
    captures[1]["pose"]["zoom"] = 0.6
    with pytest.raises(reconstruction.ReconstructionError) as error:
        reconstruction.reconstruct_panorama(captures, tmp_path / "result")
    assert error.value.code == "optical_identity_changed"


def test_rotation_reflection_is_not_silently_repaired() -> None:
    # A proper camera rotation has determinant +1; the legacy panorama basis
    # change is applied separately and must not be guessed from a malformed R.
    rotation = Rotation.from_rotvec([0, 0, math.pi / 2]).as_matrix()
    assert np.linalg.det(rotation) == pytest.approx(1)
    assert np.linalg.det(reconstruction._rotation_basis(rotation)) == pytest.approx(-1)
