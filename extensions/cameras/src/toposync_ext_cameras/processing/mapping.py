from __future__ import annotations

import base64
import hashlib
import hmac
import math
import struct
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal


try:
    import numpy as np  # type: ignore
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]


HomographyMethod = Literal["usac_magsac", "usac_default", "ransac", "dlt"]
FallbackMode = Literal["default_set", "nearest_set", "none"]
MotionPolicyMode = Literal["skip_when_moving", "use_last_idle_pose", "allow_when_confident"]
BoundaryRefinementEdge = Literal["top", "right", "bottom", "left"]
MAX_VISUAL_POSE_SIGNATURE_CANDIDATES = 16


@dataclass(frozen=True, slots=True)
class ControlPointPair:
    image_u: float
    image_v: float
    world_x: float
    world_z: float


GroundLensType = Literal["identity_rectilinear_v1", "rectilinear_brown_v1", "fisheye_kb4_v1"]
GroundPointRole = Literal["fit", "check"]


@dataclass(frozen=True, slots=True)
class GroundLens:
    """Small, self-contained lens profile for a ground-plane calibration."""

    type: GroundLensType
    fx: float = 1.0
    fy: float = 1.0
    cx: float = 0.5
    cy: float = 0.5
    coefficients: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class GroundCalibrationPoint:
    id: str
    role: GroundPointRole
    image_u: float
    image_v: float
    world_x: float
    world_z: float


@dataclass(frozen=True, slots=True)
class GroundProjectionSpec:
    lens: GroundLens
    points: tuple[GroundCalibrationPoint, ...]
    source_geometry: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GroundPlaneMappingQuality:
    status: Literal["ready", "review", "incomplete"]
    number_of_fit_points: int
    number_of_inliers: int
    inlier_ratio: float
    image_hull_area_ratio_uv: float
    check_errors_meters: tuple[float, ...]
    median_reprojection_error_uv: float | None
    p95_reprojection_error_uv: float | None
    is_numerically_unstable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "number_of_fit_points": self.number_of_fit_points,
            "number_of_inliers": self.number_of_inliers,
            "inlier_ratio": self.inlier_ratio,
            "image_hull_area_ratio_uv": self.image_hull_area_ratio_uv,
            "check_errors_meters": list(self.check_errors_meters),
            "median_reprojection_error_uv": self.median_reprojection_error_uv,
            "p95_reprojection_error_uv": self.p95_reprojection_error_uv,
            "is_numerically_unstable": self.is_numerically_unstable,
        }


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
    preset_token: str | None = None
    preset_name: str | None = None
    geometry_safe: bool | None = None
    motion_epoch: int | None = None
    motion_state: str | None = None
    physical_updated_at: float | None = None


@dataclass(frozen=True, slots=True)
class VisualPoseSignature:
    algorithm: Literal["orb_hamming_v1"]
    keypoint_count: int
    keypoints_base64: str
    descriptors_base64: str
    original_width: int
    original_height: int
    digest_sha256: str


@dataclass(frozen=True, slots=True)
class ControlPointSet:
    id: str
    label: str
    pose_reference: PoseReference | None
    control_points: tuple[ControlPointPair, ...]
    refinement_points: tuple[ControlPointRefinementPoint, ...] = ()
    boundary_refinement_points: tuple[ControlPointBoundaryRefinementPoint, ...] = ()
    compatible_source_ids: tuple[str, ...] = ()
    compatible_roles: tuple[str, ...] = ()
    compatible_view_ids: tuple[str, ...] = ()
    physical_view_id: str | None = None
    visual_pose_signature: VisualPoseSignature | None = None
    ground_projection: GroundProjectionSpec | None = None
    requires_pose_evidence: bool = False


@dataclass(frozen=True, slots=True)
class PoseSelectionConfig:
    sigma_pan: float = 0.04
    sigma_tilt: float = 0.04
    sigma_zoom: float = 0.06
    max_distance: float = 3.0
    fallback_mode: FallbackMode = "none"
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
                float(self.median_reprojection_error_uv)
                if self.median_reprojection_error_uv is not None
                else None
            ),
            "p95_reprojection_error_uv": (
                float(self.p95_reprojection_error_uv)
                if self.p95_reprojection_error_uv is not None
                else None
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


@dataclass(frozen=True, slots=True)
class VisualPoseMatch:
    candidate_match_count: int
    inlier_count: int
    inlier_ratio: float
    current_coverage: float
    reference_coverage: float
    p95_reprojection_error_px: float
    overlap_ratio: float
    median_displacement_diagonal_ratio: float
    homography_current_to_reference: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]


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
        return None

    normalized_status = normalize_move_status(pan_tilt_zoom_state.move_status)
    if motion_policy_mode == "skip_when_moving" and normalized_status == "moving":
        return None

    active_preset_token = str(pan_tilt_zoom_state.preset_token or "").strip()
    if active_preset_token:
        preset_matches = [
            item
            for item in valid_sets
            if item.pose_reference is not None
            and str(item.pose_reference.preset_token or "").strip() == active_preset_token
        ]
        if len(preset_matches) == 1:
            return ControlPointSetSelection(
                control_point_set=preset_matches[0],
                pose_distance=0.0,
                pose_axes_used=("preset",),
                move_status=normalized_status,
                reason="preset_token_match",
            )

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

    return None


def select_control_point_set_by_unique_numeric_pose(
    control_point_sets: list[ControlPointSet] | tuple[ControlPointSet, ...],
    pan_tilt_zoom_state: PanTiltZoomState | None,
    config: PoseSelectionConfig,
    motion_policy_mode: MotionPolicyMode,
) -> ControlPointSetSelection | None:
    """Return numeric pose evidence only when it identifies one plausible view.

    A partially observed pose cannot exclude a view whose calibrated axes are
    unavailable in the current state. Likewise, two views inside the configured
    tolerance remain ambiguous even when one happens to have the smaller raw
    distance.
    """

    if pan_tilt_zoom_state is None:
        return None
    normalized_status = normalize_move_status(pan_tilt_zoom_state.move_status)
    if motion_policy_mode == "skip_when_moving" and normalized_status == "moving":
        return None

    pose_bound_sets = [
        item
        for item in control_point_sets
        if len(item.control_points) >= 4 and item.pose_reference is not None
    ]
    if not pose_bound_sets:
        return None

    plausible: list[tuple[float, tuple[str, ...], ControlPointSet]] = []
    for item in pose_bound_sets:
        pose_reference = item.pose_reference
        if pose_reference is None:
            continue
        distance_info = compute_pose_distance(
            pose_reference,
            pan_tilt_zoom_state,
            config,
        )
        if distance_info is None:
            return None
        distance, axes_used = distance_info
        if distance <= float(config.max_distance):
            plausible.append((float(distance), axes_used, item))

    if len(plausible) != 1:
        return None
    distance, axes_used, selected = plausible[0]
    return ControlPointSetSelection(
        control_point_set=selected,
        pose_distance=distance,
        pose_axes_used=axes_used,
        move_status=normalized_status,
        reason="unique_numeric_pose_match",
    )


def select_control_point_set_by_visual_signature(
    control_point_sets: list[ControlPointSet] | tuple[ControlPointSet, ...],
    frame: Any,
) -> tuple[ControlPointSet, VisualPoseMatch] | None:
    """Select a single view whose signed visual pose matches the exact frame.

    A preset token is deliberately not accepted here: command history can become
    stale after an external camera move or a process restart. Every in-scope
    signature is evaluated and an ambiguous result fails closed.
    """

    signed_sets = [
        control_point_set
        for control_point_set in control_point_sets
        if control_point_set.visual_pose_signature is not None
    ]
    if not signed_sets or len(signed_sets) > MAX_VISUAL_POSE_SIGNATURE_CANDIDATES:
        return None

    current_features = _extract_orb_visual_pose_features(frame)
    if current_features is None:
        return None
    current_points, current_descriptors = current_features

    accepted: list[tuple[ControlPointSet, VisualPoseMatch]] = []
    for control_point_set in signed_sets:
        signature = control_point_set.visual_pose_signature
        if signature is None:
            continue
        reference_features = _decode_visual_pose_signature(signature)
        if reference_features is None:
            continue
        reference_points, reference_descriptors = reference_features
        match = _match_orb_visual_pose(
            current_points=current_points,
            current_descriptors=current_descriptors,
            reference_points=reference_points,
            reference_descriptors=reference_descriptors,
            reference_width=int(signature.original_width),
            reference_height=int(signature.original_height),
        )
        if match is not None:
            accepted.append((control_point_set, match))
            if len(accepted) > 1:
                return None

    return accepted[0] if len(accepted) == 1 else None


def _extract_orb_visual_pose_features(frame: Any) -> tuple[Any, Any] | None:
    if np is None or frame is None:
        return None
    try:
        import cv2  # type: ignore
    except Exception:  # noqa: BLE001
        return None

    try:
        if isinstance(frame, (bytes, bytearray, memoryview)):
            encoded = bytes(frame)
            if not encoded or len(encoded) > 16 * 1024 * 1024:
                return None
            try:
                from PIL import Image

                with Image.open(BytesIO(encoded)) as image_header:
                    encoded_width, encoded_height = image_header.size
                encoded_width = int(encoded_width)
                encoded_height = int(encoded_height)
            except Exception:  # noqa: BLE001
                return None
            if (
                encoded_width < 2
                or encoded_height < 2
                or encoded_width > 50000
                or encoded_height > 50000
                or encoded_width * encoded_height > 50_000_000
            ):
                return None
            image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None or image.ndim != 2:
                return None
            if (
                int(image.shape[1]) != encoded_width
                or int(image.shape[0]) != encoded_height
            ):
                return None
        else:
            if not isinstance(frame, np.ndarray):
                return None
            image = frame
            if image.ndim not in {2, 3}:
                return None
            height, width = int(image.shape[0]), int(image.shape[1])
            if (
                height < 32
                or width < 32
                or height > 50000
                or width > 50000
                or height * width > 50_000_000
            ):
                return None
            if image.ndim == 3 and image.shape[2] not in {1, 3, 4}:
                return None
            if image.dtype != np.uint8 and not np.issubdtype(image.dtype, np.number):
                return None
            if image.ndim == 3:
                if image.shape[2] == 4:
                    image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
                elif image.shape[2] == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                elif image.shape[2] == 1:
                    image = image[:, :, 0]
                else:
                    return None
            if image.dtype != np.uint8:
                image = np.clip(image, 0, 255).astype(np.uint8)
        if image is None or image.size == 0:
            return None

        height, width = int(image.shape[0]), int(image.shape[1])
        if height < 32 or width < 32 or height > 50000 or width > 50000:
            return None
        if height * width > 50_000_000:
            return None
        max_edge = max(height, width)
        if max_edge > 1600:
            scale = 1600.0 / float(max_edge)
            width = max(2, int(round(float(width) * scale)))
            height = max(2, int(round(float(height) * scale)))
            image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

        orb = cv2.ORB_create(nfeatures=320)
        keypoints, descriptors = orb.detectAndCompute(np.ascontiguousarray(image), None)
        if descriptors is None or not keypoints:
            return None
        ordered = sorted(
            range(len(keypoints)),
            key=lambda index: (
                -float(keypoints[index].response),
                float(keypoints[index].pt[1]),
                float(keypoints[index].pt[0]),
                float(keypoints[index].size),
                float(keypoints[index].angle),
                int(keypoints[index].octave),
                int(keypoints[index].class_id),
            ),
        )[:320]
        if not ordered:
            return None
        denominator_x = float(max(1, width - 1))
        denominator_y = float(max(1, height - 1))
        points = np.asarray(
            [
                (
                    max(0.0, min(1.0, float(keypoints[index].pt[0]) / denominator_x)),
                    max(0.0, min(1.0, float(keypoints[index].pt[1]) / denominator_y)),
                )
                for index in ordered
            ],
            dtype=np.float32,
        )
        selected_descriptors = np.ascontiguousarray(descriptors[ordered], dtype=np.uint8)
        if selected_descriptors.ndim != 2 or selected_descriptors.shape[1] != 32:
            return None
        return points, selected_descriptors
    except Exception:  # noqa: BLE001
        return None


def _decode_visual_pose_signature(signature: VisualPoseSignature) -> tuple[Any, Any] | None:
    if np is None:
        return None
    try:
        keypoint_count = int(signature.keypoint_count)
        width = int(signature.original_width)
        height = int(signature.original_height)
        if signature.algorithm != "orb_hamming_v1":
            return None
        if keypoint_count < 16 or keypoint_count > 320:
            return None
        if width < 2 or width > 50000 or height < 2 or height > 50000:
            return None
        if width * height > 50_000_000:
            return None
        if len(signature.keypoints_base64) > 2048 or len(signature.descriptors_base64) > 14000:
            return None
        keypoint_bytes = base64.b64decode(signature.keypoints_base64, validate=True)
        descriptor_bytes = base64.b64decode(signature.descriptors_base64, validate=True)
        if len(keypoint_bytes) != keypoint_count * 4:
            return None
        if len(descriptor_bytes) != keypoint_count * 32:
            return None

        digest = str(signature.digest_sha256 or "").strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return None
        digest_payload = (
            b"orb_hamming_v1\0"
            + struct.pack("<III", width, height, keypoint_count)
            + keypoint_bytes
            + descriptor_bytes
        )
        expected_digest = hashlib.sha256(digest_payload).hexdigest()
        if not hmac.compare_digest(digest, expected_digest):
            return None

        quantized_points = np.frombuffer(keypoint_bytes, dtype="<u2").reshape(keypoint_count, 2)
        points = np.asarray(quantized_points, dtype=np.float32) / 65535.0
        descriptors = np.frombuffer(descriptor_bytes, dtype=np.uint8).reshape(keypoint_count, 32)
        return np.ascontiguousarray(points), np.ascontiguousarray(descriptors)
    except Exception:  # noqa: BLE001
        return None


def _match_orb_visual_pose(
    *,
    current_points: Any,
    current_descriptors: Any,
    reference_points: Any,
    reference_descriptors: Any,
    reference_width: int,
    reference_height: int,
) -> VisualPoseMatch | None:
    if np is None:
        return None
    try:
        import cv2  # type: ignore

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        forward_rows = matcher.knnMatch(current_descriptors, reference_descriptors, k=2)
        reverse_rows = matcher.knnMatch(reference_descriptors, current_descriptors, k=2)

        def _ratio_matches(rows: Any) -> dict[int, tuple[int, float]]:
            accepted: dict[int, tuple[int, float]] = {}
            for row in rows:
                if len(row) < 2:
                    continue
                best, second = row[0], row[1]
                if float(best.distance) < 0.80 * float(second.distance):
                    accepted[int(best.queryIdx)] = (int(best.trainIdx), float(best.distance))
            return accepted

        forward = _ratio_matches(forward_rows)
        reverse = _ratio_matches(reverse_rows)
        reciprocal = [
            (current_index, reference_index)
            for current_index, (reference_index, _distance) in forward.items()
            if reverse.get(reference_index, (-1, 0.0))[0] == current_index
        ]
        if len(reciprocal) < 16:
            return None

        current_normalized = np.asarray(
            [current_points[current_index] for current_index, _ in reciprocal],
            dtype=np.float64,
        )
        reference_normalized = np.asarray(
            [reference_points[reference_index] for _, reference_index in reciprocal],
            dtype=np.float64,
        )
        pixel_scale = np.asarray(
            [float(max(1, reference_width - 1)), float(max(1, reference_height - 1))],
            dtype=np.float64,
        )
        current_pixels = current_normalized * pixel_scale
        reference_pixels = reference_normalized * pixel_scale
        method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
        homography, mask = cv2.findHomography(
            current_pixels,
            reference_pixels,
            method=method,
            ransacReprojThreshold=4.0,
            maxIters=5000,
            confidence=0.999,
        )
        if homography is None or mask is None or not np.isfinite(homography).all():
            return None
        inliers = np.asarray(mask, dtype=np.uint8).reshape(-1).astype(bool)
        inlier_count = int(np.count_nonzero(inliers))
        inlier_ratio = float(inlier_count) / float(max(1, len(reciprocal)))
        if inlier_count < 12 or inlier_ratio < 0.60:
            return None

        current_inliers_normalized = current_normalized[inliers]
        reference_inliers_normalized = reference_normalized[inliers]
        current_coverage = _normalized_convex_hull_area(current_inliers_normalized, cv2=cv2)
        reference_coverage = _normalized_convex_hull_area(reference_inliers_normalized, cv2=cv2)
        if current_coverage < 0.03 or reference_coverage < 0.03:
            return None

        projected = cv2.perspectiveTransform(
            current_pixels[inliers].reshape(-1, 1, 2).astype(np.float64),
            homography,
        ).reshape(-1, 2)
        reprojection_errors = np.linalg.norm(projected - reference_pixels[inliers], axis=1)
        p95_reprojection_error = float(np.percentile(reprojection_errors, 95))
        if not math.isfinite(p95_reprojection_error) or p95_reprojection_error > 6.0:
            return None

        displacements = np.linalg.norm(
            current_inliers_normalized - reference_inliers_normalized,
            axis=1,
        ) / math.sqrt(2.0)
        median_displacement = float(np.median(displacements))
        if not math.isfinite(median_displacement) or median_displacement > 0.025:
            return None

        overlap = _visual_pose_homography_overlap(
            homography,
            reference_width=reference_width,
            reference_height=reference_height,
            cv2=cv2,
        )
        if overlap < 0.90:
            return None

        normalized_homography = _normalize_visual_pose_homography(
            homography,
            reference_width=reference_width,
            reference_height=reference_height,
        )
        if normalized_homography is None:
            return None

        return VisualPoseMatch(
            candidate_match_count=len(reciprocal),
            inlier_count=inlier_count,
            inlier_ratio=inlier_ratio,
            current_coverage=current_coverage,
            reference_coverage=reference_coverage,
            p95_reprojection_error_px=p95_reprojection_error,
            overlap_ratio=overlap,
            median_displacement_diagonal_ratio=median_displacement,
            homography_current_to_reference=normalized_homography,
        )
    except Exception:  # noqa: BLE001
        return None


def _normalize_visual_pose_homography(
    homography: Any,
    *,
    reference_width: int,
    reference_height: int,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
] | None:
    if np is None:
        return None
    try:
        scale = np.asarray(
            [
                [float(max(1, reference_width - 1)), 0.0, 0.0],
                [0.0, float(max(1, reference_height - 1)), 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        normalized = np.linalg.inv(scale) @ np.asarray(homography, dtype=np.float64) @ scale
        denominator = float(normalized[2, 2])
        if abs(denominator) <= 1e-12:
            return None
        normalized = normalized / denominator
        if not np.isfinite(normalized).all():
            return None
        return tuple(
            tuple(float(normalized[row, column]) for column in range(3))
            for row in range(3)
        )  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return None


def align_image_point_to_visual_pose_reference(
    point: tuple[float, float],
    visual_pose_match: VisualPoseMatch,
) -> tuple[float, float] | None:
    homography = visual_pose_match.homography_current_to_reference
    u = float(point[0])
    v = float(point[1])
    denominator = (
        float(homography[2][0]) * u
        + float(homography[2][1]) * v
        + float(homography[2][2])
    )
    if abs(denominator) <= 1e-12:
        return None
    aligned_u = (
        float(homography[0][0]) * u
        + float(homography[0][1]) * v
        + float(homography[0][2])
    ) / denominator
    aligned_v = (
        float(homography[1][0]) * u
        + float(homography[1][1]) * v
        + float(homography[1][2])
    ) / denominator
    if not math.isfinite(aligned_u) or not math.isfinite(aligned_v):
        return None
    epsilon = 1e-6
    if (
        aligned_u < -epsilon
        or aligned_u > 1.0 + epsilon
        or aligned_v < -epsilon
        or aligned_v > 1.0 + epsilon
    ):
        return None
    return (
        max(0.0, min(1.0, aligned_u)),
        max(0.0, min(1.0, aligned_v)),
    )


def _normalized_convex_hull_area(points: Any, *, cv2: Any) -> float:
    if np is None or len(points) < 3:
        return 0.0
    try:
        hull = cv2.convexHull(np.asarray(points, dtype=np.float32).reshape(-1, 1, 2))
        area = abs(float(cv2.contourArea(hull)))
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, min(1.0, area))


def _visual_pose_homography_overlap(
    homography: Any,
    *,
    reference_width: int,
    reference_height: int,
    cv2: Any,
) -> float:
    if np is None:
        return 0.0
    width = float(max(1, reference_width - 1))
    height = float(max(1, reference_height - 1))
    reference_quad = np.asarray(
        [[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]],
        dtype=np.float32,
    )
    try:
        projected_quad = cv2.perspectiveTransform(
            reference_quad.reshape(-1, 1, 2).astype(np.float64),
            homography,
        ).reshape(-1, 2)
        if not np.isfinite(projected_quad).all():
            return 0.0
        projected_quad = projected_quad.astype(np.float32)
        if not cv2.isContourConvex(projected_quad.reshape(-1, 1, 2)):
            return 0.0
        reference_area = abs(float(cv2.contourArea(reference_quad.reshape(-1, 1, 2))))
        projected_area = abs(float(cv2.contourArea(projected_quad.reshape(-1, 1, 2))))
        if reference_area <= 1e-9 or projected_area <= 1e-9:
            return 0.0
        intersection_area, _intersection = cv2.intersectConvexConvex(reference_quad, projected_quad)
        return max(
            0.0,
            min(1.0, float(intersection_area) / max(reference_area, projected_area)),
        )
    except Exception:  # noqa: BLE001
        return 0.0


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
    boundary_refinement_points: list[ControlPointBoundaryRefinementPoint]
    | tuple[ControlPointBoundaryRefinementPoint, ...],
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
        predicted_image = apply_homography(
            H_world_to_image, float(point.world_x), float(point.world_z)
        )
        if predicted_image is None:
            continue
        error = math.dist(expected_image, predicted_image)
        if inlier_mask[index]:
            image_errors.append(float(error))
            inlier_image_points.append(expected_image)

    if not image_errors:
        points_for_quality = fallback_image_points
        for point in pairs:
            predicted_image = apply_homography(
                H_world_to_image, float(point.world_x), float(point.world_z)
            )
            if predicted_image is None:
                continue
            image_errors.append(
                math.dist((float(point.image_u), float(point.image_v)), predicted_image)
            )
    else:
        points_for_quality = inlier_image_points

    hull_area_ratio = _convex_hull_area_ratio(points_for_quality)
    is_near_collinear = hull_area_ratio <= 1e-4
    is_numerically_unstable = _is_homography_numerically_unstable(
        H_world_to_image, H_image_to_world
    )
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
        refinement_points: list[ControlPointRefinementPoint]
        | tuple[ControlPointRefinementPoint, ...] = (),
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
        boundary_delta = _boundary_refinement_delta(
            self._boundary_refinement_displacements, float(u), float(v)
        )
        delta = _local_refinement_delta(self._refinement_displacements, float(u), float(v))
        return float(base[0]) + boundary_delta[0] + delta[0], float(base[1]) + boundary_delta[
            1
        ] + delta[1]

    def map_world_to_image(self, x: float, z: float) -> tuple[float, float] | None:
        base = apply_homography(self._H_world_to_image, x, z)
        if base is None or (
            not self._refinement_displacements and not self._boundary_refinement_displacements
        ):
            return base
        return _invert_refined_image_point(self, float(x), float(z), base)


class GroundPlaneMapper:
    """Map only the ground evidence actually supplied by the operator.

    V2 deliberately has no local deformation. The valid polygons make a
    homography useful without pretending that the whole frame is the ground.
    """

    def __init__(
        self,
        projection: GroundProjectionSpec,
        config: HomographyEstimationConfig | None = None,
    ) -> None:
        self._lens = projection.lens
        fit_points = tuple(point for point in projection.points if point.role == "fit")
        check_points = tuple(point for point in projection.points if point.role == "check")
        if len(fit_points) < 4:
            raise ValueError("At least 4 fit points are required")

        ideal_pairs: list[ControlPointPair] = []
        original_points: list[GroundCalibrationPoint] = []
        for point in fit_points:
            ideal = _ground_lens_image_to_ideal(self._lens, point.image_u, point.image_v)
            if ideal is None:
                raise ValueError("Invalid lens profile or image point")
            ideal_pairs.append(
                ControlPointPair(
                    image_u=ideal[0],
                    image_v=ideal[1],
                    world_x=float(point.world_x),
                    world_z=float(point.world_z),
                )
            )
            original_points.append(point)

        estimate = estimate_homography_world_to_image(ideal_pairs, config=config)
        self._H_world_to_ideal = estimate.H_world_to_image
        self._H_ideal_to_world = estimate.H_image_to_world
        self.inlier_mask = estimate.inlier_mask
        self.method_used = estimate.method_used
        inlier_points = [
            point for index, point in enumerate(original_points) if estimate.inlier_mask[index]
        ]
        if len(inlier_points) < 4:
            raise ValueError("Insufficient inliers")
        self._image_polygon = tuple(
            _convex_hull([(point.image_u, point.image_v) for point in inlier_points])
        )
        self._world_polygon = tuple(
            _convex_hull([(point.world_x, point.world_z) for point in inlier_points])
        )
        if len(self._image_polygon) < 3 or len(self._world_polygon) < 3:
            raise ValueError("Calibration points are collinear")

        check_errors: list[float] = []
        for point in check_points:
            mapped = self._map_unbounded(point.image_u, point.image_v)
            if mapped is None:
                check_errors.append(float("inf"))
                continue
            check_errors.append(math.dist(mapped, (point.world_x, point.world_z)))

        homography_quality = estimate.quality
        ready = (
            len(fit_points) >= 6
            and len(inlier_points) >= 6
            and homography_quality.inlier_ratio >= 0.8
            and homography_quality.convex_hull_area_ratio_uv >= 0.05
            and not homography_quality.is_near_collinear
            and not homography_quality.is_numerically_unstable
            and len(check_errors) >= 2
            and all(math.isfinite(error) and error <= 0.5 for error in check_errors)
        )
        self.quality = GroundPlaneMappingQuality(
            status="ready" if ready else "review" if len(fit_points) >= 4 else "incomplete",
            number_of_fit_points=len(fit_points),
            number_of_inliers=len(inlier_points),
            inlier_ratio=homography_quality.inlier_ratio,
            image_hull_area_ratio_uv=_convex_hull_area_ratio(
                [(point.image_u, point.image_v) for point in inlier_points]
            ),
            check_errors_meters=tuple(check_errors),
            median_reprojection_error_uv=homography_quality.median_reprojection_error_uv,
            p95_reprojection_error_uv=homography_quality.p95_reprojection_error_uv,
            is_numerically_unstable=homography_quality.is_numerically_unstable,
        )

    @property
    def image_polygon(self) -> tuple[tuple[float, float], ...]:
        return self._image_polygon

    @property
    def world_polygon(self) -> tuple[tuple[float, float], ...]:
        return self._world_polygon

    def _map_unbounded(self, u: float, v: float) -> tuple[float, float] | None:
        ideal = _ground_lens_image_to_ideal(self._lens, u, v)
        if ideal is None:
            return None
        return apply_homography(self._H_ideal_to_world, *ideal)

    def map(self, u: float, v: float) -> tuple[float, float] | None:
        if not _point_in_convex_polygon((u, v), self._image_polygon):
            return None
        mapped = self._map_unbounded(u, v)
        if mapped is None or not _point_in_convex_polygon(mapped, self._world_polygon):
            return None
        return mapped

    def map_image_to_world(self, u: float, v: float) -> tuple[float, float] | None:
        return self.map(u, v)

    def map_world_to_image(self, x: float, z: float) -> tuple[float, float] | None:
        if not _point_in_convex_polygon((x, z), self._world_polygon):
            return None
        ideal = apply_homography(self._H_world_to_ideal, x, z)
        if ideal is None:
            return None
        image = _ground_lens_ideal_to_image(self._lens, *ideal)
        if image is None or not _point_in_convex_polygon(image, self._image_polygon):
            return None
        return image


def _ground_lens_image_to_ideal(
    lens: GroundLens, u: float, v: float
) -> tuple[float, float] | None:
    if not all(math.isfinite(value) for value in (u, v)) or not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        return None
    if lens.type == "identity_rectilinear_v1":
        return float(u), float(v)
    if np is None or lens.fx <= 0.0 or lens.fy <= 0.0:
        return None
    try:
        import cv2  # type: ignore

        points = np.asarray([[[float(u), float(v)]]], dtype=np.float64)
        matrix = np.asarray(
            [[lens.fx, 0.0, lens.cx], [0.0, lens.fy, lens.cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        coefficients = np.asarray(lens.coefficients, dtype=np.float64)
        if lens.type == "fisheye_kb4_v1":
            if coefficients.shape != (4,):
                return None
            ideal = cv2.fisheye.undistortPoints(points, matrix, coefficients)
        else:
            if coefficients.shape not in {(4,), (5,), (8,)}:
                return None
            ideal = cv2.undistortPoints(points, matrix, coefficients)
        x, y = (float(value) for value in ideal.reshape(-1, 2)[0])
        return (x, y) if math.isfinite(x) and math.isfinite(y) else None
    except Exception:  # noqa: BLE001
        return None


def _ground_lens_ideal_to_image(
    lens: GroundLens, x: float, y: float
) -> tuple[float, float] | None:
    if not all(math.isfinite(value) for value in (x, y)):
        return None
    if lens.type == "identity_rectilinear_v1":
        return float(x), float(y)
    if lens.fx <= 0.0 or lens.fy <= 0.0:
        return None
    if lens.type == "rectilinear_brown_v1":
        if len(lens.coefficients) not in {4, 5, 8}:
            return None
        k1, k2, p1, p2, *rest = lens.coefficients
        k3 = rest[0] if rest else 0.0
        radius2 = x * x + y * y
        numerator = 1.0 + k1 * radius2 + k2 * radius2 * radius2 + k3 * radius2**3
        if len(rest) == 4:
            k4, k5, k6 = rest[1:]
            denominator = 1.0 + k4 * radius2 + k5 * radius2 * radius2 + k6 * radius2**3
            if abs(denominator) <= 1e-12:
                return None
            radial = numerator / denominator
        else:
            radial = numerator
        distorted_x = x * radial + 2.0 * p1 * x * y + p2 * (radius2 + 2.0 * x * x)
        distorted_y = y * radial + p1 * (radius2 + 2.0 * y * y) + 2.0 * p2 * x * y
    elif lens.type == "fisheye_kb4_v1":
        if len(lens.coefficients) != 4:
            return None
        radius = math.hypot(x, y)
        if radius <= 1e-12:
            distorted_x = distorted_y = 0.0
        else:
            theta = math.atan(radius)
            k1, k2, k3, k4 = lens.coefficients
            theta_d = theta * (
                1.0 + k1 * theta * theta + k2 * theta**4 + k3 * theta**6 + k4 * theta**8
            )
            scale = theta_d / radius
            distorted_x, distorted_y = x * scale, y * scale
    else:
        return None
    u = lens.fx * distorted_x + lens.cx
    v = lens.fy * distorted_y + lens.cy
    return (u, v) if math.isfinite(u) and math.isfinite(v) else None


def _point_in_convex_polygon(
    point: tuple[float, float], polygon: tuple[tuple[float, float], ...]
) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    sign = 0
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        cross = (end[0] - start[0]) * (y - start[1]) - (end[1] - start[1]) * (x - start[0])
        if abs(cross) <= 1e-9:
            continue
        current = 1 if cross > 0.0 else -1
        if sign and current != sign:
            return False
        sign = current
    return True


def build_calibration_mapper(
    control_point_set: ControlPointSet,
    config: HomographyEstimationConfig | None = None,
) -> ControlPointMapper | GroundPlaneMapper:
    if control_point_set.ground_projection is not None:
        return GroundPlaneMapper(control_point_set.ground_projection, config=config)
    return ControlPointMapper(
        list(control_point_set.control_points),
        config=config,
        refinement_points=control_point_set.refinement_points,
        boundary_refinement_points=control_point_set.boundary_refinement_points,
    )


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
    edge_points = sorted(
        (point for point in displacements if point.edge == edge), key=lambda point: point.t
    )
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
