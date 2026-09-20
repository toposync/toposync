"""Synthetic API geometry checks, not physical camera qualification."""

from dataclasses import asdict

import pytest

from test_cameras_mapping_api import _create_client_with_cameras, _valid_ray_ground_view
from test_metric_camera import projection_fixture


def _metric_view(lens_type="rectilinear_brown_v1"):
    projection = projection_fixture(lens_type)
    view = _valid_ray_ground_view()
    view["projection_model"]["lens"] = asdict(projection.lens)
    view["projection_model"]["correspondences"] = [
        {
            "id": point.id,
            "role": point.role,
            "image": {"x": point.image_u, "y": point.image_v},
            "world": {"x": point.world_x, "z": point.world_z},
        }
        for point in projection.points
    ]
    return view


def _solve(client, view):
    response = client.post("/api/cameras/projection/solve", json={"calibrated_view": view})
    assert response.status_code == 200, response.text
    return response.json()


def test_identity_lens_keeps_legacy_ground_acceptance_without_metric_claim(tmp_path, monkeypatch):
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, _valid_ray_ground_view())
    assert result["accepted"] is True
    assert result["status"] == result["quality"]["status"] == "ready"
    assert result["metric_geometry"] == {
        "status": "unavailable",
        "reason": "metric_intrinsics_required",
    }


@pytest.mark.parametrize("lens_type", ["rectilinear_brown_v1", "fisheye_kb4_v1"])
def test_known_camera_exposes_only_metric_diagnostic(tmp_path, monkeypatch, lens_type):
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, _metric_view(lens_type))
    assert result["accepted"] is True
    diagnostic = result["metric_geometry"]
    assert set(diagnostic) == {"status", "maximum_reprojection_error", "check_errors_meters"}
    assert diagnostic["status"] == "ready"
    assert diagnostic["maximum_reprojection_error"] < 1e-6
    assert len(diagnostic["check_errors_meters"]) == 2
    assert max(diagnostic["check_errors_meters"]) < 1e-5
    assert "camera_center" not in result
    assert "world_to_camera" not in result


def test_six_plus_two_homography_does_not_prove_metric_camera(tmp_path, monkeypatch):
    view = _metric_view()
    for point in view["projection_model"]["correspondences"]:
        point["world"]["x"] = 2 * point["world"]["x"] + point["world"]["z"]
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, view)
    assert result["accepted"] is True  # Exact homography despite incompatible intrinsics.
    assert result["quality"]["number_of_fit_points"] == 6
    assert max(result["quality"]["check_errors_meters"]) < 1e-5
    assert result["metric_geometry"] == {
        "status": "unavailable",
        "reason": "metric_camera_reprojection_or_check_failed",
    }


@pytest.mark.parametrize("check_error,metric_ready", [(0.29, True), (0.31, False), (0.4, False)])
def test_metric_check_limit_is_independent_from_legacy_half_meter_limit(
    tmp_path, monkeypatch, check_error, metric_ready
):
    view = _metric_view()
    for point in view["projection_model"]["correspondences"]:
        # Scaling the measured scene keeps image coverage unchanged and makes
        # the check's image reprojection error < .005 on both sides of .30 m.
        point["world"]["x"] *= 100
        point["world"]["z"] *= 100
        if point["role"] == "check":
            point["world"]["x"] += check_error
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, view)
    assert result["accepted"] is True
    assert result["quality"]["check_errors_meters"] == pytest.approx([check_error] * 2, abs=1e-5)
    if metric_ready:
        assert result["metric_geometry"]["status"] == "ready"
        assert result["metric_geometry"]["maximum_reprojection_error"] < 0.005
        assert result["metric_geometry"]["check_errors_meters"] == pytest.approx(
            [check_error] * 2, abs=1e-5
        )
    else:
        assert result["metric_geometry"] == {
            "status": "unavailable",
            "reason": "metric_camera_reprojection_or_check_failed",
        }


def test_incomplete_ground_stays_unaccepted_with_metric_reason(tmp_path, monkeypatch):
    view = _metric_view()
    view["projection_model"]["correspondences"].pop()
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, view)
    assert result["accepted"] is False
    assert result["metric_geometry"] == {
        "status": "unavailable",
        "reason": "validated_ground_calibration_required",
    }


@pytest.mark.parametrize(
    "failure", [RuntimeError("private engine details"), ValueError("private details")]
)
def test_metric_failure_does_not_break_legacy_response_or_expose_details(
    tmp_path, monkeypatch, failure
):
    from toposync_ext_cameras.processing import metric_camera

    def fail(_projection):
        raise failure

    monkeypatch.setattr(metric_camera, "solve_metric_camera", fail)
    with _create_client_with_cameras(tmp_path, monkeypatch) as client:
        result = _solve(client, _metric_view())
    assert result["accepted"] is True
    assert result["metric_geometry"] == {
        "status": "unavailable",
        "reason": "metric_camera_solution_unavailable",
    }
