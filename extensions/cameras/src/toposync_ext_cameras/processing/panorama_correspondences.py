"""Image-space qualification shared by reconstruction and live localization."""
from __future__ import annotations

import numpy as np
import cv2


def track_image_points(previous: np.ndarray, current: np.ndarray, points: np.ndarray,
                       maximum_error: float) -> tuple[np.ndarray, np.ndarray]:
    """Bidirectional image measurements; never infer missing point positions."""
    source = np.asarray(points, np.float32).reshape(-1, 2)
    invalid = np.zeros(len(source), dtype=bool)
    if not len(source) or previous.shape != current.shape:
        return source.copy(), invalid
    parameters = dict(winSize=(21, 21), maxLevel=3,
                      criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    following, status, _ = cv2.calcOpticalFlowPyrLK(
        previous, current, source.reshape(-1, 1, 2), None, **parameters)
    if following is None or status is None:
        return source.copy(), invalid
    returning, back_status, _ = cv2.calcOpticalFlowPyrLK(
        current, previous, following, None, **parameters)
    if returning is None or back_status is None:
        return source.copy(), invalid
    target = following.reshape(-1, 2)
    height, width = previous.shape
    valid = (status.ravel().astype(bool) & back_status.ravel().astype(bool)
             & (np.linalg.norm(returning.reshape(-1, 2) - source, axis=1) <= maximum_error)
             & np.isfinite(target).all(axis=1)
             & (target[:, 0] >= 0) & (target[:, 0] < width)
             & (target[:, 1] >= 0) & (target[:, 1] < height))
    return target, valid


def stationary_overlay_points(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Find screen-fixed features contradicted by nearby, coherently moving scene.

    Coordinates use the shared 960-pixel analysis scale. Neither a stationary
    camera nor a point near a rotation axis is sufficient evidence of an overlay.
    This test precedes pose estimation and never examines validation residuals.
    """
    vectors = second - first
    displacement = np.linalg.norm(vectors, axis=1)
    excluded = np.zeros(len(first), dtype=bool)
    if len(first) < 12 or np.median(displacement) <= 12:
        return excluded
    moving = np.flatnonzero(displacement > 12)
    for index in np.flatnonzero(displacement < 1.25):
        distances = np.linalg.norm(first[moving] - first[index], axis=1)
        neighbours = moving[np.argsort(distances, kind="stable")[:8]]
        neighbours = neighbours[np.linalg.norm(first[neighbours] - first[index], axis=1) < 240]
        if len(neighbours) < 6:
            continue
        local = vectors[neighbours]
        predicted = np.median(local, axis=0)
        magnitude = np.linalg.norm(predicted)
        excluded[index] = magnitude > 12 and np.percentile(
            np.linalg.norm(local - predicted, axis=1), 75
        ) < magnitude * 0.4
    return excluded
