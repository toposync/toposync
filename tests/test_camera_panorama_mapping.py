from __future__ import annotations

import copy
import json
import math

import cv2
import numpy as np
import pytest

from toposync_ext_cameras.processing.panorama_mapping import (
    estimate_panorama_mapping,
    image_pixel_to_ray,
    map_ray_to_world,
    map_world_to_ray,
    panorama_pixel_to_ray,
    pan_tilt_to_ray,
    preview_panorama_mapping,
    ray_to_image_pixel,
    ray_to_panorama_pixel,
    ray_to_pan_tilt,
    render_panorama,
)


def _ground_ray(x: float, z: float, *, height: float = 3.0) -> list[float]:
    # Independent physical oracle: camera at (0, 0, height), ground at Z=0.
    distance = math.sqrt(x * x + z * z + height * height)
    return [x / distance, z / distance, -height / distance]


def _points() -> list[dict]:
    return [
        {"id": str(index), "role": "fit", "world_x": x, "world_z": z, "ray": _ground_ray(x, z)}
        for index, (x, z) in enumerate(
            [
                (-6, -6),
                (6, -6),
                (6, 6),
                (-6, 6),
                (0, -6),
                (0, 6),
                (-6, 0),
                (6, 0),
            ]
        )
    ] + [
        {
            "id": "check-one",
            "role": "check",
            "world_x": 2.0,
            "world_z": 1.0,
            "ray": _ground_ray(2, 1),
        },
        {
            "id": "check-two",
            "role": "check",
            "world_x": -2.0,
            "world_z": -1.0,
            "ray": _ground_ray(-2, -1),
        },
    ]


def _lens() -> dict:
    return {
        "width": 101,
        "height": 81,
        "fx": 70.0,
        "fy": 72.0,
        "cx": 50.0,
        "cy": 40.0,
        "distortion": [],
    }


def test_full_circle_angles_and_poles_have_defined_conventions() -> None:
    assert pan_tilt_to_ray(0, 0) == pytest.approx((1, 0, 0))
    assert pan_tilt_to_ray(math.pi / 2, 0) == pytest.approx((0, 1, 0))
    assert pan_tilt_to_ray(2 * math.pi, 0) == pytest.approx((1, 0, 0))
    assert pan_tilt_to_ray(0, math.pi / 2) == pytest.approx((0, 0, 1))
    assert ray_to_pan_tilt([0, 0, -1]) == pytest.approx((0, -math.pi / 2))
    assert panorama_pixel_to_ray(0, 0.5) == pytest.approx(panorama_pixel_to_ray(1, 0.5))
    for pan in (-math.pi + 0.001, -2.0, 0.0, math.pi - 0.001):
        ray = pan_tilt_to_ray(pan, -0.7)
        assert ray_to_pan_tilt(ray) == pytest.approx((pan, -0.7))
        assert panorama_pixel_to_ray(*ray_to_panorama_pixel(ray)) == pytest.approx(ray)
    with pytest.raises(ValueError):
        pan_tilt_to_ray(0, 2)
    with pytest.raises(ValueError):
        ray_to_pan_tilt([0, 0, 0])


def test_optical_pixels_use_intrinsics_and_the_observed_pose() -> None:
    lens = _lens()
    assert image_pixel_to_ray(50, 40, lens, 0, 0) == pytest.approx((1, 0, 0))
    assert image_pixel_to_ray(50, 40, lens, math.pi / 2, 0) == pytest.approx((0, 1, 0))
    # One positive focal offset points right (positive pan); top pixels point up.
    expected = np.array([1.0, 0.5, 0.25])
    expected /= np.linalg.norm(expected)
    assert image_pixel_to_ray(85, 22, lens, 0, 0) == pytest.approx(expected)
    for coefficients in ([], [0.01, -0.002, 0.001, -0.001], [0.01, 0, 0, 0, 0.0001, 0.001, 0, 0]):
        lens["distortion"] = coefficients
        direction = image_pixel_to_ray(65, 55, lens, -2.8, -0.4)
        assert ray_to_image_pixel(direction, lens, -2.8, -0.4) == pytest.approx((65, 55), abs=1e-5)
    assert ray_to_image_pixel([-1, 0, 0], lens, 0, 0) is None
    with pytest.raises(ValueError):
        image_pixel_to_ray(-1, 20, lens, 0, 0)
    with pytest.raises(ValueError):
        image_pixel_to_ray(1, 1, {**lens, "fx": 0}, 0, 0)


def test_known_plane_recovers_full_pan_support_and_rejects_opposite_rays() -> None:
    result = estimate_panorama_mapping(_points())
    assert result["quality"]["status"] == "ready"
    assert result["preview"] == {"eligible": True, "status": "ready", "reasons": []}
    assert result["quality"]["maximum_check_error_radians"] < 1e-8
    assert len(result["support_polygon"]) == 4
    json.dumps(result, allow_nan=False)
    for x, z in ((-4, 0.01), (-4, -0.01), (4, 2), (-5, 5), (0, 0)):
        ray = map_world_to_ray(result, x, z)
        assert ray == pytest.approx(_ground_ray(x, z), abs=1e-8)
        assert map_ray_to_world(result, ray) == pytest.approx((x, z), abs=1e-8)
        assert map_ray_to_world(result, [-value for value in ray]) is None
    assert map_world_to_ray(result, 8, 0) is None
    assert map_ray_to_world(result, _ground_ray(8, 0)) is None
    assert map_ray_to_world(result, [1, 0, 0]) is None


def test_noise_and_an_outlier_do_not_corrupt_independent_predictions() -> None:
    points = _points()
    generator = np.random.default_rng(21)
    for point in points:
        if point["role"] == "fit":
            point["ray"] = (np.array(point["ray"]) + generator.normal(0, 0.0005, 3)).tolist()
    points[4]["ray"] = [0, 0, 1]
    result = estimate_panorama_mapping(points, angular_threshold_radians=0.01)
    assert result["quality"]["status"] == "ready"
    assert "4" not in result["inlier_point_ids"]
    assert result["quality"]["number_of_inliers"] == 7
    assert result["quality"]["maximum_check_error_radians"] < 0.005
    assert result["preview"]["eligible"] is True
    assert map_world_to_ray(result, 1, 2) == pytest.approx(_ground_ray(1, 2), abs=0.002)
    points[5]["ray"] = [0, 0, 1]
    insufficient = estimate_panorama_mapping(points, angular_threshold_radians=0.01)
    assert insufficient["quality"]["number_of_inliers"] == 6
    assert insufficient["quality"]["inlier_ratio"] == 0.75
    assert insufficient["preview"]["eligible"] is False


def test_holdouts_do_not_fit_the_model_and_must_be_independent() -> None:
    original = _points()
    reference = estimate_panorama_mapping(original)
    changed = copy.deepcopy(original)
    changed[-1]["ray"] = _ground_ray(-2, -1, height=1.0)
    rejected = estimate_panorama_mapping(changed)
    assert rejected["matrix"] == reference["matrix"]
    assert rejected["quality"]["status"] == "review"
    assert "check_error_exceeds_threshold" in rejected["quality"]["reasons"]
    assert rejected["preview"]["eligible"] is False
    original[-1] = {**original[0], "id": "copied-fit", "role": "check"}
    assert estimate_panorama_mapping(original)["quality"]["status"] == "review"
    original[-1] = {**original[-2], "id": "copied-check"}
    assert estimate_panorama_mapping(original)["quality"]["status"] == "review"
    assert estimate_panorama_mapping(original[:-2])["quality"]["status"] == "review"
    assert estimate_panorama_mapping(original[:5])["quality"]["status"] == "incomplete"


def test_preview_allows_only_missing_checks_and_keeps_activation_quality() -> None:
    for points in (_points()[:6], _points()[:-2], _points()[:-1]):
        result = estimate_panorama_mapping(points)
        assert result["quality"]["status"] == "review"
        assert result["preview"] == {
            "eligible": True,
            "status": "provisional",
            "reasons": ["at_least_two_independent_check_points_required"],
        }
    points = _points()
    points[-1] = {**points[0], "id": "copied-fit", "role": "check"}
    assert estimate_panorama_mapping(points)["preview"]["eligible"] is False
    points = _points()
    points[-1].update(world_x=20, world_z=20, ray=_ground_ray(20, 20))
    assert estimate_panorama_mapping(points)["preview"]["eligible"] is False
    assert estimate_panorama_mapping(points[:5])["preview"]["eligible"] is False
    for point in points[:8]:
        point.update(world_z=point["world_x"], ray=_ground_ray(point["world_x"], point["world_x"]))
    assert estimate_panorama_mapping(points)["preview"]["eligible"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("number_of_inliers", 5),
        ("inlier_ratio", 0.79),
        ("number_of_fit_points", True),
        ("median_fit_error_radians", None),
        ("p95_fit_error_radians", math.nan),
        ("angular_threshold_radians", 0),
        ("maximum_check_error_radians", 0.1),
        ("number_of_check_points", 0),
        ("status", "unknown"),
        ("status", []),
        ("status", {}),
        ("reasons", ["unclassified_future_reason"]),
        ("reasons", ["check_error_exceeds_threshold"]),
        ("reasons", None),
    ],
)
def test_preview_rechecks_explicit_quality_evidence(field: str, value: object) -> None:
    result = estimate_panorama_mapping(_points())
    result["quality"][field] = value
    assert preview_panorama_mapping(result)["eligible"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "unknown_projection"),
        ("quality", None),
        ("matrix", None),
        ("matrix", [[1, 0, 0], [0, 1, 0], [0, 0, 0]]),
        ("matrix", [[1, 0, 0], [0, 1, 0], [0, 0, math.inf]]),
        ("inverse_matrix", [[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
        ("support_polygon", []),
        ("support_polygon", [[0, 0], [1, 1], [2, 2]]),
        ("support_polygon", [[0, 0], [1, 1], [0, 1], [1, 0]]),
        ("support_polygon", [[0, 0], [1, 1], [0, math.nan]]),
        ("support_polygon", [[-1e308, -1e308], [1e308, -1e308], [1e308, 1e308], [-1e308, 1e308]]),
    ],
)
def test_preview_rejects_invalid_or_stale_geometry(field: str, value: object) -> None:
    result = estimate_panorama_mapping(_points())
    result[field] = value
    assert preview_panorama_mapping(result)["eligible"] is False


def test_collinearity_duplicate_points_and_antipodal_inconsistency_are_rejected() -> None:
    points = _points()
    for index, point in enumerate(points):
        point.update(world_x=index, world_z=index * 1e-8, ray=_ground_ray(index, index * 1e-8))
    assert estimate_panorama_mapping(points)["matrix"] is None
    points = _points()
    points[1].update(world_x=points[0]["world_x"], world_z=points[0]["world_z"])
    assert estimate_panorama_mapping(points)["matrix"] is None
    points = _points()
    for point in points[:4]:
        point["ray"] = [-value for value in point["ray"]]
    assert estimate_panorama_mapping(points)["quality"]["status"] != "ready"
    points = _points()
    points[0]["world_x"] = math.nan
    with pytest.raises(ValueError):
        estimate_panorama_mapping(points)


def test_geometric_render_preserves_source_coverage_and_independent_pixel_rays() -> None:
    lens = _lens()
    left = np.zeros((81, 101, 3), np.uint8)
    left[:, :, 0] = np.arange(101)
    left[:, :, 1] = np.arange(81)[:, None]
    right = np.full((81, 101, 3), (0, 0, 255), np.uint8)
    result = render_panorama(
        [
            {"id": "front", "image": left, "lens": lens, "pan_radians": 0, "tilt_radians": 0},
            {"id": "back", "image": right, "lens": lens, "pan_radians": math.pi, "tilt_radians": 0},
        ],
        width=360,
        height=180,
    )
    assert result["image"].shape == (180, 360, 3)
    assert 0 < result["coverage_ratio"] < 1
    assert np.all(result["coverage_mask"] == ((result["source_indices"] >= 0) * 255))
    assert result["source_indices"][90, 180] == 0
    assert result["source_indices"][90, 0] == 1
    assert result["source_indices"][90, -1] == 1
    assert result["source_indices"][0, 180] == -1
    assert np.all(result["image"][result["source_indices"] < 0] == 0)
    # Independent gnomonic projection for one output pixel, without production helpers.
    column, row = 193, 82
    pan = ((column + 0.5) / 360 * 2 - 1) * math.pi
    elevation = (0.5 - (row + 0.5) / 180) * math.pi
    expected_x = lens["cx"] + lens["fx"] * math.tan(pan)
    expected_y = lens["cy"] - lens["fy"] * math.tan(elevation) / math.cos(pan)
    assert result["image"][row, column, :2] == pytest.approx((expected_x, expected_y), abs=1)


def test_large_render_handles_opencv_remap_dimension_limit() -> None:
    result = render_panorama(
        [
            {
                "id": "wide",
                "image": np.full((81, 101, 3), 200, np.uint8),
                "lens": {**_lens(), "fx": 30, "fy": 30},
                "pan_radians": 0,
                "tilt_radians": 0,
            }
        ],
        width=1024,
        height=512,
    )
    assert np.count_nonzero(result["coverage_mask"]) > 32767
    assert np.all(result["image"][result["source_indices"] >= 0] == 200)


def test_bad_render_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        render_panorama([], width=100, height=100)
    with pytest.raises(ValueError):
        render_panorama(
            [
                {
                    "id": "wrong",
                    "image": np.zeros((2, 2, 3), np.uint8),
                    "lens": _lens(),
                    "pan_radians": 0,
                    "tilt_radians": 0,
                }
            ]
        )
    assert cv2.__version__  # Rendering uses the already-installed camera dependency.
