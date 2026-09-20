"""Top-down BlazePose inference using the pinned OpenCV Zoo ONNX export.

The caller owns person detection. The backend neither detects people nor keeps
temporal state. Its hip-relative 3D prediction is not a world-map coordinate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from ..contracts import DetectionObject, PoseObject
from .onnxruntime_backend import _OnnxRuntimeSessionBackend


MEDIAPIPE_NAMES = (
    "nose",
    "left_eye_inner",
    "left_eye",
    "left_eye_outer",
    "right_eye_inner",
    "right_eye",
    "right_eye_outer",
    "left_ear",
    "right_ear",
    "mouth_left",
    "mouth_right",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_pinky",
    "right_pinky",
    "left_index",
    "right_index",
    "left_thumb",
    "right_thumb",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
)


def prepare_person_crop(frame, bbox01):
    """Square upright crop, equivalent to the reference with zero rotation.

    Padding is explicit and black. Output coordinates are allowed outside the
    image. Pixel coordinates follow the Zoo reference's edge convention.
    """
    import cv2

    image = np.asarray(frame)
    if image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 2:
        raise ValueError("Pose requires a BGR image with three channels and size >= 2")
    height, width = image.shape[:2]
    # Use pixel centres throughout the pipeline's normalized-image contract.
    x1, y1, x2, y2 = np.asarray(bbox01, dtype=float) * [
        width - 1,
        height - 1,
        width - 1,
        height - 1,
    ]
    if not np.isfinite([x1, y1, x2, y2]).all() or x2 <= x1 or y2 <= y1:
        raise ValueError("Pose person bounding box is empty or invalid")
    center = np.asarray([(x1 + x2) / 2, (y1 + y2) / 2])
    radius = max(x2 - x1, y2 - y1) * 0.625
    left, top = (center - radius).astype(int)
    right, bottom = (center + radius).astype(int)
    if right - left > 4 * max(width, height) or bottom - top > 4 * max(width, height):
        raise ValueError("Pose crop exceeds image support")
    ix1, iy1 = max(0, left), max(0, top)
    ix2, iy2 = min(width, right), min(height, bottom)
    if ix2 <= ix1 or iy2 <= iy1:
        raise ValueError("Pose person bounding box is outside the image")
    crop = cv2.copyMakeBorder(
        image[iy1:iy2, ix1:ix2],
        iy1 - top,
        bottom - iy2,
        ix1 - left,
        right - ix2,
        cv2.BORDER_CONSTANT,
        value=(0, 0, 0),
    )
    tensor = cv2.resize(crop, (256, 256), interpolation=cv2.INTER_AREA)
    tensor = cv2.cvtColor(tensor, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return tensor[None], (int(left), int(top), int(right), int(bottom))


class MediaPipePoseBackend(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest):
        spec = manifest.input
        expected_input = (
            spec.width == 256
            and spec.height == 256
            and spec.dtype == "float32"
            and spec.layout == "nhwc"
            and spec.color_order == "rgb"
            and spec.resize_mode == "stretch"
            and spec.pad_value == 0.0
            and np.isclose(spec.rescale_factor, 1 / 255, rtol=1e-9, atol=0)
            and spec.normalization.mean in ([], [0.0, 0.0, 0.0])
            and spec.normalization.std in ([], [1.0, 1.0, 1.0])
            and spec.tensor_name in ("", "input_1")
        )
        if manifest.artifact_format != "onnx" or not expected_input:
            raise ValueError(
                "Incompatible pose manifest: MediaPipe requires ONNX, float32 NHWC "
                "256x256 RGB, 1/255 rescaling, black square crop and no normalization"
            )
        if manifest.postprocess.output_name not in ("", "Identity"):
            raise ValueError("Incompatible pose manifest output: expected Identity")
        path = manifest.resolve_artifact_path()
        if not path.is_file():
            raise FileNotFoundError(
                f"Pose model missing: {path}. Install it on the selected processing server."
            )
        if manifest.sha256:
            with Path(path).open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != manifest.sha256:
                raise ValueError("Pose model checksum mismatch. Reinstall the selected model.")
        try:
            super().__init__(manifest, task="pose", supported_postprocess={"mediapipe_pose_33"})
        except Exception as exc:
            raise ValueError(
                f"Pose model could not be opened. Verify the ONNX artifact and runtime: {exc}"
            ) from exc
        inputs = self._session.get_inputs()
        if (
            len(inputs) != 1
            or inputs[0].name != "input_1"
            or inputs[0].shape != [1, 256, 256, 3]
            or inputs[0].type != "tensor(float)"
        ):
            raise ValueError("Incompatible pose input: expected float32 NHWC [1,256,256,3]")
        outputs = {item.name: (item.shape, item.type) for item in self._session.get_outputs()}
        required = {"Identity": [1, 195], "Identity_1": [1, 1], "Identity_4": [1, 117]}
        if any(outputs.get(name) != (shape, "tensor(float)") for name, shape in required.items()):
            raise ValueError(
                "Incompatible pose outputs: expected the OpenCV Zoo MediaPipe 2023mar export"
            )
        self._threshold = manifest.postprocess.confidence_threshold_default
        if self._threshold is None:
            self._threshold = 0.5

    def estimate_pose(self, frame, *, detections: list[DetectionObject] | None = None):
        from ..pose_landmarks import PoseLandmark

        poses = []
        for index, detection in enumerate(detections or []):
            if detection.label != "person":
                continue
            tensor, crop = prepare_person_crop(frame, detection.bbox01)
            height, width = np.asarray(frame).shape[:2]
            outputs = self._session.run(
                ["Identity", "Identity_1", "Identity_4"], {self._input_name: tensor}
            )
            score = float(outputs[1][0, 0])
            if not np.isfinite(score) or score < self._threshold:
                continue
            raw = np.asarray(outputs[0]).reshape(39, 5)[:33]
            relative = np.asarray(outputs[2]).reshape(39, 3)[:33]
            left, top, right, bottom = crop
            xy = raw[:, :2] / 256.0 * [right - left, bottom - top] + [left, top]
            xy /= [width - 1, height - 1]
            visibility_presence = 1 / (1 + np.exp(-np.clip(raw[:, 3:5], -80, 80)))
            keypoints = [
                (float(x), float(y), float(min(visibility_presence[i])))
                for i, (x, y) in enumerate(xy)
            ]
            landmarks = [
                PoseLandmark(i, name, tuple(xy[i]), float(min(visibility_presence[i])))
                for i, name in enumerate(MEDIAPIPE_NAMES)
            ]
            poses.append(
                PoseObject(
                    label="person",
                    score=score,
                    bbox01=detection.bbox01,
                    keypoints=keypoints,
                    model_id=self._manifest.model_id,
                    skeleton_id="mediapipe_pose_33",
                    landmarks=landmarks,
                    metadata={
                        "source_detection_index": detection.metadata.get(
                            "pose_detection_index", index
                        ),
                        **(
                            {"actor_subject_id": detection.metadata["actor_subject_id"]}
                            if isinstance(detection.metadata.get("actor_subject_id"), str)
                            and detection.metadata["actor_subject_id"]
                            else {}
                        ),
                        "crop_pixels": list(crop),
                        "visibility_scores": [
                            float(v) if np.isfinite(v) else None for v in visibility_presence[:, 0]
                        ],
                        "presence_scores": [
                            float(v) if np.isfinite(v) else None for v in visibility_presence[:, 1]
                        ],
                        "relative_landmarks_3d": {
                            "reference": "hip_center_camera_axes",
                            "units": "model_meters",
                            "axes": "x_right_y_down_z_away",
                            "provenance": "image_estimate",
                            "metric_calibrated": False,
                            "positions": [
                                p.tolist() if np.isfinite(p).all() else None for p in relative
                            ],
                    },
                },
            ))
        return poses
