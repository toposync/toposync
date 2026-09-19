"""Compact, non-raster visual anchors for interrupted PTZ recovery.

An anchor deliberately stores no image pixels. It retains a bounded set of
SIFT locations and descriptors so a later frame can be checked using the same
Lowe-ratio and RANSAC geometry gates as panorama correspondence. Persistence,
encryption and retention are responsibilities of the caller.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import zlib
from collections.abc import Mapping
from typing import Any

import cv2
import numpy as np


ANCHOR_SCHEMA_VERSION = 1
ANCHOR_ALGORITHM = "sift_lowe_ransac_v1"
ANALYSIS_WIDTH = 960
DESCRIPTOR_WIDTH = 128
MINIMUM_FEATURES = 24
MAXIMUM_FEATURES = 768
_RATIO_LIMIT = 0.7
_RANSAC_REPROJECTION_THRESHOLD = 2.5


def create_visual_anchor(image: np.ndarray) -> dict[str, Any]:
    """Return a bounded visual anchor or a typed rejection.

    The returned mapping does not contain the input raster.  Its binary feature
    payload is checksummed so a partial or substituted persisted anchor can
    never be used as positive recovery evidence.
    """
    gray = _analysis_gray(image)
    sift = cv2.SIFT_create(nfeatures=2000, contrastThreshold=0.025)
    keypoints, descriptors = sift.detectAndCompute(gray, None)
    if descriptors is None or len(keypoints) < MINIMUM_FEATURES:
        return {"created": False, "code": "insufficient_texture"}

    ranked = sorted(
        enumerate(keypoints), key=lambda item: (-float(item[1].response), item[0])
    )[:MAXIMUM_FEATURES]
    indexes = np.asarray([index for index, _keypoint in ranked], dtype=np.intp)
    points = np.asarray([keypoints[index].pt for index in indexes], dtype=np.float32)
    selected_descriptors = np.asarray(descriptors[indexes], dtype=np.float32)
    if (
        points.shape != (len(indexes), 2)
        or selected_descriptors.shape != (len(indexes), DESCRIPTOR_WIDTH)
        or not np.isfinite(points).all()
        or not np.isfinite(selected_descriptors).all()
    ):
        return {"created": False, "code": "invalid_feature_payload"}
    support = _support(points, width=gray.shape[1], height=gray.shape[0])
    if not support["distributed"]:
        return {
            "created": False,
            "code": "insufficient_distributed_texture",
            "feature_count": int(len(points)),
            **support,
        }
    digest = _digest(points, selected_descriptors)
    return {
        "created": True,
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "algorithm": ANCHOR_ALGORITHM,
        "analysis_size": [int(gray.shape[1]), int(gray.shape[0])],
        "feature_count": int(len(points)),
        "keypoints_f32_zlib_b64": _pack(points),
        "descriptors_f32_zlib_b64": _pack(selected_descriptors),
        "payload_sha256": digest,
        "support": support,
    }


def match_visual_anchor(anchor: Mapping[str, Any], image: np.ndarray) -> dict[str, Any]:
    """Check a fresh raster against a persisted anchor without restoring it."""
    try:
        points, descriptors, width, height = _decode_anchor(anchor)
    except ValueError:
        return {"verified": False, "code": "visual_anchor_invalid"}
    gray = _analysis_gray(image)
    if gray.shape != (height, width):
        return {"verified": False, "code": "dimensions_changed"}
    sift = cv2.SIFT_create(nfeatures=2000, contrastThreshold=0.025)
    keypoints, current_descriptors = sift.detectAndCompute(gray, None)
    if current_descriptors is None:
        return {"verified": False, "code": "insufficient_texture"}
    current_descriptors = np.asarray(current_descriptors, dtype=np.float32)
    if current_descriptors.ndim != 2 or current_descriptors.shape[1] != DESCRIPTOR_WIDTH:
        return {"verified": False, "code": "invalid_current_features"}
    pairs = cv2.BFMatcher().knnMatch(descriptors, current_descriptors, k=2)
    matches = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and float(pair[0].distance) < float(pair[1].distance) * _RATIO_LIMIT
    ]
    if len(matches) < MINIMUM_FEATURES:
        return {"verified": False, "code": "insufficient_correspondences"}
    source = np.float32([points[match.queryIdx] for match in matches])
    target = np.float32([keypoints[match.trainIdx].pt for match in matches])
    candidates: list[dict[str, Any]] = []
    for _ in range(3):
        if len(source) < MINIMUM_FEATURES:
            return {
                "verified": False,
                "code": "correspondences_not_distributed",
                "model_candidates": candidates,
            }
        homography, inliers = cv2.findHomography(
            source, target, cv2.RANSAC, _RANSAC_REPROJECTION_THRESHOLD
        )
        if homography is None or inliers is None or not np.isfinite(homography).all():
            return {
                "verified": False,
                "code": "correspondence_model_failed",
                "model_candidates": candidates,
            }
        inlier_mask = inliers.ravel().astype(bool)
        support = _support(source[inlier_mask], width=width, height=height)
        candidates.append({"inliers": int(inlier_mask.sum()), **support})
        if support["distributed"]:
            break
        source, target = source[~inlier_mask], target[~inlier_mask]
    else:
        return {
            "verified": False,
            "code": "correspondences_not_distributed",
            "model_candidates": candidates,
        }
    corners = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    transformed = cv2.perspectiveTransform(corners[None], homography)[0]
    if not cv2.isContourConvex(transformed):
        return {"verified": False, "code": "correspondence_model_folded"}
    intersection, _ = cv2.intersectConvexConvex(corners, transformed)
    grid = np.float32(
        [[width * x, height * y] for y in (0.2, 0.5, 0.8) for x in (0.2, 0.5, 0.8)]
    )
    displaced = cv2.perspectiveTransform(grid[None], homography)[0] - grid
    return {
        "verified": True,
        "inliers": int(inlier_mask.sum()),
        "model_candidates": candidates,
        "analysis_size": [width, height],
        "overlap": float(np.clip(intersection / (width * height), 0, 1)),
        "shift_x": float(np.median(displaced[:, 0])),
        "shift_y": float(np.median(displaced[:, 1])),
        "displacement": float(np.percentile(np.linalg.norm(displaced, axis=1), 95)),
        "homography": homography.tolist(),
    }


def visual_anchor_summary(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata safe for logs and reports, excluding feature payloads."""
    return {
        key: anchor.get(key)
        for key in (
            "created",
            "code",
            "schema_version",
            "algorithm",
            "analysis_size",
            "feature_count",
            "payload_sha256",
            "support",
        )
        if key in anchor
    }


def _analysis_gray(image: np.ndarray) -> np.ndarray:
    value = np.asarray(image)
    if value.ndim == 3:
        value = cv2.cvtColor(value, cv2.COLOR_BGR2GRAY)
    if value.ndim != 2 or value.shape[0] < 32 or value.shape[1] < 32:
        raise ValueError("image is too small for a visual anchor")
    height = max(1, round(value.shape[0] * ANALYSIS_WIDTH / value.shape[1]))
    return cv2.resize(value, (ANALYSIS_WIDTH, height), interpolation=cv2.INTER_AREA)


def _support(points: np.ndarray, *, width: int, height: int) -> dict[str, Any]:
    if len(points) < 3:
        area = 0.0
    else:
        area = float(cv2.contourArea(cv2.convexHull(np.asarray(points, dtype=np.float32))))
    cells = {
        (min(3, int(float(x) * 4 / width)), min(2, int(float(y) * 3 / height)))
        for x, y in points
    }
    fraction = area / (width * height)
    return {
        "occupied_cells": len(cells),
        "hull_fraction": fraction,
        "distributed": bool(
            len(points) >= MINIMUM_FEATURES and len(cells) >= 4 and fraction >= 0.12
        ),
    }


def _pack(array: np.ndarray) -> str:
    return base64.b64encode(zlib.compress(np.asarray(array, dtype=np.float32).tobytes())).decode("ascii")


def _unpack(value: Any, *, shape: tuple[int, ...]) -> np.ndarray:
    if not isinstance(value, str) or len(value) > 4 * math.prod(shape) * 4:
        raise ValueError("invalid visual anchor payload")
    try:
        compressed = base64.b64decode(value.encode("ascii"), validate=True)
        decoder = zlib.decompressobj()
        expected_bytes = math.prod(shape) * 4
        data = decoder.decompress(compressed, expected_bytes + 1)
        if (
            len(data) != expected_bytes
            or decoder.unconsumed_tail
            or decoder.unused_data
            or not decoder.eof
        ):
            raise ValueError("invalid visual anchor payload")
    except (ValueError, zlib.error) as error:
        raise ValueError("invalid visual anchor payload") from error
    array = np.frombuffer(data, dtype=np.float32).reshape(shape)
    if not np.isfinite(array).all():
        raise ValueError("invalid visual anchor payload")
    return array


def _digest(points: np.ndarray, descriptors: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(np.asarray(points, dtype=np.float32).tobytes())
    digest.update(np.asarray(descriptors, dtype=np.float32).tobytes())
    return digest.hexdigest()


def _decode_anchor(anchor: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, int, int]:
    if (
        anchor.get("created") is not True
        or anchor.get("schema_version") != ANCHOR_SCHEMA_VERSION
        or anchor.get("algorithm") != ANCHOR_ALGORITHM
    ):
        raise ValueError("unsupported visual anchor")
    size = anchor.get("analysis_size")
    count = anchor.get("feature_count")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(value) is not int or value < 32 for value in size)
        or type(count) is not int
        or not MINIMUM_FEATURES <= count <= MAXIMUM_FEATURES
    ):
        raise ValueError("invalid visual anchor metadata")
    width, height = size
    points = _unpack(anchor.get("keypoints_f32_zlib_b64"), shape=(count, 2))
    descriptors = _unpack(
        anchor.get("descriptors_f32_zlib_b64"), shape=(count, DESCRIPTOR_WIDTH)
    )
    digest = anchor.get("payload_sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(digest, _digest(points, descriptors)):
        raise ValueError("visual anchor checksum mismatch")
    return points, descriptors, width, height
