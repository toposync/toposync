from __future__ import annotations

import base64
import hashlib
import math
import struct
from io import BytesIO
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    from PIL import Image
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]

from .mapping import (
    ControlPointMapper,
    ControlPointPair,
    ControlPointRefinementPoint,
    ControlPointSet,
    HomographyEstimationConfig,
    VisualPoseSignature as RuntimeVisualPoseSignature,
    apply_homography,
    estimate_homography_world_to_image,
    select_control_point_set_by_visual_signature,
)


MAX_IMAGE_SIDE_PX = 1600
MAX_INPUT_IMAGE_PIXELS = 50_000_000
MIN_CANDIDATE_MATCHES = 12
MIN_INLIERS = 10
MIN_INLIER_RATIO = 0.5
MIN_COVERAGE_RATIO = 0.05
MIN_OVERLAP_RATIO = 0.6
MIN_VIEW_CHANGE_RATIO = 0.01
MAX_SOURCE_EXTRAPOLATION_RATIO = 0.2
MAX_P95_REPROJECTION_ERROR_PX = 8.0
MAX_REFINEMENT_POINTS = 16
SIFT_RATIO_THRESHOLD = 0.75
COMPETING_HYPOTHESIS_INLIER_FRACTION = 0.6
COMPETING_HYPOTHESIS_MIN_DIVERGENCE_UV = 0.04
SELF_SYMMETRY_MAX_DESCRIPTOR_DISTANCE = 120.0
SELF_SYMMETRY_MIN_INLIERS = 20
SELF_SYMMETRY_MIN_COVERAGE_RATIO = 0.005
MAX_VISUAL_POSE_SIGNATURE_KEYPOINTS = 320
MIN_VISUAL_POSE_SIGNATURE_KEYPOINTS = 16
VISUAL_POSE_SIGNATURE_ALGORITHM = "orb_hamming_v1"


class VisualPoseSignature(TypedDict):
    algorithm: Literal["orb_hamming_v1"]
    keypoint_count: int
    keypoints_base64: str
    descriptors_base64: str
    original_width: int
    original_height: int
    digest_sha256: str


@dataclass(slots=True)
class VisualCalibrationQuality:
    method: str = ""
    source_width: int = 0
    source_height: int = 0
    target_width: int = 0
    target_height: int = 0
    source_keypoints: int = 0
    target_keypoints: int = 0
    candidate_matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    competing_inliers: int = 0
    ambiguity_ratio: float = 0.0
    symmetry_inliers: int = 0
    symmetry_coverage_ratio: float = 0.0
    median_reprojection_error_px: float | None = None
    p95_reprojection_error_px: float | None = None
    source_coverage_ratio: float = 0.0
    target_coverage_ratio: float = 0.0
    overlap_ratio: float = 0.0
    median_displacement_ratio: float = 0.0
    refinement_points: int = 0
    discarded_refinement_points: int = 0
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class VisualCalibrationResult:
    accepted: bool
    reason: str | None
    projection_model: dict[str, Any] | None
    source_visual_pose_signature: VisualPoseSignature | None
    quality: VisualCalibrationQuality

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _MatchedPoint:
    source_u: float
    source_v: float
    target_u: float
    target_v: float
    descriptor_distance: float


@dataclass(frozen=True, slots=True)
class _RefinementCandidate:
    id: str
    image_u: float
    image_v: float
    world_x: float
    world_z: float


@dataclass(frozen=True, slots=True)
class _DecodedGrayscaleImage:
    pixels: Any
    original_width: int
    original_height: int


def propagate_visual_calibration(
    source_image_bytes: bytes,
    target_image_bytes: bytes,
    source_control_point_set: ControlPointSet,
) -> VisualCalibrationResult:
    started = time.perf_counter()
    result = _propagate_visual_calibration(
        source_image_bytes,
        target_image_bytes,
        source_control_point_set,
    )
    result.quality.duration_ms = max(0, int(round((time.perf_counter() - started) * 1000.0)))
    return result


def _propagate_visual_calibration(
    source_image_bytes: bytes,
    target_image_bytes: bytes,
    source_control_point_set: ControlPointSet,
) -> VisualCalibrationResult:
    """Propagate one accepted image-to-world mapping into an overlapping camera view."""
    quality = VisualCalibrationQuality()
    if cv2 is None or np is None or not hasattr(cv2, "SIFT_create"):
        return _rejected("visual_features_unavailable", quality)
    if len(source_control_point_set.control_points) < 4:
        return _rejected("source_calibration_incomplete", quality)

    source_decoded = _decode_and_resize_grayscale(source_image_bytes)
    if source_decoded is None:
        return _rejected("invalid_source_image", quality)
    source_image = source_decoded.pixels
    quality.source_height, quality.source_width = source_image.shape[:2]

    existing_source_signature = source_control_point_set.visual_pose_signature
    if existing_source_signature is not None and not _runtime_visual_pose_signature_matches_frame(
        existing_source_signature,
        source_image,
    ):
        return _rejected("source_visual_pose_mismatch", quality)

    target_decoded = _decode_and_resize_grayscale(target_image_bytes)
    if target_decoded is None:
        return _rejected("invalid_target_image", quality)
    target_image = target_decoded.pixels
    quality.target_height, quality.target_width = target_image.shape[:2]

    try:
        sift = cv2.SIFT_create(nfeatures=5000)
        source_keypoints, source_descriptors = sift.detectAndCompute(source_image, None)
        target_keypoints, target_descriptors = sift.detectAndCompute(target_image, None)
    except Exception:  # noqa: BLE001
        return _rejected("feature_extraction_failed", quality)

    quality.source_keypoints = len(source_keypoints or [])
    quality.target_keypoints = len(target_keypoints or [])
    if source_descriptors is None or target_descriptors is None:
        return _rejected("insufficient_visual_features", quality)

    matches = _reciprocal_ratio_matches(
        source_keypoints,
        source_descriptors,
        target_keypoints,
        target_descriptors,
        source_width=quality.source_width,
        source_height=quality.source_height,
        target_width=quality.target_width,
        target_height=quality.target_height,
    )
    quality.candidate_matches = len(matches)
    if len(matches) < MIN_CANDIDATE_MATCHES:
        return _rejected("insufficient_candidate_matches", quality)

    pairs = [
        ControlPointPair(
            image_u=match.target_u,
            image_v=match.target_v,
            world_x=match.source_u,
            world_z=match.source_v,
        )
        for match in matches
    ]
    try:
        estimate = estimate_homography_world_to_image(
            pairs,
            HomographyEstimationConfig(method="usac_magsac"),
        )
    except Exception:  # noqa: BLE001
        return _rejected("homography_estimation_failed", quality)

    quality.method = estimate.method_used
    if "dlt" in estimate.method_used:
        return _rejected("robust_homography_unavailable", quality)

    inlier_matches = [
        match for match, is_inlier in zip(matches, estimate.inlier_mask, strict=True) if is_inlier
    ]
    quality.inliers = len(inlier_matches)
    quality.inlier_ratio = quality.inliers / max(1, quality.candidate_matches)
    if quality.inliers < MIN_INLIERS:
        return _rejected("insufficient_inliers", quality)
    if quality.inlier_ratio < MIN_INLIER_RATIO:
        return _rejected("low_inlier_ratio", quality)
    if estimate.quality.is_near_collinear or estimate.quality.is_numerically_unstable:
        return _rejected("unstable_homography", quality)

    quality.source_coverage_ratio = _coverage_ratio(
        [(match.source_u, match.source_v) for match in inlier_matches]
    )
    quality.target_coverage_ratio = _coverage_ratio(
        [(match.target_u, match.target_v) for match in inlier_matches]
    )
    if min(quality.source_coverage_ratio, quality.target_coverage_ratio) < MIN_COVERAGE_RATIO:
        return _rejected("insufficient_spatial_coverage", quality)

    source_us = [float(point.image_u) for point in source_control_point_set.control_points]
    source_vs = [float(point.image_v) for point in source_control_point_set.control_points]
    source_bounds = (min(source_us), min(source_vs), max(source_us), max(source_vs))
    source_span_u = source_bounds[2] - source_bounds[0]
    source_span_v = source_bounds[3] - source_bounds[1]
    if min(source_span_u, source_span_v) <= 1e-9:
        return _rejected("source_calibration_incomplete", quality)

    quality.overlap_ratio = _target_overlap_ratio(
        estimate.H_world_to_image,
        source_bounds=source_bounds,
    )
    if quality.overlap_ratio < MIN_OVERLAP_RATIO:
        return _rejected("insufficient_overlap", quality)

    quality.median_displacement_ratio = float(
        np.median(
            [
                math.hypot(
                    match.target_u - match.source_u,
                    match.target_v - match.source_v,
                )
                for match in inlier_matches
            ]
        )
    )
    if quality.median_displacement_ratio < MIN_VIEW_CHANGE_RATIO:
        return _rejected("insufficient_view_change", quality)

    reprojection_errors = _reprojection_errors_px(
        estimate.H_world_to_image,
        inlier_matches,
        target_width=quality.target_width,
        target_height=quality.target_height,
    )
    if not reprojection_errors:
        return _rejected("reprojection_failed", quality)
    quality.median_reprojection_error_px = float(np.percentile(reprojection_errors, 50.0))
    quality.p95_reprojection_error_px = float(np.percentile(reprojection_errors, 95.0))
    if quality.p95_reprojection_error_px > MAX_P95_REPROJECTION_ERROR_PX:
        return _rejected("high_reprojection_error", quality)

    source_symmetry = _self_symmetry_evidence(
        source_keypoints,
        source_descriptors,
        width=quality.source_width,
        height=quality.source_height,
    )
    target_symmetry = _self_symmetry_evidence(
        target_keypoints,
        target_descriptors,
        width=quality.target_width,
        height=quality.target_height,
    )
    strongest_symmetry = max(source_symmetry, target_symmetry, key=lambda item: item[0])
    quality.symmetry_inliers = strongest_symmetry[0]
    quality.symmetry_coverage_ratio = strongest_symmetry[1]
    if (
        quality.symmetry_inliers
        >= max(
            SELF_SYMMETRY_MIN_INLIERS,
            int(math.ceil(quality.inliers * COMPETING_HYPOTHESIS_INLIER_FRACTION)),
        )
        and quality.symmetry_coverage_ratio >= SELF_SYMMETRY_MIN_COVERAGE_RATIO
        and strongest_symmetry[2] >= COMPETING_HYPOTHESIS_MIN_DIVERGENCE_UV
    ):
        return _rejected("ambiguous_visual_pattern", quality)

    quality.competing_inliers, competing_divergence = _competing_homography_evidence(
        matches,
        estimate.inlier_mask,
        estimate.H_world_to_image,
        primary_inliers=quality.inliers,
        target_width=quality.target_width,
        target_height=quality.target_height,
    )
    quality.ambiguity_ratio = quality.competing_inliers / max(1, quality.inliers)
    if (
        quality.ambiguity_ratio >= COMPETING_HYPOTHESIS_INLIER_FRACTION
        and competing_divergence >= COMPETING_HYPOTHESIS_MIN_DIVERGENCE_UV
    ):
        return _rejected("ambiguous_visual_pattern", quality)

    try:
        source_mapper = ControlPointMapper(
            list(source_control_point_set.control_points),
            refinement_points=source_control_point_set.refinement_points,
            boundary_refinement_points=source_control_point_set.boundary_refinement_points,
        )
    except Exception:  # noqa: BLE001
        return _rejected("source_mapping_failed", quality)

    target_corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    source_margin_u = MAX_SOURCE_EXTRAPOLATION_RATIO * source_span_u
    source_margin_v = MAX_SOURCE_EXTRAPOLATION_RATIO * source_span_v
    world_corners: list[tuple[float, float]] = []
    for target_u, target_v in target_corners:
        source_point = apply_homography(estimate.H_image_to_world, target_u, target_v)
        if source_point is None:
            return _rejected("target_corner_projection_failed", quality)
        if not (
            source_bounds[0] - source_margin_u
            <= source_point[0]
            <= source_bounds[2] + source_margin_u
            and source_bounds[1] - source_margin_v
            <= source_point[1]
            <= source_bounds[3] + source_margin_v
        ):
            return _rejected("excessive_source_extrapolation", quality)
        world_point = source_mapper.map_image_to_world(source_point[0], source_point[1])
        if world_point is None or not all(math.isfinite(value) for value in world_point):
            return _rejected("target_corner_mapping_failed", quality)
        world_corners.append((float(world_point[0]), float(world_point[1])))

    if not _is_valid_convex_quad(world_corners):
        return _rejected("invalid_world_quad", quality)

    source_visual_pose_signature = (
        _runtime_visual_pose_signature_as_payload(existing_source_signature)
        if existing_source_signature is not None
        else _extract_visual_pose_signature(
            source_image,
            original_width=source_decoded.original_width,
            original_height=source_decoded.original_height,
        )
    )
    target_visual_pose_signature = _extract_visual_pose_signature(
        target_image,
        original_width=target_decoded.original_width,
        original_height=target_decoded.original_height,
    )
    if source_visual_pose_signature is None or target_visual_pose_signature is None:
        return _rejected("visual_pose_signature_failed", quality)

    refinements = _transport_refinements(
        source_control_point_set,
        estimate.H_world_to_image,
    )
    if refinements and _refined_mapping_has_foldover(world_corners, refinements):
        quality.discarded_refinement_points = len(refinements)
        refinements = []
    quality.refinement_points = len(refinements)
    projection_model = {
        "type": "image_quad_on_world",
        "image_region": {
            "top_left": {"x": 0.0, "y": 0.0},
            "bottom_right": {"x": 1.0, "y": 1.0},
        },
        "world_quad": {
            "top_left": _world_dict(world_corners[0]),
            "top_right": _world_dict(world_corners[1]),
            "bottom_right": _world_dict(world_corners[2]),
            "bottom_left": _world_dict(world_corners[3]),
        },
        "refinement": (
            {
                "model": "local_rbf_v1",
                "points": [
                    {
                        "id": point.id,
                        "image": {"x": point.image_u, "y": point.image_v},
                        "world": {"x": point.world_x, "z": point.world_z},
                    }
                    for point in refinements
                ],
            }
            if refinements
            else None
        ),
        "boundary_refinement": None,
        "visual_pose_signature": target_visual_pose_signature,
    }
    return VisualCalibrationResult(
        accepted=True,
        reason=None,
        projection_model=projection_model,
        source_visual_pose_signature=source_visual_pose_signature,
        quality=quality,
    )


def _extract_visual_pose_signature(
    grayscale_image: Any,
    *,
    original_width: int,
    original_height: int,
) -> VisualPoseSignature | None:
    if cv2 is None or np is None or grayscale_image is None:
        return None
    try:
        orb = cv2.ORB_create(nfeatures=MAX_VISUAL_POSE_SIGNATURE_KEYPOINTS)
        keypoints, descriptors = orb.detectAndCompute(grayscale_image, None)
    except Exception:  # noqa: BLE001
        return None
    if not keypoints or descriptors is None:
        return None

    usable_count = min(
        len(keypoints),
        int(descriptors.shape[0]),
        MAX_VISUAL_POSE_SIGNATURE_KEYPOINTS,
    )
    if (
        usable_count < MIN_VISUAL_POSE_SIGNATURE_KEYPOINTS
        or descriptors.ndim != 2
        or descriptors.shape[1] != 32
    ):
        return None

    ranked_indexes = sorted(
        range(usable_count),
        key=lambda index: (
            -float(keypoints[index].response),
            float(keypoints[index].pt[1]),
            float(keypoints[index].pt[0]),
            float(keypoints[index].size),
            float(keypoints[index].angle),
            int(keypoints[index].octave),
            int(keypoints[index].class_id),
        ),
    )[:MAX_VISUAL_POSE_SIGNATURE_KEYPOINTS]
    if not ranked_indexes:
        return None

    image_height, image_width = grayscale_image.shape[:2]
    denominator_x = max(1, int(image_width) - 1)
    denominator_y = max(1, int(image_height) - 1)
    normalized_keypoints = np.empty((len(ranked_indexes), 2), dtype="<u2")
    for output_index, keypoint_index in enumerate(ranked_indexes):
        x, y = keypoints[keypoint_index].pt
        normalized_keypoints[output_index, 0] = int(
            round(min(1.0, max(0.0, float(x) / denominator_x)) * 65535.0)
        )
        normalized_keypoints[output_index, 1] = int(
            round(min(1.0, max(0.0, float(y) / denominator_y)) * 65535.0)
        )

    selected_descriptors = np.ascontiguousarray(
        descriptors[np.asarray(ranked_indexes, dtype=np.intp)],
        dtype=np.uint8,
    )
    keypoint_bytes = normalized_keypoints.tobytes(order="C")
    descriptor_bytes = selected_descriptors.tobytes(order="C")
    keypoint_count = len(ranked_indexes)

    digest = hashlib.sha256()
    digest.update(VISUAL_POSE_SIGNATURE_ALGORITHM.encode("ascii"))
    digest.update(b"\0")
    digest.update(
        struct.pack(
            "<III",
            int(original_width),
            int(original_height),
            keypoint_count,
        )
    )
    digest.update(keypoint_bytes)
    digest.update(descriptor_bytes)

    signature: VisualPoseSignature = {
        "algorithm": VISUAL_POSE_SIGNATURE_ALGORITHM,
        "keypoint_count": keypoint_count,
        "keypoints_base64": base64.b64encode(keypoint_bytes).decode("ascii"),
        "descriptors_base64": base64.b64encode(descriptor_bytes).decode("ascii"),
        "original_width": int(original_width),
        "original_height": int(original_height),
        "digest_sha256": digest.hexdigest(),
    }
    if not _visual_pose_signature_matches_own_frame(signature, grayscale_image):
        return None
    return signature


def _visual_pose_signature_matches_own_frame(
    signature: VisualPoseSignature,
    grayscale_image: Any,
) -> bool:
    """Prove that the runtime selector can recognize this exact signed frame."""

    if int(signature["keypoint_count"]) < MIN_VISUAL_POSE_SIGNATURE_KEYPOINTS:
        return False
    try:
        runtime_signature = RuntimeVisualPoseSignature(
            algorithm=signature["algorithm"],
            keypoint_count=int(signature["keypoint_count"]),
            keypoints_base64=signature["keypoints_base64"],
            descriptors_base64=signature["descriptors_base64"],
            original_width=int(signature["original_width"]),
            original_height=int(signature["original_height"]),
            digest_sha256=signature["digest_sha256"],
        )
    except Exception:  # noqa: BLE001
        return False
    return _runtime_visual_pose_signature_matches_frame(runtime_signature, grayscale_image)


def _runtime_visual_pose_signature_matches_frame(
    signature: RuntimeVisualPoseSignature,
    grayscale_image: Any,
) -> bool:
    if int(signature.keypoint_count) < MIN_VISUAL_POSE_SIGNATURE_KEYPOINTS:
        return False
    try:
        sentinel = ControlPointSet(
            id="visual-pose-self-check",
            label="Visual pose self-check",
            pose_reference=None,
            control_points=(),
            visual_pose_signature=signature,
        )
        selected = select_control_point_set_by_visual_signature((sentinel,), grayscale_image)
    except Exception:  # noqa: BLE001
        return False
    return selected is not None and selected[0] is sentinel


def _runtime_visual_pose_signature_as_payload(
    signature: RuntimeVisualPoseSignature,
) -> VisualPoseSignature:
    return {
        "algorithm": signature.algorithm,
        "keypoint_count": int(signature.keypoint_count),
        "keypoints_base64": signature.keypoints_base64,
        "descriptors_base64": signature.descriptors_base64,
        "original_width": int(signature.original_width),
        "original_height": int(signature.original_height),
        "digest_sha256": signature.digest_sha256,
    }


def _decode_and_resize_grayscale(image_bytes: bytes) -> _DecodedGrayscaleImage | None:
    if not isinstance(image_bytes, (bytes, bytearray, memoryview)) or not image_bytes:
        return None
    input_width: int | None = None
    input_height: int | None = None
    if Image is not None:
        try:
            with Image.open(BytesIO(image_bytes)) as image_header:
                input_width, input_height = image_header.size
            if (
                input_width < 2
                or input_height < 2
                or input_width * input_height > MAX_INPUT_IMAGE_PIXELS
            ):
                return None
        except Exception:  # noqa: BLE001
            return None
    try:
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except Exception:  # noqa: BLE001
        return None
    if image is None or image.ndim != 2 or min(image.shape[:2]) < 2:
        return None
    height, width = image.shape[:2]
    original_width = int(input_width if input_width is not None else width)
    original_height = int(input_height if input_height is not None else height)
    longest_side = max(height, width)
    if longest_side <= MAX_IMAGE_SIDE_PX:
        return _DecodedGrayscaleImage(
            pixels=image,
            original_width=original_width,
            original_height=original_height,
        )
    scale = MAX_IMAGE_SIDE_PX / float(longest_side)
    return _DecodedGrayscaleImage(
        pixels=cv2.resize(
            image,
            (max(2, int(round(width * scale))), max(2, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        ),
        original_width=original_width,
        original_height=original_height,
    )


def _reciprocal_ratio_matches(
    source_keypoints: Any,
    source_descriptors: Any,
    target_keypoints: Any,
    target_descriptors: Any,
    *,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> list[_MatchedPoint]:
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    try:
        forward = matcher.knnMatch(source_descriptors, target_descriptors, k=2)
        reverse = matcher.knnMatch(target_descriptors, source_descriptors, k=2)
    except Exception:  # noqa: BLE001
        return []

    forward_matches = _ratio_filtered_matches(forward)
    reverse_matches = _ratio_filtered_matches(reverse)
    reverse_by_target = {match.queryIdx: match for match in reverse_matches}
    source_denominator_x = max(1, source_width - 1)
    source_denominator_y = max(1, source_height - 1)
    target_denominator_x = max(1, target_width - 1)
    target_denominator_y = max(1, target_height - 1)

    output: list[_MatchedPoint] = []
    for match in forward_matches:
        reciprocal = reverse_by_target.get(match.trainIdx)
        if reciprocal is None or reciprocal.trainIdx != match.queryIdx:
            continue
        source_x, source_y = source_keypoints[match.queryIdx].pt
        target_x, target_y = target_keypoints[match.trainIdx].pt
        output.append(
            _MatchedPoint(
                source_u=float(source_x) / source_denominator_x,
                source_v=float(source_y) / source_denominator_y,
                target_u=float(target_x) / target_denominator_x,
                target_v=float(target_y) / target_denominator_y,
                descriptor_distance=float(match.distance),
            )
        )
    return sorted(output, key=lambda item: item.descriptor_distance)


def _ratio_filtered_matches(raw_matches: Any) -> list[Any]:
    output: list[Any] = []
    for candidates in raw_matches:
        if len(candidates) < 2:
            continue
        best, second = candidates[0], candidates[1]
        if float(best.distance) < SIFT_RATIO_THRESHOLD * float(second.distance):
            output.append(best)
    return output


def _coverage_ratio(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    try:
        contour = np.asarray(points, dtype=np.float32).reshape((-1, 1, 2))
        hull = cv2.convexHull(contour)
        return max(0.0, min(1.0, float(cv2.contourArea(hull))))
    except Exception:  # noqa: BLE001
        return 0.0


def _reprojection_errors_px(
    homography_source_to_target: Any,
    matches: list[_MatchedPoint],
    *,
    target_width: int,
    target_height: int,
) -> list[float]:
    errors: list[float] = []
    target_denominator_x = max(1, target_width - 1)
    target_denominator_y = max(1, target_height - 1)
    for match in matches:
        predicted = apply_homography(
            homography_source_to_target,
            match.source_u,
            match.source_v,
        )
        if predicted is None:
            continue
        errors.append(
            math.hypot(
                (predicted[0] - match.target_u) * target_denominator_x,
                (predicted[1] - match.target_v) * target_denominator_y,
            )
        )
    return errors


def _competing_homography_evidence(
    matches: list[_MatchedPoint],
    primary_inlier_mask: tuple[bool, ...],
    primary_homography: Any,
    *,
    primary_inliers: int,
    target_width: int,
    target_height: int,
) -> tuple[int, float]:
    outlier_matches = [
        match
        for match, is_primary_inlier in zip(matches, primary_inlier_mask, strict=True)
        if not is_primary_inlier
    ]
    required_inliers = max(
        MIN_INLIERS,
        int(math.ceil(primary_inliers * COMPETING_HYPOTHESIS_INLIER_FRACTION)),
    )
    if len(outlier_matches) < required_inliers:
        return 0, 0.0

    pairs = [
        ControlPointPair(
            image_u=match.target_u,
            image_v=match.target_v,
            world_x=match.source_u,
            world_z=match.source_v,
        )
        for match in outlier_matches
    ]
    try:
        estimate = estimate_homography_world_to_image(
            pairs,
            HomographyEstimationConfig(method="usac_magsac"),
        )
    except Exception:  # noqa: BLE001
        return 0, 0.0
    if "dlt" in estimate.method_used:
        return 0, 0.0

    competing_matches = [
        match
        for match, is_inlier in zip(outlier_matches, estimate.inlier_mask, strict=True)
        if is_inlier
    ]
    competing_inliers = len(competing_matches)
    if competing_inliers < required_inliers:
        return competing_inliers, 0.0
    if (
        min(
            _coverage_ratio([(match.source_u, match.source_v) for match in competing_matches]),
            _coverage_ratio([(match.target_u, match.target_v) for match in competing_matches]),
        )
        < MIN_COVERAGE_RATIO
    ):
        return competing_inliers, 0.0
    errors = _reprojection_errors_px(
        estimate.H_world_to_image,
        competing_matches,
        target_width=target_width,
        target_height=target_height,
    )
    if not errors or float(np.percentile(errors, 95.0)) > MAX_P95_REPROJECTION_ERROR_PX:
        return competing_inliers, 0.0

    divergences: list[float] = []
    for source_u, source_v in (
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
        (0.5, 0.5),
    ):
        primary_point = apply_homography(primary_homography, source_u, source_v)
        competing_point = apply_homography(estimate.H_world_to_image, source_u, source_v)
        if primary_point is None or competing_point is None:
            return competing_inliers, 0.0
        divergences.append(math.dist(primary_point, competing_point))
    return competing_inliers, float(np.median(divergences))


def _self_symmetry_evidence(
    keypoints: Any,
    descriptors: Any,
    *,
    width: int,
    height: int,
) -> tuple[int, float, float]:
    if descriptors is None or len(keypoints or []) < SELF_SYMMETRY_MIN_INLIERS:
        return 0, 0.0, 0.0
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    try:
        raw_matches = matcher.knnMatch(descriptors, descriptors, k=4)
    except Exception:  # noqa: BLE001
        return 0, 0.0, 0.0

    denominator_x = max(1, width - 1)
    denominator_y = max(1, height - 1)
    matches: list[_MatchedPoint] = []
    for query_index, candidates in enumerate(raw_matches):
        source_x, source_y = keypoints[query_index].pt
        selected = None
        for candidate in candidates:
            if candidate.trainIdx == query_index:
                continue
            target_x, target_y = keypoints[candidate.trainIdx].pt
            if (
                math.hypot(
                    (float(target_x) - float(source_x)) / denominator_x,
                    (float(target_y) - float(source_y)) / denominator_y,
                )
                < 0.02
            ):
                continue
            selected = candidate
            break
        if selected is None or float(selected.distance) > SELF_SYMMETRY_MAX_DESCRIPTOR_DISTANCE:
            continue
        target_x, target_y = keypoints[selected.trainIdx].pt
        matches.append(
            _MatchedPoint(
                source_u=float(source_x) / denominator_x,
                source_v=float(source_y) / denominator_y,
                target_u=float(target_x) / denominator_x,
                target_v=float(target_y) / denominator_y,
                descriptor_distance=float(selected.distance),
            )
        )
    if len(matches) < SELF_SYMMETRY_MIN_INLIERS:
        return 0, 0.0, 0.0

    pairs = [
        ControlPointPair(
            image_u=match.target_u,
            image_v=match.target_v,
            world_x=match.source_u,
            world_z=match.source_v,
        )
        for match in matches
    ]
    try:
        estimate = estimate_homography_world_to_image(
            pairs,
            HomographyEstimationConfig(method="usac_magsac"),
        )
    except Exception:  # noqa: BLE001
        return 0, 0.0, 0.0
    if "dlt" in estimate.method_used:
        return 0, 0.0, 0.0

    inlier_matches = [
        match for match, is_inlier in zip(matches, estimate.inlier_mask, strict=True) if is_inlier
    ]
    coverage = min(
        _coverage_ratio([(match.source_u, match.source_v) for match in inlier_matches]),
        _coverage_ratio([(match.target_u, match.target_v) for match in inlier_matches]),
    )
    divergences: list[float] = []
    for u, v in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.5, 0.5)):
        point = apply_homography(estimate.H_world_to_image, u, v)
        if point is None:
            return len(inlier_matches), coverage, 0.0
        divergences.append(math.dist((u, v), point))
    return len(inlier_matches), coverage, float(np.median(divergences))


def _target_overlap_ratio(
    homography_source_to_target: Any,
    *,
    source_bounds: tuple[float, float, float, float],
) -> float:
    mapped: list[tuple[float, float]] = []
    left, top, right, bottom = source_bounds
    for source_u, source_v in (
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
    ):
        point = apply_homography(homography_source_to_target, source_u, source_v)
        if point is None or not all(math.isfinite(value) for value in point):
            return 0.0
        mapped.append(point)
    try:
        source_polygon = np.asarray(mapped, dtype=np.float32).reshape((-1, 1, 2))
        if not cv2.isContourConvex(source_polygon):
            return 0.0
        target_polygon = np.asarray(
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            dtype=np.float32,
        ).reshape((-1, 1, 2))
        intersection_area, _intersection = cv2.intersectConvexConvex(source_polygon, target_polygon)
        return max(0.0, min(1.0, float(intersection_area)))
    except Exception:  # noqa: BLE001
        return 0.0


def _transport_refinements(
    source_control_point_set: ControlPointSet,
    homography_source_to_target: Any,
) -> list[_RefinementCandidate]:
    candidates: list[_RefinementCandidate] = []
    # Boundary handles encode an edge-specific falloff. Converting them into
    # unconstrained local radial-basis-function points can fold the target mesh,
    # so only refinements whose semantics survive the view change are carried.
    for index, point in enumerate(source_control_point_set.refinement_points):
        target = apply_homography(
            homography_source_to_target,
            float(point.image_u),
            float(point.image_v),
        )
        if target is None or not (0.0 <= target[0] <= 1.0 and 0.0 <= target[1] <= 1.0):
            continue
        world_x = float(point.world_x)
        world_z = float(point.world_z)
        if not all(math.isfinite(value) for value in (target[0], target[1], world_x, world_z)):
            continue
        candidates.append(
            _RefinementCandidate(
                id=str(point.id or "").strip() or f"local-{index + 1}",
                image_u=float(target[0]),
                image_v=float(target[1]),
                world_x=world_x,
                world_z=world_z,
            )
        )
    return _select_distributed_refinements(candidates, MAX_REFINEMENT_POINTS)


def _select_distributed_refinements(
    candidates: list[_RefinementCandidate],
    limit: int,
) -> list[_RefinementCandidate]:
    if len(candidates) <= limit:
        return candidates
    remaining = sorted(candidates, key=lambda item: item.id)
    first = max(
        remaining,
        key=lambda item: (item.image_u - 0.5) ** 2 + (item.image_v - 0.5) ** 2,
    )
    selected = [first]
    remaining.remove(first)
    while remaining and len(selected) < limit:
        next_point = max(
            remaining,
            key=lambda candidate: min(
                (candidate.image_u - chosen.image_u) ** 2
                + (candidate.image_v - chosen.image_v) ** 2
                for chosen in selected
            ),
        )
        selected.append(next_point)
        remaining.remove(next_point)
    return selected


def _refined_mapping_has_foldover(
    world_corners: list[tuple[float, float]],
    refinements: list[_RefinementCandidate],
    *,
    divisions: int = 34,
) -> bool:
    pairs = [
        ControlPointPair(image_u=u, image_v=v, world_x=world[0], world_z=world[1])
        for (u, v), world in zip(
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            world_corners,
            strict=True,
        )
    ]
    refinement_points = [
        ControlPointRefinementPoint(
            id=point.id,
            image_u=point.image_u,
            image_v=point.image_v,
            world_x=point.world_x,
            world_z=point.world_z,
        )
        for point in refinements
    ]
    try:
        mapper = ControlPointMapper(pairs, refinement_points=refinement_points)
    except Exception:  # noqa: BLE001
        return True

    sign = 0
    for row in range(divisions):
        top = row / divisions
        bottom = (row + 1) / divisions
        for column in range(divisions):
            left = column / divisions
            right = (column + 1) / divisions
            points = [
                mapper.map_image_to_world(left, top),
                mapper.map_image_to_world(right, top),
                mapper.map_image_to_world(right, bottom),
                mapper.map_image_to_world(left, bottom),
            ]
            if any(point is None for point in points):
                return True
            a, b, c, d = points
            assert a is not None and b is not None and c is not None and d is not None
            for first, second, third in ((a, b, c), (a, c, d)):
                area = (
                    (second[0] - first[0]) * (third[1] - first[1])
                    - (second[1] - first[1]) * (third[0] - first[0])
                ) / 2.0
                if not math.isfinite(area) or abs(area) <= 1e-9:
                    return True
                current_sign = 1 if area > 0.0 else -1
                if sign == 0:
                    sign = current_sign
                elif current_sign != sign:
                    return True
    return False


def _is_valid_convex_quad(points: list[tuple[float, float]]) -> bool:
    if len(points) != 4 or not all(math.isfinite(value) for point in points for value in point):
        return False
    area = abs(
        sum(
            points[index][0] * points[(index + 1) % 4][1]
            - points[(index + 1) % 4][0] * points[index][1]
            for index in range(4)
        )
        / 2.0
    )
    if area <= 1e-9:
        return False
    cross_products = []
    for index in range(4):
        origin = points[index]
        first = points[(index + 1) % 4]
        second = points[(index + 2) % 4]
        cross_products.append(
            (first[0] - origin[0]) * (second[1] - first[1])
            - (first[1] - origin[1]) * (second[0] - first[0])
        )
    return all(value > 1e-9 for value in cross_products) or all(
        value < -1e-9 for value in cross_products
    )


def _world_dict(point: tuple[float, float]) -> dict[str, float]:
    return {"x": float(point[0]), "z": float(point[1])}


def _rejected(reason: str, quality: VisualCalibrationQuality) -> VisualCalibrationResult:
    return VisualCalibrationResult(
        accepted=False,
        reason=reason,
        projection_model=None,
        source_visual_pose_signature=None,
        quality=quality,
    )
