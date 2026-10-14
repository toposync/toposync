"""Frame-bound matching of a single immutable source photograph."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from copy import deepcopy

import cv2
import numpy as np

from toposync.runtime.pipelines.image_geometry import geometry_matrix, source_pixels

from .panorama_localization import (
    ANALYSIS_WIDTH, MINIMUM_MATCHES, _matching_descriptors, _mutual_matches,
    _support, correspondence_groups, frame_identity,
)


def fit_frame_homography(current, reference, size):
    """Hold-out points never participate in fitting or choosing the model."""
    width, height = size
    indices, holdout = correspondence_groups(current, reference, width)
    current, reference = current[indices], reference[indices]
    if len(current) < MINIMUM_MATCHES or holdout.sum() < 12 or (~holdout).sum() < 40:
        return None
    factor = min(1.0, ANALYSIS_WIDTH / width)
    transform, inliers = cv2.findHomography(current[~holdout], reference[~holdout], cv2.RANSAC, 3 / factor)
    if transform is None or inliers is None or not np.isfinite(transform).all():
        return None
    inliers = inliers.ravel().astype(bool)
    if inliers.sum() < 40 or inliers.mean() < .65 or abs(np.linalg.det(transform)) < 1e-10:
        return None
    if not _support(current[~holdout][inliers], width, height):
        return None
    try:
        projected = source_pixels(current[holdout], transform)
    except ValueError:
        return None
    residual = np.linalg.norm(projected - reference[holdout], axis=1)
    inverse_residual = np.linalg.norm(source_pixels(reference[holdout], np.linalg.inv(transform)) - current[holdout], axis=1)
    residual = np.maximum(residual, inverse_residual)
    error = float(np.percentile(residual, 95))
    # Single-image regions need a tighter check than global panorama orientation.
    if error * factor > 3 or not _support(current[holdout][residual * factor <= 3], width, height):
        return None
    corners = np.float32([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]])
    mapped = cv2.perspectiveTransform(corners[None], transform)[0]
    area = cv2.contourArea(mapped, oriented=True)
    if not cv2.isContourConvex(mapped) or not .2 < area / (width * height) < 5:
        return None
    return {"status": "localized", "homography": transform.tolist(), "reference_size": list(size),
            "validation_p95_pixels": error * factor, "analysis_width": min(width, ANALYSIS_WIDTH),
            "validation_matches": int(holdout.sum()),
            "support_pixels": current[holdout][residual * factor <= 3].tolist()}


class PlanarLocalizer:
    def __init__(self, reference, source_size):
        if reference is None or reference.dtype != np.uint8 or list(reference.shape[1::-1]) != list(source_size):
            raise ValueError("visual_region_reference_geometry_invalid")
        self.size = tuple(source_size)
        self._lock = threading.Lock()
        self.results = OrderedDict()
        self.sift = cv2.SIFT_create(nfeatures=4000, contrastThreshold=.025)
        self.reference_points, self.reference_descriptors = self._features(reference)

    def _features(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        scale = min(1.0, ANALYSIS_WIDTH / image.shape[1])
        if scale < 1:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        keys, descriptors = self.sift.detectAndCompute(gray, None)
        return (np.float32([key.pt for key in keys]).reshape(-1, 2) / scale,
                _matching_descriptors(descriptors) if descriptors is not None else None)

    def locate(self, image, evidence, image_geometry=None):
        with self._lock:
            return self._locate(image, evidence, image_geometry)

    def _locate(self, image, evidence, image_geometry):
        unknown = {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        if frame_identity(evidence) is None:
            return {**unknown, "reason": "panorama_frame_identity_missing"}
        try:
            if image.dtype != np.uint8 or image.ndim not in (2, 3):
                raise ValueError("Invalid pixels")
            matrix = geometry_matrix(image_geometry, image.shape[1], image.shape[0])
            if (image_geometry.get("capture_evidence") != evidence
                    or image_geometry["source_size"] != list(self.size)):
                raise ValueError("Invalid provenance")
        except (AttributeError, KeyError, TypeError, ValueError):
            return {**unknown, "reason": "panorama_source_geometry_changed"}
        key = (frame_identity(evidence), hashlib.blake2b(image.tobytes(), digest_size=16).digest(), matrix.tobytes())
        if key in self.results:
            self.results.move_to_end(key)
            return deepcopy(self.results[key])
        current, descriptors = self._features(image)
        result = unknown
        if descriptors is not None and self.reference_descriptors is not None:
            matches = _mutual_matches(cv2.BFMatcher(), descriptors, self.reference_descriptors)
            if len(matches) >= MINIMUM_MATCHES:
                pixels = source_pixels(current[[m.queryIdx for m in matches]], matrix)
                reference = self.reference_points[[m.trainIdx for m in matches]]
                result = fit_frame_homography(pixels, reference, self.size) or unknown
        result = {**result, "capture_evidence": dict(evidence)}
        self.results[key] = deepcopy(result)
        while len(self.results) > 4:
            self.results.popitem(last=False)
        return result
