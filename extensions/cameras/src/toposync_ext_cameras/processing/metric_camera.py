"""Metric camera geometry derived from measured intrinsics and ground points.

Unlike a ground homography, this model can cast rays above the support plane.
World coordinates are Toposync metres (Y up); camera axes are OpenCV right,
down, forward. A low reprojection error is not anatomical-model validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math

import numpy as np

from .mapping import (
    GroundLens,
    GroundPlaneMapper,
    GroundProjectionSpec,
    _ground_lens_ideal_to_image,
    _point_in_convex_polygon,
    _convex_hull,
)


def validate_pose_image_axes(packet, pose):
    """Require current source pixels and an upright uniform image transform.

    Image-plane rotation, reflection, shear and projective warps do not preserve
    the camera-axis convention of hip-relative model coordinates.
    """
    from toposync.runtime.pipelines.image_geometry import geometry_matrix

    metadata = pose.get("metadata")
    estimate = metadata.get("image_estimate") if isinstance(metadata, dict) else None
    if not isinstance(estimate, dict) or not isinstance(estimate.get("artifact_name"), str):
        raise ValueError("pose_3d_source_geometry_required")
    artifact = packet.artifacts.get(estimate.get("artifact_name"))
    geometry = artifact.metadata.get("image_geometry") if artifact is not None else None
    if not geometry:
        raise ValueError("pose_3d_source_geometry_required")
    shape = getattr(artifact.data, "shape", ())
    if len(shape) < 2:
        raise ValueError("pose_3d_source_pixels_required")
    matrix = geometry_matrix(geometry, shape[1], shape[0])
    if (
        not np.allclose(matrix[[0, 1, 2, 2, 2], [1, 0, 0, 1, 2]], [0, 0, 0, 0, 1])
        or matrix[0, 0] <= 0
        or not math.isclose(matrix[0, 0], matrix[1, 1], rel_tol=1e-6)
    ):
        raise ValueError("pose_3d_source_orientation_not_supported")
    evidence = packet.payload.get("capture_evidence")
    if evidence is not None and geometry.get("capture_evidence") != evidence:
        raise ValueError("pose_3d_capture_evidence_mismatch")


def image_ray_camera(lens: GroundLens, image: tuple[float, float]) -> np.ndarray:
    """Undistort without clipping inferred joints to the sensor boundary."""
    import cv2

    if lens.type == "identity_rectilinear_v1":
        raise ValueError("metric_intrinsics_required")
    values = (*image, lens.fx, lens.fy, lens.cx, lens.cy, *lens.coefficients)
    if not all(math.isfinite(value) for value in values) or min(lens.fx, lens.fy) <= 0:
        raise ValueError("invalid_metric_intrinsics_or_image_point")
    matrix = np.array([[lens.fx, 0, lens.cx], [0, lens.fy, lens.cy], [0, 0, 1]], dtype=float)
    coefficients = np.array(lens.coefficients, dtype=float)
    points = np.array(image, dtype=float).reshape(1, 1, 2)
    if lens.type == "fisheye_kb4_v1" and len(coefficients) == 4:
        ideal = cv2.fisheye.undistortPoints(points, matrix, coefficients)
    elif lens.type == "rectilinear_brown_v1" and len(coefficients) in (4, 5, 8):
        ideal = cv2.undistortPoints(points, matrix, coefficients)
    else:
        raise ValueError("unsupported_metric_lens")
    ray = np.array([*ideal.reshape(2), 1.0])
    if not np.isfinite(ray).all():
        raise ValueError("invalid_metric_ray")
    return ray / np.linalg.norm(ray)


@dataclass(frozen=True)
class MetricCamera:
    lens: GroundLens
    camera_center: tuple[float, float, float]
    world_to_camera: tuple[tuple[float, float, float], ...]
    ground_polygon: tuple[tuple[float, float], ...]
    ground_image_polygon: tuple[tuple[float, float], ...]
    calibration_digest: str
    maximum_reprojection_error: float
    check_errors_meters: tuple[float, ...]

    def to_dict(self) -> dict:
        return {"schema_version": 1, "units": "meters", "world_axes": "x_y_up_z", **asdict(self)}

    @classmethod
    def from_dict(cls, value: dict) -> MetricCamera:
        if (
            value.get("schema_version") != 1
            or value.get("units") != "meters"
            or value.get("world_axes") != "x_y_up_z"
        ):
            raise ValueError("unsupported_metric_camera_contract")
        lens = GroundLens(**value["lens"])
        center = np.asarray(value["camera_center"], dtype=float)
        rotation = np.asarray(value["world_to_camera"], dtype=float)
        polygon = np.asarray(value["ground_polygon"], dtype=float)
        if (
            center.shape != (3,)
            or rotation.shape != (3, 3)
            or polygon.ndim != 2
            or polygon.shape[1] != 2
            or len(polygon) < 3
            or not all(np.isfinite(v).all() for v in (center, rotation, polygon))
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6)
            or abs(np.linalg.det(rotation) - 1) > 1e-6
            or center[1] <= 0
        ):
            raise ValueError("invalid_metric_camera_geometry")
        image_polygon = np.asarray(value["ground_image_polygon"], dtype=float)

        def area(points):
            return (
                abs(
                    sum(
                        points[i][0] * points[(i + 1) % len(points)][1]
                        - points[(i + 1) % len(points)][0] * points[i][1]
                        for i in range(len(points))
                    )
                )
                / 2
            )

        for domain in (polygon, image_polygon):
            if (
                domain.ndim != 2
                or domain.shape[1] != 2
                or len(domain) > 64
                or not np.isfinite(domain).all()
            ):
                raise ValueError("invalid_metric_ground_domain")
            hull = _convex_hull([tuple(point) for point in domain])
            if (
                len(hull) < 3
                or len(hull) != len(domain)
                or area(hull) <= 1e-6
                or not math.isclose(area(domain), area(hull), rel_tol=1e-8)
            ):
                raise ValueError("invalid_metric_ground_domain")
        image_ray_camera(lens, (0.5, 0.5))
        error = float(value["maximum_reprojection_error"])
        checks = tuple(float(v) for v in value["check_errors_meters"])
        digest = str(value["calibration_digest"])
        if (
            not math.isfinite(error)
            or not 0 <= error <= 0.005
            or len(checks) < 2
            or any(not math.isfinite(v) or not 0 <= v <= 0.30 for v in checks)
            or len(digest) != 64
            or any(v not in "0123456789abcdef" for v in digest)
        ):
            raise ValueError("unvalidated_metric_camera_geometry")
        return cls(
            lens,
            tuple(center),
            tuple(tuple(row) for row in rotation),
            tuple(tuple(row) for row in polygon),
            tuple(tuple(row) for row in image_polygon),
            digest,
            error,
            checks,
        )

    def ray(self, image: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
        direction = np.asarray(self.world_to_camera).T @ image_ray_camera(self.lens, image)
        return np.array(self.camera_center), direction / np.linalg.norm(direction)

    def contains_ground(self, position: tuple[float, float, float]) -> bool:
        if not all(math.isfinite(v) for v in position) or not _point_in_convex_polygon(
            (position[0], position[2]),
            self.ground_polygon,
        ):
            return False
        # Preserve the existing mapper's intersection of image/world domains.
        # Evaluate the support projection, not the elevated joint's pixel.
        support_image = self.project((position[0], 0.0, position[2]))
        return support_image is not None and _point_in_convex_polygon(
            support_image, self.ground_image_polygon
        )

    def point_at_height(
        self, image: tuple[float, float], height: float
    ) -> tuple[float, float, float] | None:
        """Intersect a ray with Y=height, retaining the bounded support domain.

        Its vertical projection is a body anchor, not evidence of a foot.
        """
        if not math.isfinite(height) or height < 0:
            return None
        try:
            origin, direction = self.ray(image)
        except ValueError:
            return None
        if abs(direction[1]) < 1e-6:
            return None
        distance = (height - origin[1]) / direction[1]
        if distance <= 0:
            return None
        point = tuple(float(v) for v in origin + distance * direction)
        return point if self.contains_ground(point) else None

    def project(self, position: tuple[float, float, float]) -> tuple[float, float] | None:
        if not all(math.isfinite(v) for v in position):
            return None
        camera = np.asarray(self.world_to_camera) @ (np.asarray(position) - self.camera_center)
        if camera[2] <= 1e-6:
            return None
        return _ground_lens_ideal_to_image(
            self.lens, float(camera[0] / camera[2]), float(camera[1] / camera[2])
        )


def solve_metric_camera(projection: GroundProjectionSpec) -> MetricCamera:
    """Resolve calibrated planar PnP, checking both IPPE solutions.

    Uses only fit points to solve. Check points remain independent. The input
    lens must contain actual intrinsics, not the identity homography fallback.
    """
    import cv2

    if projection.lens.type == "identity_rectilinear_v1":
        raise ValueError("metric_intrinsics_required")
    mapper = GroundPlaneMapper(projection)
    if mapper.quality.status != "ready":
        raise ValueError("validated_ground_calibration_required")
    fit = [p for p in projection.points if p.role == "fit"]
    check = [p for p in projection.points if p.role == "check"]
    if len(check) < 2:
        raise ValueError("independent_calibration_checks_required")
    for index, point in enumerate(check):
        if any(
            math.hypot(point.world_x - other.world_x, point.world_z - other.world_z) < 0.01
            or math.hypot(point.image_u - other.image_u, point.image_v - other.image_v) < 1e-5
            for other in fit + check[:index]
        ):
            raise ValueError("independent_calibration_checks_required")
    objects = np.array([(p.world_x, 0, p.world_z) for p in fit], dtype=float)
    rays = [image_ray_camera(projection.lens, (p.image_u, p.image_v)) for p in fit]
    images = np.array([ray[:2] / ray[2] for ray in rays], dtype=float)
    solved, rotations, translations, *_ = cv2.solvePnPGeneric(
        objects,
        images,
        np.eye(3),
        None,
        flags=cv2.SOLVEPNP_IPPE,
    )
    if not solved:
        raise ValueError("metric_camera_solution_unavailable")
    digest = hashlib.sha256(repr(projection).encode()).hexdigest()
    candidates = []
    for rotation_vector, translation in zip(rotations, translations):
        rotation, _ = cv2.Rodrigues(rotation_vector)
        center = -rotation.T @ translation.reshape(3)
        if not np.isfinite(center).all() or center[1] <= 0:
            continue
        camera = MetricCamera(
            projection.lens,
            tuple(center),
            tuple(tuple(row) for row in rotation),
            mapper.world_polygon,
            mapper.image_polygon,
            digest,
            0.0,
            (),
        )
        errors = []
        check_errors = []
        for point in fit + check:
            image = camera.project((point.world_x, 0.0, point.world_z))
            if image is None:
                break
            errors.append(math.dist(image, (point.image_u, point.image_v)))
            if point.role == "check":
                world = camera.point_at_height((point.image_u, point.image_v), 0.0)
                if world is None:
                    break
                check_errors.append(math.dist((world[0], world[2]), (point.world_x, point.world_z)))
        else:
            # Normalized full-image error, about 6.4 pixels at width 1280.
            if max(errors) <= 0.005 and len(check_errors) >= 2 and max(check_errors) <= 0.30:
                candidates.append(
                    MetricCamera(
                        camera.lens,
                        camera.camera_center,
                        camera.world_to_camera,
                        camera.ground_polygon,
                        camera.ground_image_polygon,
                        digest,
                        max(errors),
                        tuple(check_errors),
                    )
                )
    if not candidates:
        raise ValueError("metric_camera_reprojection_or_check_failed")
    candidates.sort(key=lambda camera: camera.maximum_reprojection_error)
    best = candidates[0]
    for alternative in candidates[1:]:
        separation = np.linalg.norm(np.asarray(best.camera_center) - alternative.camera_center)
        if (
            separation > 0.05
            and alternative.maximum_reprojection_error <= best.maximum_reprojection_error + 0.001
        ):
            raise ValueError("metric_camera_solution_ambiguous")
    return best
