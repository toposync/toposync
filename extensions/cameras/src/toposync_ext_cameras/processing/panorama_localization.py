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
from copy import deepcopy
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
    def calibrated_transition(self, previous: np.ndarray, current: np.ndarray) -> dict | None:
        """Recover observed displacement, never arrival, using the bound lens.

        Reuse the loaded contextual matcher. No panorama target or cached pose
        participates in this independent two-frame rotation estimate.
        """
        matching = self._contextual_matcher
        if (matching is None or previous.shape != current.shape or current.ndim != 2
                or (matching.height, matching.width) != current.shape):
            return None
        matrix, distortion, width, height = _lens_arrays(self.lens)
        if abs(current.shape[1] / current.shape[0] - width / height) > 1e-6:
            return None
        before, after = matching.correspondences(matching.features(previous), matching.features(current))
        scale = width / current.shape[1]
        fitted = fit_frame_rotation(after * scale, before * scale, self.lens, np.eye(3))
        if fitted is None:
            return None
        grid = np.asarray([(width * x, height * y) for y in (.15, .5, .85)
                           for x in (.125, .375, .625, .875)])
        coefficients = np.zeros(5) if distortion is None else distortion
        rays = _local_rays(grid, matrix, coefficients) @ np.asarray(fitted["rotation_matrix"]).T
        if np.any(rays[:, 2] <= .05):
            return None
        displacement = float(np.median(np.linalg.norm(_project(rays, matrix, coefficients) - grid, axis=1)) / scale)
        # A small fitting wobble is not causal motion. This fallback is for
        # lost large transitions, never subpixel settling or optical zoom.
        if displacement <= max(3.0, 3 * fitted["validation_p95_pixels"]):
            return None
        return {"method": "calibrated_frame_rotation", "motion_pixels": displacement,
                **{key: fitted[key] for key in ("inliers", "inlier_fraction", "validation_matches",
                                               "validation_p95_pixels")}}

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
        self.results: OrderedDict[tuple[Any, ...], tuple[dict[str, Any], dict[str, Any]]] = OrderedDict()
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
        self, image: np.ndarray, evidence: dict[str, Any], image_geometry: dict | None = None,
        *, reference_candidates: bool = False,
    ) -> dict[str, Any]:
        """Locate current pixels; cross-capture proposals require a source-qualified caller."""
        with self._lock:
            return self._locate_frame(image, evidence, image_geometry,
                                      reference_candidates=reference_candidates)

    def locate_diagnostic(self, image: np.ndarray, evidence: dict[str, Any]) -> dict[str, Any]:
        """Private navigation replay; no images or feature arrays enter the API."""
        with self._lock:
            diagnostics: dict[str, Any] = {}
            result = self._locate_frame(image, evidence, None, diagnostics)
            return {**result, "diagnostics": {"model_sha256": self.model_digest,
                "references": {reference["id"]: reference["sha256"] for reference in self.references},
                **diagnostics}}

    def _locate_frame(
        self, image: np.ndarray, evidence: dict[str, Any], image_geometry: dict | None,
        diagnostics: dict[str, Any] | None = None,
        *, reference_candidates: bool = False,
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
        key = (*identity, image.shape, digest, matrix.tobytes())
        if key in self.results:
            result, record = self.results[key]
            if diagnostics is not None:
                diagnostics.update(deepcopy(record))
            return deepcopy(result)
        # Keep the evidence with the original decision, even for ordinary calls.
        # Inspecting it must not repeat recognition or refresh a tracking anchor.
        record: dict[str, Any] = {}
        # Measure a valid anchor before spending its freshness budget on full
        # recognition. This still fits original-reference pixels independently;
        # no previous orientation is reused or advanced through a frame chain.
        result = (self._tracked_locate(image, matrix, identity, record, other_capture=True)
                  if reference_candidates else None)
        if result is None:
            result = self._tracked_locate(image, matrix, identity, record)
        if result is None:
            result = self._locate(image, matrix, record)
            if result.get("reason") in {
                "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
            }:
                # Preserve qualified native matches and never override ambiguity.
                record["native_attempt"] = dict(record)
                result = self._locate(image, matrix, record, normalize=True)
            if result.get("reason") in {
                "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
            } and getattr(self, "model_directory", None) is not None:
                result = self._contextual_locate(image, matrix, identity, record)
            if result.get("status") == "localized" and record.get("photometry") in {"native", "local_contrast"}:
                self._anchor_native(image, matrix, identity, record)
        result = {**result, "capture_evidence": dict(evidence), "image_digest": digest}
        self.results[key] = (result, record)
        while len(self.results) > 4:
            self.results.popitem(last=False)
        if diagnostics is not None:
            diagnostics.update(deepcopy(record))
        return deepcopy(result)

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
        # A different decoder may have just recognized this same optical source.
        # Reuse only its central photograph as a search hint, never its pixels,
        # frame identity or pose. The new raster must qualify independently.
        for anchor in reversed(getattr(self, "_anchors", {}).values()):
            neighbors = anchor.get("neighbors", [])
            if neighbors and time.monotonic() - anchor["seen"] <= 5:
                centered = neighbors[0]
                if all(item["id"] != centered["id"] for item in candidates):
                    candidates.append(centered)
                break
        return candidates

    def _anchor_native(self, image: np.ndarray, matrix: np.ndarray, identity: tuple, record: dict) -> None:
        """Reuse original photographic correspondences, never the fitted pose."""
        from toposync.runtime.pipelines.image_geometry import source_pixels

        gray = _gray(image)
        references = {reference["id"]: reference for reference in self.references}
        pairs = []
        for candidate in record.get("candidates", []):
            if not candidate.get("current") or candidate["reference_id"] not in references:
                continue
            pixels = source_pixels(np.asarray(candidate["current"]), np.linalg.inv(matrix))
            pixels = (pixels + 0.5) * [gray.shape[1] / image.shape[1], gray.shape[0] / image.shape[0]] - 0.5
            # Retain competing reference pairs, including failed fits. Tracking
            # must independently reject ambiguity just as recognition does.
            pairs.append((references[candidate["reference_id"]], pixels.astype(np.float32),
                          np.asarray(candidate["reference"])))
        if pairs:
            key = (*identity[:2], matrix.tobytes(), image.shape)
            self._anchors[key] = {"gray": gray.copy(), "pairs": pairs, "seen": time.monotonic(),
                                  "sequence": identity[2], "native": True}
            self._anchors.move_to_end(key)
            while len(self._anchors) > 4:
                self._anchors.popitem(last=False)

    def _tracked_locate(self, image: np.ndarray, matrix: np.ndarray, identity: tuple,
                        diagnostics: dict | None, *, other_capture: bool = False) -> dict | None:
        """Track original-reference pairs, not a previously fitted orientation.

        The anchor raster and its reference pixels never advance through a chain
        of frames. Every new orientation must pass the original geometric gates.
        Ordinary tracking cannot cross epochs. A source-qualified observer may
        separately propose another full raster's reference pairs for a new fit.
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
        if other_capture:
            # The caller has qualified the same source/artifact. Cross-decoder
            # proposals additionally require complete, equally sampled optical
            # rasters: equal aspect ratio alone would admit crops and flips.
            def full_raster(shape: tuple, transform: np.ndarray) -> bool:
                sx, sy = self.lens["width"] / shape[1], self.lens["height"] / shape[0]
                expected = np.array([[sx, 0, (sx - 1) / 2],
                                     [0, sy, (sy - 1) / 2], [0, 0, 1]])
                return abs(sx / sy - 1) <= 0.005 and np.allclose(transform, expected, atol=1e-8, rtol=0)

            if not full_raster(image.shape, matrix):
                return None
            candidate = next(((candidate_key, candidate_anchor)
                              for candidate_key, candidate_anchor in reversed(self._anchors.items())
                              if candidate_key[0] != identity[0]
                              and candidate_anchor["gray"].shape == gray.shape
                              and full_raster(candidate_key[3], np.frombuffer(candidate_key[2], dtype=matrix.dtype).reshape(3, 3))), None)
            if candidate is None:
                return None
            key, anchor = candidate
        pairs = []
        if anchor is not None and (other_capture or identity[2] > anchor["sequence"]):
            # All references share the same two rasters. Build their optical
            # flow pyramids once per attempt, then keep each reference's pairs
            # separate for fitting and ambiguity checks.
            original_points = (np.concatenate([points for _, points, _ in anchor["pairs"]])
                               if anchor["pairs"] else np.empty((0, 2), np.float32))
            for large_displacement in (False, True):
                pairs = []
                # A fixed anchor can span several PT pulses. Retry one coarser
                # level only after insufficient support, as before.
                measured, valid = track_image_points(anchor["gray"], gray, original_points, 0.75,
                                                      large_displacement=large_displacement)
                offset = 0
                for reference, points, reference_points in anchor["pairs"]:
                    end = offset + len(points)
                    selected = valid[offset:end]
                    pairs.append((reference, measured[offset:end][selected], reference_points[selected]))
                    offset = end
                result = self._fit_contextual(pairs, image, matrix, diagnostics,
                                             "cross_capture_correspondences" if other_capture else "tracked_correspondences")
                if (result["status"] == "localized"
                        or result.get("reason") == "panorama_visual_localization_ambiguous"):
                    break
            if result["status"] == "localized":
                anchor["seen"] = started
                if not other_capture:
                    anchor["sequence"] = identity[2]
                anchor["neighbors"] = self._recognition_neighbors(result)
                self._anchors.move_to_end(key)
                return result
            if result.get("reason") == "panorama_visual_localization_ambiguous":
                self._anchors.pop(key, None)
                return result
            if other_capture:
                # No fitted pose or new raster becomes an anchor. Failed
                # proposals leave ordinary recognition as the recovery path.
                return None
            if anchor.get("native"):
                # A native anchor has a cheap full recognition fallback. Do not
                # force learned matching or keep rejecting after visual loss.
                return None
            # Old geometry selects photographs only. Fresh learned pairs must
            # independently requalify the current frame; no old pose is returned.
            if anchor.get("neighbors"):
                result = self._contextual_locate(image, matrix, identity, diagnostics,
                                                references=anchor["neighbors"],
                                                normalize=anchor.get("normalize", False))
                if result.get("reason") == "panorama_visual_localization_ambiguous":
                    self._anchors.pop(key, None)
            # A rejected frame does not renew the anchor or authorize a pose.
            # Keep its original pixels until the existing five-second expiry,
            # so the next fresh frame can recover after a transient. Spending
            # another global search here would age out that recovery opportunity.
            return result
        return None

    def _contextual_locate(self, image: np.ndarray, matrix: np.ndarray, identity: tuple,
                          diagnostics: dict | None, *, references: list[dict] | None = None,
                          normalize: bool = False, retry_photometry: bool = True,
                          previous_pairs: list | None = None) -> dict:
        from .panorama_features import ContextualMatcher

        gray = _gray(image)
        height, width = gray.shape
        key = (*identity[:2], matrix.tobytes(), image.shape)
        started = time.monotonic()
        feature_key = "contextual_normalized_features" if normalize else "contextual_features"
        if started < self._matching_retry_after:
            return {"status": "unlocalized", "reason": "panorama_visual_localization_failed"}
        try:
            matching = self._contextual_matcher
            if matching is None or (matching.width, matching.height) != (width, height):
                matching = ContextualMatcher(self.model_directory, width, height)
                self._contextual_matcher = matching
                for reference in self.references:
                    reference.pop("contextual_features", None)
                    reference.pop("contextual_normalized_features", None)
                self._anchors.clear()
            current = matching.features(image, normalize=normalize)
            if references is None:
                if normalize:
                    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
                _, coarse = cv2.ORB_create(nfeatures=400).detectAndCompute(gray, None)
                selected = self._rank_references(coarse, "normalized_" if normalize else "")
            else:
                selected = references

            def prepare_reference(reference: dict) -> None:
                if feature_key not in reference:
                    encoded = Path(reference["path"]).read_bytes()
                    if hashlib.sha256(encoded).hexdigest() != reference["sha256"]:
                        raise ValueError("Reference photograph changed")
                    reference_image = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
                    reference[feature_key] = matching.features(reference_image, normalize=normalize)

            def measure_reference(reference: dict) -> tuple:
                a, b = matching.correspondences(current, reference[feature_key])
                overlay = stationary_overlay_points(a * ANALYSIS_WIDTH / width, b * ANALYSIS_WIDTH / width)
                a, b = a[~overlay], b[~overlay]
                b = (b + 0.5) * [self.lens["width"] / width, self.lens["height"] / height] - 0.5
                return reference, a, b

            for reference in selected:
                prepare_reference(reference)
            # Bounded recovery, one frame in flight. Reference descriptors
            # are prepared serially; workers only compare immutable arrays.
            # Global recovery retains the same candidates and ambiguity checks.
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(selected)))) as workers:
                pairs = list(workers.map(measure_reference, selected))
            method = "local_contextual_correspondences" if references is not None else "contextual_correspondences"
            if normalize:
                method = "local_contrast_" + method
            result = self._fit_contextual(pairs, image, matrix, diagnostics, method)
            if previous_pairs is not None and result.get("reason") in {
                "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
            }:
                # Complementary photometries can cover different parts of the
                # same photograph. Merge proposals, not fitted inliers: the
                # existing pixel grouping removes duplicates/conflicts and
                # fixes the independent validation partition before fitting.
                merged = {reference["id"]: (reference, a, b) for reference, a, b in previous_pairs}
                for reference, a, b in pairs:
                    previous = merged.get(reference["id"])
                    merged[reference["id"]] = (reference, a, b) if previous is None else (
                        reference, np.concatenate((previous[1], a)), np.concatenate((previous[2], b)))
                pairs = list(merged.values())
                method = "combined_photometry_contextual_correspondences"
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
            # Tracking always measures the original raster. Photometric changes
            # affect descriptor proposals only, never source pixel geometry.
            self._anchors[key] = {"gray": _gray(image).copy(), "pairs": pairs, "seen": time.monotonic(),
                                  "sequence": identity[2], "neighbors": neighbors, "normalize": normalize}
            self._anchors.move_to_end(key)
            while len(self._anchors) > 4:
                self._anchors.popitem(last=False)
        elif retry_photometry and result.get("reason") in {
            "panorama_visual_support_insufficient", "panorama_visual_localization_failed"
        }:
            # Preserve qualified and ambiguous decisions. Both photographs and
            # current image use the same contrast transform, with separate caches.
            # A contrast-qualified anchor can return to original photometry;
            # try each form once, including when recovery starts normalized.
            return self._contextual_locate(image, matrix, identity, diagnostics,
                                           references=references, normalize=not normalize,
                                           retry_photometry=False, previous_pairs=pairs)
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
