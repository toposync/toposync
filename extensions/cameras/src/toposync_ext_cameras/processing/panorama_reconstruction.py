"""Bounded, offline reconstruction of a fixed optical state's PTZ photographs.

No camera, configuration, network, or physical-position side effects live here.
Native positions are hints for candidate matching, never angles. The estimated
SO(3) rotations and Brown lens describe a visual panorama, not a floorplan or a
certified actuator model. The first camera defines an arbitrary, scale-free frame.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from scipy.optimize import brentq, least_squares
from scipy.sparse import lil_matrix
from scipy.sparse.linalg import lsmr
from scipy.spatial.transform import Rotation

from .panorama_mapping import _rotation_basis

ALGORITHM_VERSION = "automatic_rotation_brown_v3"
ANALYSIS_WIDTH = 960
OUTPUT_WIDTH = 4096
OUTPUT_HEIGHT = 2048
MAX_CAPTURES = 256
MAX_INPUT_BYTES = 2 * 1024**3
MAX_IMAGE_PIXELS = 50_000_000


class ReconstructionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.supported_indices: list[int] | None = None


class ReconstructionCancelled(Exception):
    pass


@dataclass
class _FeatureImage:
    identifier: str
    path: Path
    width: int
    height: int
    points: np.ndarray
    descriptors: np.ndarray
    brightness: np.ndarray
    overlay_points: list[tuple[float, float]]
    hints: dict[str, Any]


@dataclass
class _Edge:
    first: int
    second: int
    first_keys: np.ndarray
    second_keys: np.ndarray
    first_pixels: np.ndarray
    second_pixels: np.ndarray
    homography: np.ndarray


def _check(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ReconstructionCancelled("Panorama reconstruction cancelled")


def _notify(progress: Callable[[dict], None] | None, stage: str, **values: Any) -> None:
    if progress is not None:
        progress({"stage": stage, **values})


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_image(path: Path, image: np.ndarray) -> None:
    temporary = path.with_name(path.stem + ".partial" + path.suffix)
    if not cv2.imwrite(str(temporary), image):
        raise ReconstructionError("write_failed", "Could not persist panorama image")
    temporary.replace(path)


def _read_inputs(captures: list[dict], progress: Any, cancelled: Any) -> list[_FeatureImage]:
    if not 2 <= len(captures) <= MAX_CAPTURES:
        raise ReconstructionError("capture_count", "Between two and 256 photographs are required")
    identifiers = [capture.get("id") for capture in captures]
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise ReconstructionError("capture_id", "Photographs require nonempty identifiers")
    if len(set(identifiers)) != len(identifiers):
        raise ReconstructionError("capture_id", "Photograph identifiers must be unique")
    features: list[_FeatureImage] = []
    total_bytes = 0
    # Bound descriptor memory independently of the number of 4K originals.
    feature_budget = min(4500, max(500, 160_000 // len(captures)))
    sift = cv2.SIFT_create(nfeatures=feature_budget, contrastThreshold=0.025)
    dimensions: tuple[int, int] | None = None
    reported_zoom: float | None = None
    for index, capture in enumerate(captures):
        _check(cancelled)
        raw_path = capture.get("path", capture.get("filename"))
        if not isinstance(raw_path, (str, Path)) or not Path(raw_path).is_absolute():
            raise ReconstructionError("capture_path", "Photograph paths must be absolute")
        path = Path(raw_path)
        try:
            total_bytes += path.stat().st_size
            with Image.open(path) as header:
                width, height = header.size
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise ReconstructionError("invalid_image", "Could not read a photograph") from error
        if min(width, height) < 32 or width * height > MAX_IMAGE_PIXELS:
            raise ReconstructionError("image_dimensions", "Photograph dimensions exceed the budget")
        if total_bytes > MAX_INPUT_BYTES:
            raise ReconstructionError("input_budget", "Photographs exceed the storage budget")
        if dimensions is not None and dimensions != (width, height):
            raise ReconstructionError("optical_identity_changed", "Photograph dimensions changed")
        dimensions = (width, height)
        zoom = (capture.get("pose") or {}).get("zoom")
        if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and math.isfinite(zoom):
            if reported_zoom is not None and abs(float(zoom) - reported_zoom) > 1e-4:
                raise ReconstructionError(
                    "optical_identity_changed", "Reported optical zoom changed"
                )
            reported_zoom = float(zoom)
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (height, width):
            raise ReconstructionError("invalid_image", "Could not decode a photograph")
        scale = min(1.0, ANALYSIS_WIDTH / width)
        size = (round(width * scale), round(height * scale))
        gray = cv2.cvtColor(
            cv2.resize(image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY
        )
        del image
        keys, descriptors = sift.detectAndCompute(gray, None)
        points = np.asarray([key.pt for key in keys], dtype=np.float64).reshape(-1, 2)
        smoothed = cv2.GaussianBlur(gray, (5, 5), 0)
        coordinates = np.rint(points).astype(int)
        brightness = smoothed[coordinates[:, 1], coordinates[:, 0]] if len(points) else np.empty(0)
        features.append(
            _FeatureImage(
                capture["id"],
                path,
                width,
                height,
                points,
                descriptors if descriptors is not None else np.empty((0, 128), np.float32),
                brightness,
                [],
                capture,
            )
        )
        _notify(progress, "features", completed=index + 1, total=len(captures))
    return features


def _candidate_pairs(features: list[_FeatureImage]) -> list[tuple[int, int]]:
    count = len(features)
    if count <= 40:
        return [(first, second) for first in range(count) for second in range(first + 1, count)]
    pairs: set[tuple[int, int]] = set()
    # Local acquisition neighbours plus global descriptor votes bound matching
    # work without assuming row spacing, native units, or a particular camera.
    index = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=32))
    descriptors = [feature.descriptors for feature in features if len(feature.descriptors)]
    original_indices = [
        number for number, feature in enumerate(features) if len(feature.descriptors)
    ]
    if descriptors:
        index.add(descriptors)
        index.train()
    for first, feature in enumerate(features):
        candidates = set(range(max(0, first - 3), min(count, first + 4)))
        candidates.update((0, count - 1))
        if len(feature.descriptors) and descriptors:
            # FLANN requires a contiguous query matrix; strided descriptor
            # sampling is only used on larger acquisitions and otherwise fails
            # inside OpenCV before candidate matching can start.
            samples = np.ascontiguousarray(
                feature.descriptors[:: max(1, len(feature.descriptors) // 150)]
            )
            votes: dict[int, int] = {}
            for neighbours in index.knnMatch(samples, k=min(5, sum(map(len, descriptors)))):
                for match in neighbours:
                    other = original_indices[match.imgIdx]
                    if other != first:
                        votes[other] = votes.get(other, 0) + 1
            candidates.update(sorted(votes, key=lambda value: (-votes[value], value))[:8])
        pairs.update(tuple(sorted((first, second))) for second in candidates if first != second)
    return sorted(pairs)


def _match(features: list[_FeatureImage], progress: Any, cancelled: Any) -> list[_Edge]:
    pairs = _candidate_pairs(features)
    edges: list[_Edge] = []
    observations_per_edge = (
        180 if len(features) <= 40 else max(24, min(180, 32_000 // max(1, len(pairs))))
    )
    matcher = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=64))
    for number, (first, second) in enumerate(pairs):
        _check(cancelled)
        left, right = features[first], features[second]
        if min(len(left.descriptors), len(right.descriptors)) < 20:
            continue
        neighbours = matcher.knnMatch(left.descriptors, right.descriptors, k=2)
        selected = [
            pair[0]
            for pair in neighbours
            if len(pair) == 2 and pair[0].distance < 0.70 * pair[1].distance
        ]
        # Several source descriptors can pass the ratio test against the same
        # target. Counting them as independent support admits collapsing
        # homographies and lets a few incompatible tracks distort the lens fit.
        # Discard every contested target rather than arbitrarily choosing one.
        target_counts = Counter(match.trainIdx for match in selected)
        selected = [match for match in selected if target_counts[match.trainIdx] == 1]
        if len(selected) < 20:
            continue
        first_keys = np.asarray([match.queryIdx for match in selected])
        second_keys = np.asarray([match.trainIdx for match in selected])
        a, b = left.points[first_keys], right.points[second_keys]
        displacement = np.linalg.norm(a - b, axis=1)
        if np.median(displacement) > 12:
            stationary = displacement < 1.25
            left.overlay_points.extend(map(tuple, a[stationary]))
            right.overlay_points.extend(map(tuple, b[stationary]))
            a, b = a[~stationary], b[~stationary]
            first_keys, second_keys = first_keys[~stationary], second_keys[~stationary]
        if len(a) < 20:
            continue
        homography, inliers = cv2.findHomography(
            a, b, cv2.RANSAC, 3.0, maxIters=2500, confidence=0.995
        )
        if homography is None or inliers is None or int(inliers.sum()) < 20:
            continue
        valid = np.flatnonzero(inliers.ravel())
        if len(valid) / len(a) < 0.20:
            continue
        # A match concentrated in a clock, repeated window, or moving leaf is
        # insufficient even when a homography fits its local pixels perfectly.
        # A clock-dominated consensus must not gain apparent spatial support
        # from one distant match. Require the central 80% of correspondences
        # to cover both image axes, in both views; zero motion itself is valid.
        first_extent = np.diff(np.quantile(a[valid], (0.10, 0.90), axis=0), axis=0)[0]
        second_extent = np.diff(np.quantile(b[valid], (0.10, 0.90), axis=0), axis=0)[0]
        extent = np.minimum(first_extent, second_extent)
        image_scale = min(1.0, ANALYSIS_WIDTH / left.width)
        if (
            extent[0] < left.width * image_scale * 0.12
            or extent[1] < left.height * image_scale * 0.10
        ):
            continue
        if len(valid) > observations_per_edge:
            valid = valid[np.linspace(0, len(valid) - 1, observations_per_edge).astype(int)]
        edges.append(
            _Edge(
                first, second, first_keys[valid], second_keys[valid], a[valid], b[valid], homography
            )
        )
        if number % 8 == 0:
            _notify(
                progress,
                "matching",
                completed=number + 1,
                total=len(pairs),
                verified_pairs=len(edges),
            )
    return edges


def _connected(count: int, edges: list[_Edge]) -> list[list[int]]:
    neighbours: list[set[int]] = [set() for _ in range(count)]
    for edge in edges:
        neighbours[edge.first].add(edge.second)
        neighbours[edge.second].add(edge.first)
    remaining = set(range(count))
    components = []
    while remaining:
        pending, component = [min(remaining)], set()
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(neighbours[node] - component)
        remaining -= component
        components.append(sorted(component))
    return sorted(components, key=lambda value: (-len(value), value[0]))


def _split_tracks(
    edges: list[_Edge],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    parents: dict[tuple[int, int], tuple[int, int]] = {}
    observations: dict[tuple[int, int], dict[int, int]] = {}

    def find(key: tuple[int, int]) -> tuple[int, int]:
        parents.setdefault(key, key)
        observations.setdefault(key, {key[0]: key[1]})
        root = key
        while parents[root] != root:
            root = parents[root]
        while parents[key] != key:
            previous = parents[key]
            parents[key] = root
            key = previous
        return root

    accepted: set[tuple[int, int]] = set()
    for edge_index, edge in sorted(enumerate(edges), key=lambda item: -len(item[1].first_keys)):
        for match_index, (left, right) in enumerate(
            zip(edge.first_keys, edge.second_keys, strict=True)
        ):
            a, b = find((edge.first, int(left))), find((edge.second, int(right)))
            shared = observations[a].keys() & observations[b].keys()
            if any(observations[a][view] != observations[b][view] for view in shared):
                # One physical track cannot contain two features from one image.
                # Repeated structures must not merge the graph into a giant fake
                # track whose arbitrary split consumes an entire camera's data.
                continue
            root, child = min(a, b), max(a, b)
            parents[child] = root
            observations[root].update(observations[child])
            accepted.add((edge_index, match_index))
    first, second, a, b, checks = [], [], [], [], []
    for edge_index, edge in enumerate(edges):
        for match_index, (left, right, pa, pb) in enumerate(
            zip(
                edge.first_keys,
                edge.second_keys,
                edge.first_pixels,
                edge.second_pixels,
                strict=True,
            )
        ):
            if (edge_index, match_index) not in accepted:
                continue
            root = find((edge.first, int(left)))
            digest = hashlib.sha256(f"{root[0]}:{root[1]}".encode()).digest()
            first.append(edge.first)
            second.append(edge.second)
            a.append(pa)
            b.append(pb)
            checks.append(digest[0] % 5 == 0)
    return (
        np.asarray(first),
        np.asarray(second),
        np.asarray(a),
        np.asarray(b),
        np.asarray(checks, dtype=bool),
    )


def _intrinsics(parameters: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    focal = math.exp(parameters[0])
    matrix = np.array(
        [
            [focal, 0, (width - 1) / 2],
            [0, focal * math.exp(parameters[3]), (height - 1) / 2],
            [0, 0, 1.0],
        ]
    )
    # This radial family is monotone everywhere, avoiding secondary roots of a
    # Brown polynomial that a fixed-iteration undistorter can mistake for a lens.
    radial_second = parameters[2] + 0.46 * min(parameters[1], 0.0) ** 2
    return matrix, np.array([parameters[1], radial_second, 0.0, 0.0, 0.0])


def _local_rays(pixels: np.ndarray, matrix: np.ndarray, distortion: np.ndarray) -> np.ndarray:
    points = (pixels - matrix[:2, 2]) / np.array([matrix[0, 0], matrix[1, 1]])
    observed = np.linalg.norm(points, axis=1)
    k1, k2 = distortion[:2]
    lower, upper = np.zeros_like(observed), np.maximum(1.0, observed)
    for _ in range(10):
        insufficient = upper * (1 + k1 * upper**2 + k2 * upper**4) < observed
        if not np.any(insufficient):
            break
        upper[insufficient] *= 2
    radius = np.minimum(observed, upper)
    for _ in range(32):
        value = radius * (1 + k1 * radius**2 + k2 * radius**4) - observed
        if float(np.max(np.abs(value), initial=0)) < 1e-12:
            break
        upper = np.where(value > 0, radius, upper)
        lower = np.where(value <= 0, radius, lower)
        derivative = 1 + 3 * k1 * radius**2 + 5 * k2 * radius**4
        proposal = radius - value / np.maximum(derivative, 1e-6)
        updated = np.where((proposal > lower) & (proposal < upper), proposal, (lower + upper) / 2)
        radius = np.where(np.abs(value) < 1e-12, radius, updated)
    points *= (radius / np.maximum(observed, 1e-15))[:, None]
    rays = np.column_stack((points, np.ones(len(points))))
    return rays / np.linalg.norm(rays, axis=1)[:, None]


def _maximum_optical_radius(
    matrix: np.ndarray, distortion: np.ndarray, width: int, height: int
) -> float:
    corners = np.array(
        [[0, 0], [width - 1, 0], [0, height - 1], [width - 1, height - 1]], dtype=float
    )
    observed = float(
        np.max(
            np.linalg.norm(
                (corners - matrix[:2, 2]) / np.array([matrix[0, 0], matrix[1, 1]]), axis=1
            )
        )
    )
    k1, k2 = distortion[:2]
    roots = np.roots([5 * k2, 3 * k1, 1])
    critical = [float(root.real) for root in roots if abs(root.imag) < 1e-9 and root.real > 0]
    upper = math.sqrt(min(critical)) if critical else max(1.0, observed * 2)

    def forward(radius: float) -> float:
        return radius * (1 + k1 * radius**2 + k2 * radius**4)

    if critical and forward(upper) <= observed:
        raise ReconstructionError(
            "noninvertible_lens", "Estimated lens cannot cover the full photograph monotonically"
        )
    for _ in range(10):
        if forward(upper) >= observed:
            return float(brentq(lambda radius: forward(radius) - observed, 0, upper))
        upper *= 2
    raise ReconstructionError("noninvertible_lens", "Estimated lens inverse did not converge")


def align_unit_rays(source: np.ndarray, destination: np.ndarray) -> np.ndarray:
    """Least-squares SO(3) mapping source unit rays into destination rays."""
    u, _, vt = np.linalg.svd(destination.T @ source)
    return u @ np.diag([1.0, 1.0, np.linalg.det(u @ vt)]) @ vt


def _initial_rotations(
    count: int, edges: list[_Edge], matrix: np.ndarray, distortion: np.ndarray
) -> np.ndarray:
    rotations = np.tile(np.eye(3), (count, 1, 1))
    known = {0}
    while len(known) < count:
        frontier = [edge for edge in edges if (edge.first in known) != (edge.second in known)]
        if not frontier:
            raise ReconstructionError(
                "disconnected_graph", "Photographs do not form one connected panorama"
            )
        edge = max(frontier, key=lambda value: len(value.first_pixels))
        first = _local_rays(edge.first_pixels, matrix, distortion)
        second = _local_rays(edge.second_pixels, matrix, distortion)
        relative = align_unit_rays(first, second)
        if edge.first in known:
            rotations[edge.second] = rotations[edge.first] @ relative.T
            known.add(edge.second)
        else:
            rotations[edge.first] = rotations[edge.second] @ relative
            known.add(edge.first)
    return rotations


def _project(rays: np.ndarray, matrix: np.ndarray, distortion: np.ndarray) -> np.ndarray:
    normalized = rays[:, :2] / np.maximum(rays[:, 2:3], 0.05)
    radius = np.minimum(np.sum(normalized**2, axis=1), 25)
    factor = 1 + distortion[0] * radius + distortion[1] * radius**2
    return normalized * factor[:, None] * np.array([matrix[0, 0], matrix[1, 1]]) + matrix[:2, 2]


def _fit(
    features: list[_FeatureImage], edges: list[_Edge], progress: Any, cancelled: Any
) -> tuple[dict, list[np.ndarray], dict]:
    first, second, pixels_a, pixels_b, holdout = _split_tracks(edges)
    if int(holdout.sum()) < 20 or int((~holdout).sum()) < 50:
        raise ReconstructionError("insufficient_validation", "Not enough independent visual tracks")
    count = len(features)
    width = min(ANALYSIS_WIDTH, features[0].width)
    height = round(features[0].height * width / features[0].width)
    training = ~holdout
    fit_first, fit_second = first[training], second[training]
    fit_a, fit_b = pixels_a[training], pixels_b[training]
    # Every edge needs training evidence; no holdout is moved into fitting.
    training_edges = []
    for edge in edges:
        selected = training & (first == edge.first) & (second == edge.second)
        if int(selected.sum()) >= 10:
            training_edges.append(
                _Edge(
                    edge.first,
                    edge.second,
                    np.empty(0),
                    np.empty(0),
                    pixels_a[selected],
                    pixels_b[selected],
                    edge.homography,
                )
            )
    supported = _connected(count, training_edges)[0]
    if len(supported) != count:
        error = ReconstructionError(
            "insufficient_validation", "Reserved tracks disconnect the fitting graph"
        )
        error.supported_indices = supported
        raise error
    parameter_count = 4 + (count - 1) * 3
    priors_count = 3
    sparsity = lil_matrix((len(fit_a) * 4 + priors_count, parameter_count), dtype=int)
    sparsity[:, :4] = 1
    for match, (left, right) in enumerate(zip(fit_first, fit_second, strict=True)):
        for view in (left, right):
            if view:
                sparsity[match * 4 : match * 4 + 4, 4 + (view - 1) * 3 : 4 + view * 3] = 1
    lower = np.r_[math.log(width * 0.30), -0.70, 0.0, -0.12, np.full((count - 1) * 3, -0.8)]
    upper = np.r_[math.log(width * 4.0), 0.60, 0.50, 0.12, np.full((count - 1) * 3, 0.8)]

    def rotations_for(parameters: np.ndarray, initial: np.ndarray) -> np.ndarray:
        delta = Rotation.from_rotvec(parameters[4:].reshape(-1, 3)).as_matrix()
        return np.concatenate((np.eye(3)[None], initial[1:] @ delta))

    def errors(
        parameters: np.ndarray,
        initial: np.ndarray,
        a: np.ndarray,
        b: np.ndarray,
        left: np.ndarray,
        right: np.ndarray,
    ) -> np.ndarray:
        matrix, distortion = _intrinsics(parameters, width, height)
        rotations = rotations_for(parameters, initial)
        local_a, local_b = _local_rays(a, matrix, distortion), _local_rays(b, matrix, distortion)
        global_a = np.einsum("nij,nj->ni", rotations[left], local_a)
        global_b = np.einsum("nij,nj->ni", rotations[right], local_b)
        prediction_b = np.einsum("nji,nj->ni", rotations[right], global_a)
        prediction_a = np.einsum("nji,nj->ni", rotations[left], global_b)
        return np.column_stack(
            (
                _project(prediction_a, matrix, distortion) - a,
                _project(prediction_b, matrix, distortion) - b,
            )
        )

    candidates = []
    for candidate_index, (focal_ratio, radial) in enumerate(
        ((0.65, 0.0), (0.85, -0.30), (1.35, 0.0))
    ):
        _check(cancelled)
        initial_parameters = np.r_[
            math.log(width * focal_ratio),
            radial,
            0.08 if radial else 0.0,
            0.0,
            np.zeros((count - 1) * 3),
        ]
        matrix, distortion = _intrinsics(initial_parameters, width, height)
        initial = _initial_rotations(count, training_edges, matrix, distortion)

        def residual(parameters: np.ndarray) -> np.ndarray:
            _check(cancelled)
            data = errors(parameters, initial, fit_a, fit_b, fit_first, fit_second)
            # Weak scene-independent assumptions, never native-position degrees.
            priors = np.array(
                [parameters[3] * 20, parameters[2] * 2, (parameters[0] - math.log(width)) * 0.10]
            )
            return np.r_[data.ravel(), priors]

        result = least_squares(
            residual,
            initial_parameters,
            bounds=(lower, upper),
            loss="soft_l1",
            f_scale=1.5,
            jac_sparsity=sparsity.tocsr(),
            max_nfev=85,
            ftol=1e-6,
            xtol=1e-6,
        )
        value = errors(result.x, initial, fit_a, fit_b, fit_first, fit_second)
        score = float(np.median(np.linalg.norm(value.reshape(-1, 2), axis=1)))
        candidates.append((score, result, initial))
        _notify(
            progress,
            "estimating",
            completed=candidate_index + 1,
            total=3,
            training_error_pixels=score,
        )
    _, result, initial = min(candidates, key=lambda value: value[0])
    parameters = result.x
    matrix, distortion = _intrinsics(parameters, width, height)
    rotations = rotations_for(parameters, initial)
    residuals = errors(parameters, initial, pixels_a, pixels_b, first, second)
    pixel_errors = np.max(np.linalg.norm(residuals.reshape(-1, 2, 2), axis=2), axis=1)
    heldout_errors = pixel_errors[holdout]
    train_errors = pixel_errors[~holdout]
    global_a = np.einsum("nij,nj->ni", rotations[first], _local_rays(pixels_a, matrix, distortion))
    global_b = np.einsum("nij,nj->ni", rotations[second], _local_rays(pixels_b, matrix, distortion))
    angular_errors = np.degrees(
        np.arctan2(
            np.linalg.norm(np.cross(global_a, global_b), axis=1),
            np.sum(global_a * global_b, axis=1),
        )
    )
    # Check radial invertibility over the observed optical domain, not an
    # arbitrary unit disk. Brown derivatives cannot fold within photographed rays.
    maximum_radius = _maximum_optical_radius(matrix, distortion, width, height)
    radius_squared = np.linspace(0, maximum_radius**2, 100)
    monotonic = bool(
        np.all(
            1 + 3 * distortion[0] * radius_squared + 5 * distortion[1] * radius_squared**2 > 0.05
        )
    )
    # The intrinsic block after projecting out pose is an observability diagnostic;
    # use the data rows only, so regularization cannot manufacture confidence.
    jacobian = result.jac[:-priors_count]
    intrinsic_jacobian = jacobian[:, :4].toarray()
    pose_jacobian = jacobian[:, 4:]
    # Do not densify the pose Jacobian: its dimensions grow with captures and
    # observations. Four sparse solves leave only an observations-by-four array.
    projected = np.column_stack(
        [
            intrinsic_jacobian[:, column]
            - pose_jacobian
            @ lsmr(pose_jacobian, intrinsic_jacobian[:, column], atol=1e-7, btol=1e-7, maxiter=250)[
                0
            ]
            for column in range(4)
        ]
    )
    singular = np.linalg.svd(projected, compute_uv=False)
    condition = float(singular[0] / singular[-1]) if singular[-1] > 1e-7 else 1e16
    bound_hit = bool(
        np.any((parameters[[0, 1, 3]] - lower[[0, 1, 3]]) < 0.01)
        or np.any((upper[:4] - parameters[:4]) < 0.01)
    )
    reasons = []
    if not monotonic:
        raise ReconstructionError(
            "noninvertible_lens", "Estimated lens folds inside the photographed image"
        )
    if float(np.quantile(heldout_errors, 0.95)) > 8.0 * width / ANALYSIS_WIDTH:
        reasons.append("independent_alignment_error")
    if condition > 100_000 or bound_hit:
        reasons.append("weak_intrinsic_observability")
    if not result.success:
        reasons.append("optimizer_budget_reached")
    if len(edges) < count:
        reasons.append("no_redundant_overlap")
    scale = features[0].width / width
    lens = {
        "width": features[0].width,
        "height": features[0].height,
        "fx": float(matrix[0, 0] * scale),
        "fy": float(matrix[1, 1] * scale),
        "cx": float((matrix[0, 2] + 0.5) * scale - 0.5),
        "cy": float((matrix[1, 2] + 0.5) * scale - 0.5),
        "distortion": distortion.tolist(),
    }
    quality = {
        "status": "review" if reasons else "ready",
        "reasons": reasons,
        "evaluation_width": width,
        "training_tracks_observations": int(training.sum()),
        "holdout_tracks_observations": int(holdout.sum()),
        "holdout_unit": "complete_feature_track",
        "holdout_scope": "verified_correspondences_not_independent_hardware_ground_truth",
        "training_error_pixels": dict(
            zip(("p50", "p95"), map(float, np.quantile(train_errors, (0.5, 0.95))), strict=True)
        ),
        "holdout_error_pixels": dict(
            zip(("p50", "p95"), map(float, np.quantile(heldout_errors, (0.5, 0.95))), strict=True)
        ),
        "holdout_error_degrees": dict(
            zip(
                ("p50", "p95"),
                map(float, np.quantile(angular_errors[holdout], (0.5, 0.95))),
                strict=True,
            )
        ),
        "intrinsic_condition_number": condition,
        "lens_monotonic": monotonic,
        "verified_pairs": len(edges),
        "optimizer_success": bool(result.success),
        "threshold_policy": "experimental_v1_8px_at_960_holdout_p95_not_actuator_precision",
    }
    return lens, list(rotations), quality


def _horizontal_axis_evidence(
    rotations: list[np.ndarray], features: list[_FeatureImage], mean_up: np.ndarray
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """Estimate presentation up from observed pan-only capture transitions."""
    axes: list[np.ndarray] = []
    angles: list[float] = []
    for first, second in zip(range(len(features) - 1), range(1, len(features)), strict=True):
        first_hint, second_hint = features[first].hints, features[second].hints
        movement = second_hint.get("movement")
        if isinstance(movement, dict):
            if movement.get("axis") != "pan" or movement.get("anchor_capture_id") not in {
                None, features[first].identifier
            }:
                continue
        else:
            first_target, second_target = (
                first_hint.get("requested_target"), second_hint.get("requested_target")
            )
            absolute_pan = (
                isinstance(first_target, dict)
                and isinstance(second_target, dict)
                and all(
                    isinstance(target.get(axis), int | float) and not isinstance(target.get(axis), bool)
                    for target in (first_target, second_target)
                    for axis in ("pan", "tilt")
                )
                and abs(float(first_target["tilt"]) - float(second_target["tilt"])) <= 0.002
                and abs(float(first_target["pan"]) - float(second_target["pan"])) > 1e-5
            )
            overlap = second_hint.get("previous_overlap")
            shift_x = overlap.get("shift_x") if isinstance(overlap, dict) else None
            shift_y = overlap.get("shift_y") if isinstance(overlap, dict) else None
            observed_pan = (
                first_hint.get("row_index") == second_hint.get("row_index")
                and type(first_hint.get("row_index")) is int
                and isinstance(shift_x, int | float) and not isinstance(shift_x, bool)
                and isinstance(shift_y, int | float) and not isinstance(shift_y, bool)
                and abs(float(shift_x)) >= max(2.0, 2.0 * abs(float(shift_y)))
            )
            if not absolute_pan and not observed_pan:
                continue
        vector = Rotation.from_matrix(
            rotations[second] @ rotations[first].T
        ).as_rotvec()
        angle = float(np.linalg.norm(vector))
        if not math.radians(2) <= angle < math.radians(165):
            continue
        axis = vector / angle
        if np.dot(axis, mean_up) < 0:
            axis = -axis
        axes.append(axis)
        angles.append(angle)

    evidence: dict[str, Any] = {
        "horizontal_motion_pairs": len(axes),
        "horizontal_rotation_degrees": math.degrees(sum(angles)),
        "axis_divergence_degrees_p95": None,
    }
    if not axes:
        return None, evidence
    candidate = np.mean(axes, axis=0)
    candidate /= max(float(np.linalg.norm(candidate)), 1e-12)
    divergence = np.degrees(
        np.arccos(np.clip(np.abs(np.asarray(axes) @ candidate), 0.0, 1.0))
    )
    evidence["axis_divergence_degrees_p95"] = float(np.percentile(divergence, 95))
    verified = (
        len(axes) >= 3
        and sum(angles) >= math.radians(10)
        and evidence["axis_divergence_degrees_p95"] <= 3.0
    )
    return (candidate if verified else None), evidence


def _presentation_frame(
    rotations: list[np.ndarray], features: list[_FeatureImage]
) -> tuple[list[np.ndarray], dict]:
    """Reorient the sphere using verified pan motion, never claimed gravity."""
    mean_up = np.mean([rotation @ [0.0, -1.0, 0.0] for rotation in rotations], axis=0)
    up = mean_up / max(float(np.linalg.norm(mean_up)), 1e-12)
    method = "mean_image_up"
    horizontal_up, evidence = _horizontal_axis_evidence(rotations, features, mean_up)
    if horizontal_up is not None:
        up = horizontal_up
        method = "verified_mechanical_pan_axis"
    forward = np.mean([rotation[:, 2] for rotation in rotations], axis=0)
    forward -= up * np.dot(forward, up)
    if np.linalg.norm(forward) < 0.10:
        forward = rotations[0][:, 2] - up * np.dot(rotations[0][:, 2], up)
    if np.linalg.norm(forward) < 1e-6:
        return rotations, {
            "method": "first_camera",
            "status": "unverified",
            "gravity_verified": False,
            "rotation_matrix": np.eye(3).tolist(),
            **evidence,
        }
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, up)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    transform = np.column_stack((right, down, forward)).T
    return [transform @ rotation for rotation in rotations], {
        "method": method,
        "status": "verified" if horizontal_up is not None else "unverified",
        "gravity_verified": False,
        "rotation_matrix": transform.tolist(),
        **evidence,
    }


def _exposure_gains(features: list[_FeatureImage], edges: list[_Edge]) -> list[float]:
    rows, values = [], []
    for edge in edges:
        a = features[edge.first].brightness[edge.first_keys].astype(float)
        b = features[edge.second].brightness[edge.second_keys].astype(float)
        valid = (a > 12) & (a < 240) & (b > 12) & (b < 240)
        if int(valid.sum()) < 12:
            continue
        row = np.zeros(len(features))
        row[edge.first], row[edge.second] = 1, -1
        rows.append(row)
        values.append(float(np.median(np.log(b[valid] / a[valid]))))
    if not rows:
        return [1.0] * len(features)
    rows.append(np.ones(len(features)))
    values.append(0.0)
    gains = np.linalg.lstsq(np.asarray(rows), np.asarray(values), rcond=1e-8)[0]
    return np.exp(np.clip(gains, math.log(0.75), math.log(1.33))).tolist()


def _overlay_mask(feature: _FeatureImage) -> np.ndarray:
    mask = np.full((feature.height, feature.width), 255, np.uint8)
    scale = feature.width / min(ANALYSIS_WIDTH, feature.width)
    for x, y in feature.overlay_points:
        cv2.circle(
            mask,
            (round((x + 0.5) * scale - 0.5), round((y + 0.5) * scale - 0.5)),
            max(2, round(5 * scale)),
            0,
            -1,
        )
    return mask


def _warp(
    feature: _FeatureImage,
    lens: dict,
    rotation: np.ndarray,
    width: int,
    height: int,
    cancelled: Any,
    *,
    row_start: int = 0,
    row_end: int | None = None,
    include_image: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = cv2.imread(str(feature.path), cv2.IMREAD_COLOR) if include_image else None
    if include_image and (image is None or image.shape[:2] != (feature.height, feature.width)):
        raise ReconstructionError("image_changed", "Photograph changed during reconstruction")
    row_end = height if row_end is None else row_end
    row_count = row_end - row_start
    input_mask = _overlay_mask(feature)
    output = (
        np.zeros((row_count, width, 3), np.uint8)
        if include_image
        else np.empty((0, 0, 3), np.uint8)
    )
    mask = np.zeros((row_count, width), np.uint8)
    scores = np.zeros((row_count, width), np.float32)
    basis = _rotation_basis(rotation).astype(np.float32)
    matrix = np.array([[lens["fx"], 0, lens["cx"]], [0, lens["fy"], lens["cy"]], [0, 0, 1.0]])
    maximum_radius_squared = (
        _maximum_optical_radius(
            matrix, np.asarray(lens["distortion"]), feature.width, feature.height
        )
        ** 2
    )
    pan = ((np.arange(width, dtype=np.float32) + 0.5) / width * 2 - 1) * np.pi
    k1, k2 = lens["distortion"][:2]
    for top in range(row_start, row_end, 64):
        _check(cancelled)
        bottom = min(row_end, top + 64)
        target = slice(top - row_start, bottom - row_start)
        tilt = (0.5 - (np.arange(top, bottom, dtype=np.float32) + 0.5) / height) * np.pi
        directions = np.stack(
            np.broadcast_arrays(
                np.cos(tilt[:, None]) * np.cos(pan),
                np.cos(tilt[:, None]) * np.sin(pan),
                np.sin(tilt[:, None]) * np.ones_like(pan),
            ),
            axis=-1,
        )
        local = directions @ basis
        x = local[:, :, 0] / np.maximum(local[:, :, 2], 0.05)
        y = local[:, :, 1] / np.maximum(local[:, :, 2], 0.05)
        radius = x * x + y * y
        factor = 1 + k1 * radius + k2 * radius**2
        map_x = (x * factor * lens["fx"] + lens["cx"]).astype(np.float32)
        map_y = (y * factor * lens["fy"] + lens["cy"]).astype(np.float32)
        valid = (
            (local[:, :, 2] > 0.05)
            & (radius <= maximum_radius_squared * 1.001)
            & (factor > 0)
            & (1 + 3 * k1 * radius + 5 * k2 * radius**2 > 0.05)
            & (map_x >= 0)
            & (map_x < feature.width - 1)
            & (map_y >= 0)
            & (map_y < feature.height - 1)
        )
        map_x[~valid] = -1
        map_y[~valid] = -1
        if include_image:
            output[target] = cv2.remap(image, map_x, map_y, cv2.INTER_LINEAR)
        strip_mask = cv2.remap(input_mask, map_x, map_y, cv2.INTER_NEAREST)
        valid &= strip_mask > 0
        mask[target] = valid.astype(np.uint8) * 255
        scores[target] = np.where(valid, local[:, :, 2], 0)
    return output, mask, scores


def _render(
    features: list[_FeatureImage],
    lens: dict,
    rotations: list[np.ndarray],
    gains: list[float],
    output_dir: Path,
    progress: Any,
    cancelled: Any,
) -> dict:
    width, height = OUTPUT_WIDTH, OUTPUT_HEIGHT
    sources = np.full((height, width), -1, np.int16)
    best = np.zeros((height, width), np.float32)
    # Memory stays bounded by output dimensions and one decoded source. Originals
    # are reopened in the second pass instead of accumulating warped 4K images.
    for index, (feature, rotation) in enumerate(zip(features, rotations, strict=True)):
        warped, mask, scores = _warp(
            feature, lens, rotation, width, height, cancelled, include_image=False
        )
        chosen = (mask > 0) & (scores > best)
        sources[chosen], best[chosen] = index, scores[chosen]
        del warped, mask, scores
        _notify(progress, "projecting", completed=index + 1, total=len(features))
    coverage = (sources >= 0).astype(np.uint8) * 255
    del best, chosen
    occupied = np.any(coverage, axis=0)
    if not np.any(occupied):
        raise ReconstructionError("empty_coverage", "No photographed rays could be projected")
    if not np.all(occupied):
        empty = np.concatenate((~occupied, ~occupied))
        starts = np.flatnonzero(np.diff(np.r_[False, empty, False].astype(int)) == 1)
        ends = np.flatnonzero(np.diff(np.r_[False, empty, False].astype(int)) == -1)
        run = max(zip(starts, ends, strict=True), key=lambda pair: pair[1] - pair[0])
        seam = int((run[0] + min(run[1] - run[0], width) / 2) % width)
    else:
        weights = np.cos((0.5 - (np.arange(height) + 0.5) / height) * np.pi)
        column_support = np.einsum("ij,i->j", coverage, weights) / 255
        radius = max(1, width // 128)
        padded = np.pad(column_support, radius, mode="wrap")
        smoothed = np.convolve(padded, np.ones(radius * 2 + 1), mode="valid")
        seam = int(np.argmin(smoothed))
    shift = (-seam) % width
    azimuth = shift * 2 * math.pi / width
    yaw_rotation = Rotation.from_euler("y", azimuth).as_matrix()
    rotations[:] = [yaw_rotation @ rotation for rotation in rotations]
    sources = np.roll(sources, shift, axis=1)
    coverage = np.roll(coverage, shift, axis=1)
    panorama = np.zeros((height, width, 3), np.uint8)
    kernel = np.ones((31, 31), np.uint8)
    # The blender is also bounded: full-sphere multiband pyramids can consume
    # nearly a gigabyte despite streaming original images. Overlapping strips
    # retain five-band support while avoiding one giant pyramid allocation.
    for top in range(0, height, 128):
        bottom = min(height, top + 128)
        halo_top, halo_bottom = max(0, top - 64), min(height, bottom + 64)
        if not np.any(coverage[halo_top:halo_bottom]):
            continue
        blender = cv2.detail_MultiBandBlender(0, 5)
        blender.prepare((0, 0, width, halo_bottom - halo_top))
        for index, (feature, rotation) in enumerate(zip(features, rotations, strict=True)):
            primary = (sources[halo_top:halo_bottom] == index).astype(np.uint8) * 255
            if not np.any(primary):
                continue
            warped, valid, scores = _warp(
                feature,
                lens,
                rotation,
                width,
                height,
                cancelled,
                row_start=halo_top,
                row_end=halo_bottom,
            )
            del scores
            warped = cv2.convertScaleAbs(warped, alpha=gains[index])
            padded = np.pad(primary, ((0, 0), (16, 16)), mode="wrap")
            blend_mask = cv2.dilate(padded, kernel)[:, 16:-16] & valid
            if np.any(blend_mask):
                blender.feed(warped.astype(np.int16), blend_mask, (0, 0))
            del warped, valid, primary, padded, blend_mask
        _check(cancelled)
        blended, _ = blender.blend(None, None)
        del blender
        center = blended[top - halo_top : bottom - halo_top]
        panorama[top:bottom] = np.clip(center, 0, 255).astype(np.uint8)
        del center, blended
        _notify(progress, "blending", completed=bottom, total=height)
    panorama[coverage == 0] = 0
    _write_image(output_dir / "panorama.png", panorama)
    _write_image(output_dir / "coverage.png", coverage)
    _write_image(
        output_dir / "thumbnail.jpg",
        cv2.resize(panorama, (1024, 512), interpolation=cv2.INTER_AREA),
    )
    temporary = output_dir / "source-indices.partial.npy"
    np.save(temporary, sources, allow_pickle=False)
    temporary.replace(output_dir / "source-indices.npy")
    rows = np.flatnonzero(np.any(coverage, axis=1))
    columns = np.flatnonzero(np.any(coverage, axis=0))
    if not len(rows):
        raise ReconstructionError("empty_coverage", "No photographed rays could be projected")
    weights = np.cos((0.5 - (np.arange(height) + 0.5) / height) * np.pi)
    return {
        "pixel_ratio": float(np.count_nonzero(coverage) / (width * height)),
        "solid_angle_ratio": float(
            np.dot(np.count_nonzero(coverage, axis=1), weights) / (width * np.sum(weights))
        ),
        "bounds_pixels": {
            "left": int(columns.min()),
            "top": int(rows.min()),
            "right": int(columns.max()) + 1,
            "bottom": int(rows.max()) + 1,
        },
        "domain_status": "unknown_from_photographs",
        "complete": False,
        "provenance": "dominant_geometric_source_before_multiband",
        "presentation_azimuth_shift_radians": azimuth,
    }


def reconstruct_panorama(
    captures: list[dict],
    output_dir: Path,
    *,
    progress: Callable[[dict], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Reconstruct photographs without manual lens parameters or native degrees.

    Files are relative to output_dir; no source path or credential is exported.
    ``ready`` means the visual quality gate passed, not full mechanical coverage.
    The acquisition service owns completeness and temporal/physical evidence.
    """
    _check(cancelled)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    features = _read_inputs(captures, progress, cancelled)
    edges = _match(features, progress, cancelled)
    components = _connected(len(features), edges)
    if len(components[0]) < 2:
        raise ReconstructionError(
            "insufficient_overlap", "Photographs have insufficient verified overlap"
        )
    selected = components[0]
    omitted = [
        feature.identifier for number, feature in enumerate(features) if number not in selected
    ]
    renumber = {old: new for new, old in enumerate(selected)}
    edges = [
        _Edge(
            renumber[edge.first],
            renumber[edge.second],
            edge.first_keys,
            edge.second_keys,
            edge.first_pixels,
            edge.second_pixels,
            edge.homography,
        )
        for edge in edges
        if edge.first in renumber and edge.second in renumber
    ]
    features = [features[index] for index in selected]
    while True:
        try:
            lens, rotations, quality = _fit(features, edges, progress, cancelled)
            break
        except ReconstructionError as error:
            supported = error.supported_indices
            if supported is None or len(supported) < 2 or len(supported) >= len(features):
                raise
            # Preserve a useful partial artifact without reclassifying check
            # tracks as training or trusting unsupported photograph positions.
            omitted.extend(
                feature.identifier
                for index, feature in enumerate(features)
                if index not in supported
            )
            renumber = {old: new for new, old in enumerate(supported)}
            edges = [
                _Edge(
                    renumber[edge.first],
                    renumber[edge.second],
                    edge.first_keys,
                    edge.second_keys,
                    edge.first_pixels,
                    edge.second_pixels,
                    edge.homography,
                )
                for edge in edges
                if edge.first in renumber and edge.second in renumber
            ]
            features = [features[index] for index in supported]
    rotations, presentation = _presentation_frame(rotations, features)
    gains = _exposure_gains(features, edges)
    overlap_links = [
        [features[edge.first].identifier, features[edge.second].identifier]
        for edge in edges
    ]
    for feature in features:
        feature.points = np.empty((0, 2))
        feature.descriptors = np.empty((0, 128), np.float32)
        feature.brightness = np.empty(0)
    edges.clear()
    if omitted:
        quality["reasons"].append("disconnected_captures")
        quality["status"] = "review"
    model = {
        "algorithm_version": ALGORITHM_VERSION,
        "lens": lens,
        "status": "estimated_for_visual_panorama_only",
        "positioning_status": "not_validated",
        "reference_frame": "estimated_presentation_right_down_forward_SO3_then_canonical_panorama_basis",
        "presentation": presentation,
        "exposure_gains": gains,
        "origin": "conventional_without_physical_scale_or_translation",
        "overlap_links": overlap_links,
        "captures": [
            {"id": feature.identifier, "rotation_matrix": rotation.tolist()}
            for feature, rotation in zip(features, rotations, strict=True)
        ],
    }
    coverage = _render(features, lens, rotations, gains, output_dir, progress, cancelled)
    model["captures"] = [
        {"id": feature.identifier, "rotation_matrix": rotation.tolist()}
        for feature, rotation in zip(features, rotations, strict=True)
    ]
    yaw_rotation = Rotation.from_euler(
        "y", coverage["presentation_azimuth_shift_radians"]
    ).as_matrix()
    model["presentation"]["rotation_matrix"] = (
        yaw_rotation @ np.asarray(presentation["rotation_matrix"])
    ).tolist()
    model["presentation"]["seam_method"] = "largest_empty_azimuth_gap_or_least_solid_angle_support"
    files = {
        "panorama": "panorama.png",
        "thumbnail": "thumbnail.jpg",
        "coverage": "coverage.png",
        "source_indices": "source-indices.npy",
        "model": "model.json",
        "report": "report.json",
    }
    report = {
        "status": "ready" if quality["status"] == "ready" else "partial",
        "algorithm_version": ALGORITHM_VERSION,
        "positioning_status": "not_validated",
        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "source_ids": [feature.identifier for feature in features],
        "omitted_source_ids": omitted,
        "model": model,
        "quality": quality,
        "coverage": coverage,
        "files": files,
    }
    _check(cancelled)
    _write_json(output_dir / "model.json", model)
    _write_json(output_dir / "report.json", report)
    _notify(progress, "complete", status=report["status"])
    return report
