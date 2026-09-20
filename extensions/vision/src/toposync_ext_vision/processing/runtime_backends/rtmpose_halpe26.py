"""Top-down Halpe26 adapter for the pinned RTMPose M SimCC export.

Geometry follows MMPose 1.1.0 Python's upright affine convention (padding 1.25),
not a claim of MMDeploy SDK sampling or PyTorch/export parity. SimCC responses
are raw scores, not probabilities, visibility labels or metric 3D measurements.
"""

from __future__ import annotations

import hashlib

import numpy as np

from ..contracts import DetectionObject, PoseObject
from ..pose_landmarks import PoseLandmark
from .onnxruntime_backend import _OnnxRuntimeSessionBackend


HALPE26_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee",
    "right_knee", "left_ankle", "right_ankle", "head", "neck", "hip",
    "left_big_toe", "right_big_toe", "left_small_toe", "right_small_toe",
    "left_heel", "right_heel",
)
_MEAN = [123.675, 116.28, 103.53]
_STD = [58.395, 57.12, 57.375]


def prepare_person_crop(frame, bbox01):
    """Return RGB normalized NCHW tensor, source center and padded affine scale.

The affine samples directly into a fixed-size image with black border padding.
Neither the source box nor decoded scientific coordinates are clipped.
"""
    import cv2

    image = np.asarray(frame)
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 2:
        raise ValueError("Pose requires a BGR image with three channels and size >= 2")
    height, width = image.shape[:2]
    box = np.asarray(bbox01, dtype=np.float32)
    if box.shape != (4,):
        raise ValueError("Pose person bounding box must contain four coordinates")
    pixels = box * np.array([width - 1, height - 1, width - 1, height - 1], np.float32)
    if not np.isfinite(pixels).all() or np.any(pixels[2:] <= pixels[:2]):
        raise ValueError("Pose person bounding box is empty or invalid")
    center = (pixels[:2] + pixels[2:]) * 0.5
    scale = (pixels[2:] - pixels[:2]) * 1.25
    w, h = np.hsplit(scale, [1])
    scale = np.where(w > h * 0.75, np.hstack([w, w / 0.75]), np.hstack([h * 0.75, h]))
    if not np.isfinite(center).all() or not np.isfinite(scale).all():
        raise ValueError("Pose person bounding box exceeds finite affine support")
    # Same three float32 anchors as get_warp_matrix(rot=0, shift=(0,0)).
    source = np.zeros((3, 2), dtype=np.float32)
    source[0] = center
    source[1] = center + np.array([0.0, scale[0] * -0.5])
    direction = source[0] - source[1]
    source[2] = source[1] + np.array([-direction[1], direction[0]])
    if source[0, 1] == source[1, 1]:
        raise ValueError("Pose person bounding box is numerically degenerate")
    destination = np.array([[96, 128], [96, 32], [0, 32]], dtype=np.float32)
    matrix = cv2.getAffineTransform(source, destination)
    crop = cv2.warpAffine(image, matrix, (192, 256), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32)
    normalized = (rgb - np.array(_MEAN, np.float32)) / np.array(_STD, np.float32)
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None]), center, scale


def _compatible_shape(shape, expected):
    """Dynamic dimensions are checked again against actual inference arrays."""
    return len(shape) == len(expected) and all(
        value is None or isinstance(value, str) or (type(value) is int and value == size)
        for value, size in zip(shape, expected, strict=True)
    )


class RTMPoseHalpe26Backend(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest):
        spec = manifest.input
        if not (
            manifest.artifact_format == "onnx"
            and manifest.resolved_adapter_family() == "rtmpose_halpe26"
            and spec.width == 192 and spec.height == 256
            and spec.dtype == "float32" and spec.layout == "nchw"
            and spec.color_order == "rgb" and spec.resize_mode == "stretch"
            and spec.pad_value == 0 and spec.rescale_factor == 1
            and spec.tensor_name in ("", "input")
            and spec.normalization.mean == _MEAN and spec.normalization.std == _STD
            and manifest.postprocess.output_name in ("", "simcc_x")
            and manifest.postprocess.confidence_threshold_default is None
        ):
            raise ValueError(
                "Incompatible pose manifest: RTMPose Halpe26 requires float32 NCHW "
                "192x256 RGB, raw-scale ImageNet normalization, black affine padding, "
                "simcc_x output and no global pose confidence threshold"
            )
        path = manifest.resolve_artifact_path()
        if not path.is_file():
            raise FileNotFoundError(
                f"Pose model missing: {path}. Install it on the selected processing server."
            )
        if manifest.sha256:
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != manifest.sha256:
                raise ValueError("Pose model checksum mismatch. Reinstall the selected model.")
        try:
            super().__init__(manifest, task="pose", supported_postprocess={"rtmpose_halpe26"})
        except Exception as exc:
            raise ValueError(
                f"Pose model could not be opened. Verify the ONNX artifact and runtime: {exc}"
            ) from exc
        inputs = self._session.get_inputs()
        if (len(inputs) != 1 or inputs[0].name != "input"
                or inputs[0].type != "tensor(float)"
                or not _compatible_shape(inputs[0].shape, [1, 3, 256, 192])):
            raise ValueError("Incompatible pose input: expected float32 NCHW [1,3,256,192]")
        outputs = {item.name: item for item in self._session.get_outputs()}
        if set(outputs) != {"simcc_x", "simcc_y"} or any(
            outputs[name].type != "tensor(float)"
            or not _compatible_shape(outputs[name].shape, [1, 26, bins])
            for name, bins in (("simcc_x", 384), ("simcc_y", 512))
        ):
            raise ValueError("Incompatible pose outputs: expected Halpe26 SimCC x384/y512")

    def estimate_pose(self, frame, *, detections: list[DetectionObject] | None = None):
        poses = []
        for index, detection in enumerate(detections or []):
            if detection.label != "person":
                continue
            tensor, center, scale = prepare_person_crop(frame, detection.bbox01)
            raw = self._session.run(["simcc_x", "simcc_y"], {self._input_name: tensor})
            if len(raw) != 2 or any(
                np.asarray(array).shape != (1, 26, bins)
                or np.asarray(array).dtype != np.float32
                for array, bins in zip(raw, (384, 512), strict=True)
            ):
                raise ValueError("Incompatible pose outputs: expected float32 [1,26,384/512]")
            x, y = raw[0][0], raw[1][0]
            height, width = np.asarray(frame).shape[:2]
            landmarks, keypoints = [], []
            for slot, name in enumerate(HALPE26_NAMES):
                position, score, reason = None, None, None
                if not np.isfinite(x[slot]).all() or not np.isfinite(y[slot]).all():
                    reason = "nonfinite_response"
                else:
                    score = float(min(x[slot].max(), y[slot].max()))
                    if score <= 0:
                        reason = "nonpositive_response"
                    else:
                        location = np.array([x[slot].argmax(), y[slot].argmax()])
                        pixels = location / 2 / [192, 256] * scale + center - scale / 2
                        position = tuple(pixels / [width - 1, height - 1])
                landmarks.append(PoseLandmark(slot, name, position, score, invalid_reason=reason))
                # Compatibility only: scientific consumers use the unbounded named landmarks.
                keypoints.append((*position, score) if position is not None else (0.0, 0.0, 0.0))
            poses.append(PoseObject(
                label="person", score=detection.score, bbox01=detection.bbox01,
                keypoints=keypoints, model_id=self._manifest.model_id, skeleton_id="halpe26",
                landmarks=landmarks, metadata={
                    "source_detection_index": detection.metadata.get("pose_detection_index", index),
                    **({"actor_subject_id": detection.metadata["actor_subject_id"]}
                       if isinstance(detection.metadata.get("actor_subject_id"), str)
                       and detection.metadata["actor_subject_id"] else {}),
                    "pose_score_source": "source_detection",
                    "landmark_score_semantics": "simcc_min_axis_maximum",
                },
            ))
        return poses
