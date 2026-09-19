"""Geometry for a fixed-zoom, approximately central PTZ camera above one plane.

Pan and elevation are physical radians, never device position units. Pan zero
looks along ray axis X; increasing pan turns towards Y (image right), and ray Z
points up. Composition coordinates remain the existing world_x/world_z plane.
An optical lens uses pixel coordinates, with (0, 0) the top-left pixel centre.
The equirectangular panorama uses normalized coordinates: u spans [-pi, pi]
pan, v spans [+pi/2, -pi/2] elevation. No local stitching warp changes its rays.

Mapping results are JSON-compatible. A quality status of ready describes the
supplied geometric evidence only; it never certifies hardware or activates it.
"""

from __future__ import annotations

import itertools
import math
from typing import Any, Literal, NotRequired, TypedDict

import cv2
import numpy as np

from .mapping import _convex_hull, _point_in_convex_polygon, _polygon_area


class PanoramaLens(TypedDict):
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    distortion: NotRequired[list[float]]


class PanoramaPoint(TypedDict):
    id: str
    role: Literal["fit", "check"]
    world_x: float
    world_z: float
    ray: list[float]


def _finite(value: Any) -> float:
    if isinstance(value, (str, bool)):
        raise ValueError("Expected a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Expected a finite number") from error
    if not math.isfinite(number):
        raise ValueError("Expected a finite number")
    return number


def _unit(ray: Any) -> Any:
    try:
        vector = np.asarray([_finite(value) for value in ray], dtype=np.float64)
    except TypeError as error:
        raise ValueError("Expected a three-dimensional direction") from error
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise ValueError("Expected a finite three-dimensional direction")
    length = float(np.linalg.norm(vector))
    if not math.isfinite(length) or length <= 1e-12:
        raise ValueError("Direction must have nonzero finite length")
    return vector / length


def pan_tilt_to_ray(pan_radians: float, tilt_radians: float) -> tuple[float, float, float]:
    pan, tilt = _finite(pan_radians), _finite(tilt_radians)
    if not -math.pi / 2 <= tilt <= math.pi / 2:
        raise ValueError("Elevation must be between -pi/2 and pi/2 radians")
    return math.cos(tilt) * math.cos(pan), math.cos(tilt) * math.sin(pan), math.sin(tilt)


def ray_to_pan_tilt(ray: Any) -> tuple[float, float]:
    x, y, z = _unit(ray)
    horizontal = math.hypot(x, y)
    # Pan is undefined at a pole; choose the documented canonical zero.
    return (math.atan2(y, x) if horizontal > 1e-12 else 0.0), math.atan2(z, horizontal)


def panorama_pixel_to_ray(u: float, v: float) -> tuple[float, float, float]:
    u, v = _finite(u), _finite(v)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        raise ValueError("Panorama coordinates must be in [0, 1]")
    return pan_tilt_to_ray((u * 2 - 1) * math.pi, (0.5 - v) * math.pi)


def ray_to_panorama_pixel(ray: Any) -> tuple[float, float]:
    pan, tilt = ray_to_pan_tilt(ray)
    return ((pan + math.pi) / (2 * math.pi)) % 1.0, 0.5 - tilt / math.pi


def _lens_arrays(lens: dict[str, Any]) -> tuple[Any, Any, int, int]:
    width, height = _finite(lens.get("width")), _finite(lens.get("height"))
    if width != int(width) or height != int(height) or min(width, height) < 2:
        raise ValueError("Lens image dimensions must be integers of at least two pixels")
    if width * height > 50_000_000:
        raise ValueError("Lens image exceeds the supported pixel limit")
    fx, fy, cx, cy = (_finite(lens.get(key)) for key in ("fx", "fy", "cx", "cy"))
    if min(fx, fy) <= 0 or not (0 <= cx < width and 0 <= cy < height):
        raise ValueError("Invalid optical intrinsics")
    coefficients = np.asarray(
        [_finite(value) for value in lens.get("distortion", [])], dtype=np.float64
    )
    if coefficients.shape not in {(0,), (4,), (5,), (8,)}:
        raise ValueError("Brown distortion requires zero, four, five or eight coefficients")
    matrix = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])
    return matrix, coefficients if coefficients.size else None, int(width), int(height)


def _camera_basis(pan: float, tilt: float) -> Any:
    forward = pan_tilt_to_ray(pan, tilt)
    right = (-math.sin(pan), math.cos(pan), 0.0)
    down = (math.sin(tilt) * math.cos(pan), math.sin(tilt) * math.sin(pan), -math.cos(tilt))
    return np.column_stack((right, down, forward))


def _rotation_basis(rotation_matrix: Any) -> Any:
    """Camera-to-reference-camera SO(3), expressed in the panorama axes.

    The reference camera has right/down/forward coordinates. Identity therefore
    produces exactly the legacy pan=tilt=0 basis (whose world axes are left
    handed). This explicit change of basis preserves existing pixel conventions.
    """
    try:
        rotation = np.asarray(
            [[_finite(value) for value in row] for row in rotation_matrix], dtype=np.float64
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Expected a finite three-by-three rotation matrix") from error
    if (
        rotation.shape != (3, 3)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6, rtol=0)
        or not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-6)
    ):
        raise ValueError("Rotation matrix must be orthonormal with determinant one")
    return _camera_basis(0.0, 0.0) @ rotation


def image_pixel_to_ray(
    pixel_x: float,
    pixel_y: float,
    lens: dict[str, Any],
    pan_radians: float = 0.0,
    tilt_radians: float = 0.0,
    *,
    rotation_matrix: Any = None,
) -> tuple[float, float, float]:
    matrix, coefficients, width, height = _lens_arrays(lens)
    x, y = _finite(pixel_x), _finite(pixel_y)
    if not (0 <= x <= width - 1 and 0 <= y <= height - 1):
        raise ValueError("Pixel is outside the optical image")
    if rotation_matrix is None:
        # Preserve the established legacy runtime path and its cost.
        ideal = cv2.undistortPoints(np.array([[[x, y]]]), matrix, coefficients).reshape(2)
    else:
        ideal = cv2.undistortPointsIter(
            np.array([[[x, y]]]), matrix, coefficients, None, None,
            (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-12),
        ).reshape(2)
        recovered, _ = cv2.projectPoints(
            np.array([[ideal[0], ideal[1], 1.0]]), np.zeros(3), np.zeros(3), matrix, coefficients
        )
        if not np.isfinite(ideal).all() or np.linalg.norm(recovered.reshape(2) - [x, y]) > 1e-4:
            raise ValueError("Optical pixel inversion did not converge")
    basis = (
        _rotation_basis(rotation_matrix)
        if rotation_matrix is not None
        else _camera_basis(_finite(pan_radians), _finite(tilt_radians))
    )
    ray = _unit(basis @ np.array([ideal[0], ideal[1], 1.0]))
    return tuple(float(value) for value in ray)


def ray_to_image_pixel(
    ray: Any,
    lens: dict[str, Any],
    pan_radians: float = 0.0,
    tilt_radians: float = 0.0,
    *,
    rotation_matrix: Any = None,
) -> tuple[float, float] | None:
    matrix, coefficients, width, height = _lens_arrays(lens)
    basis = (
        _rotation_basis(rotation_matrix)
        if rotation_matrix is not None
        else _camera_basis(_finite(pan_radians), _finite(tilt_radians))
    )
    local = basis.T @ _unit(ray)
    if local[2] <= 1e-9:
        return None
    projected, _ = cv2.projectPoints(
        local.reshape(1, 3), np.zeros(3), np.zeros(3), matrix, coefficients
    )
    x, y = (float(value) for value in projected.reshape(2))
    return (x, y) if 0 <= x <= width - 1 and 0 <= y <= height - 1 else None


def render_panorama(
    captures: list[dict[str, Any]], *, width: int = 2048, height: int = 1024
) -> dict[str, Any]:
    """Render BGR uint8 captures using optical geometry, with source provenance.

    Every capture has id, image (BGR array), lens, and either pan/tilt radians
    or rotation_matrix (camera-to-reference-camera, a proper SO(3) rotation).
    Unknown pixels remain masked, never synthesized. Pixel centres use
    ((column + .5)/width, (row + .5)/height) in the equirectangular projection.
    """
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or min(width, height) < 2
        or width > 4096
        or height > 2048
    ):
        raise ValueError("Panorama dimensions must be between 2 and 4096 by 2048")
    if not captures or len(captures) > 256:
        raise ValueError("Between one and 256 captures are required")
    identifiers = [capture.get("id") for capture in captures]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise ValueError("Capture identifiers must be nonempty strings")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Capture identifiers must be unique")
    pan = ((np.arange(width) + 0.5) / width * 2 - 1) * math.pi
    tilt = (0.5 - (np.arange(height) + 0.5) / height) * math.pi
    directions = np.stack(
        np.broadcast_arrays(
            np.cos(tilt[:, None]) * np.cos(pan),
            np.cos(tilt[:, None]) * np.sin(pan),
            np.sin(tilt[:, None]) * np.ones(width),
        ),
        axis=-1,
    )
    output = np.zeros((height, width, 3), dtype=np.uint8)
    source_indices = np.full((height, width), -1, dtype=np.int16)
    best_score = np.zeros((height, width), dtype=np.float64)
    for index, capture in enumerate(captures):
        matrix, coefficients, image_width, image_height = _lens_arrays(capture["lens"])
        image = np.asarray(capture["image"])
        if image.shape != (image_height, image_width, 3) or image.dtype != np.uint8:
            raise ValueError("Capture must be a BGR uint8 image matching its lens dimensions")
        basis = (
            _rotation_basis(capture["rotation_matrix"])
            if capture.get("rotation_matrix") is not None
            else _camera_basis(_finite(capture["pan_radians"]), _finite(capture["tilt_radians"]))
        )
        local = directions @ basis
        candidates = local[:, :, 2] > np.maximum(best_score, 1e-9)
        if not candidates.any():
            continue
        locations = np.argwhere(candidates)
        candidate_directions = local[candidates]
        # OpenCV also allocates a large Jacobian; bounded batches keep the
        # geometric renderer's memory independent of that unused derivative.
        projected = np.concatenate(
            [
                cv2.projectPoints(
                    candidate_directions[start : start + 16000],
                    np.zeros(3),
                    np.zeros(3),
                    matrix,
                    coefficients,
                )[0].reshape(-1, 2)
                for start in range(0, len(candidate_directions), 16000)
            ]
        )
        valid = (
            np.isfinite(projected).all(axis=1)
            & (projected[:, 0] >= 0)
            & (projected[:, 0] <= image_width - 1)
            & (projected[:, 1] >= 0)
            & (projected[:, 1] <= image_height - 1)
        )
        rows, columns = locations[valid].T
        pixels = projected[valid].astype(np.float32)
        if not len(pixels):
            continue
        # ponytail: select the most frontal capture; add seam blending only if
        # visible seams harm point selection. Geometry and provenance stay fixed.
        sampled = np.concatenate(
            [
                cv2.remap(
                    image,
                    pixels[start : start + 16000, 0].reshape(-1, 1),
                    pixels[start : start + 16000, 1].reshape(-1, 1),
                    cv2.INTER_LINEAR,
                ).reshape(-1, 3)
                for start in range(0, len(pixels), 16000)
            ]
        )
        output[rows, columns] = sampled
        source_indices[rows, columns] = index
        best_score[rows, columns] = local[rows, columns, 2]
    coverage_mask = (source_indices >= 0).astype(np.uint8) * 255
    return {
        "image": output,
        "coverage_mask": coverage_mask,
        "source_indices": source_indices,
        "source_ids": identifiers,
        "coverage_ratio": float(np.count_nonzero(coverage_mask) / (width * height)),
    }


def _angular_errors(predicted: Any, actual: Any) -> Any:
    norms = np.linalg.norm(predicted, axis=1)
    normalized = predicted / np.maximum(norms[:, None], 1e-15)
    return np.where(
        norms > 1e-12,
        np.arctan2(
            np.linalg.norm(np.cross(normalized, actual), axis=1),
            np.sum(normalized * actual, axis=1),
        ),
        math.pi,
    )


def _solve_rays(positions: Any, rays: Any) -> Any:
    centered = positions - np.mean(positions, axis=0)
    spread = np.linalg.svd(centered, compute_uv=False)
    if spread[0] <= 1e-12 or spread[-1] / spread[0] < 1e-3:
        raise ValueError("Composition points are nearly collinear")
    scale = math.sqrt(2) / float(np.mean(np.linalg.norm(centered, axis=1)))
    center = np.mean(positions, axis=0)
    normalization = np.array(
        [[scale, 0, -scale * center[0]], [0, scale, -scale * center[1]], [0, 0, 1]]
    )
    homogeneous = np.column_stack((positions, np.ones(len(positions)))) @ normalization.T
    rows = []
    for position, (x, y, z) in zip(homogeneous, rays, strict=True):
        cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        rows.extend(np.kron(cross, position))
    _, singular_values, vectors = np.linalg.svd(np.asarray(rows), full_matrices=False)
    if singular_values[-2] < singular_values[0] * 1e-8:
        raise ValueError("Direction correspondences are degenerate")
    normalized_matrix = vectors[-1].reshape(3, 3)
    if np.linalg.cond(normalized_matrix) > 1e8:
        raise ValueError("Direction mapping is numerically unstable")
    matrix = normalized_matrix @ normalization
    predicted = np.column_stack((positions, np.ones(len(positions)))) @ matrix.T
    dots = np.sum(predicted * rays, axis=1)
    if np.median(dots) < 0:
        matrix, dots = -matrix, -dots
    if np.any(dots <= 0):
        raise ValueError("Correspondences disagree on the direction of their rays")
    return matrix / np.linalg.norm(matrix)


def _refine_angular(matrix: Any, positions: Any, rays: Any) -> Any:
    """Small damped least-squares refinement of angular tangent residuals."""
    homogeneous = np.column_stack((positions, np.ones(len(positions))))
    pivot = int(np.argmax(np.abs(matrix)))
    fixed = matrix.ravel()[pivot] / abs(matrix.ravel()[pivot])
    free = np.arange(9) != pivot
    parameters = (matrix.ravel() / abs(matrix.ravel()[pivot]))[free]

    def unpack(values: Any) -> Any:
        flat = np.empty(9)
        flat[pivot], flat[free] = fixed, values
        return flat.reshape(3, 3)

    def residual(values: Any) -> Any:
        predicted = homogeneous @ unpack(values).T
        predicted /= np.maximum(np.linalg.norm(predicted, axis=1)[:, None], 1e-15)
        cross = np.cross(predicted, rays)
        sine = np.linalg.norm(cross, axis=1)
        angle = np.arctan2(sine, np.sum(predicted * rays, axis=1))
        return (cross * (angle / np.maximum(sine, 1e-12))[:, None]).ravel()

    damping = 1e-5
    for _ in range(12):
        current = residual(parameters)
        if np.linalg.norm(current) < 1e-10:
            break
        steps = 1e-6 * np.maximum(1, np.abs(parameters))
        jacobian = np.column_stack(
            [
                (residual(parameters + np.eye(8)[index] * step) - current) / step
                for index, step in enumerate(steps)
            ]
        )
        change = np.linalg.lstsq(
            np.vstack((jacobian, math.sqrt(damping) * np.eye(8))),
            np.concatenate((-current, np.zeros(8))),
            rcond=None,
        )[0]
        proposed = parameters + change
        if np.linalg.norm(residual(proposed)) < np.linalg.norm(current):
            parameters, damping = proposed, damping / 3
            if np.linalg.norm(change) < 1e-10:
                break
        else:
            damping *= 10
    refined = unpack(parameters)
    if not np.isfinite(refined).all() or np.linalg.cond(refined) > 1e12:
        return matrix
    if np.any(np.sum((homogeneous @ refined.T) * rays, axis=1) <= 0):
        return matrix
    return refined / np.linalg.norm(refined)


def preview_panorama_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Classify geometric preview evidence without weakening activation quality.

    Source identity, revision and captured coverage are checked by the consumer.
    Only absent independent checks may yield a provisional preview.
    """
    missing_checks = "at_least_two_independent_check_points_required"
    quality = mapping.get("quality")
    if not isinstance(quality, dict):
        return {"eligible": False, "status": "blocked", "reasons": ["invalid_quality"]}
    supplied_reasons = quality.get("reasons")
    if not isinstance(supplied_reasons, list) or any(
        not isinstance(reason, str) for reason in supplied_reasons
    ):
        return {"eligible": False, "status": "blocked", "reasons": ["invalid_quality"]}
    reasons = [reason for reason in supplied_reasons if reason != missing_checks]
    if quality.get("status") not in ("ready", "review"):
        reasons.append("invalid_quality_status")
    try:
        fit_count, inlier_count, check_count = (
            _finite(quality.get(key))
            for key in ("number_of_fit_points", "number_of_inliers", "number_of_check_points")
        )
        ratio = _finite(quality.get("inlier_ratio"))
        threshold = _finite(quality.get("angular_threshold_radians"))
        median = _finite(quality.get("median_fit_error_radians"))
        percentile = _finite(quality.get("p95_fit_error_radians"))
        if (
            any(count != int(count) for count in (fit_count, inlier_count, check_count))
            or not 6 <= inlier_count <= fit_count <= 64
            or not 0 <= check_count <= 64 - fit_count
            or not 0.8 <= ratio <= 1
            or not math.isclose(ratio, inlier_count / fit_count, abs_tol=1e-9)
            or not 0 < threshold < math.pi / 4
            or not 0 <= median <= percentile <= threshold
        ):
            reasons.append("insufficient_consistent_geometry")
        if (check_count < 2) != (missing_checks in supplied_reasons):
            reasons.append("invalid_check_evidence")
        if check_count and not 0 <= _finite(quality.get("maximum_check_error_radians")) <= threshold:
            reasons.append("check_error_exceeds_threshold")
    except (ValueError, ZeroDivisionError):
        reasons.append("invalid_quality")
    try:
        matrix, inverse = (
            np.asarray([[_finite(value) for value in row] for row in mapping.get(key, [])])
            for key in ("matrix", "inverse_matrix")
        )
        if (
            mapping.get("type") != "panorama_ray_plane_v1"
            or matrix.shape != (3, 3)
            or inverse.shape != (3, 3)
            or np.linalg.cond(matrix) > 1e12
            or not np.allclose(matrix @ inverse, np.eye(3), atol=1e-6, rtol=0)
            or not np.allclose(inverse @ matrix, np.eye(3), atol=1e-6, rtol=0)
        ):
            reasons.append("unstable_geometry")
    except (TypeError, ValueError, np.linalg.LinAlgError):
        reasons.append("unstable_geometry")
    try:
        support = [tuple(_finite(value) for value in point) for point in mapping.get("support_polygon", [])]
        if not 3 <= len(support) <= 64 or any(len(point) != 2 for point in support):
            raise ValueError("Invalid support polygon")
        hull = _convex_hull(support)
        area = abs(_polygon_area(support))
        if (
            not math.isfinite(area)
            or area <= 1e-9
            or len(hull) != len(support)
            or not math.isclose(area, abs(_polygon_area(hull)), rel_tol=1e-8, abs_tol=1e-9)
        ):
            raise ValueError("Invalid support polygon")
    except (TypeError, ValueError):
        reasons.append("invalid_support_polygon")
    return {
        "eligible": not reasons,
        "status": "blocked" if reasons else "provisional" if missing_checks in supplied_reasons else "ready",
        "reasons": list(dict.fromkeys(reasons)) if reasons else supplied_reasons.copy(),
    }


def estimate_panorama_mapping(
    points: list[dict[str, Any]], *, angular_threshold_radians: float = math.radians(1.0)
) -> dict[str, Any]:
    """Fit only role=fit points; role=check points are independent evidence.

    The threshold is a configurable angular evidence gate, not a measured
    equipment specification. At most 64 points and 512 robust samples bound work.
    """
    threshold = _finite(angular_threshold_radians)
    if not 0 < threshold < math.pi / 4:
        raise ValueError("Angular threshold must be positive and smaller than pi/4")
    if len(points) > 64:
        raise ValueError("At most 64 calibration points are supported")
    identifiers: set[str] = set()
    parsed = []
    for point in points:
        identifier = point.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("Point identifiers must be unique nonempty strings")
        identifiers.add(identifier)
        if point.get("role") not in {"fit", "check"}:
            raise ValueError("Point role must be fit or check")
        parsed.append(
            {
                "id": identifier,
                "role": point["role"],
                "position": (_finite(point.get("world_x")), _finite(point.get("world_z"))),
                "ray": _unit(point.get("ray")),
            }
        )
    fit = [point for point in parsed if point["role"] == "fit"]
    checks = [point for point in parsed if point["role"] == "check"]
    quality: dict[str, Any] = {
        "status": "incomplete",
        "reasons": [],
        "number_of_fit_points": len(fit),
        "number_of_check_points": len(checks),
        "number_of_inliers": 0,
        "inlier_ratio": 0.0,
        "angular_threshold_radians": threshold,
        "median_fit_error_radians": None,
        "p95_fit_error_radians": None,
        "maximum_check_error_radians": None,
        "point_errors": [],
    }
    result: dict[str, Any] = {
        "type": "panorama_ray_plane_v1",
        "matrix": None,
        "inverse_matrix": None,
        "support_polygon": [],
        "inlier_point_ids": [],
        "quality": quality,
    }
    if len(fit) < 6:
        quality["reasons"].append("at_least_six_fit_points_required")
        return {**result, "preview": preview_panorama_mapping(result)}
    positions = np.asarray([point["position"] for point in fit])
    rays = np.asarray([point["ray"] for point in fit])
    if len(np.unique(positions, axis=0)) != len(fit):
        quality.update(status="review", reasons=["duplicate_fit_positions"])
        return {**result, "preview": preview_panorama_mapping(result)}
    homogeneous = np.column_stack((positions, np.ones(len(fit))))
    # ponytail: bounded deterministic spherical RANSAC; a larger solver is only
    # needed if more than 64 operator-supplied points become a real requirement.
    if math.comb(len(fit), 4) <= 512:
        samples = list(itertools.combinations(range(len(fit)), 4))
    else:
        generator = np.random.default_rng(0)
        samples = sorted(
            {tuple(sorted(generator.choice(len(fit), 4, replace=False))) for _ in range(512)}
        )
    best_matrix, best_mask, best_score = None, None, (-1, -math.inf)
    for sample in [tuple(range(len(fit))), *samples]:
        indices = np.asarray(sample)
        try:
            matrix = _solve_rays(positions[indices], rays[indices])
        except (ValueError, np.linalg.LinAlgError):
            continue
        errors = _angular_errors(homogeneous @ matrix.T, rays)
        mask = errors <= threshold
        score = (int(np.sum(mask)), -float(np.median(errors[mask])) if mask.any() else -math.inf)
        if score > best_score:
            best_matrix, best_mask, best_score = matrix, mask, score
    if best_matrix is None or best_mask is None or np.count_nonzero(best_mask) < 6:
        quality.update(status="review", reasons=["insufficient_consistent_geometry"])
        return {**result, "preview": preview_panorama_mapping(result)}
    try:
        matrix = _solve_rays(positions[best_mask], rays[best_mask])
        matrix = _refine_angular(matrix, positions[best_mask], rays[best_mask])
        inverse = np.linalg.inv(matrix)
    except (ValueError, np.linalg.LinAlgError):
        quality.update(status="review", reasons=["unstable_geometry"])
        return {**result, "preview": preview_panorama_mapping(result)}
    errors = _angular_errors(homogeneous @ matrix.T, rays)
    inliers = errors <= threshold
    support = _convex_hull([tuple(point) for point in positions[inliers]])
    reasons = []
    if np.count_nonzero(inliers) < 6 or len(support) < 3:
        quality.update(status="review", reasons=["insufficient_consistent_geometry"])
        return {**result, "preview": preview_panorama_mapping(result)}
    if np.mean(inliers) < 0.8:
        reasons.append("too_many_inconsistent_points")
    valid_checks = []
    for point in checks:
        position = point["position"]
        independent = all(math.dist(position, other["position"]) > 1e-6 for other in fit)
        independent = independent and all(
            math.dist(position, other) > 1e-6 for other in valid_checks
        )
        inside = _point_in_convex_polygon(position, tuple(support))
        error = float(
            _angular_errors(np.asarray([matrix @ [*position, 1]]), np.asarray([point["ray"]]))[0]
        )
        quality["point_errors"].append(
            {
                "id": point["id"],
                "role": "check",
                "error_radians": error,
                "inside_support": inside,
                "independent": independent,
            }
        )
        if independent and inside:
            valid_checks.append(position)
        else:
            reasons.append("check_points_must_be_independent_and_inside_support")
        if error > threshold:
            reasons.append("check_error_exceeds_threshold")
    if len(valid_checks) < 2:
        reasons.append("at_least_two_independent_check_points_required")
    quality["point_errors"] = [
        {"id": point["id"], "role": "fit", "error_radians": float(error), "inlier": bool(inlier)}
        for point, error, inlier in zip(fit, errors, inliers, strict=True)
    ] + quality["point_errors"]
    check_errors = [
        point["error_radians"] for point in quality["point_errors"] if point["role"] == "check"
    ]
    quality.update(
        status="review" if reasons else "ready",
        reasons=list(dict.fromkeys(reasons)),
        number_of_inliers=int(np.count_nonzero(inliers)),
        inlier_ratio=float(np.mean(inliers)),
        median_fit_error_radians=float(np.median(errors[inliers])),
        p95_fit_error_radians=float(np.percentile(errors[inliers], 95)),
        maximum_check_error_radians=max(check_errors) if check_errors else None,
        support_area=abs(_polygon_area(support)),
    )
    result.update(
        matrix=matrix.tolist(),
        inverse_matrix=inverse.tolist(),
        support_polygon=[[float(value) for value in point] for point in support],
        inlier_point_ids=[point["id"] for point, keep in zip(fit, inliers, strict=True) if keep],
    )
    return {**result, "preview": preview_panorama_mapping(result)}


def map_world_to_ray(
    mapping: dict[str, Any], world_x: float, world_z: float
) -> tuple[float, float, float] | None:
    position = (_finite(world_x), _finite(world_z))
    if mapping.get("matrix") is None or not _point_in_convex_polygon(
        position, mapping.get("support_polygon", [])
    ):
        return None
    ray = _unit(np.asarray(mapping["matrix"]) @ [*position, 1.0])
    return tuple(float(value) for value in ray)


def map_ray_to_world(mapping: dict[str, Any], ray: Any) -> tuple[float, float] | None:
    direction = _unit(ray)
    if mapping.get("inverse_matrix") is None:
        return None
    point = np.asarray(mapping["inverse_matrix"]) @ direction
    # A negative homogeneous scale is the same projective line but the opposite
    # physical ray. Reject it, as well as the ground-plane horizon.
    if not np.isfinite(point).all() or point[2] <= 1e-12:
        return None
    position = (float(point[0] / point[2]), float(point[1] / point[2]))
    return (
        position if _point_in_convex_polygon(position, mapping.get("support_polygon", [])) else None
    )
