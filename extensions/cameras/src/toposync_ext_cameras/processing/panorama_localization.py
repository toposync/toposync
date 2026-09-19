"""Frame-bound visual orientation against immutable original panorama photographs.

No actuator position, blended panorama or last known pose is an observation.
Matching is bounded; a missing or ambiguous match stays unlocalized. References
use the presentation rotation already stored by reconstruction, exactly once.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .panorama_mapping import _lens_arrays, _rotation_basis, ray_to_image_pixel
from .panorama_reconstruction import _local_rays, _project, align_unit_rays

ANALYSIS_WIDTH = 960
MAXIMUM_REFERENCES = 256
MAXIMUM_CANDIDATES = 8
MINIMUM_MATCHES = 60
MAXIMUM_ERROR_PIXELS = 8.0


def frame_identity(evidence: Any) -> tuple[str, int, int] | None:
    if not isinstance(evidence, dict):
        return None
    instance = evidence.get("capture_instance")
    generation, sequence = evidence.get("generation"), evidence.get("sequence")
    if (
        not isinstance(instance, str)
        or not instance
        or len(instance) > 128
        or type(generation) is not int
        or generation < 0
        or type(sequence) is not int
        or sequence <= 0
    ):
        return None
    return instance, generation, sequence


def _gray(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    scale = min(1.0, ANALYSIS_WIDTH / gray.shape[1])
    return cv2.resize(
        gray,
        (round(gray.shape[1] * scale), round(gray.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )


def _support(pixels: np.ndarray, width: int, height: int) -> bool:
    if len(pixels) < 12:
        return False
    cells = set(map(tuple, np.minimum((pixels / [width, height] * [4, 3]).astype(int), [3, 2])))
    return (
        len(cells) >= 6
        and len({x for x, _ in cells}) >= 3
        and len({y for _, y in cells}) >= 2
        and cv2.contourArea(cv2.convexHull(pixels.astype(np.float32))) / (width * height) >= 0.2
    )


def fit_frame_rotation(
    current: np.ndarray, reference: np.ndarray, lens: dict[str, Any], reference_rotation: Any
) -> dict[str, Any] | None:
    """Robust rotation with a deterministic held-out correspondence partition.

    Pixel coordinates are original optical pixels, independent of feature scale.
    Held-out observations never fit or select a RANSAC model.
    """
    if len(current) != len(reference) or len(current) < MINIMUM_MATCHES:
        return None
    matrix, distortion, width, height = _lens_arrays(lens)
    distortion = np.zeros(5) if distortion is None else distortion
    rotation = np.asarray(reference_rotation, dtype=float)
    _rotation_basis(rotation)
    source = _local_rays(current, matrix, distortion)
    destination = _local_rays(reference, matrix, distortion) @ rotation.T
    holdout = np.arange(len(source)) % 5 == 0
    training = np.flatnonzero(~holdout)
    factor = min(1.0, ANALYSIS_WIDTH / width)

    def errors(candidate: np.ndarray) -> np.ndarray:
        local = destination @ candidate
        result = np.linalg.norm(_project(local, matrix, distortion) - current, axis=1) * factor
        result[local[:, 2] <= 0.05] = np.inf
        return result

    generator = np.random.default_rng(7261)
    best = None
    best_count = 0
    for _ in range(128):
        indices = generator.choice(training, 3, replace=False)
        if np.linalg.svd(source[indices], compute_uv=False)[1] < 0.01:
            continue
        candidate = align_unit_rays(source[indices], destination[indices])
        inliers = (errors(candidate) <= 3.0) & ~holdout
        count = int(inliers.sum())
        if count > best_count:
            best, best_count = inliers, count
    if best is None or best_count < 40 or best_count / len(training) < 0.55:
        return None
    fitted = align_unit_rays(source[best], destination[best])
    residual = errors(fitted)
    validation = holdout & (residual <= MAXIMUM_ERROR_PIXELS)
    if (
        validation.sum() < 12
        or validation.sum() / holdout.sum() < 0.8
        or not _support(current[best], width, height)
        or not _support(reference[best], width, height)
        or not _support(current[validation], width, height)
    ):
        return None
    # The full held-out distribution counts bad matches; it cannot hide a tail.
    percentile = float(np.percentile(residual[holdout], 95))
    if not np.isfinite(percentile) or percentile > MAXIMUM_ERROR_PIXELS:
        return None
    return {
        "rotation_matrix": fitted.tolist(),
        "inliers": best_count,
        "inlier_fraction": best_count / len(training),
        "validation_matches": int(holdout.sum()),
        "validation_p95_pixels": percentile,
        "analysis_width": min(width, ANALYSIS_WIDTH),
    }


class PanoramaLocalizer:
    """Bounded per-artifact descriptors and per-frame results, serialized by caller."""

    def __init__(self, model: dict[str, Any], photographs: list[dict[str, Any]]):
        self._lock = threading.RLock()
        self.model = model
        self.lens = dict(model["lens"])
        _lens_arrays(self.lens)
        self.references = []
        self.last_reference: str | None = None
        self.results: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        indexed = {photo["id"]: photo for photo in photographs}
        captures = model["captures"]
        if not 1 <= len(captures) <= MAXIMUM_REFERENCES:
            raise ValueError("Invalid number of visual references")
        for capture in captures:
            photo = indexed[capture["id"]]
            path = Path(photo["path"])
            if photo.get("sha256"):
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != photo["sha256"]:
                        raise ValueError("Reference photograph changed")
            image = cv2.imread(str(path))
            if image is None or image.shape[:2] != (self.lens["height"], self.lens["width"]):
                raise ValueError("Reference photograph does not match the optical model")
            gray = _gray(image)
            points, descriptors, coarse = self._features(gray)
            _rotation_basis(capture["rotation_matrix"])
            self.references.append(
                {
                    "id": capture["id"],
                    "rotation_matrix": capture["rotation_matrix"],
                    "path": str(path),
                    "pose": photo.get("pose", {}),
                    "points": points / (gray.shape[1] / image.shape[1]),
                    "descriptors": descriptors,
                    "coarse": coarse,
                }
            )

    def target_reference(self, ray: Any) -> dict[str, Any] | None:
        choices = []
        for reference in self.references:
            pixel = ray_to_image_pixel(ray, self.lens, rotation_matrix=reference["rotation_matrix"])
            if pixel is not None:
                margin = min(
                    pixel[0],
                    pixel[1],
                    self.lens["width"] - 1 - pixel[0],
                    self.lens["height"] - 1 - pixel[1],
                )
                choices.append((margin, reference))
        return max(choices, key=lambda choice: choice[0])[1] if choices else None

    def measure_target(
        self, image: np.ndarray, located: dict[str, Any], ray: Any
    ) -> dict[str, Any] | None:
        """Measure a textured target in pixels, independently of its pose prediction.

        Rotation predicts a search neighborhood. Photographic correlation must
        identify a distinct peak there before any arrival can be confirmed.
        """
        reference = self.target_reference(ray)
        predicted = ray_to_image_pixel(ray, self.lens, rotation_matrix=located["rotation_matrix"])
        if reference is None or predicted is None:
            return None
        current = _gray(image)
        stored_image = cv2.imread(reference["path"], cv2.IMREAD_GRAYSCALE)
        if stored_image is None:
            return None
        stored = _gray(stored_image)
        scale = current.shape[1] / self.lens["width"]
        center = np.rint(np.asarray(predicted) * scale).astype(int)
        radius, search = 32, 20
        extent = radius + search
        if (
            min(center) < extent
            or center[0] + extent >= current.shape[1]
            or center[1] + extent >= current.shape[0]
        ):
            return None
        horizontal, vertical = np.meshgrid(
            np.arange(center[0] - radius, center[0] + radius + 1),
            np.arange(center[1] - radius, center[1] + radius + 1),
        )
        pixels = np.column_stack((horizontal.ravel(), vertical.ravel())) / scale
        matrix, distortion, _, _ = _lens_arrays(self.lens)
        distortion = np.zeros(5) if distortion is None else distortion
        local = _local_rays(pixels, matrix, distortion)
        reference_rays = (
            local
            @ np.asarray(located["rotation_matrix"]).T
            @ np.asarray(reference["rotation_matrix"])
        )
        projected = _project(reference_rays, matrix, distortion) * scale
        valid = (
            (reference_rays[:, 2] > 0.05)
            & (projected[:, 0] >= 0)
            & (projected[:, 0] < stored.shape[1] - 1)
            & (projected[:, 1] >= 0)
            & (projected[:, 1] < stored.shape[0] - 1)
        )
        if not valid.all():
            return None
        template = cv2.remap(
            stored,
            projected[:, 0].reshape(horizontal.shape).astype(np.float32),
            projected[:, 1].reshape(vertical.shape).astype(np.float32),
            cv2.INTER_LINEAR,
        )
        if template.std() < 5:
            return None
        region = current[
            center[1] - extent : center[1] + extent + 1, center[0] - extent : center[0] + extent + 1
        ]
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, peak, _, position = cv2.minMaxLoc(scores)
        competitors = scores.copy()
        cv2.circle(competitors, position, 8, -1, -1)
        if (
            peak < 0.8
            or peak - float(competitors.max()) < 0.05
            or min(position) <= 0
            or max(position) >= 2 * search
        ):
            return None
        offset = np.asarray(position, float) - search
        # Parabolic subpixel peak; no integer-pixel dead zone around the centre.
        for axis in (0, 1):
            x, y = position
            before = float(scores[y - (axis == 1), x - (axis == 0)])
            after = float(scores[y + (axis == 1), x + (axis == 0)])
            curvature = before - 2 * peak + after
            if curvature < -1e-6:
                offset[axis] += np.clip(0.5 * (before - after) / curvature, -0.5, 0.5)
        observed = np.asarray(predicted) * scale + offset
        error = observed - np.array([self.lens["cx"], self.lens["cy"]]) * scale
        return {
            "error_pixels": error.tolist(),
            "center_error_pixels": float(np.linalg.norm(error)),
            "correlation": peak,
            "reference_id": reference["id"],
            "analysis_width": current.shape[1],
        }

    @staticmethod
    def _features(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
        keys, descriptors = cv2.SIFT_create(
            nfeatures=1200, contrastThreshold=0.025
        ).detectAndCompute(gray, None)
        _, coarse = cv2.ORB_create(nfeatures=400).detectAndCompute(gray, None)
        # OpenCV SIFT bins are quantized integers. Store them in one byte; the
        # maximum 256 references stay below 42 MiB, including points and ORB.
        return (
            np.float32([key.pt for key in keys]).reshape(-1, 2),
            descriptors.astype(np.uint8) if descriptors is not None else None,
            coarse,
        )

    def locate(
        self, image: np.ndarray, evidence: dict[str, Any], image_geometry: dict | None = None
    ) -> dict[str, Any]:
        with self._lock:
            return self._locate_frame(image, evidence, image_geometry)

    def _locate_frame(
        self, image: np.ndarray, evidence: dict[str, Any], image_geometry: dict | None
    ) -> dict[str, Any]:
        identity = frame_identity(evidence)
        if identity is None:
            return {"status": "unlocalized", "reason": "panorama_frame_identity_missing"}
        from toposync.runtime.pipelines.image_geometry import geometry_matrix

        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim not in (2, 3):
            return {"status": "unlocalized", "reason": "panorama_source_geometry_changed"}
        matrix = np.eye(3)
        try:
            if image_geometry is not None:
                matrix = geometry_matrix(image_geometry, image.shape[1], image.shape[0])
                if (
                    image_geometry["source_size"] != [self.lens["width"], self.lens["height"]]
                    or image_geometry.get("capture_evidence") != evidence
                ):
                    raise ValueError("Frame provenance changed")
            elif image.shape[:2] != (self.lens["height"], self.lens["width"]):
                raise ValueError("Unknown geometry")
        except (ValueError, TypeError, np.linalg.LinAlgError):
            return {"status": "unlocalized", "reason": "panorama_source_geometry_changed"}
        # Digest binds cached evidence to bytes, including same-sized transforms.
        digest = hashlib.blake2b(
            memoryview(np.ascontiguousarray(image)), digest_size=16
        ).hexdigest()
        key = (*identity, digest, matrix.tobytes())
        if key in self.results:
            return dict(self.results[key])
        result = self._locate(image, matrix)
        result = {**result, "capture_evidence": dict(evidence), "image_digest": digest}
        self.results[key] = result
        while len(self.results) > 4:
            self.results.popitem(last=False)
        return dict(result)

    def _locate(self, image: np.ndarray, to_source: np.ndarray | None = None) -> dict[str, Any]:
        gray = _gray(image)
        points, descriptors, coarse = self._features(gray)
        if descriptors is None or len(points) < MINIMUM_MATCHES:
            return {"status": "unlocalized", "reason": "panorama_visual_support_insufficient"}
        points = points / (gray.shape[1] / image.shape[1])
        if to_source is not None:
            from toposync.runtime.pipelines.image_geometry import source_pixels

            points = source_pixels(points, to_source)
        matcher = cv2.BFMatcher()
        coarse_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        ranking = []
        for reference in self.references:
            score = 0
            if coarse is not None and reference["coarse"] is not None:
                score = sum(
                    len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance
                    for pair in coarse_matcher.knnMatch(coarse, reference["coarse"], k=2)
                )
            ranking.append((score, reference))
        ranking.sort(key=lambda item: item[0], reverse=True)
        candidates = [reference for _, reference in ranking[:MAXIMUM_CANDIDATES]]
        previous = next(
            (reference for reference in self.references if reference["id"] == self.last_reference),
            None,
        )
        if previous is not None and all(item["id"] != previous["id"] for item in candidates):
            candidates.append(previous)
        accepted = []
        for reference in candidates:
            stored = reference["descriptors"]
            if stored is None or len(stored) < 2:
                continue
            first, second = descriptors.astype(np.float32), stored.astype(np.float32)
            forward = matcher.knnMatch(first, second, k=2)
            backward = matcher.knnMatch(second, first, k=2)
            reverse = {
                pair[0].queryIdx: pair[0].trainIdx
                for pair in backward
                if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7
            }
            pairs = [
                pair[0]
                for pair in forward
                if len(pair) == 2
                and pair[0].distance < pair[1].distance * 0.7
                and reverse.get(pair[0].trainIdx) == pair[0].queryIdx
            ]
            if len(pairs) < MINIMUM_MATCHES:
                continue
            fit = fit_frame_rotation(
                points[[pair.queryIdx for pair in pairs]],
                reference["points"][[pair.trainIdx for pair in pairs]],
                self.lens,
                reference["rotation_matrix"],
            )
            if fit:
                accepted.append({**fit, "reference_id": reference["id"]})
        if not accepted:
            return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        accepted.sort(key=lambda item: item["inliers"], reverse=True)
        best = accepted[0]
        first = np.asarray(best["rotation_matrix"])
        for alternative in accepted[1:]:
            difference = first.T @ np.asarray(alternative["rotation_matrix"])
            angle = np.arccos(np.clip((np.trace(difference) - 1) / 2, -1, 1))
            if angle > np.radians(1.0) and alternative["inliers"] >= best["inliers"] * 0.8:
                return {"status": "unlocalized", "reason": "panorama_visual_localization_ambiguous"}
        self.last_reference = best["reference_id"]
        return {"status": "localized", "lens": self.lens, **best}
