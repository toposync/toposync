"""Synthetic geometry tests; not real-person model qualification."""

from dataclasses import replace
import asyncio
import json

import numpy as np
import pytest

from toposync_ext_cameras.processing.mapping import (
    GroundCalibrationPoint,
    GroundLens,
    GroundProjectionSpec,
)
from toposync_ext_cameras.processing.metric_camera import MetricCamera, solve_metric_camera


def known_camera(lens_type="rectilinear_brown_v1"):
    camera = MetricCamera(
        GroundLens(lens_type, 0.8, 0.8, 0.5, 0.5, (0.01, -0.002, 0, 0)),
        (0.0, 3.0, 4.0),
        ((1.0, 0.0, 0.0), (0.0, -0.8, 0.6), (0.0, -0.6, -0.8)),
        ((-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)),
        ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        "fixture",
        0.0,
        (),
    )
    from toposync_ext_cameras.processing.mapping import _convex_hull

    return replace(
        camera,
        ground_image_polygon=tuple(
            _convex_hull([camera.project((x, 0, z)) for x, z in camera.ground_polygon])
        ),
    )


def projection_fixture(lens_type="rectilinear_brown_v1"):
    camera = known_camera(lens_type)
    coordinates = [(-2, -2), (2, -2), (2, 2), (-2, 2), (0, -2), (0, 2), (-0.5, -0.5), (0.5, 0.5)]
    points = []
    for index, (x, z) in enumerate(coordinates):
        u, v = camera.project((x, 0, z))
        points.append(
            GroundCalibrationPoint(str(index), "fit" if index < 6 else "check", u, v, x, z)
        )
    return GroundProjectionSpec(camera.lens, tuple(points))


@pytest.mark.parametrize("lens_type", ["rectilinear_brown_v1", "fisheye_kb4_v1"])
def test_metric_camera_recovers_known_center_rotation_and_elevated_joint(lens_type):
    expected = known_camera(lens_type)
    solved = solve_metric_camera(projection_fixture(lens_type))
    assert solved.camera_center == pytest.approx(expected.camera_center, abs=1e-5)
    assert np.asarray(solved.world_to_camera) == pytest.approx(
        np.asarray(expected.world_to_camera), abs=1e-5
    )
    for point in [(0.0, 0.0, 0.0), (0.3, 1.4, -0.5), (-0.4, 1.8, 0.3)]:
        image = expected.project(point)
        assert solved.project(point) == pytest.approx(image, abs=1e-6)
        assert solved.point_at_height(image, point[1]) == pytest.approx(point, abs=1e-5)
    assert solved.maximum_reprojection_error < 1e-6
    assert max(solved.check_errors_meters) < 1e-5


def test_metric_camera_requires_intrinsics_and_independent_checks():
    projection = projection_fixture()
    with pytest.raises(ValueError, match="metric_intrinsics_required"):
        solve_metric_camera(replace(projection, lens=GroundLens("identity_rectilinear_v1")))
    with pytest.raises(ValueError, match="validated_ground_calibration_required"):
        solve_metric_camera(replace(projection, points=projection.points[:6]))


def test_metric_camera_rejects_failed_independent_checks():
    projection = projection_fixture()
    corrupt = tuple(
        replace(p, world_x=p.world_x + 2) if p.role == "check" else p for p in projection.points
    )
    with pytest.raises(ValueError, match="validated_ground_calibration_required"):
        solve_metric_camera(replace(projection, points=corrupt))


def test_ray_rejects_behind_parallel_and_outside_domain_without_clipping():
    camera = known_camera()
    assert camera.point_at_height(camera.project((3, 0, 0)), 0) is None
    assert camera.point_at_height((0.5, 0.5), 4) is None
    assert camera.point_at_height((float("nan"), 0.5), 0) is None
    assert camera.project((0, 3, 5)) is None
    origin, direction = camera.ray((1.2, 0.5))
    assert np.isfinite(origin).all() and np.isfinite(direction).all()
    assert camera.project(tuple(origin + 3 * direction)) == pytest.approx((1.2, 0.5), abs=1e-6)


def test_calibration_digest_changes_with_measurements():
    projection = projection_fixture()
    changed = replace(projection, lens=replace(projection.lens, fx=projection.lens.fx + 0.00001))
    assert (
        solve_metric_camera(projection).calibration_digest
        != solve_metric_camera(changed).calibration_digest
    )


def test_metric_domain_preserves_both_image_and_world_ground_boundaries():
    from toposync_ext_cameras.processing.mapping import GroundPlaneMapper

    projection = projection_fixture("fisheye_kb4_v1")
    camera = solve_metric_camera(projection)
    mapper = GroundPlaneMapper(projection)
    image = camera.project((1, 0, 1.99))
    assert mapper.map(*image) is None
    assert camera.point_at_height(image, 0) is None


def test_metric_checks_must_not_duplicate_fit_or_other_check_points():
    projection = projection_fixture()
    copied = tuple(
        replace(point, role="check", id=f"copied-{index}")
        for index, point in enumerate(projection.points[:2])
    )
    with pytest.raises(ValueError, match="independent_calibration_checks_required"):
        solve_metric_camera(replace(projection, points=projection.points[:6] + copied))


def test_metric_camera_contract_roundtrip_and_rejects_nonrotation():
    camera = solve_metric_camera(projection_fixture())
    raw = json.loads(json.dumps(camera.to_dict(), allow_nan=False))
    restored = MetricCamera.from_dict(raw)
    assert restored.project((0.3, 1.4, -0.5)) == pytest.approx(camera.project((0.3, 1.4, -0.5)))
    raw["world_to_camera"][0][0] = 2
    with pytest.raises(ValueError, match="invalid_metric_camera_geometry"):
        MetricCamera.from_dict(raw)


@pytest.mark.parametrize(
    "polygon", [[[0, 0]] * 3, [[0, 0], [1, 1], [2, 2]], [[-1, -1], [1, 1], [1, -1], [-1, 1]]]
)
def test_metric_camera_rejects_degenerate_or_crossed_support_domain(polygon):
    raw = solve_metric_camera(projection_fixture()).to_dict()
    raw["ground_polygon"] = polygon
    with pytest.raises(ValueError, match="invalid_metric_ground_domain"):
        MetricCamera.from_dict(raw)


def test_mapping_attaches_current_metric_calibration_even_when_bbox_is_not_ground():
    from dataclasses import asdict
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Lifecycle, Packet
    from toposync_ext_cameras.pipelines.postprocess import CameraMappingRuntime

    async def scenario():
        projection = projection_fixture()
        view = {
            "id": "fixed",
            "stream_scope": {"physical_view_id": "fixed", "compatible_source_ids": ["recording"]},
            "projection_quality": {"status": "ready"},
            "projection_model": {
                "type": "camera_ray_ground_v2",
                "source_geometry": {"width": 1280, "height": 720},
                "lens": asdict(projection.lens),
                "correspondences": [
                    {
                        "id": p.id,
                        "role": p.role,
                        "image": {"x": p.image_u, "y": p.image_v},
                        "world": {"x": p.world_x, "z": p.world_z},
                    }
                    for p in projection.points
                ],
            },
        }
        config = {
            "calibrated_views": [view],
            "attach_metric_camera": True,
            "ptz_state_fetch": {"enabled": False},
        }
        runtime = CameraMappingRuntime(config, PipelineRuntimeDependencies())
        payload = {
            "camera_id": "test",
            "media": {"width": 1280, "height": 720},
            "source": {"source_id": "recording", "view_id": "fixed"},
            "image_uv": {"u": 0.02, "v": 0.02},
        }
        packet = Packet.create(stream_id="camera:test", payload=payload)
        result = (await runtime.process_packet(packet, None))[0]
        assert result.payload["mapping"]["status"] == "unmapped"
        metric = result.payload["spatial"]["camera"]
        assert metric["status"] == "ready"
        assert metric["frame_packet_id"] == packet.packet_id
        assert MetricCamera.from_dict(metric["geometry"]).camera_center == pytest.approx((0, 3, 4))
        # Wrong source and CLOSE must not retain the preceding geometry.
        changed = Packet.create(
            stream_id="camera:test",
            payload={**result.payload, "source": {"source_id": "other", "view_id": "other"}},
        )
        invalid = (await runtime.process_packet(changed, None))[0]
        assert invalid.payload["spatial"]["camera"]["status"] == "unavailable"
        assert "geometry" not in invalid.payload["spatial"]["camera"]
        closed = (await runtime.process_packet(replace(result, lifecycle=Lifecycle.CLOSE), None))[0]
        assert closed.payload["spatial"]["camera"]["reason"] == "subject_closed"
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,expected",
    [
        ("aspect", "metric_source_aspect_ratio_changed"),
        ("mirror", "metric_source_transform_requires_calibration"),
        ("rotation", "metric_source_transform_requires_calibration"),
        ("unknown_size", "metric_source_dimensions_required"),
        ("old_pose", "metric_pose_not_bound_to_frame"),
    ],
)
def test_mapping_rejects_changed_source_geometry_and_stale_pose(case, expected):
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Packet
    from toposync_ext_cameras.pipelines.postprocess import CameraMappingRuntime
    from test_person_ground import mapping_config

    async def scenario():
        config = mapping_config()
        config["attach_metric_camera"] = True
        view = config["calibrated_views"][0]
        geometry = view["projection_model"]["source_geometry"]
        if case == "mirror":
            geometry["mirror_x"] = True
        if case == "rotation":
            geometry["rotation_degrees"] = 90
        if case == "old_pose":
            view["pose_reference"] = {"pan": 0, "tilt": 0, "zoom": 1}
            view["requires_pose_evidence"] = True
        runtime = CameraMappingRuntime(config, PipelineRuntimeDependencies())
        payload = {
            "camera_id": "camera",
            "source": {"source_id": "recording", "view_id": "fixed"},
            "media": {"ts": 1000.0, "width": 1280, "height": 720},
            "image_uv": {"u": 0.5, "v": 0.5},
        }
        if case == "aspect":
            payload["media"]["width"] = 720
        if case == "unknown_size":
            payload["media"] = {"ts": 1000.0}
        if case == "old_pose":
            payload["pan_tilt_zoom_state"] = {
                "pan": 0,
                "tilt": 0,
                "zoom": 1,
                "geometry_safe": True,
                "move_status": "idle",
                "physical_updated_at": 1.0,
            }
        result = (
            await runtime.process_packet(Packet.create(stream_id="camera", payload=payload), None)
        )[0]
        record = result.payload["spatial"]["camera"]
        assert record["status"] == "unavailable"
        assert record["reason"] == expected
        assert "geometry" not in record
        await runtime.shutdown()

    asyncio.run(scenario())
