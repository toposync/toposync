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
    mapping_extrapolated,
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
        {"id": str(index), "world_x": x, "world_z": z, "ray": _ground_ray(x, z)}
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


def test_four_controls_recover_known_plane_and_extrapolate_both_directions() -> None:
    result = estimate_panorama_mapping(_points()[:4])
    assert result["preview"] == {"eligible": True, "status": "ready", "reasons": []}
    assert result["quality"]["warnings"] == []
    json.dumps(result, allow_nan=False)
    for x, z in ((-4, .01), (-4, -.01), (0, 0), (6, 0), (6.0001, 0), (80, 25)):
        ray = map_world_to_ray(result, x, z)
        assert ray == pytest.approx(_ground_ray(x, z), abs=1e-8)
        assert map_ray_to_world(result, ray) == pytest.approx((x, z), abs=1e-8)
        assert map_ray_to_world(result, [-value for value in ray]) is None
        assert mapping_extrapolated(result, x, z) == (x > 6)
    assert map_ray_to_world(result, [1, 0, 0]) is None


def test_every_control_influences_approximate_fit_without_an_error_gate() -> None:
    original = _points()
    reference = estimate_panorama_mapping(original)
    changed = copy.deepcopy(original)
    changed[0]["ray"] = _ground_ray(-6, -6, height=8)
    result = estimate_panorama_mapping(changed)
    assert result["matrix"] != reference["matrix"]
    assert result["preview"]["eligible"] is True
    assert result["quality"]["status"] == "ready"
    assert result["quality"]["warnings"] == ["approximate_fit"]
    assert len(result["quality"]["point_errors"]) == len(original)
    assert {point["id"] for point in result["quality"]["point_errors"]} == {point["id"] for point in original}
    assert estimate_panorama_mapping(changed) == result
    assert map_world_to_ray(result, 1, 2) != pytest.approx(map_world_to_ray(reference, 1, 2), abs=.001)


@pytest.mark.parametrize("count", range(4))
def test_preview_requires_four_complete_controls(count: int) -> None:
    result = estimate_panorama_mapping(_points()[:count])
    assert result["quality"]["reasons"] == ["at_least_four_fit_points_required"]
    assert result["preview"]["eligible"] is False


def test_additional_noisy_controls_remain_usable_and_deterministic() -> None:
    points = _points()
    generator = np.random.default_rng(19)
    for point in points:
        point["ray"] = (np.array(point["ray"]) + generator.normal(0, .008, 3)).tolist()
    result = estimate_panorama_mapping(points)
    assert result["preview"]["eligible"] is True
    assert len(result["quality"]["point_errors"]) == len(points)
    assert estimate_panorama_mapping(points) == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("number_of_fit_points", 3),
        ("number_of_fit_points", True),
        ("median_fit_error_radians", None),
        ("p95_fit_error_radians", math.nan),
        ("angular_threshold_radians", 0),
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


def test_refinement_failure_keeps_validated_linear_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_refinement(*args: object) -> None:
        raise np.linalg.LinAlgError("Synthetic singular refinement")

    monkeypatch.setattr(
        "toposync_ext_cameras.processing.panorama_mapping._refine_angular", fail_refinement
    )
    result = estimate_panorama_mapping(_points())
    assert result["quality"]["status"] == "ready"
    assert result["quality"]["p95_fit_error_radians"] < 1e-8


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
