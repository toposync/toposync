"""Geometry for the pinned MediaPipe palm export, without person detection.

Model contract: OpenCV Zoo, revision 47534e27c9851bb1128ccc0102f1145e27f23f98,
models/palm_detection_mediapipe. This implementation generates the fixed anchor
grid, uses actual letterbox dimensions, and corrects the reference's xyxy/Rect
suppression mismatch. It does not assign a hand to a person or an anatomical side.
"""

from __future__ import annotations

import numpy as np


def _anchors() -> np.ndarray:
    grids = []
    for side, repetitions in ((24, 2), (12, 6)):
        y, x = np.mgrid[:side, :side]
        centres = np.column_stack((x.ravel() + .5, y.ravel() + .5)) / side
        grids.append(np.repeat(centres, repetitions, axis=0))
    result = np.concatenate(grids)
    result.flags.writeable = False
    return result


_PALM_ANCHORS = _anchors()


def prepare_palm_input(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return RGB NHWC tensor and inverse letterbox in continuous edge pixels.

The export reports continuous coordinates relative to a 192-pixel image extent.
The inverse maps that extent to the original image extent, including fractional
padding offsets. It does not clip model predictions to the sensor boundary.
    """
    import cv2

    image = np.asarray(frame)
    if (image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8
            or min(image.shape[:2]) < 2):
        raise ValueError("Palm inference requires a nonempty uint8 BGR image")
    height, width = image.shape[:2]
    extent = max(height, width)
    resized_height, resized_width = height * 192 // extent, width * 192 // extent
    if min(resized_height, resized_width) < 1:
        raise ValueError("BGR image aspect ratio leaves no supported palm input")
    left, top = (192 - resized_width) // 2, (192 - resized_height) // 2
    image = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    image = cv2.copyMakeBorder(image, top, 192 - resized_height - top,
                              left, 192 - resized_width - left,
                              cv2.BORDER_CONSTANT, value=(0, 0, 0))
    tensor = np.ascontiguousarray(image[:, :, ::-1], dtype=np.float32)[None] / 255
    scale_x, scale_y = width / resized_width, height / resized_height
    inverse = np.array([[scale_x, 0, -left * scale_x],
                        [0, scale_y, -top * scale_y], [0, 0, 1]], dtype=np.float64)
    return tensor, inverse


def decode_palms(
    regression: np.ndarray,
    logits: np.ndarray,
    input_to_image: np.ndarray,
    *,
    score_threshold: float = .5,
    overlap_threshold: float = .3,
) -> np.ndarray:
    """Decode [box xyxy, seven ordered xy points, raw model confidence].

Only the inverse of axis-aligned positive-scale letterboxing is accepted here.
Warp/crop-to-stream composition belongs to the caller's image geometry chain.
Malformed model output raises instead of silently becoming a negative frame.
    """
    import cv2

    regression = np.asarray(regression, dtype=np.float64)
    logits = np.asarray(logits, dtype=np.float64)
    matrix = np.asarray(input_to_image, dtype=np.float64)
    if regression.shape != (1, 2016, 18) or logits.shape != (1, 2016, 1):
        raise ValueError("Palm output shape does not match the pinned export")
    if not all(np.isfinite(value).all() for value in (regression, logits, matrix)):
        raise ValueError("Palm output and transform must be finite")
    if (matrix.shape != (3, 3) or not np.array_equal(matrix[2], [0, 0, 1])
            or matrix[0, 1] != 0 or matrix[1, 0] != 0
            or matrix[0, 0] <= 0 or matrix[1, 1] <= 0):
        raise ValueError("Palm transform must invert an axis-aligned letterbox")
    if not 0 <= score_threshold <= 1 or not 0 <= overlap_threshold <= 1:
        raise ValueError("Palm confidence and overlap thresholds must be in [0, 1]")
    scores = np.exp(-np.logaddexp(0, -logits[0, :, 0]))
    eligible = np.flatnonzero(scores > score_threshold)
    if not len(eligible):
        return np.empty((0, 19), dtype=np.float64)
    values = regression[0, eligible]
    if np.any(values[:, 2:4] <= 0):
        raise ValueError("Accepted palm boxes must have positive width and height")
    centres = values[:, :2] + _PALM_ANCHORS[eligible] * 192
    half_size = values[:, 2:4] / 2
    scale, offset = np.diag(matrix)[:2], matrix[:2, 2]
    with np.errstate(over="ignore", invalid="ignore"):
        boxes = np.column_stack(((centres - half_size) * scale + offset,
                                 (centres + half_size) * scale + offset))
        landmarks = (values[:, 4:].reshape(-1, 7, 2)
                     + _PALM_ANCHORS[eligible, None, :] * 192) * scale + offset
    if not np.isfinite(boxes).all() or not np.isfinite(landmarks).all():
        raise ValueError("Decoded palm coordinates must be finite")
    # OpenCV NMSBoxes takes Rect(x, y, width, height), NOT opposing corners.
    # https://docs.opencv.org/4.x/d2/d44/classcv_1_1Rect__.html
    rectangles = boxes.copy()
    with np.errstate(over="ignore", invalid="ignore"):
        rectangles[:, 2:] -= rectangles[:, :2]
    if not np.isfinite(rectangles[:, 2:]).all() or np.any(rectangles[:, 2:] <= 0):
        raise ValueError("Decoded palm boxes must retain finite positive dimensions")
    selected = np.asarray(cv2.dnn.NMSBoxes(
        rectangles.tolist(), scores[eligible].tolist(), score_threshold, overlap_threshold,
    ), dtype=int).reshape(-1)
    return np.column_stack((boxes[selected], landmarks[selected].reshape(-1, 14),
                            scores[eligible[selected]]))
