"""Bounded, image-based capture readiness for an automatic panorama.

This module neither moves a camera nor obtains frames. Media timestamps must be
seconds from a validated decoder time base, never wall-clock receive times. A
positive result is acquisition evidence only; it does not satisfy the legacy
``freshness=physical`` or metric mapping contracts.

An explicit ``allow_observation_timing`` opt-in supports visual mosaics on
decoders without PTS. It measures displacement and the real observation window,
never media speed. This mode cannot disprove every possible transport backlog.

Call ``reset`` before a move, observe its frames continuously, then ``arm_stop``
after requesting its stop. A physical timestamp flag is accepted only when the
caller has independently verified that this frame was exposed after that stop.
Without that evidence, an observed motion-to-stability transition is mandatory.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import cv2
import numpy as np

from .panorama_correspondences import track_image_points


@dataclass(frozen=True)
class StabilitySettings:
    """Internal thresholds, expressed at ``analysis_width`` pixels."""

    analysis_width: int = 960
    minimum_observation_seconds: float = 0.5
    stable_window_seconds: float = 0.8
    minimum_window_frames: int = 5
    timeout_seconds: float = 12.0
    maximum_frame_interval_seconds: float = 1.0
    maximum_speed_pixels_per_second: float = 1.0
    maximum_observed_displacement_pixels: float = 0.15
    maximum_drift_pixels: float = 0.5
    motion_transition_speed_pixels_per_second: float = 3.0
    motion_transition_pixels: float = 0.35
    minimum_tracks: int = 80
    minimum_occupied_cells: int = 6
    minimum_inlier_fraction: float = 0.55
    maximum_forward_backward_error_pixels: float = 0.75
    maximum_model_error_pixels: float = 0.8
    pose_tolerance: float = 0.003
    minimum_sharpness_fraction: float = 0.75

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not 160 <= self.analysis_width <= 1920 or self.minimum_tracks < 8:
            raise ValueError("Analysis dimensions and visual support are too small")
        if self.minimum_window_frames < 3 or self.minimum_occupied_cells > 12:
            raise ValueError("Invalid temporal or spatial support")
        if self.timeout_seconds <= max(
            self.stable_window_seconds, self.minimum_observation_seconds
        ):
            raise ValueError("Timeout must exceed the observation window")
        if not 0 < self.minimum_inlier_fraction <= 1:
            raise ValueError("Inlier fraction must be in (0, 1]")
        if not 0 < self.minimum_sharpness_fraction <= 1:
            raise ValueError("Sharpness fraction must be in (0, 1]")
        if self.motion_transition_speed_pixels_per_second <= (self.maximum_speed_pixels_per_second):
            raise ValueError("Motion transition must exceed the stability threshold")


@dataclass
class _Frame:
    image: np.ndarray
    timeline_time: float
    timing_basis: str
    received_monotonic: float
    sequence: int
    generation: int
    pose: dict[str, float]
    sharpness: float
    exposure: float
    physical_timestamp_verified: bool


@dataclass
class _Sample:
    frame: _Frame
    transform: np.ndarray


class VisualStabilityDetector:
    """Observe ordered frames and return JSON-serializable readiness evidence.

    Unknown position, movement status and zoom stay unknown. Optional overlay
    rectangles are normalized ``(left, top, right, bottom)`` coordinates, obtained
    from the source rather than a hard-coded clock location. Screen-fixed points
    discovered during well-supported camera motion are also excluded.

    Memory is bounded: only the previous grayscale frame and scalar/transform
    records for one window are retained. ``qualified_frames`` ranks the approved
    window by sharpness so the caller can select an original still in its bounded
    capture buffer and bind ``best_sequence`` to that actual photograph.
    """

    def __init__(
        self,
        settings: StabilitySettings | None = None,
        *,
        overlay_regions: list[tuple[float, float, float, float]] | None = None,
        allow_observation_timing: bool = False,
        transition_estimator: Callable[[np.ndarray, np.ndarray], dict | None] | None = None,
    ) -> None:
        self.settings = settings or StabilitySettings()
        self.allow_observation_timing = bool(allow_observation_timing)
        self.transition_estimator = transition_estimator
        self.overlay_regions = tuple(overlay_regions or ())
        for left, top, right, bottom in self.overlay_regions:
            if not all(math.isfinite(value) for value in (left, top, right, bottom)):
                raise ValueError("Overlay coordinates must be finite")
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise ValueError("Overlay rectangles must be normalized and nonempty")
        self.reset()

    def reset(self, require_motion_transition: bool = True, now: float | None = None) -> None:
        """Begin a new attempt; this never approves timestamp-free static frames.

        ``require_motion_transition=False`` permits an initial reference with
        independently verified physical timestamps; it is not a safety bypass.
        """
        if now is not None and not math.isfinite(now):
            raise ValueError("Attempt start must be finite")
        self._required_transition = bool(require_motion_transition)
        self._started = now
        self._stop_received = now
        self._previous: _Frame | None = None
        self._transition_anchor: _Frame | None = None
        self._transition_tracking_lost = False
        self._last_transition_search: float | None = None
        self._recovery_diagnostic: dict[str, Any] = {}
        self._window: deque[_Sample] = deque(maxlen=240)
        self._has_motion_transition = False
        self._allow_repeated_endpoint_samples = False
        self._timed_out = False
        self._learned_overlay: np.ndarray | None = None
        self._overlay_votes: np.ndarray | None = None
        self._last_received: float | None = None
        self._last_sequence: int | None = None
        self._last_generation: int | None = None
        self._last_timeline_time: float | None = None

    def arm_stop(self, *, now: float) -> None:
        """Mark the command boundary, preserving motion already seen in video.

        Receiving a stop response is not proof of physical stop. The subsequent
        visual window must still pass all tests. Calling this resets no timeout.
        """
        if not math.isfinite(now):
            raise ValueError("Stop time must be finite")
        self._stop_received = now
        self._window.clear()

    def arm_verified_endpoint_transition(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Seed a transition proven by a separate endpoint match.

        Some transports deliver the first new view only after a fast movement
        has already stopped. The ordinary adjacent-frame observer cannot see
        that jump. A caller may use this method only on a fresh detector and
        only with the complete result of the scanner's independent geometric
        matcher. A spatially localized result is accepted only when the scanner
        marks it as command-bound and supplies substantially more inliers. This
        proves displacement; the frames observed afterwards must still satisfy
        every ordinary continuity, drift, sharpness, exposure and timing gate
        before ``stable`` can be returned.
        """
        if self._previous is not None or self._window or self._has_motion_transition:
            raise ValueError("Endpoint transition must arm a fresh detector")
        if not isinstance(evidence, dict):
            return self._result("endpoint_transition_unverified")
        displacement = evidence.get("displacement")
        overlap = evidence.get("overlap")
        shift_x = evidence.get("shift_x")
        shift_y = evidence.get("shift_y")
        inliers = evidence.get("inliers")
        candidates = evidence.get("model_candidates")
        try:
            homography = np.asarray(evidence.get("homography"), dtype=np.float64)
        except (TypeError, ValueError):
            homography = np.empty((0, 0), dtype=np.float64)
        distributed = bool(
            isinstance(candidates, list)
            and any(
                isinstance(candidate, dict)
                and type(candidate.get("inliers")) is int
                and candidate["inliers"] >= 24
                and type(candidate.get("occupied_cells")) is int
                and candidate["occupied_cells"] >= 4
                and isinstance(candidate.get("hull_fraction"), (int, float))
                and not isinstance(candidate["hull_fraction"], bool)
                and math.isfinite(candidate["hull_fraction"])
                and candidate["hull_fraction"] >= 0.12
                for candidate in candidates
            )
        )
        localized_command = bool(
            evidence.get("support_scope") == "localized_command_transition"
            and isinstance(candidates, list)
            and any(
                isinstance(candidate, dict)
                and type(candidate.get("inliers")) is int
                and candidate["inliers"] >= 80
                and type(candidate.get("occupied_cells")) is int
                and candidate["occupied_cells"] >= 4
                and isinstance(candidate.get("hull_fraction"), (int, float))
                and not isinstance(candidate["hull_fraction"], bool)
                and math.isfinite(candidate["hull_fraction"])
                and candidate["hull_fraction"] >= 0.06
                for candidate in candidates
            )
        )
        banded_command = bool(
            evidence.get("support_scope") == "banded_command_transition"
            and isinstance(candidates, list)
            and any(
                isinstance(candidate, dict)
                and type(candidate.get("inliers")) is int
                and candidate["inliers"] >= 80
                and type(candidate.get("occupied_cells")) is int
                and candidate["occupied_cells"] >= 3
                and isinstance(candidate.get("hull_fraction"), (int, float))
                and not isinstance(candidate["hull_fraction"], bool)
                and math.isfinite(candidate["hull_fraction"])
                and candidate["hull_fraction"] >= 0.10
                for candidate in candidates
            )
        )
        sparse_distributed_command = bool(
            evidence.get("support_scope")
            == "sparse_distributed_command_transition"
            and isinstance(candidates, list)
            and any(
                isinstance(candidate, dict)
                and type(candidate.get("inliers")) is int
                and candidate["inliers"] >= 20
                and type(candidate.get("occupied_cells")) is int
                and candidate["occupied_cells"] >= 4
                and isinstance(candidate.get("hull_fraction"), (int, float))
                and not isinstance(candidate["hull_fraction"], bool)
                and math.isfinite(candidate["hull_fraction"])
                and candidate["hull_fraction"] >= 0.12
                for candidate in candidates
            )
        )
        consensus = evidence.get("temporal_consensus")
        temporal_command = bool(
            evidence.get("support_scope") == "temporal_command_transition"
            and isinstance(candidates, list)
            and any(
                isinstance(candidate, dict)
                and type(candidate.get("inliers")) is int
                and candidate["inliers"] >= 24
                and type(candidate.get("occupied_cells")) is int
                and candidate["occupied_cells"] >= 2
                and isinstance(candidate.get("hull_fraction"), (int, float))
                and not isinstance(candidate["hull_fraction"], bool)
                and math.isfinite(candidate["hull_fraction"])
                and candidate["hull_fraction"] >= 0.03
                for candidate in candidates
            )
            and isinstance(consensus, dict)
            and consensus.get("axis") in {"pan", "tilt"}
            and type(consensus.get("distinct_frames")) is int
            and consensus["distinct_frames"] >= 5
            and type(consensus.get("sampled_frames")) is int
            and consensus["sampled_frames"] >= 5
            and type(consensus.get("required_models")) is int
            and consensus["required_models"] >= 5
            and type(consensus.get("verified_models")) is int
            and consensus["verified_models"] >= consensus["required_models"]
            and consensus.get("direction_sign") in {-1, 1}
            and type(consensus.get("direction_consistent_models")) is int
            and consensus["direction_consistent_models"]
            >= consensus["required_models"]
            and type(consensus.get("required_magnitude_models")) is int
            and consensus["required_magnitude_models"] >= 4
            and consensus["required_magnitude_models"]
            <= consensus["direction_consistent_models"]
            and type(consensus.get("consistent_models")) is int
            and consensus["consistent_models"]
            >= consensus["required_magnitude_models"]
            and isinstance(
                consensus.get("median_primary_shift_pixels"), (int, float)
            )
            and not isinstance(consensus["median_primary_shift_pixels"], bool)
            and math.isfinite(consensus["median_primary_shift_pixels"])
            and consensus["median_primary_shift_pixels"]
            * consensus["direction_sign"]
            > 0
            and isinstance(
                consensus.get("maximum_primary_deviation_pixels"), (int, float)
            )
            and not isinstance(consensus["maximum_primary_deviation_pixels"], bool)
            and math.isfinite(consensus["maximum_primary_deviation_pixels"])
            and isinstance(
                consensus.get("allowed_primary_deviation_pixels"), (int, float)
            )
            and not isinstance(consensus["allowed_primary_deviation_pixels"], bool)
            and math.isfinite(consensus["allowed_primary_deviation_pixels"])
            and consensus["maximum_primary_deviation_pixels"]
            <= consensus["allowed_primary_deviation_pixels"]
        )
        numeric = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in (displacement, overlap, shift_x, shift_y)
        )
        accepted = bool(
            evidence.get("verified") is True
            and numeric
            and type(inliers) is int
            and inliers >= (
                80
                if banded_command or localized_command
                else 20
                if sparse_distributed_command
                else 24
            )
            and 0.25 <= overlap <= 1.0
            and displacement >= self.settings.motion_transition_pixels
            and (
                distributed
                or localized_command
                or banded_command
                or sparse_distributed_command
                or temporal_command
            )
            and homography.shape == (3, 3)
            and np.isfinite(homography).all()
        )
        metrics = {
            "motion_pixels": float(displacement) if numeric else None,
            "overlap": float(overlap) if numeric else None,
            "inliers": inliers if type(inliers) is int else None,
            "support_scope": evidence.get("support_scope"),
        }
        if not accepted:
            return self._result("endpoint_transition_unverified", **metrics)
        self._has_motion_transition = True
        # This exception is deliberately command-bound. The caller has already
        # proven that the accepted PTZ command changed the view with an
        # independent geometric match. Ordered decoder frames may therefore
        # extend the endpoint window even when a motionless, low-entropy scene
        # encodes to identical pixels. Ordinary observation and initial
        # reference acquisition continue to reject repeated images.
        self._allow_repeated_endpoint_samples = True
        self._recovery_diagnostic = {
            "recovery_code": "verified_endpoint_transition",
            "recovery_metrics": metrics,
        }
        return self._result("motion_reacquired", **metrics)

    @staticmethod
    def _numeric_pose(pose: dict[str, Any] | None) -> dict[str, float]:
        return {
            key: float(value)
            for key in ("pan", "tilt", "zoom")
            if isinstance((value := (pose or {}).get(key)), (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        }

    def _result(self, code: str, **metrics: Any) -> dict[str, Any]:
        return {
            "stable": code == "stable",
            "state": "stable"
            if code == "stable"
            else ("timeout" if code == "timeout" else "observing"),
            "code": code,
            "motion_pixels": None,
            "speed_px_s": None,
            "drift_pixels": None,
            "confidence": 0.0,
            "sharpness": None,
            "has_motion_transition": self._has_motion_transition,
            "evidence": "insufficient",
            "timing_basis": None,
            "best_sequence": None,
            **self._recovery_diagnostic,
            **metrics,
        }

    def _invalidate(self, *, forget_transition: bool = False) -> None:
        self._window.clear()
        if forget_transition:
            self._has_motion_transition = False
            self._allow_repeated_endpoint_samples = False
            self._transition_anchor = None
            self._transition_tracking_lost = False
            self._last_transition_search = None
            self._recovery_diagnostic = {}
            self._last_timeline_time = None

    def _gray(self, image: np.ndarray) -> np.ndarray:
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
            raise ValueError("Frame must be an unsigned 8-bit image")
        if image.ndim not in (2, 3) or min(image.shape[:2]) < 32:
            raise ValueError("Frame must have at least 32 pixels on each axis")
        if image.size > 150_000_000:
            raise ValueError("Frame exceeds the supported pixel limit")
        if image.ndim == 3:
            if image.shape[2] not in (3, 4):
                raise ValueError("Expected gray, BGR or BGRA image")
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        scale = self.settings.analysis_width / image.shape[1]
        # Keep metric units comparable across sources, including low resolution.
        height = max(32, round(image.shape[0] * scale))
        if height > self.settings.analysis_width * 4:
            raise ValueError("Unsupported frame aspect ratio")
        return cv2.resize(image, (self.settings.analysis_width, height))

    def _mask(self, gray: np.ndarray) -> np.ndarray:
        height, width = gray.shape
        mask = np.full(gray.shape, 255, np.uint8)
        for left, top, right, bottom in self.overlay_regions:
            mask[
                int(top * height) : math.ceil(bottom * height),
                int(left * width) : math.ceil(right * width),
            ] = 0
        if self._learned_overlay is not None:
            mask[self._learned_overlay > 0] = 0
        return mask

    def _corners(self, gray: np.ndarray) -> np.ndarray | None:
        height, width = gray.shape
        mask = self._mask(gray)
        corners = []
        for row in range(3):
            for column in range(4):
                left, right = column * width // 4, (column + 1) * width // 4
                top, bottom = row * height // 3, (row + 1) * height // 3
                points = cv2.goodFeaturesToTrack(
                    gray[top:bottom, left:right],
                    maxCorners=55,
                    qualityLevel=0.015,
                    minDistance=7,
                    mask=mask[top:bottom, left:right],
                    blockSize=5,
                )
                if points is not None:
                    points += np.array([left, top], np.float32)
                    corners.append(points)
        return np.concatenate(corners) if corners else None

    @staticmethod
    def _grid(shape: tuple[int, int]) -> np.ndarray:
        height, width = shape
        return np.asarray(
            [
                (width * x, height * y)
                for y in (0.15, 0.5, 0.85)
                for x in (0.125, 0.375, 0.625, 0.875)
            ],
            np.float64,
        )

    @staticmethod
    def _displacement(transform: np.ndarray, points: np.ndarray) -> np.ndarray:
        return np.linalg.norm(points @ transform[:2, :2].T + transform[:2, 2] - points, axis=1)

    def _motion(
        self, previous: np.ndarray, current: np.ndarray
    ) -> tuple[np.ndarray | None, dict[str, Any]]:
        points = self._corners(previous)
        if points is None or len(points) < self.settings.minimum_tracks:
            return None, {"tracks": 0 if points is None else len(points)}
        target, valid = track_image_points(
            previous, current, points, self.settings.maximum_forward_backward_error_pixels)
        source = points.reshape(-1, 2)
        source, target = source[valid], target[valid]
        transform, metrics = self._fit_motion(source, target, previous.shape)
        if transform is None and not self._has_motion_transition:
            transition = self._distributed_transition(source, target, previous.shape)
            if transition:
                metrics["distributed_transition"] = transition
        return transform, metrics

    def _distributed_transition(
        self, source: np.ndarray, target: np.ndarray, shape: tuple[int, int]
    ) -> dict[str, Any] | None:
        """Qualify displacement through distorted optics, never a stable window.

        A nearby scene through a wide lens need not obey one image homography.
        Independent local fits must agree on a distributed direction. All
        support fractions retain the original correspondence denominator.
        """
        if len(source) < self.settings.minimum_tracks:
            return None
        height, width = shape
        cells = np.minimum((source / [width, height] * [4, 3]).astype(int), [3, 2])
        supported = np.zeros(len(source), bool)
        vectors = []
        for row in range(3):
            for column in range(4):
                selected = np.flatnonzero((cells == [column, row]).all(axis=1))
                if len(selected) < 12:
                    continue
                affine, inliers = cv2.estimateAffine2D(
                    source[selected], target[selected], method=cv2.RANSAC,
                    ransacReprojThreshold=self.settings.maximum_model_error_pixels,
                    maxIters=2000, confidence=0.995,
                )
                if affine is None or inliers is None or not np.isfinite(affine).all():
                    continue
                scales = np.linalg.svd(affine[:, :2], compute_uv=False)
                if scales.min() < 0.95 or scales.max() > 1.05 or np.linalg.det(affine[:, :2]) <= 0:
                    continue
                chosen = selected[inliers.ravel().astype(bool)]
                if len(chosen) < 8 or len(chosen) / len(selected) < self.settings.minimum_inlier_fraction:
                    continue
                vector = np.median(target[chosen] - source[chosen], axis=0)
                if np.linalg.norm(vector) < self.settings.motion_transition_pixels:
                    continue
                supported[chosen] = True
                vectors.append(vector)
        points = source[supported]
        occupied = set(map(tuple, cells[supported]))
        if (
            len(points) < self.settings.minimum_tracks
            or supported.mean() < self.settings.minimum_inlier_fraction
            or len(occupied) < self.settings.minimum_occupied_cells
            or len({column for column, _ in occupied}) < 3
            or len({row for _, row in occupied}) < 2
            or cv2.contourArea(cv2.convexHull(points)) / (height * width) < 0.2
        ):
            return None
        vectors = np.asarray(vectors)
        direction = np.median(vectors, axis=0)
        length = float(np.linalg.norm(direction))
        if length < self.settings.motion_transition_pixels or np.min(
            vectors @ direction / (np.linalg.norm(vectors, axis=1) * length)
        ) < 0.8:
            return None
        return {
            "inliers": len(points), "inlier_fraction": float(supported.mean()),
            "occupied_cells": len(occupied), "motion_pixels": length,
        }

    def _feature_motion(
        self, previous: np.ndarray, current: np.ndarray
    ) -> tuple[np.ndarray | None, dict[str, Any]]:
        """Recover a fast transition that exceeded the local flow search window."""
        detector = cv2.SIFT_create(nfeatures=2400, contrastThreshold=0.025)
        first_keys, first = detector.detectAndCompute(previous, self._mask(previous))
        second_keys, second = detector.detectAndCompute(current, self._mask(current))
        if first is None or second is None:
            return None, {}
        matcher = cv2.BFMatcher()
        forward = matcher.knnMatch(first, second, k=2)
        backward = matcher.knnMatch(second, first, k=2)
        reverse = {
            pair[0].queryIdx: pair[0].trainIdx
            for pair in backward
            if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7
        }
        pairs = [
            pair[0] for pair in forward
            if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7
            and reverse.get(pair[0].trainIdx) == pair[0].queryIdx
        ]
        source = np.float32([first_keys[pair.queryIdx].pt for pair in pairs]).reshape(-1, 2)
        target = np.float32([second_keys[pair.trainIdx].pt for pair in pairs]).reshape(-1, 2)
        return self._fit_motion(source, target, previous.shape, projective=True)

    def _fit_motion(
        self, source: np.ndarray, target: np.ndarray, shape: tuple[int, int], *, projective: bool = False
    ) -> tuple[np.ndarray | None, dict[str, Any]]:
        remaining = np.arange(len(source))
        metrics = {"tracks": len(source)}
        for candidate in range(3 if projective else 1):
            if len(remaining) < self.settings.minimum_tracks:
                return None, metrics
            if projective:
                # A finite pan/tilt rotation changes perspective. The small-motion
                # similarity model is reserved for the ordinary settling window.
                affine, inliers = cv2.findHomography(
                    source[remaining], target[remaining], cv2.RANSAC,
                    self.settings.maximum_model_error_pixels, maxIters=2000, confidence=0.995,
                )
            else:
                affine, inliers = cv2.estimateAffinePartial2D(
                    source, target, method=cv2.RANSAC,
                    ransacReprojThreshold=self.settings.maximum_model_error_pixels,
                    maxIters=2000, confidence=0.995, refineIters=10,
                )
            if affine is None or inliers is None or not np.isfinite(affine).all():
                return None, metrics
            fitted_inliers = inliers.ravel().astype(bool)
            if projective and (
                np.count_nonzero(fitted_inliers) < 4
                or abs(float(affine[2, 2])) < 1e-12
                or abs(float(np.linalg.det(affine))) < 1e-12
            ):
                return None, metrics
            if candidate:
                # Every alternative must satisfy the original inlier fraction;
                # discarding a local consensus must never shrink its denominator.
                projected = cv2.perspectiveTransform(source[None], affine)[0]
                inlier_mask = np.isfinite(projected).all(axis=1) & (
                    np.linalg.norm(projected - target, axis=1)
                    <= self.settings.maximum_model_error_pixels
                )
            else:
                inlier_mask = fitted_inliers
            transform, metrics = self._evaluate_motion(
                source, target, shape, affine, inlier_mask, projective=projective
            )
            if transform is not None or not projective or not metrics.get("spatial_support_rejected"):
                return transform, metrics
            # Only a geometrically identified, spatially rejected consensus may
            # be removed. Rejected candidates never reach overlay learning.
            remaining = remaining[~fitted_inliers]
        return None, metrics

    def _evaluate_motion(
        self, source: np.ndarray, target: np.ndarray, shape: tuple[int, int],
        affine: np.ndarray, inlier_mask: np.ndarray, *, projective: bool,
    ) -> tuple[np.ndarray | None, dict[str, Any]]:
        height, width = shape
        supported = source[inlier_mask]
        occupied = {(min(3, int(x * 4 / width)), min(2, int(y * 3 / height))) for x, y in supported}
        fraction = float(inlier_mask.mean())
        hull_fraction = (
            float(cv2.contourArea(cv2.convexHull(supported))) / (width * height)
            if len(supported) >= 3
            else 0.0
        )
        metrics = {
            "tracks": len(source),
            "inliers": len(supported),
            "occupied_cells": len(occupied),
            "inlier_fraction": fraction,
            "confidence": min(1.0, fraction * len(occupied) / 9),
        }
        spatially_rejected = (
            len(occupied) < self.settings.minimum_occupied_cells
            or len({x for x, _ in occupied}) < 3
            or len({y for _, y in occupied}) < 2
            or hull_fraction < 0.2
        )
        if (
            len(supported) < self.settings.minimum_tracks
            or fraction < self.settings.minimum_inlier_fraction
            or spatially_rejected
        ):
            return None, {**metrics, "spatial_support_rejected": spatially_rejected}
        transform = np.eye(3)
        if projective:
            transform = affine / affine[2, 2]
        else:
            transform[:2] = affine
        # Excessive scale is focus/optical change, not a stationary view.
        scale = math.hypot(float(affine[0, 0]), float(affine[1, 0]))
        if projective:
            grid = self._grid(shape)
            denominator = grid @ transform[2, :2] + transform[2, 2]
            if not np.isfinite(denominator).all() or np.any(denominator <= 0.1):
                return None, metrics
            determinant = float(np.linalg.det(transform))
            if determinant <= 0:
                return None, metrics
            # Local area scale includes perspective; a pure zoom still fails
            # the same five-percent optical-change guard.
            scale = float(np.median(np.sqrt(determinant / denominator**3)))
        if not 0.95 <= scale <= 1.05:
            return None, {**metrics, "optical_change": True}
        movement = float(np.percentile(
            np.linalg.norm(cv2.perspectiveTransform(self._grid(shape)[None], transform)[0] - self._grid(shape), axis=1)
            if projective else self._displacement(transform, self._grid(shape)), 95,
        ))
        if movement >= 0.75:
            if self._overlay_votes is None:
                self._overlay_votes = np.zeros(shape, np.uint8)
            fixed = source[np.linalg.norm(target - source, axis=1) < 0.12]
            votes = np.zeros(shape, np.uint8)
            for x, y in fixed:
                cv2.circle(votes, (round(float(x)), round(float(y))), 5, 1, -1)
            self._overlay_votes = np.minimum(
                self._overlay_votes.astype(np.uint16) + votes, 255
            ).astype(np.uint8)
            self._learned_overlay = (self._overlay_votes >= 2).astype(np.uint8)
        return transform, {**metrics, "motion_pixels": movement}

    def observe(
        self,
        image: np.ndarray,
        *,
        media_time: float | None,
        received_monotonic: float,
        sequence: int,
        generation: int,
        pose: dict[str, Any] | None = None,
        physical_timestamp_verified: bool = False,
    ) -> dict[str, Any]:
        """Process one decoder frame; timeout and invalid evidence never approve."""
        if not math.isfinite(received_monotonic):
            raise ValueError("Receive time must be finite")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (sequence, generation)
        ):
            raise ValueError("Frame sequence and decoder generation must be integers")
        if not isinstance(physical_timestamp_verified, bool):
            raise ValueError("Physical timestamp verification must be explicit boolean evidence")
        if self._started is None:
            self._started = received_monotonic
            self._stop_received = received_monotonic
        if self._timed_out or received_monotonic - self._started >= self.settings.timeout_seconds:
            self._timed_out = True
            self._invalidate()
            return self._result("timeout")
        generation_changed = (
            self._last_generation is not None and generation != self._last_generation
        )
        if self._last_received is not None and received_monotonic <= self._last_received:
            self._invalidate(forget_transition=True)
            self._previous = None
            return self._result("receive_time_not_ordered")
        if (
            not generation_changed
            and self._last_sequence is not None
            and sequence <= self._last_sequence
        ):
            self._invalidate(forget_transition=True)
            self._previous = None
            return self._result("sequence_not_ordered")
        self._last_received, self._last_sequence = received_monotonic, sequence
        self._last_generation = generation
        if generation_changed:
            self._invalidate(forget_transition=True)
            self._previous = None
            self._learned_overlay = self._overlay_votes = None
        if media_time is not None and not math.isfinite(media_time):
            self._invalidate(forget_transition=True)
            self._previous = None
            return self._result("media_time_invalid")
        if media_time is None and not self.allow_observation_timing:
            self._invalidate(forget_transition=True)
            self._previous = None
            return self._result("media_time_unavailable")
        timing_basis = "media" if media_time is not None else "local_observation"
        timeline_time = media_time if media_time is not None else received_monotonic
        gray = self._gray(image)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        current = _Frame(
            gray,
            timeline_time,
            timing_basis,
            received_monotonic,
            sequence,
            generation,
            self._numeric_pose(pose),
            sharpness,
            float(np.mean(gray)),
            bool(physical_timestamp_verified) and timing_basis == "media",
        )
        previous = self._previous
        self._previous = current
        last_timeline_time = self._last_timeline_time
        self._last_timeline_time = timeline_time
        base = {
            "sharpness": sharpness,
            "sequence": sequence,
            "generation": generation,
            "timing_basis": timing_basis,
        }
        if previous is None:
            self._transition_anchor = current
            return self._result("decoder_changed" if generation_changed else "first_frame", **base)
        if timing_basis != previous.timing_basis:
            self._invalidate(forget_transition=True)
            return self._result("timing_basis_changed", **base)
        repeated = np.array_equal(gray, previous.image)
        if repeated and not self._allow_repeated_endpoint_samples:
            # A duplicate contributes no temporal support. Keep the last distinct
            # frame so a decoder freeze cannot advance the continuity fence.
            self._previous = previous
        interval = timeline_time - previous.timeline_time
        if (
            interval <= 0 or interval > self.settings.maximum_frame_interval_seconds
            or (last_timeline_time is not None and timeline_time <= last_timeline_time)
        ):
            self._invalidate(forget_transition=True)
            return self._result(
                "media_time_discontinuity" if timing_basis == "media" else "observation_gap",
                **base,
            )
        if received_monotonic - previous.received_monotonic > self.settings.maximum_frame_interval_seconds:
            self._invalidate(forget_transition=True)
            return self._result("observation_gap", **base)
        if gray.shape != previous.image.shape:
            self._invalidate(forget_transition=True)
            self._learned_overlay = self._overlay_votes = None
            return self._result("dimensions_changed", **base)
        if repeated and not self._allow_repeated_endpoint_samples:
            return self._result("repeated_image", **base)
        if repeated:
            transform = np.eye(3)
            motion = {
                "motion_pixels": 0.0,
                "endpoint_transport_frame": True,
            }
        else:
            transform, motion = self._motion(previous.image, gray)
        metrics = {**base, **motion}
        distributed = motion.get("distributed_transition")
        if distributed and (
            timing_basis != "media"
            or distributed["motion_pixels"] / interval >= self.settings.motion_transition_speed_pixels_per_second
        ):
            self._has_motion_transition = True
            self._invalidate()
            # No local transform enters the stable-window drift calculation.
            return self._result("motion_reacquired", **metrics)
        if transform is None:
            self._transition_tracking_lost = True
        if (
            not self._has_motion_transition
            and (
                self._transition_tracking_lost
                or motion.get("motion_pixels", 0) < self.settings.motion_transition_pixels
            )
            and self._transition_anchor is not None
            and (self._last_transition_search is None or timeline_time - self._last_transition_search >= 0.5)
        ):
            self._last_transition_search = timeline_time
            # Sampling can divide a real displacement into increments below
            # the transition gate. Measure from the same continuity-fenced
            # anchor with the ordinary bidirectional flow and spatial checks.
            # Feature homography remains reserved for lost tracking: its
            # subpixel variation on repetitive stationary scenes is not an
            # interchangeable measurement of this small accumulated motion.
            recovered, recovered_metrics = self._motion(self._transition_anchor.image, gray)
            recovery_method = "anchor_optical_flow"
            distributed = recovered_metrics.get("distributed_transition")
            if recovered is None and not distributed and self._transition_tracking_lost:
                recovered, recovered_metrics = self._feature_motion(self._transition_anchor.image, gray)
                recovery_method = "feature_recovery"
                if recovered is None and self.transition_estimator is not None:
                    calibrated = self.transition_estimator(self._transition_anchor.image, gray)
                    if calibrated is not None:
                        self._has_motion_transition = True
                        self._invalidate()
                        self._recovery_diagnostic = {
                            "recovery_code": "calibrated_transition",
                            "recovery_metrics": {"anchor_sequence": self._transition_anchor.sequence, **calibrated},
                        }
                        return self._result("motion_reacquired", **base, **calibrated)
            if distributed:
                # The same distributed gate used between adjacent frames also
                # applies to accumulated displacement from the fixed anchor.
                # It supplies no transform and cannot qualify a stable window.
                self._has_motion_transition = True
                self._invalidate()
                self._recovery_diagnostic = {
                    "recovery_code": "distributed_anchor_transition",
                    "recovery_metrics": {"method": recovery_method,
                        "anchor_sequence": self._transition_anchor.sequence, **distributed},
                }
                return self._result("motion_reacquired", **base, **distributed)
            self._recovery_diagnostic = {
                "recovery_code": (
                    "optical_change" if recovered_metrics.get("optical_change") else
                    "spatial_support_rejected" if recovered_metrics.get("spatial_support_rejected") else
                    "model_unverified" if recovered is None else "motion_below_transition"
                ),
                "recovery_metrics": {
                    "method": recovery_method,
                    "anchor_sequence": self._transition_anchor.sequence,
                    **{
                        key: recovered_metrics[key]
                        for key in ("tracks", "inliers", "occupied_cells", "inlier_fraction", "confidence")
                        if key in recovered_metrics
                    },
                },
            }
            if recovered is not None and recovered_metrics["motion_pixels"] >= self.settings.motion_transition_pixels:
                self._recovery_diagnostic["recovery_code"] = "recovered"
                self._has_motion_transition = True
                self._invalidate()
                # This proves displacement, not its speed or current stability.
                # A fresh ordinary flow window must still pass every stop gate.
                return self._result("motion_reacquired", **base, **recovered_metrics)
        if transform is None:
            self._invalidate()
            return self._result("insufficient_visual_support", **metrics)
        speed = motion["motion_pixels"] / interval if timing_basis == "media" else None
        metrics["speed_px_s"] = speed
        if motion["motion_pixels"] >= self.settings.motion_transition_pixels and (
            speed is None or speed >= self.settings.motion_transition_speed_pixels_per_second
        ):
            self._has_motion_transition = True
        moving = (
            speed > self.settings.maximum_speed_pixels_per_second
            if speed is not None
            else motion["motion_pixels"] > self.settings.maximum_observed_displacement_pixels
        )
        if moving:
            self._invalidate()
            return self._result("moving", **metrics)
        if received_monotonic < (self._stop_received or self._started):
            self._invalidate()
            return self._result("before_stop", **metrics)
        causal = self._has_motion_transition or (
            current.physical_timestamp_verified and not self._required_transition
        )
        if not causal:
            self._invalidate()
            return self._result("motion_transition_unobserved", **metrics)
        # The window stores small scalar records, not a frame stack.
        scalar_frame = _Frame(
            np.empty((0, 0), np.uint8),
            timeline_time,
            timing_basis,
            received_monotonic,
            sequence,
            generation,
            current.pose,
            sharpness,
            current.exposure,
            current.physical_timestamp_verified,
        )
        cumulative = transform @ self._window[-1].transform if self._window else np.eye(3)
        self._window.append(_Sample(scalar_frame, cumulative))
        # Keep both requirements satisfiable at low or irregular frame rates:
        # the minimum duration and the minimum number of distinct observations.
        # A time-only trim can retain three frames forever while requiring five.
        while len(self._window) > max(2, self.settings.minimum_window_frames) and (
            timeline_time - self._window[1].frame.timeline_time
            >= self.settings.stable_window_seconds - 1e-9
        ):
            self._window.popleft()
        origin_inverse = np.linalg.inv(self._window[0].transform)
        grid = self._grid(gray.shape)
        drift = max(
            float(np.max(self._displacement(sample.transform @ origin_inverse, grid)))
            for sample in self._window
        )
        metrics["drift_pixels"] = drift
        first = self._window[0].frame
        metrics.update(
            window_frames=len(self._window),
            window_media_seconds=(
                timeline_time - first.timeline_time if timing_basis == "media" else None
            ),
            window_observation_seconds=received_monotonic - first.received_monotonic,
        )
        if drift > self.settings.maximum_drift_pixels:
            return self._result("drifting", **metrics)
        for axis in ("pan", "tilt", "zoom"):
            positions = [
                sample.frame.pose[axis] for sample in self._window if axis in sample.frame.pose
            ]
            if positions and max(positions) - min(positions) > self.settings.pose_tolerance:
                return self._result("pose_changing", **metrics)
        sharpness_values = [sample.frame.sharpness for sample in self._window]
        maximum_sharpness = max(sharpness_values)
        exposures = [sample.frame.exposure for sample in self._window]
        if (
            maximum_sharpness <= 0
            or min(sharpness_values) < maximum_sharpness * self.settings.minimum_sharpness_fraction
            or max(exposures) - min(exposures) > max(8.0, current.exposure * 0.15)
        ):
            return self._result("image_quality_settling", **metrics)
        if (
            len(self._window) < self.settings.minimum_window_frames
            or timeline_time - first.timeline_time < self.settings.stable_window_seconds - 1e-9
            or received_monotonic - first.received_monotonic
            < self.settings.stable_window_seconds - 1e-9
            or received_monotonic - (self._stop_received or self._started)
            < self.settings.minimum_observation_seconds - 1e-9
        ):
            return self._result("settling", **metrics)
        physical = all(sample.frame.physical_timestamp_verified for sample in self._window)
        if not self._has_motion_transition and not physical:
            return self._result("physical_time_unverified", **metrics)
        qualified = sorted(self._window, key=lambda sample: sample.frame.sharpness, reverse=True)
        return self._result(
            "stable",
            **metrics,
            evidence="physical_timestamp_verified" if physical else "visual_transition_verified",
            best_sequence=qualified[0].frame.sequence,
            qualified_frames=[
                {"sequence": sample.frame.sequence, "generation": sample.frame.generation}
                for sample in qualified
            ],
        )
