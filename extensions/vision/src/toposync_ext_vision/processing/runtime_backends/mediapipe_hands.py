"""Hand21 image geometry with an explicit inverse, without temporal identity.

Export contract: OpenCV Zoo 47534e27c9851bb1128ccc0102f1145e27f23f98,
models/handpose_estimation_mediapipe. The two-stage rotated crop follows that
contract; the inverse retains actual integer padding, including odd padding.
Relative model depth is deliberately not promoted to world-map coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

import numpy as np

from ..pose_landmarks import PoseLandmark
from .mediapipe_palms import decode_palms, prepare_palm_input
from .onnxruntime_backend import _OnnxRuntimeSessionBackend


HAND_NAMES = (
    'wrist', 'thumb_cmc', 'thumb_mcp', 'thumb_ip', 'thumb_tip',
    'index_mcp', 'index_pip', 'index_dip', 'index_tip',
    'middle_mcp', 'middle_pip', 'middle_dip', 'middle_tip',
    'ring_mcp', 'ring_pip', 'ring_dip', 'ring_tip',
    'pinky_mcp', 'pinky_pip', 'pinky_dip', 'pinky_tip',
)


def _square_crop(image: np.ndarray, bounds: np.ndarray, *, before_rotation: bool):
    import cv2

    size = bounds[1] - bounds[0]
    if not np.isfinite(bounds).all() or np.any(size <= 0):
        raise ValueError("Hand crop requires finite positive bounds")
    centre = bounds[0] + size / 2
    if not before_rotation:
        centre = centre + size * [0, -.4]
    radius = size * (2 if before_rotation else 1.5)
    expanded = np.array([centre - radius, centre + radius])
    if not np.isfinite(expanded).all():
        raise ValueError("Hand crop expansion is not finite")
    # Clip the sampling region, not the scientific output. Clip before integer
    # conversion so extreme finite proposals cannot overflow int32 coordinates.
    bounds = np.clip(expanded, [0, 0], [image.shape[1], image.shape[0]]).astype(int)
    width, height = bounds[1] - bounds[0]
    if min(width, height) < 1:
        raise ValueError("Hand crop has no image support")
    side = int(np.hypot(width, height)) if before_rotation else int(max(width, height))
    left, top = (side - width) // 2, (side - height) // 2
    region = image[bounds[0, 1]:bounds[1, 1], bounds[0, 0]:bounds[1, 0]]
    padded = cv2.copyMakeBorder(region, int(top), int(side - height - top),
                               int(left), int(side - width - left),
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
    return padded, bounds, bounds[0] - [left, top]


def prepare_hand_crop(frame: np.ndarray, palm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return RGB NHWC224 and crop-to-image affine in continuous edge pixels.

The returned transform reverses both integer crops, padding, rotation and scale.
It is independent of inferred landmarks and does not mutate frame or proposal.
    """
    import cv2

    image, proposal = np.asarray(frame), np.asarray(palm, dtype=np.float64)
    if (image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8
            or min(image.shape[:2]) < 2):
        raise ValueError("Hand inference requires a nonempty uint8 BGR image")
    if proposal.shape != (19,) or not np.isfinite(proposal).all():
        raise ValueError("Hand crop requires one finite palm proposal with seven points")
    image, bounds, first_bias = _square_crop(image, proposal[:4].reshape(2, 2), before_rotation=True)
    points = proposal[4:18].reshape(7, 2) - first_bias
    direction = points[2] - points[0]
    if np.linalg.norm(direction) < 1e-6:
        raise ValueError("Hand palm orientation is degenerate")
    angle = np.pi / 2 - np.arctan2(-direction[1], direction[0])
    angle = (angle + np.pi) % (2 * np.pi) - np.pi
    centre = ((bounds[0] + bounds[1]) / 2 - first_bias).astype(float)
    rotation = cv2.getRotationMatrix2D(tuple(centre), float(np.degrees(angle)), 1)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    rotated = cv2.warpAffine(rgb, rotation, (rgb.shape[1], rgb.shape[0]))
    rotated_points = np.c_[points, np.ones(7)] @ rotation.T
    bounds = np.array([rotated_points.min(axis=0), rotated_points.max(axis=0)])
    crop, _, second_bias = _square_crop(rotated, bounds, before_rotation=False)
    tensor = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    scale = crop.shape[0] / 224
    crop_to_rotated = np.array([[scale, 0, second_bias[0]],
                                [0, scale, second_bias[1]], [0, 0, 1]])
    first_to_image = np.eye(3)
    first_to_image[:2, 2] = first_bias
    inverse = first_to_image @ np.linalg.inv(np.vstack([rotation, [0, 0, 1]])) @ crop_to_rotated
    return np.ascontiguousarray(tensor[None]), inverse


def decode_hand_landmarks(raw: np.ndarray, input_to_image: np.ndarray) -> list[PoseLandmark]:
    """Named image landmarks; global hand confidence is not a joint score.

The caller owns the container reference/units. This function returns image pixel
coordinates, with invalid joints retained as unavailable skeleton slots.
    """
    values, matrix = np.asarray(raw, dtype=np.float64), np.asarray(input_to_image, dtype=np.float64)
    if values.shape != (1, 63):
        raise ValueError("Hand landmark shape must be [1,63]")
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.array_equal(matrix[2], [0, 0, 1])):
        raise ValueError("Hand transform must be a finite affine matrix")
    determinant = np.linalg.det(matrix[:2, :2])
    if not np.isfinite(determinant) or determinant == 0:
        raise ValueError("Hand transform must be invertible")
    xy = values.reshape(21, 3)[:, :2]
    with np.errstate(over="ignore", invalid="ignore"):
        projected = np.c_[xy, np.ones(21)] @ matrix.T
    return [PoseLandmark(index, name, tuple(projected[index, :2]))
            for index, name in enumerate(HAND_NAMES)]


@dataclass(frozen=True, slots=True)
class HandObservation:
    model_id: str
    palm_model_id: str
    confidence: float
    palm_confidence: float
    handedness_score: float
    landmarks: tuple[PoseLandmark, ...]
    landmark_reference: str = 'selected_image'

    def to_dict(self):
        return {
            'schema_version': 1,
            'skeleton_id': 'mediapipe_hand_21',
            'model_id': self.model_id,
            'palm_model_id': self.palm_model_id,
            'confidence': self.confidence,
            'palm_confidence': self.palm_confidence,
            'handedness_score': self.handedness_score,
            'landmark_reference': self.landmark_reference,
            'landmark_units': 'image_fraction',
            'landmarks': [point.to_dict() for point in self.landmarks],
            'association': {'status': 'unassigned'},
        }


class _HandModelSession(_OnnxRuntimeSessionBackend):
    def __init__(self, manifest, *, palm: bool):
        side = 192 if palm else 224
        spec = manifest.input
        valid = (
            manifest.artifact_format == 'onnx'
            and spec.width == side and spec.height == side
            and spec.dtype == 'float32' and spec.layout == 'nhwc' and spec.color_order == 'rgb'
            and spec.resize_mode == ('letterbox' if palm else 'stretch')
            and spec.pad_value == 0
            and np.isclose(spec.rescale_factor, 1 / 255, rtol=1e-9, atol=0)
            and spec.normalization.mean in ([], [0., 0., 0.])
            and spec.normalization.std in ([], [1., 1., 1.])
            and spec.tensor_name in ('', 'input_1')
            and manifest.postprocess.output_name in ('', 'Identity')
        )
        if not valid:
            raise ValueError(f'Incompatible hand manifest: expected RGB NHWC float32 {side}x{side}')
        expected_hash = manifest.sha256.strip().lower()
        if len(expected_hash) != 64 or any(c not in '0123456789abcdef' for c in expected_hash):
            raise ValueError('Hand models require an explicit SHA256 checksum')
        path = manifest.resolve_artifact_path()
        if not path.is_file():
            raise FileNotFoundError(f'Hand model missing: {path}. Prepare it on the processing server.')
        with path.open('rb') as handle:
            actual_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        if actual_hash != expected_hash:
            raise ValueError('Hand model checksum mismatch. Prepare the selected artifact again.')
        adapter = 'mediapipe_palm_detection' if palm else 'mediapipe_hand_21'
        super().__init__(manifest, task='detection' if palm else 'pose', supported_postprocess={adapter})
        inputs = self._session.get_inputs()
        if (len(inputs) != 1 or inputs[0].name != 'input_1'
                or inputs[0].shape != [1, side, side, 3] or inputs[0].type != 'tensor(float)'):
            raise ValueError(f'Incompatible hand model input: expected float32 [1,{side},{side},3]')
        required = {'Identity': [1, 2016, 18], 'Identity_1': [1, 2016, 1]} if palm else {
            'Identity': [1, 63], 'Identity_1': [1, 1], 'Identity_2': [1, 1], 'Identity_3': [1, 63],
        }
        actual = {item.name: (item.shape, item.type) for item in self._session.get_outputs()}
        if any(actual.get(name) != (shape, 'tensor(float)') for name, shape in required.items()):
            raise ValueError('Incompatible hand model outputs: expected the pinned export interface')


class MediaPipeHandsBackend:
    """Two reusable sessions; caller owns person presence and association.

No model download, person detector, tracker, or hidden temporal state. Individual
landmarks have no confidence in this export; hand confidence remains separate.
    """

    backend_id = 'onnxruntime'

    def __init__(self, palm_manifest, hand_manifest, *, maximum_palms: int = 32):
        if type(maximum_palms) is not int or not 1 <= maximum_palms <= 2016:
            raise ValueError('Hand proposal capacity must be an integer in [1,2016]')
        self._maximum_palms = maximum_palms
        self._palm = _HandModelSession(palm_manifest, palm=True)
        self._hand = _HandModelSession(hand_manifest, palm=False)
        self._palm_threshold = palm_manifest.postprocess.confidence_threshold_default
        self._hand_threshold = hand_manifest.postprocess.confidence_threshold_default
        self._overlap_threshold = palm_manifest.postprocess.iou_threshold_default
        self._palm_threshold = .5 if self._palm_threshold is None else self._palm_threshold
        self._hand_threshold = .8 if self._hand_threshold is None else self._hand_threshold
        self._overlap_threshold = .3 if self._overlap_threshold is None else self._overlap_threshold

    @property
    def providers(self):
        return {'palm': self._palm.providers, 'hand': self._hand.providers}

    def estimate_hands(self, frame) -> list[HandObservation]:
        tensor, inverse = prepare_palm_input(frame)
        values = self._palm._session.run(['Identity', 'Identity_1'], {'input_1': tensor})
        _validate_tensors(values, ((1, 2016, 18), (1, 2016, 1)))
        palms = decode_palms(*values, inverse, score_threshold=self._palm_threshold,
                             overlap_threshold=self._overlap_threshold)
        if len(palms) > self._maximum_palms:
            raise ValueError('Hand proposal capacity exceeded')
        observations = []
        height, width = np.asarray(frame).shape[:2]
        for palm in palms:
            tensor, inverse = prepare_hand_crop(frame, palm)
            values = self._hand._session.run(
                ['Identity', 'Identity_1', 'Identity_2'], {'input_1': tensor})
            _validate_tensors(values, ((1, 63), (1, 1), (1, 1)))
            raw, confidence, handedness = values
            score, handedness_score = float(confidence[0, 0]), float(handedness[0, 0])
            if not all(np.isfinite(v) and 0 <= v <= 1 for v in (score, handedness_score)):
                raise ValueError('Hand model score must be finite and in [0,1]')
            if score < self._hand_threshold:
                continue
            points = decode_hand_landmarks(raw, inverse)
            normalized = tuple(replace(point, position=(point.position[0] / (width - 1),
                                                        point.position[1] / (height - 1)))
                               if point.position is not None else point for point in points)
            observations.append(HandObservation(
                self._hand._manifest.model_id, self._palm._manifest.model_id,
                score, float(palm[-1]), handedness_score, normalized,
            ))
        return observations


def _validate_tensors(values, shapes):
    if (not isinstance(values, (list, tuple)) or len(values) != len(shapes)
            or any(not isinstance(value, np.ndarray) or value.shape != shape
                   or value.dtype != np.float32 for value, shape in zip(values, shapes))):
        raise ValueError('Hand model returned an incompatible tensor interface')
