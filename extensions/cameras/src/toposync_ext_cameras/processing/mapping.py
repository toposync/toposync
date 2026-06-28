from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Literal


try:
    import numpy as np  # type: ignore
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]


HomographyMethod = Literal["usac_magsac", "usac_default", "ransac", "dlt"]
FallbackMode = Literal["default_set", "nearest_set", "none"]
MotionPolicyMode = Literal["skip_when_moving", "use_last_idle_pose", "allow_when_confident"]
BoundaryRefinementEdge = Literal["top", "right", "bottom", "left"]


@dataclass(frozen=True, slots=True)
class ControlPointPair:
    image_u: float
    image_v: float
    world_x: float
    world_z: float


@dataclass(frozen=True, slots=True)
class ControlPointRefinementPoint:
    id: str
    image_u: float
    image_v: float
    world_x: float
    world_z: float


@dataclass(frozen=True, slots=True)
class ControlPointBoundaryRefinementPoint:
    id: str
    edge: BoundaryRefinementEdge
    t: float
    image_u: float
    image_v: float
    world_x: float
    world_z: float


@dataclass(frozen=True, slots=True)
class PoseReference:
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    preset_token: str | None = None
    preset_name: str | None = None


@dataclass(frozen=True, slots=True)
class PanTiltZoomState:
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    move_status: str | None = None
    utc_time: str | None = None
    error: str | None = None
    source: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ControlPointSet:
    id: str
    label: str
    pose_reference: PoseReference | None
    control_points: tuple[ControlPointPair, ...]
    refinement_points: tuple[ControlPointRefinementPoint, ...] = ()
    boundary_refinement_points: tuple[ControlPointBoundaryRefinementPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class PoseSelectionConfig:
    sigma_pan: float = 0.04
    sigma_tilt: float = 0.04
    sigma_zoom: float = 0.06
    max_distance: float = 3.0
    fallback_mode: FallbackMode = "default_set"
    min_shared_axes: int = 1


@dataclass(frozen=True, slots=True)
class HomographyEstimationConfig:
    method: HomographyMethod = "usac_magsac"
    normalized_image_threshold: float = 0.005
    confidence: float = 0.999
    max_iterations: int = 10000


@dataclass(frozen=True, slots=True)
class HomographyQuality:
    number_of_points: int
    number_of_inliers: int
    inlier_ratio: float
    median_reprojection_error_uv: float | None
    p95_reprojection_error_uv: float | None
    convex_hull_area_ratio_uv: float
    is_near_collinear: bool
    is_numerically_unstable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "number_of_points": int(self.number_of_points),
            "number_of_inliers": int(self.number_of_inliers),
            "inlier_ratio": float(self.inlier_ratio),
            "median_reprojection_error_uv": (
                float(self.median_reprojection_error_uv) if self.median_reprojection_error_uv is not None else None
            ),
            "p95_reprojection_error_uv": (
                float(self.p95_reprojection_error_uv) if self.p95_reprojection_error_uv is not None else None
            ),
            "convex_hull_area_ratio_uv": float(self.convex_hull_area_ratio_uv),
            "is_near_collinear": bool(self.is_near_collinear),
            "is_numerically_unstable": bool(self.is_numerically_unstable),
        }


@dataclass(frozen=True, slots=True)
class HomographyEstimate:
    H_world_to_image: Any
    H_image_to_world: Any
    inlier_mask: tuple[bool, ...]
    quality: HomographyQuality
    method_used: str


@dataclass(frozen=True, slots=True)
class ControlPointSetSelection:
    control_point_set: ControlPointSet
    pose_distance: float | None
    pose_axes_used: tuple[str, ...]
    move_status: str | None
    reason: str


def normalize_move_status(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in {"idle", "stopped", "stop", "stationary"}:
        return "idle"
    if raw in {"moving", "move"}:
        return "moving"
    if "move" in raw or "pan" in raw or "tilt" in raw or "zoom" in raw:
        return "moving"
    if "idle" in raw or "stop" in raw or "stationary" in raw:
        return "idle"
    return "unknown"


def compute_pose_distance(
    pose_reference: PoseReference,
    pan_tilt_zoom_state: PanTiltZoomState,
    config: PoseSelectionConfig,
) -> tuple[float, tuple[str, ...]] | None:
    axes_used: list[str] = []
    normalized_terms: list[float] = []
    for axis, sigma in (
        ("pan", float(config.sigma_pan)),
        ("tilt", float(config.sigma_tilt)),
        ("zoom", float(config.sigma_zoom)),
    ):
        sigma_value = abs(float(sigma))
        if sigma_value <= 1e-9:
            continue
        pose_value = getattr(pose_reference, axis)
        state_value = getattr(pan_tilt_zoom_state, axis)
        if pose_value is None or state_value is None:
            continue
        axes_used.append(axis)
        normalized_terms.append(((float(state_value) - float(pose_value)) / sigma_value) ** 2)

    if len(axes_used) < max(1, int(config.min_shared_axes)):
        return None

    distance = math.sqrt(sum(normalized_terms) / max(1, len(normalized_terms)))
    return distance, tuple(axes_used)


def select_control_point_set(
    control_point_sets: list[ControlPointSet],
    pan_tilt_zoom_state: PanTiltZoomState | None,
    config: PoseSelectionConfig,
    motion_policy_mode: MotionPolicyMode,
) -> ControlPointSetSelection | None:
    valid_sets = [item for item in control_point_sets if len(item.control_points) >= 4]
    if not valid_sets:
        return None

    default_set = next((item for item in valid_sets if item.pose_reference is None), None)
    if pan_tilt_zoom_state is None:
        if default_set is not None:
            return ControlPointSetSelection(
                control_point_set=default_set,
                pose_distance=None,
                pose_axes_used=(),
                move_status=None,
                reason="missing_pose_state:default_set",
            )
        if len(valid_sets) == 1:
            return ControlPointSetSelection(
                control_point_set=valid_sets[0],
                pose_distance=None,
                pose_axes_used=(),
                move_status=None,
                reason="missing_pose_state:single_set",
            )
        return None

    normalized_status = normalize_move_status(pan_tilt_zoom_state.move_status)
    if motion_policy_mode == "skip_when_moving" and normalized_status == "moving":
        return None

    nearest_any: tuple[float, tuple[str, ...], ControlPointSet] | None = None
    nearest_in_range: tuple[float, tuple[str, ...], ControlPointSet] | None = None
    for item in valid_sets:
        if item.pose_reference is None:
            continue
        distance_info = compute_pose_distance(item.pose_reference, pan_tilt_zoom_state, config)
        if distance_info is None:
            continue
        distance, axes_used = distance_info
        candidate = (float(distance), axes_used, item)
        if nearest_any is None or candidate[0] < nearest_any[0]:
            nearest_any = candidate
        if distance <= float(config.max_distance) and (
            nearest_in_range is None or candidate[0] < nearest_in_range[0]
        ):
            nearest_in_range = candidate

    if nearest_in_range is not None:
        distance, axes_used, selected = nearest_in_range
        return ControlPointSetSelection(
            control_point_set=selected,
            pose_distance=distance,
            pose_axes_used=axes_used,
            move_status=normalized_status,
            reason="nearest_pose_match",
        )

    if str(config.fallback_mode) == "nearest_set" and nearest_any is not None:
        distance, axes_used, selected = nearest_any
        return ControlPointSetSelection(
            control_point_set=selected,
            pose_distance=distance,
            pose_axes_used=axes_used,
            move_status=normalized_status,
            reason="fallback:nearest_set",
        )

    if str(config.fallback_mode) == "default_set" and default_set is not None:
        return ControlPointSetSelection(
            control_point_set=default_set,
            pose_distance=None,
            pose_axes_used=(),
            move_status=normalized_status,
            reason="fallback:default_set",
        )

    if default_set is not None and nearest_any is None:
        return ControlPointSetSelection(
            control_point_set=default_set,
            pose_distance=None,
            pose_axes_used=(),
            move_status=normalized_status,
            reason="fallback:default_set_without_pose_axes",
        )

    return None


def compute_control_points_signature(control_points: list[ControlPointPair] | tuple[ControlPointPair, ...]) -> str:
    raw = "\n".join(
        f"{float(point.image_u):.12g}|{float(point.image_v):.12g}|{float(point.world_x):.12g}|{float(point.world_z):.12g}"
        for point in control_points
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_refinement_points_signature(
    refinement_points: list[ControlPointRefinementPoint] | tuple[ControlPointRefinementPoint, ...],
) -> str:
    raw = "\n".join(
        "|".join(
            [
                str(point.id or "").strip(),
                f"{float(point.image_u):.12g}",
                f"{float(point.image_v):.12g}",
                f"{float(point.world_x):.12g}",
                f"{float(point.world_z):.12g}",
            ]
        )
        for point in refinement_points
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_boundary_refinement_points_signature(
    boundary_refinement_points: list[ControlPointBoundaryRefinementPoint] | tuple[ControlPointBoundaryRefinementPoint, ...],
) -> str:
    raw = "\n".join(
        "|".join(
            [
                str(point.id or "").strip(),
                str(point.edge or "").strip(),
                f"{float(point.t):.12g}",
                f"{float(point.image_u):.12g}",
                f"{float(point.image_v):.12g}",
                f"{float(point.world_x):.12g}",
                f"{float(point.world_z):.12g}",
            ]
        )
        for point in boundary_refinement_points
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def estimate_homography_world_to_image(
    pairs: list[ControlPointPair] | tuple[ControlPointPair, ...],
    config: HomographyEstimationConfig | None = None,
) -> HomographyEstimate:
    if np is None:
        raise RuntimeError("numpy is required for control point mapping")
    if len(pairs) < 4:
        raise ValueError("At least 4 control points are required")

    safe_config = config or HomographyEstimationConfig()
    world = np.array([[p.world_x, p.world_z] for p in pairs], dtype=np.float64)
    image = np.array([[p.image_u, p.image_v] for p in pairs], dtype=np.float64)

    H_world_to_image = None
    inlier_mask: tuple[bool, ...] | None = None
    method_used = "dlt"
    robust_attempted = False
    try:
        import cv2  # type: ignore

        for method_name in _candidate_homography_methods(str(safe_config.method)):
            if method_name == "dlt":
                continue
            method_value = getattr(cv2, method_name, None)
            if method_value is None:
                continue
            robust_attempted = True
            try:
                H_candidate, raw_mask = cv2.findHomography(
                    world,
                    image,
                    method=method_value,
                    ransacReprojThreshold=float(safe_config.normalized_image_threshold),
                    maxIters=int(safe_config.max_iterations),
                    confidence=float(safe_config.confidence),
                )
            except TypeError:
                H_candidate, raw_mask = cv2.findHomography(
                    world,
                    image,
                    method_value,
                    float(safe_config.normalized_image_threshold),
                    None,
                    int(safe_config.max_iterations),
                    float(safe_config.confidence),
                )
            except Exception:
                H_candidate = None
                raw_mask = None
            if H_candidate is None:
                continue
            candidate_mask = _mask_to_tuple(raw_mask, len(pairs))
            if sum(1 for flag in candidate_mask if flag) < 4:
                continue
            H_world_to_image = H_candidate
            inlier_mask = candidate_mask
            method_used = method_name.lower()
            break
    except Exception:
        H_world_to_image = None

    if H_world_to_image is None:
        H_world_to_image = _solve_homography(world, image)
        inlier_mask = tuple([True] * len(pairs))
        method_used = "dlt" if not robust_attempted else f"{safe_config.method}:dlt_fallback"

    H_image_to_world = invert_homography(H_world_to_image)
    quality = compute_homography_quality_metrics(
        pairs=pairs,
        H_world_to_image=H_world_to_image,
        H_image_to_world=H_image_to_world,
        inlier_mask=inlier_mask or tuple([True] * len(pairs)),
    )
    return HomographyEstimate(
        H_world_to_image=H_world_to_image,
        H_image_to_world=H_image_to_world,
        inlier_mask=inlier_mask or tuple([True] * len(pairs)),
        quality=quality,
        method_used=method_used,
    )


def invert_homography(H: Any) -> Any:
    if np is None:
        raise RuntimeError("numpy is required for control point mapping")
    inv = np.linalg.inv(H)
    if abs(float(inv[2, 2])) > 1e-12:
        inv = inv / float(inv[2, 2])
    return inv


def apply_homography(H: Any, a: float, b: float) -> tuple[float, float] | None:
    if np is None:
        return None
    p = np.array([float(a), float(b), 1.0], dtype=np.float64)
    out = H @ p
    w = float(out[2]) if out.shape[0] >= 3 else 0.0
    if abs(w) < 1e-9:
        return None
    x = float(out[0]) / w
    y = float(out[1]) / w
    if not (x == x and y == y):
        return None
    return x, y


def compute_homography_quality_metrics(
    *,
    pairs: list[ControlPointPair] | tuple[ControlPointPair, ...],
    H_world_to_image: Any,
    H_image_to_world: Any,
    inlier_mask: tuple[bool, ...],
) -> HomographyQuality:
    image_errors: list[float] = []
    inlier_image_points: list[tuple[float, float]] = []
    fallback_image_points: list[tuple[float, float]] = []
    for index, point in enumerate(pairs):
        expected_image = (float(point.image_u), float(point.image_v))
        fallback_image_points.append(expected_image)
        predicted_image = apply_homography(H_world_to_image, float(point.world_x), float(point.world_z))
        if predicted_image is None:
            continue
        error = math.dist(expected_image, predicted_image)
        if inlier_mask[index]:
            image_errors.append(float(error))
            inlier_image_points.append(expected_image)

    if not image_errors:
        points_for_quality = fallback_image_points
        for point in pairs:
            predicted_image = apply_homography(H_world_to_image, float(point.world_x), float(point.world_z))
            if predicted_image is None:
                continue
            image_errors.append(math.dist((float(point.image_u), float(point.image_v)), predicted_image))
    else:
        points_for_quality = inlier_image_points

    hull_area_ratio = _convex_hull_area_ratio(points_for_quality)
    is_near_collinear = hull_area_ratio <= 1e-4
    is_numerically_unstable = _is_homography_numerically_unstable(H_world_to_image, H_image_to_world)
    return HomographyQuality(
        number_of_points=len(pairs),
        number_of_inliers=sum(1 for flag in inlier_mask if flag),
        inlier_ratio=(sum(1 for flag in inlier_mask if flag) / max(1, len(pairs))),
        median_reprojection_error_uv=_percentile(image_errors, 50.0),
        p95_reprojection_error_uv=_percentile(image_errors, 95.0),
        convex_hull_area_ratio_uv=hull_area_ratio,
        is_near_collinear=is_near_collinear,
        is_numerically_unstable=is_numerically_unstable,
    )


@dataclass(frozen=True, slots=True)
class _LocalRefinementDisplacement:
    image_u: float
    image_v: float
    world_x: float
    world_z: float
    delta_x: float
    delta_z: float


@dataclass(frozen=True, slots=True)
class _BoundaryRefinementDisplacement:
    id: str
    edge: BoundaryRefinementEdge
    t: float
    image_u: float
    image_v: float
    world_x: float
    world_z: float
    delta_x: float
    delta_z: float


LOCAL_REFINEMENT_SIGMA_UV = 0.22
LOCAL_REFINEMENT_EDGE_LOW = 0.015
LOCAL_REFINEMENT_EDGE_HIGH = 0.12
LOCAL_REFINEMENT_EPSILON = 1e-9
BOUNDARY_REFINEMENT_FALLOFF_UV = 0.48
BOUNDARY_REFINEMENT_EDGES: tuple[BoundaryRefinementEdge, ...] = ("top", "right", "bottom", "left")


class ControlPointMapper:
    def __init__(
        self,
        pairs: list[ControlPointPair],
        config: HomographyEstimationConfig | None = None,
        refinement_points: list[ControlPointRefinementPoint] | tuple[ControlPointRefinementPoint, ...] = (),
        boundary_refinement_points: list[ControlPointBoundaryRefinementPoint]
        | tuple[ControlPointBoundaryRefinementPoint, ...] = (),
    ) -> None:
        estimate = estimate_homography_world_to_image(pairs, config=config)
        self._pairs = tuple(pairs)
        self._refinement_points = tuple(refinement_points)
        self._boundary_refinement_points = tuple(boundary_refinement_points)
        self._estimate = estimate
        self._H_world_to_image = estimate.H_world_to_image
        self._H_image_to_world = estimate.H_image_to_world
        self._boundary_refinement_displacements = _boundary_refinement_displacements(
            self._H_image_to_world,
            self._boundary_refinement_points,
        )
        self._refinement_displacements = _local_refinement_displacements(
            self._H_image_to_world,
            self._refinement_points,
        )
        self.quality = estimate.quality
        self.inlier_mask = estimate.inlier_mask
        self.method_used = estimate.method_used

    def map(self, u: float, v: float) -> tuple[float, float] | None:
        return self.map_image_to_world(u, v)

    def map_image_to_world(self, u: float, v: float) -> tuple[float, float] | None:
        base = apply_homography(self._H_image_to_world, u, v)
        if base is None:
            return None
        boundary_delta = _boundary_refinement_delta(self._boundary_refinement_displacements, float(u), float(v))
        delta = _local_refinement_delta(self._refinement_displacements, float(u), float(v))
        return float(base[0]) + boundary_delta[0] + delta[0], float(base[1]) + boundary_delta[1] + delta[1]

    def map_world_to_image(self, x: float, z: float) -> tuple[float, float] | None:
        base = apply_homography(self._H_world_to_image, x, z)
        if base is None or (not self._refinement_displacements and not self._boundary_refinement_displacements):
            return base
        return _invert_refined_image_point(self, float(x), float(z), base)


def _local_refinement_displacements(
    H_image_to_world: Any,
    refinement_points: tuple[ControlPointRefinementPoint, ...],
) -> tuple[_LocalRefinementDisplacement, ...]:
    out: list[_LocalRefinementDisplacement] = []
    for point in refinement_points:
        image_u = float(point.image_u)
        image_v = float(point.image_v)
        world_x = float(point.world_x)
        world_z = float(point.world_z)
        if not all(math.isfinite(value) for value in (image_u, image_v, world_x, world_z)):
            continue
        if not (0.0 <= image_u <= 1.0 and 0.0 <= image_v <= 1.0):
            continue
        base = apply_homography(H_image_to_world, image_u, image_v)
        if base is None:
            continue
        out.append(
            _LocalRefinementDisplacement(
                image_u=image_u,
                image_v=image_v,
                world_x=world_x,
                world_z=world_z,
                delta_x=world_x - float(base[0]),
                delta_z=world_z - float(base[1]),
            )
        )
    return tuple(out)


def _boundary_image_for_edge(edge: str, t: float) -> tuple[float, float]:
    normalized_t = max(0.0, min(1.0, float(t)))
    if edge == "top":
        return normalized_t, 0.0
    if edge == "right":
        return 1.0, normalized_t
    if edge == "bottom":
        return 1.0 - normalized_t, 1.0
    return 0.0, 1.0 - normalized_t


def _boundary_refinement_displacements(
    H_image_to_world: Any,
    boundary_refinement_points: tuple[ControlPointBoundaryRefinementPoint, ...],
) -> tuple[_BoundaryRefinementDisplacement, ...]:
    out: list[_BoundaryRefinementDisplacement] = []
    per_edge: dict[str, int] = {}
    for point in boundary_refinement_points:
        edge = str(point.edge or "").strip()
        if edge not in BOUNDARY_REFINEMENT_EDGES:
            continue
        t = float(point.t)
        world_x = float(point.world_x)
        world_z = float(point.world_z)
        if not all(math.isfinite(value) for value in (t, world_x, world_z)):
            continue
        if not 0.0 <= t <= 1.0:
            continue
        edge_count = per_edge.get(edge, 0)
        if edge_count >= 8 or len(out) >= 32:
            continue
        per_edge[edge] = edge_count + 1
        image_u, image_v = _boundary_image_for_edge(edge, t)
        base = apply_homography(H_image_to_world, image_u, image_v)
        if base is None:
            continue
        out.append(
            _BoundaryRefinementDisplacement(
                id=str(point.id or "").strip(),
                edge=edge,  # type: ignore[arg-type]
                t=t,
                image_u=image_u,
                image_v=image_v,
                world_x=world_x,
                world_z=world_z,
                delta_x=world_x - float(base[0]),
                delta_z=world_z - float(base[1]),
            )
        )
    return tuple(out)


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 1.0 if value >= edge1 else 0.0
    t = max(0.0, min(1.0, (float(value) - float(edge0)) / (float(edge1) - float(edge0))))
    return t * t * (3.0 - 2.0 * t)


def _local_refinement_edge_falloff(u: float, v: float) -> float:
    edge_distance = min(float(u), float(v), 1.0 - float(u), 1.0 - float(v))
    return _smoothstep(LOCAL_REFINEMENT_EDGE_LOW, LOCAL_REFINEMENT_EDGE_HIGH, edge_distance)


def _local_refinement_delta(
    displacements: tuple[_LocalRefinementDisplacement, ...],
    u: float,
    v: float,
) -> tuple[float, float]:
    if not displacements:
        return 0.0, 0.0

    total_weight = 0.0
    total_x = 0.0
    total_z = 0.0
    sigma = LOCAL_REFINEMENT_SIGMA_UV
    for displacement in displacements:
        distance = math.hypot(float(u) - displacement.image_u, float(v) - displacement.image_v)
        if distance <= LOCAL_REFINEMENT_EPSILON:
            return displacement.delta_x, displacement.delta_z
        weight = math.exp(-((distance / sigma) ** 2))
        if weight <= 1e-12:
            continue
        total_weight += weight
        total_x += weight * displacement.delta_x
        total_z += weight * displacement.delta_z

    if total_weight <= 1e-12:
        return 0.0, 0.0
    edge = _local_refinement_edge_falloff(float(u), float(v))
    return edge * (total_x / total_weight), edge * (total_z / total_weight)


def _boundary_axis(edge: BoundaryRefinementEdge, u: float, v: float) -> float:
    if edge == "top":
        return float(u)
    if edge == "right":
        return float(v)
    if edge == "bottom":
        return 1.0 - float(u)
    return 1.0 - float(v)


def _boundary_distance(edge: BoundaryRefinementEdge, u: float, v: float) -> float:
    if edge == "top":
        return float(v)
    if edge == "right":
        return 1.0 - float(u)
    if edge == "bottom":
        return 1.0 - float(v)
    return float(u)


def _boundary_influence(edge: BoundaryRefinementEdge, u: float, v: float) -> float:
    distance = _boundary_distance(edge, u, v)
    if distance <= 1e-9:
        return 1.0
    normalized = max(0.0, min(1.0, distance / BOUNDARY_REFINEMENT_FALLOFF_UV))
    eased = normalized * normalized * (3.0 - 2.0 * normalized)
    return 1.0 - eased


def _boundary_delta_at_edge(
    displacements: tuple[_BoundaryRefinementDisplacement, ...],
    edge: BoundaryRefinementEdge,
    t: float,
) -> tuple[float, float]:
    edge_points = sorted((point for point in displacements if point.edge == edge), key=lambda point: point.t)
    if not edge_points:
        return 0.0, 0.0
    anchors: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
    anchors.extend((point.t, point.delta_x, point.delta_z) for point in edge_points)
    anchors.append((1.0, 0.0, 0.0))
    normalized_t = max(0.0, min(1.0, float(t)))
    for index in range(len(anchors) - 1):
        left_t, left_x, left_z = anchors[index]
        right_t, right_x, right_z = anchors[index + 1]
        if normalized_t < left_t or normalized_t > right_t:
            continue
        span = right_t - left_t
        local = (normalized_t - left_t) / span if span > 1e-9 else 0.0
        return left_x + (right_x - left_x) * local, left_z + (right_z - left_z) * local
    return anchors[-1][1], anchors[-1][2]


def _boundary_refinement_delta(
    displacements: tuple[_BoundaryRefinementDisplacement, ...],
    u: float,
    v: float,
) -> tuple[float, float]:
    if not displacements:
        return 0.0, 0.0
    total_x = 0.0
    total_z = 0.0
    for edge in BOUNDARY_REFINEMENT_EDGES:
        delta_x, delta_z = _boundary_delta_at_edge(displacements, edge, _boundary_axis(edge, u, v))
        influence = _boundary_influence(edge, u, v)
        total_x += delta_x * influence
        total_z += delta_z * influence
    return total_x, total_z


def _invert_refined_image_point(
    mapper: ControlPointMapper,
    x: float,
    z: float,
    initial_uv: tuple[float, float],
) -> tuple[float, float] | None:
    u = float(initial_uv[0])
    v = float(initial_uv[1])
    for _attempt in range(8):
        mapped = mapper.map_image_to_world(u, v)
        if mapped is None:
            return initial_uv
        error_x = float(mapped[0]) - float(x)
        error_z = float(mapped[1]) - float(z)
        if math.hypot(error_x, error_z) <= 1e-7:
            return u, v

        epsilon = 1e-4
        mapped_u = mapper.map_image_to_world(u + epsilon, v)
        mapped_v = mapper.map_image_to_world(u, v + epsilon)
        if mapped_u is None or mapped_v is None:
            return initial_uv

        j11 = (float(mapped_u[0]) - float(mapped[0])) / epsilon
        j21 = (float(mapped_u[1]) - float(mapped[1])) / epsilon
        j12 = (float(mapped_v[0]) - float(mapped[0])) / epsilon
        j22 = (float(mapped_v[1]) - float(mapped[1])) / epsilon
        determinant = j11 * j22 - j12 * j21
        if not math.isfinite(determinant) or abs(determinant) <= 1e-10:
            return initial_uv

        delta_u = (error_x * j22 - j12 * error_z) / determinant
        delta_v = (j11 * error_z - error_x * j21) / determinant
        if not (math.isfinite(delta_u) and math.isfinite(delta_v)):
            return initial_uv
        u = max(-0.25, min(1.25, u - delta_u))
        v = max(-0.25, min(1.25, v - delta_v))

    return u, v


def _candidate_homography_methods(method: str) -> tuple[str, ...]:
    normalized = str(method or "").strip().lower()
    if normalized == "dlt":
        return ("dlt",)
    if normalized == "ransac":
        return ("RANSAC", "dlt")
    if normalized == "usac_default":
        return ("USAC_DEFAULT", "RANSAC", "dlt")
    return ("USAC_MAGSAC", "USAC_DEFAULT", "RANSAC", "dlt")


def _mask_to_tuple(raw_mask: Any, expected_length: int) -> tuple[bool, ...]:
    if np is None:
        return tuple([True] * expected_length)
    if raw_mask is None:
        return tuple([True] * expected_length)
    flat = np.asarray(raw_mask).reshape(-1).tolist()
    if len(flat) != expected_length:
        return tuple([True] * expected_length)
    return tuple(bool(int(item)) for item in flat)


def _is_homography_numerically_unstable(H_world_to_image: Any, H_image_to_world: Any) -> bool:
    if np is None:
        return False
    try:
        if not np.isfinite(H_world_to_image).all() or not np.isfinite(H_image_to_world).all():
            return True
        cond_world = float(np.linalg.cond(H_world_to_image))
        cond_image = float(np.linalg.cond(H_image_to_world))
        if not math.isfinite(cond_world) or not math.isfinite(cond_image):
            return True
        return max(cond_world, cond_image) > 1e12
    except Exception:
        return True


def _percentile(values: list[float], percentile: float) -> float | None:
    if np is None or not values:
        return None
    return float(np.percentile(np.array(values, dtype=np.float64), percentile))


def _convex_hull_area_ratio(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    hull = _convex_hull(points)
    if len(hull) < 3:
        return 0.0
    area = abs(_polygon_area(hull))
    if not math.isfinite(area):
        return 0.0
    return max(0.0, min(1.0, float(area)))


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set((float(x), float(y)) for x, y in points))
    if len(unique) <= 1:
        return unique

    def cross(
        origin: tuple[float, float],
        a: tuple[float, float],
        b: tuple[float, float],
    ) -> float:
        return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def _polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        area += point[0] * next_point[1]
        area -= next_point[0] * point[1]
    return area / 2.0


def _solve_homography(src: Any, dst: Any) -> Any:
    if np is None:
        raise RuntimeError("numpy is required for control point mapping")

    A = []
    for (a, b), (x, y) in zip(src, dst, strict=False):
        A.append([-a, -b, -1, 0, 0, 0, a * x, b * x, x])
        A.append([0, 0, 0, -a, -b, -1, a * y, b * y, y])
    A = np.array(A, dtype=np.float64)
    _u, _s, vh = np.linalg.svd(A)
    h = vh[-1, :]
    H = h.reshape((3, 3))
    if abs(float(H[2, 2])) > 1e-12:
        H = H / float(H[2, 2])
    return H
