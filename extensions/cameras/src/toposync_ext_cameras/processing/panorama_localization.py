"""Frame-bound visual orientation against immutable original panorama photographs.

No actuator position, blended panorama or last known pose is an observation.
Matching is bounded; a missing or ambiguous match stays unlocalized. References
use the presentation rotation already stored by reconstruction, exactly once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .panorama_mapping import _lens_arrays, _rotation_basis, image_pixel_to_ray, ray_to_image_pixel
from .panorama_correspondences import stationary_overlay_points, track_image_points
from .panorama_reconstruction import _local_rays, _project, align_unit_rays

ANALYSIS_WIDTH = 960
MAXIMUM_REFERENCES = 256
MAXIMUM_CANDIDATES = 8
MINIMUM_MATCHES = 60
MAXIMUM_ERROR_PIXELS = 8.0


def _matching_descriptors(descriptors: np.ndarray) -> np.ndarray:
    """RootSIFT: compare normalized gradient distributions, not bin magnitudes.

    The Hellinger embedding improves discrimination under illumination changes.
    Original quantized descriptors remain compact in the immutable cache.
    Arandjelovic and Zisserman, CVPR 2012, doi:10.1109/CVPR.2012.6248018.
    """
    values = descriptors.astype(np.float32)
    values /= np.maximum(values.sum(axis=1, keepdims=True), 1)
    return np.sqrt(values, out=values)


def _mutual_matches(matcher: Any, first: np.ndarray, second: np.ndarray) -> list:
    forward = [pair[0] for pair in matcher.knnMatch(first, second, k=2)
               if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7]
    if len(forward) < MINIMUM_MATCHES:
        return []
    # Only forward candidates can survive reciprocity. Their reverse search
    # still compares against EVERY current descriptor, with the exact metric.
    # Avoid the quadratic work on thousands of already rejected descriptors.
    indices = np.unique([pair.trainIdx for pair in forward])
    backward = matcher.knnMatch(second[indices], first, k=2)
    reverse = {indices[pair[0].queryIdx]: pair[0].trainIdx for pair in backward
               if len(pair) == 2 and pair[0].distance < pair[1].distance * 0.7}
    return [pair for pair in forward if reverse.get(pair.trainIdx) == pair.queryIdx]


def correspondence_groups(current: np.ndarray, reference: np.ndarray, width: int):
    """One vote per physical support, keyed by the immutable reference image.

    SIFT can describe several orientations at precisely the same location. A
    contested location is discarded, rather than choosing its best residual.
    The reference-pixel hash also fixes the validation partition across frames.
    """
    factor = min(1.0, ANALYSIS_WIDTH / width)
    current_keys = np.rint(current * factor).astype(np.int64)
    reference_keys = np.rint(reference * factor).astype(np.int64)
    by_reference: dict[tuple[int, int], list[int]] = {}
    by_current: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for index, (a, b) in enumerate(zip(current_keys, reference_keys, strict=True)):
        key = tuple(b)
        by_reference.setdefault(key, []).append(index)
        by_current.setdefault(tuple(a), set()).add(key)
    selected = []
    for key, indices in sorted(by_reference.items()):
        if len({tuple(current_keys[index]) for index in indices}) != 1:
            continue
        if len(by_current[tuple(current_keys[indices[0]])]) != 1:
            continue
        # Canonical representative; descriptor order and multiplicity have no vote.
        selected.append(min(indices, key=lambda index: (*reference[index], *current[index])))
    indices = np.asarray(selected, dtype=int)
    keys = reference_keys[indices]
    holdout = np.asarray([
        int.from_bytes(hashlib.blake2s(f"support-v1:{x}:{y}".encode(), digest_size=4).digest(), "big") % 5 == 0
        for x, y in keys
    ], dtype=bool)
    return indices, holdout


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
    current: np.ndarray, reference: np.ndarray, lens: dict[str, Any], reference_rotation: Any,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Robust rotation with a deterministic held-out correspondence partition.

    Pixel coordinates are original optical pixels, independent of feature scale.
    Held-out observations never fit or select a RANSAC model.
    """
    record = diagnostics if diagnostics is not None else {}
    record.update(stage="matches", matches=len(current), partition_version="support-v1")
    if len(current) != len(reference) or len(current) < MINIMUM_MATCHES:
        return None
    matrix, distortion, width, height = _lens_arrays(lens)
    indices, holdout = correspondence_groups(current, reference, width)
    current, reference = current[indices], reference[indices]
    record.update(stage="independent_support", groups=len(current), validation_matches=int(holdout.sum()))
    if diagnostics is not None:
        record.update(current=current.tolist(), reference=reference.tolist(), holdout=holdout.tolist())
    if len(current) < MINIMUM_MATCHES or holdout.sum() < 12:
        return None
    distortion = np.zeros(5) if distortion is None else distortion
    rotation = np.asarray(reference_rotation, dtype=float)
    _rotation_basis(rotation)
    source = _local_rays(current, matrix, distortion)
    destination = _local_rays(reference, matrix, distortion) @ rotation.T
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
    record.update(stage="training", inliers=best_count, training_matches=len(training))
    if best is None or best_count < 40 or best_count / len(training) < 0.55:
        return None
    fitted = align_unit_rays(source[best], destination[best])
    residual = errors(fitted)
    validation = holdout & (residual <= MAXIMUM_ERROR_PIXELS)
    record.update(stage="spatial_support", validation_good=int(validation.sum()))
    if diagnostics is not None:
        record["residual_pixels"] = [float(value) if np.isfinite(value) else None for value in residual]
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
    record.update(stage="validation", validation_p95_pixels=percentile if np.isfinite(percentile) else None)
    if not np.isfinite(percentile) or percentile > MAXIMUM_ERROR_PIXELS:
        return None
    record["stage"] = "accepted"
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

    def __init__(self, model: dict[str, Any], photographs: list[dict[str, Any]], *,
                 model_directory: Path | None = None):
        self._lock = threading.RLock()
        self.model = model
        self.model_digest = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
        self.lens = dict(model["lens"])
        _lens_arrays(self.lens)
        self.references = []
        self.last_reference: str | None = None
        self.results: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self.model_directory = model_directory
        self._contextual_matcher = None
        self._matching_retry_after = 0.0
        self._anchors: OrderedDict[tuple, dict] = OrderedDict()
        indexed = {photo["id"]: photo for photo in photographs}
        captures = model["captures"]
        if not 1 <= len(captures) <= MAXIMUM_REFERENCES:
            raise ValueError("Invalid number of visual references")
        for capture in captures:
            photo = indexed[capture["id"]]
            path = Path(photo["path"])
            with path.open("rb") as stream:
                photo_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if photo.get("sha256") and photo_digest != photo["sha256"]:
                raise ValueError("Reference photograph changed")
            image = cv2.imread(str(path))
            if image is None or image.shape[:2] != (self.lens["height"], self.lens["width"]):
                raise ValueError("Reference photograph does not match the optical model")
            gray = _gray(image)
            points, descriptors, coarse = self._features(gray)
            normalized_points, normalized_descriptors, normalized_coarse = self._features(gray, normalize=True)
            _rotation_basis(capture["rotation_matrix"])
            self.references.append(
                {
                    "id": capture["id"],
                    "sha256": photo_digest,
                    "rotation_matrix": capture["rotation_matrix"],
                    "path": str(path),
                    "pose": photo.get("pose", {}),
                    "points": points / (gray.shape[1] / image.shape[1]),
                    "descriptors": descriptors,
                    "coarse": coarse,
                    "normalized_points": normalized_points / (gray.shape[1] / image.shape[1]),
                    "normalized_descriptors": normalized_descriptors,
                    "normalized_coarse": normalized_coarse,
                }
            )

    def target_reference(self, ray: Any) -> dict[str, Any] | None:
        choices = self._references_around(ray)
        return choices[0] if choices else None

    def _references_around(self, ray: Any) -> list[dict[str, Any]]:
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
        return [reference for _, reference in sorted(choices, key=lambda choice: choice[0], reverse=True)]

    def _recognition_neighbors(self, located: dict) -> list[dict]:
        ray = image_pixel_to_ray(self.lens["cx"], self.lens["cy"], self.lens,
                                 rotation_matrix=located["rotation_matrix"])
        candidates = self._references_around(ray)[:MAXIMUM_CANDIDATES]
        previous = next(reference for reference in self.references
                        if reference["id"] == located["reference_id"])
        if all(reference["id"] != previous["id"] for reference in candidates):
            candidates.append(previous)
        return candidates

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
    def _features(gray: np.ndarray, *, normalize: bool = False) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
        if normalize:
            # Apply the same photometric transform to references and live frames.
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        keys, descriptors = cv2.SIFT_create(
            nfeatures=4500, contrastThreshold=0.025
        ).detectAndCompute(gray, None)
        _, coarse = cv2.ORB_create(nfeatures=400).detectAndCompute(gray, None)
        # OpenCV SIFT bins are quantized integers. Store them in one byte; the
        # maximum 256 references stay below 320 MiB for both photometric forms,
        # including points and ORB. Original photographs remain unchanged.
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

    def locate_diagnostic(self, image: np.ndarray, evidence: dict[str, Any]) -> dict[str, Any]:
        """Private navigation replay; no images or feature arrays enter the API."""
        with self._lock:
            diagnostics: dict[str, Any] = {"model_sha256": self.model_digest,
                "references": {reference["id"]: reference["sha256"] for reference in self.references}}
            result = self._locate_frame(image, evidence, None, diagnostics)
            return {**result, "diagnostics": diagnostics}

    def _locate_frame(
        self, image: np.ndarray, evidence: dict[str, Any], image_geometry: dict | None,
        diagnostics: dict[str, Any] | None = None,
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
            if hasattr(self, "_anchors"):
                self._anchors.clear()
            return {"status": "unlocalized", "reason": "panorama_source_geometry_changed"}
        # Digest binds cached evidence to bytes, including same-sized transforms.
        digest = hashlib.blake2b(
            memoryview(np.ascontiguousarray(image)), digest_size=16
        ).hexdigest()
        key = (*identity, digest, matrix.tobytes())
        if key in self.results and diagnostics is None:
            return dict(self.results[key])
        # Measure a valid anchor before spending its freshness budget on full
        # recognition. This still fits original-reference pixels independently;
        # no previous orientation is reused or advanced through a frame chain.
        result = self._tracked_locate(image, matrix, identity, diagnostics)
        if result is None:
            result = self._locate(image, matrix, diagnostics)
            if result.get("reason") in {
                "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
            }:
                # Preserve qualified native matches and never override ambiguity.
                if diagnostics is not None:
                    diagnostics["native_attempt"] = dict(diagnostics)
                result = self._locate(image, matrix, diagnostics, normalize=True)
            if result.get("reason") in {
                "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
            } and getattr(self, "model_directory", None) is not None:
                result = self._contextual_locate(image, matrix, identity, diagnostics)
        result = {**result, "capture_evidence": dict(evidence), "image_digest": digest}
        self.results[key] = result
        while len(self.results) > 4:
            self.results.popitem(last=False)
        return dict(result)

    def _locate(self, image: np.ndarray, to_source: np.ndarray | None = None,
                diagnostics: dict[str, Any] | None = None, *, normalize: bool = False) -> dict[str, Any]:
        gray = _gray(image)
        points, descriptors, coarse = self._features(gray, normalize=normalize)
        prefix = "normalized_" if normalize else ""
        if diagnostics is not None:
            diagnostics["photometry"] = "local_contrast" if normalize else "native"
        if descriptors is None or len(points) < MINIMUM_MATCHES:
            return {"status": "unlocalized", "reason": "panorama_visual_support_insufficient"}
        points = points / (gray.shape[1] / image.shape[1])
        if to_source is not None:
            from toposync.runtime.pipelines.image_geometry import source_pixels

            points = source_pixels(points, to_source)
        matcher = cv2.BFMatcher()
        current_descriptors = _matching_descriptors(descriptors)
        candidates = self._rank_references(coarse, prefix)
        accepted = []
        if diagnostics is not None:
            diagnostics["candidates"] = []
        for reference in candidates:
            stored = reference[prefix + "descriptors"]
            if stored is None or len(stored) < 2:
                continue
            pairs = _mutual_matches(matcher, current_descriptors, _matching_descriptors(stored))
            if len(pairs) < MINIMUM_MATCHES:
                continue
            current_points = points[[pair.queryIdx for pair in pairs]]
            reference_points = reference[prefix + "points"][[pair.trainIdx for pair in pairs]]
            scale = min(1.0, ANALYSIS_WIDTH / self.lens["width"])
            overlay = stationary_overlay_points(current_points * scale, reference_points * scale)
            candidate = {} if diagnostics is not None else None
            fit = fit_frame_rotation(
                current_points[~overlay],
                reference_points[~overlay],
                self.lens,
                reference["rotation_matrix"],
                candidate,
            )
            if diagnostics is not None:
                diagnostics["candidates"].append({"reference_id": reference["id"],
                    "overlay_points": int(overlay.sum()), **candidate})
            if fit:
                accepted.append({**fit, "reference_id": reference["id"]})
        return self._select_orientation(accepted)

    def _rank_references(self, coarse: np.ndarray | None, prefix: str = "") -> list[dict]:
        coarse_matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        ranking = []
        for reference in self.references:
            score = 0
            if coarse is not None and reference[prefix + "coarse"] is not None:
                score = sum(
                    len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance
                    for pair in coarse_matcher.knnMatch(coarse, reference[prefix + "coarse"], k=2)
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
        return candidates

    def _tracked_locate(self, image: np.ndarray, matrix: np.ndarray, identity: tuple,
                        diagnostics: dict | None) -> dict | None:
        """Track original-reference pairs, not a previously fitted orientation.

        The anchor raster and its reference pixels never advance through a chain
        of frames. Every new orientation must pass the original geometric gates.
        A transport gap, epoch change or lost image support requires recognition.
        """
        if not getattr(self, "_anchors", None):
            return None
        gray = _gray(image)
        key = (*identity[:2], matrix.tobytes(), image.shape)
        started = time.monotonic()
        for old_key, old in list(self._anchors.items()):
            if started - old["seen"] > 5:
                self._anchors.pop(old_key)
        anchor = self._anchors.get(key)
        pairs = []
        if anchor is not None and identity[2] > anchor["sequence"]:
            for reference, original_points, reference_points in anchor["pairs"]:
                measured, valid = track_image_points(anchor["gray"], gray, original_points, 0.75)
                pairs.append((reference, measured[valid], reference_points[valid]))
            result = self._fit_contextual(pairs, image, matrix, diagnostics, "tracked_correspondences")
            if result["status"] == "localized":
                anchor["seen"], anchor["sequence"] = started, identity[2]
                anchor["neighbors"] = self._recognition_neighbors(result)
                self._anchors.move_to_end(key)
                return result
            if result.get("reason") == "panorama_visual_localization_ambiguous":
                self._anchors.pop(key, None)
                return result
            # Old geometry selects photographs only. Fresh learned pairs must
            # independently requalify the current frame; no old pose is returned.
            if anchor.get("neighbors"):
                result = self._contextual_locate(image, matrix, identity, diagnostics,
                                                references=anchor["neighbors"])
                if result.get("reason") == "panorama_visual_localization_ambiguous":
                    self._anchors.pop(key, None)
            # A rejected frame does not renew the anchor or authorize a pose.
            # Keep its original pixels until the existing five-second expiry,
            # so the next fresh frame can recover after a transient. Spending
            # another global search here would age out that recovery opportunity.
            return result
        return None

    def _contextual_locate(self, image: np.ndarray, matrix: np.ndarray, identity: tuple,
                          diagnostics: dict | None, *, references: list[dict] | None = None) -> dict:
        from .panorama_features import ContextualMatcher

        gray = _gray(image)
        height, width = gray.shape
        key = (*identity[:2], matrix.tobytes(), image.shape)
        started = time.monotonic()
        if started < self._matching_retry_after:
            return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        try:
            matching = self._contextual_matcher
            if matching is None or (matching.width, matching.height) != (width, height):
                matching = ContextualMatcher(self.model_directory, width, height)
                self._contextual_matcher = matching
                for reference in self.references:
                    reference.pop("contextual_features", None)
                self._anchors.clear()
            current = matching.features(image)
            if references is None:
                _, coarse = cv2.ORB_create(nfeatures=400).detectAndCompute(gray, None)
                selected = self._rank_references(coarse)
            else:
                selected = references

            def prepare_reference(reference: dict) -> None:
                if "contextual_features" not in reference:
                    encoded = Path(reference["path"]).read_bytes()
                    if hashlib.sha256(encoded).hexdigest() != reference["sha256"]:
                        raise ValueError("Reference photograph changed")
                    reference_image = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
                    reference["contextual_features"] = matching.features(reference_image)

            def measure_reference(reference: dict) -> tuple:
                a, b = matching.correspondences(current, reference["contextual_features"])
                overlay = stationary_overlay_points(a * ANALYSIS_WIDTH / width, b * ANALYSIS_WIDTH / width)
                a, b = a[~overlay], b[~overlay]
                b = (b + 0.5) * [self.lens["width"] / width, self.lens["height"] / height] - 0.5
                return reference, a, b

            for reference in selected:
                prepare_reference(reference)
            if references is not None:
                # Bounded recovery, one frame in flight. Reference descriptors
                # are prepared serially; workers only compare immutable arrays.
                with ThreadPoolExecutor(max_workers=min(4, len(selected))) as workers:
                    pairs = list(workers.map(measure_reference, selected))
            else:
                pairs = [measure_reference(reference) for reference in selected]
            method = "local_contextual_correspondences" if references is not None else "contextual_correspondences"
            result = self._fit_contextual(pairs, image, matrix, diagnostics, method)
            if result["status"] == "localized":
                # Coarse appearance can select an oblique photograph at night.
                # Once geometry is independently qualified, also measure the
                # original photograph with greatest margin around the optical
                # centre, using the same selection as target measurement.
                # At most one extra reference; retain all competing candidates.
                ray = image_pixel_to_ray(self.lens["cx"], self.lens["cy"], self.lens,
                                         rotation_matrix=result["rotation_matrix"])
                centered = self.target_reference(ray)
                if centered is not None and all(reference["id"] != centered["id"]
                                                for reference, _, _ in pairs):
                    prepare_reference(centered)
                    pairs.append(measure_reference(centered))
                    result = self._fit_contextual(pairs, image, matrix, diagnostics, method)
            if result["status"] == "localized":
                neighbors = self._recognition_neighbors(result)
                for reference in neighbors:
                    prepare_reference(reference)
        except Exception as error:
            logging.getLogger(__name__).warning("Contextual panorama matching unavailable: %s", type(error).__name__)
            self._matching_retry_after = time.monotonic() + 30
            return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        if result["status"] == "localized":
            self._anchors[key] = {"gray": gray.copy(), "pairs": pairs, "seen": time.monotonic(),
                                  "sequence": identity[2], "neighbors": neighbors}
            self._anchors.move_to_end(key)
            while len(self._anchors) > 4:
                self._anchors.popitem(last=False)
        return result

    def _fit_contextual(self, pairs: list, image: np.ndarray, matrix: np.ndarray,
                        diagnostics: dict | None, method: str) -> dict:
        from toposync.runtime.pipelines.image_geometry import source_pixels

        height, width = _gray(image).shape
        accepted = []
        if diagnostics is not None:
            diagnostics["photometry"] = method
            diagnostics["candidates"] = []
        for reference, a, b in pairs:
            a = (a + 0.5) * [image.shape[1] / width, image.shape[0] / height] - 0.5
            a = source_pixels(a, matrix)
            candidate = {} if diagnostics is not None else None
            fit = fit_frame_rotation(a, b, self.lens, reference["rotation_matrix"], candidate)
            if diagnostics is not None:
                diagnostics["candidates"].append({"reference_id": reference["id"],
                                                **candidate})
            if fit:
                accepted.append({**fit, "reference_id": reference["id"]})
        return self._select_orientation(accepted)

    def _select_orientation(self, accepted: list[dict]) -> dict:
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
