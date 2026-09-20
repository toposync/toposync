"""Constant ONNX and analytic geometry tests; these do not qualify pose accuracy."""

import hashlib
import json

import numpy as np
import pytest

from toposync_ext_vision.processing.contracts import DetectionObject
from toposync_ext_vision.registry import ModelManifest


def backend_type():
    from toposync_ext_vision.processing.runtime_backends.rtmpose_halpe26 import (
        RTMPoseHalpe26Backend,
    )
    return RTMPoseHalpe26Backend


def distributions():
    x = np.zeros((1, 26, 384), dtype=np.float32)
    y = np.zeros((1, 26, 512), dtype=np.float32)
    x[:, :, 192], y[:, :, 256] = 0.9, 0.8
    return x, y


def model(path, *, values=None, input_shape=None, input_name="input", output_dtype=1):
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes, outputs = [], []
    for name, array in zip(("simcc_x", "simcc_y"), values or distributions(), strict=True):
        array = array.astype(np.float64 if output_dtype == 11 else np.float32)
        nodes.append(helper.make_node("Constant", [], [name], value=numpy_helper.from_array(array)))
        outputs.append(helper.make_tensor_value_info(name, output_dtype, list(array.shape)))
    graph = helper.make_graph(nodes, "synthetic_simcc", [helper.make_tensor_value_info(
        input_name, TensorProto.FLOAT, input_shape or ["batch", 3, 256, 192]
    )], outputs)
    fixture = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    fixture.ir_version = 10
    onnx.save(fixture, path)
    return path


def manifest(path):
    return ModelManifest(
        model_id="test.rtmpose", display_name="Synthetic RTMPose", task="pose",
        runtime="onnxruntime", artifact_format="onnx", artifact_path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "",
        input={"width": 192, "height": 256, "dtype": "float32", "layout": "nchw",
               "color_order": "rgb", "tensor_name": "input", "rescale_factor": 1,
               "normalization": {"mean": [123.675, 116.28, 103.53],
                                 "std": [58.395, 57.12, 57.375]}},
        postprocess={"type": "rtmpose_halpe26", "output_name": "simcc_x"},
    )


def person(bbox=(0.1, 0.2, 0.6, 0.8), **metadata):
    return DetectionObject("person", 0, 0.73, bbox, "detector", metadata=metadata)


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    monkeypatch.setenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", "CPUExecutionProvider")


def test_real_session_preserves_names_raw_scores_invalid_slots_and_association(tmp_path):
    x, y = distributions()
    x[0, 0, 192], y[0, 0, 256] = 1.4, 1.2
    x[0, 1] = 0
    y[0, 2] = -0.1
    x[0, 3, 3] = np.nan
    y[0, 4, 4] = np.inf
    y[0, 5, 4] = -np.inf
    backend = backend_type()(manifest(model(tmp_path / "pose.onnx", values=(x, y))))
    poses = backend.estimate_pose(np.zeros((480, 640, 3), np.uint8), detections=[
        person(pose_detection_index=7, actor_subject_id="actor:7")])
    pose = poses[0]
    assert backend.providers == ["CPUExecutionProvider"]
    assert pose.score == 0.73 and pose.metadata["pose_score_source"] == "source_detection"
    assert pose.metadata["landmark_score_semantics"] == "simcc_min_axis_maximum"
    assert pose.metadata["source_detection_index"] == 7
    assert pose.metadata["actor_subject_id"] == "actor:7"
    assert pose.skeleton_id == "halpe26" and pose.landmark_reference == "selected_image"
    assert len(pose.landmarks) == len(pose.keypoints) == 26
    assert [point.index for point in pose.landmarks] == list(range(26))
    assert [point.name for point in pose.landmarks] == [
        "nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder",
        "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
        "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle",
        "head", "neck", "hip", "left_big_toe", "right_big_toe", "left_small_toe",
        "right_small_toe", "left_heel", "right_heel"]
    assert pose.landmarks[0].model_score == pytest.approx(1.2)
    assert pose.keypoints[0][2] == 1  # Legacy clamping never changes scientific scores.
    assert pose.landmarks[2].model_score == pytest.approx(-0.1)
    assert all(point.position is None and point.invalid_reason for point in pose.landmarks[1:6])
    assert all(point.model_score is None for point in pose.landmarks[3:6])
    assert pose.landmarks[6].position == pytest.approx((0.35, 0.5), abs=1e-6)
    assert all(point.visibility == "unknown" and point.provenance == "image_estimate"
               for point in pose.landmarks)
    assert "relative_landmarks_3d" not in pose.metadata
    json.dumps([point.to_dict() for point in pose.landmarks], allow_nan=False)


def test_only_people_multiple_associations_and_no_people_is_noop(tmp_path):
    backend = backend_type()(manifest(model(tmp_path / "pose.onnx")))
    assert backend.estimate_pose(None) == []
    car = DetectionObject("car", 2, 0.9, (0, 0, 1, 1), "detector")
    assert backend.estimate_pose(None, detections=[car]) == []
    poses = backend.estimate_pose(np.zeros((480, 640, 3), np.uint8), detections=[
        car, person(), person((0.6, 0.1, 0.9, 0.5), actor_subject_id="second")])
    assert [pose.metadata["source_detection_index"] for pose in poses] == [1, 2]
    assert poses[1].metadata["actor_subject_id"] == "second"
    assert "actor_subject_id" not in poses[0].metadata
    assert poses[0].landmarks[0].position == pytest.approx((0.35, 0.5), abs=1e-6)
    assert poses[1].landmarks[0].position == pytest.approx((0.75, 0.3), abs=1e-6)


@pytest.mark.parametrize("size,bbox,center,scale", [
    ((640, 480), (.1, .2, .6, .8), (223.65, 239.5), (399.375, 532.5)),
    ((641, 481), (-.1, .15, .3, 1.1), (64, 300), (427.5, 570)),
    ((640, 480), (0, 0, 1, 1), (319.5, 239.5), (798.75, 1065)),
])
def test_affine_rgb_and_independent_non_square_anchors(size, bbox, center, scale):
    from toposync_ext_vision.processing.runtime_backends.rtmpose_halpe26 import prepare_person_crop

    width, height = size
    tensor, actual_center, actual_scale = prepare_person_crop(
        np.full((height, width, 3), (10, 20, 30), np.uint8), bbox)
    assert tensor.shape == (1, 3, 256, 192) and tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    np.testing.assert_allclose(actual_center, center, atol=1e-4, rtol=0)
    np.testing.assert_allclose(actual_scale, scale, atol=1e-4, rtol=0)
    np.testing.assert_allclose(tensor[0, :, 128, 96],
        (np.array([30, 20, 10]) - [123.675, 116.28, 103.53]) / [58.395, 57.12, 57.375],
        atol=1e-6, rtol=0)


def test_decoder_inverse_bins_unclipped_and_pixel_centers(tmp_path):
    x, y = distributions()
    for index, (ix, iy) in enumerate([(288, 128), (0, 0), (383, 511)]):
        x[0, index], y[0, index] = 0, 0
        x[0, index, ix], y[0, index, iy] = .8, .9
    backend = backend_type()(manifest(model(tmp_path / "pose.onnx", values=(x, y))))
    pose = backend.estimate_pose(np.zeros((480, 640, 3), np.uint8), detections=[person()])[0]
    expected = [(323.49375, 106.375), (23.9625, -26.75),
                (423.3375 - 399.375/384, 505.75 - 532.5/512)]
    for point, pixels in zip(pose.landmarks, expected):
        np.testing.assert_allclose(point.position, np.array(pixels) / [639, 479], atol=1e-6)
    assert pose.landmarks[1].position[1] < 0 and pose.landmarks[2].position[1] > 1


def test_affine_actually_samples_independent_source_anchor_and_black_padding():
    from toposync_ext_vision.processing.runtime_backends.rtmpose_halpe26 import prepare_person_crop

    yy, xx = np.indices((480, 640), dtype=np.float32)
    frame = np.stack([xx, yy, xx + 2 * yy], axis=-1)
    tensor, _, _ = prepare_person_crop(frame, (.1, .2, .6, .8))
    rgb = tensor[0, :, 64, 144] * [58.395, 57.12, 57.375] + [123.675, 116.28, 103.53]
    # Source anchor (323.49375,106.375), independent of the production inverse.
    # OpenCV INTER_LINEAR quantizes fractions to 1/32; x+2y error <= 3/64.
    np.testing.assert_allclose(rgb, [536.24375, 106.375, 323.49375], atol=.06, rtol=0)
    corner = tensor[0, :, 0, 0] * [58.395, 57.12, 57.375] + [123.675, 116.28, 103.53]
    np.testing.assert_allclose(corner, [0, 0, 0], atol=1e-5, rtol=0)


@pytest.mark.parametrize("changes", [
    {"width": 256}, {"height": 192}, {"layout": "nhwc"}, {"dtype": "uint8"},
    {"color_order": "bgr"}, {"resize_mode": "letterbox"}, {"pad_value": 114},
    {"rescale_factor": 1/255}, {"tensor_name": "wrong"},
])
def test_rejects_incompatible_manifest_before_loading(tmp_path, changes):
    spec = manifest(tmp_path / "absent.onnx")
    spec.input = spec.input.model_copy(update=changes)
    with pytest.raises(ValueError, match="Incompatible pose manifest"):
        backend_type()(spec)


@pytest.mark.parametrize("change", ["normalization", "threshold", "output", "adapter"])
def test_rejects_silent_preprocess_or_decoder_configuration(tmp_path, change):
    spec = manifest(tmp_path / "absent.onnx")
    if change == "normalization":
        spec.input.normalization.mean = [0, 0, 0]
    elif change == "threshold":
        spec.postprocess.confidence_threshold_default = .5
    elif change == "output":
        spec.postprocess.output_name = "wrong"
    else:
        spec.postprocess.type = "mediapipe_pose_33"
    with pytest.raises(ValueError, match="Incompatible pose manifest"):
        backend_type()(spec)


@pytest.mark.parametrize("kind", ["missing", "corrupt", "checksum"])
def test_missing_corrupt_and_hash_mismatch_fail_without_download(tmp_path, kind):
    path = tmp_path / "pose.onnx"
    if kind != "missing":
        path.write_bytes(b"not onnx")
    spec = manifest(path)
    if kind == "checksum":
        spec.sha256 = "0" * 64
    error = FileNotFoundError if kind == "missing" else ValueError
    message = {"missing": "Pose model missing", "corrupt": "could not be opened",
               "checksum": "checksum mismatch"}[kind]
    with pytest.raises(error, match=message):
        backend_type()(spec)


@pytest.mark.parametrize("kwargs", [
    {"input_shape": [1, 3, 192, 256]}, {"input_shape": [2, 3, 256, 192]},
    {"input_name": "wrong"}, {"output_dtype": 11},
    {"values": (np.zeros((1, 25, 384)), np.zeros((1, 25, 512)))},
])
def test_rejects_static_tensor_incompatibility(tmp_path, kwargs):
    with pytest.raises(ValueError, match="Incompatible pose (input|outputs)"):
        backend_type()(manifest(model(tmp_path / "pose.onnx", **kwargs)))


def test_every_inference_checks_effective_symbolic_output_shape(tmp_path):
    from types import SimpleNamespace

    backend = backend_type()(manifest(model(tmp_path / "pose.onnx")))
    # Session boundary stub isolates malformed runtime output from ONNX shape inference.
    backend._session = SimpleNamespace(run=lambda *args: [np.zeros((1, 25, 384)),
                                                          np.zeros((1, 26, 512))])
    with pytest.raises(ValueError, match="Incompatible pose outputs"):
        backend.estimate_pose(np.zeros((480, 640, 3), np.uint8), detections=[person()])


def test_symbolic_session_metadata_is_accepted_but_effective_values_remain_validated(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from toposync_ext_vision.processing.runtime_backends import onnxruntime_backend

    outputs = list(distributions())

    class SymbolicSession:
        def get_inputs(self):
            return [SimpleNamespace(name="input", shape=["batch", 3, 256, 192],
                                    type="tensor(float)")]

        def get_outputs(self):
            return [SimpleNamespace(name=name, shape=["batch", "keypoints", bins],
                                    type="tensor(float)")
                    for name, bins in (("simcc_x", 384), ("simcc_y", 512))]

        def run(self, names, feeds):
            assert names == ["simcc_x", "simcc_y"]
            assert feeds["input"].shape == (1, 3, 256, 192)
            return outputs

    boundary = SimpleNamespace(InferenceSession=lambda *args, **kwargs: SymbolicSession(),
                               get_available_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(onnxruntime_backend, "_import_onnxruntime", lambda: boundary)
    backend = backend_type()(manifest(model(tmp_path / "pose.onnx")))
    frame = np.zeros((80, 120, 3), np.uint8)
    assert len(backend.estimate_pose(frame, detections=[person()])[0].landmarks) == 26
    outputs[1] = np.zeros((1, 26, 511), np.float32)
    with pytest.raises(ValueError, match="Incompatible pose outputs"):
        backend.estimate_pose(frame, detections=[person()])
    outputs[1] = np.zeros((1, 26, 512), np.float64)
    with pytest.raises(ValueError, match="Incompatible pose outputs"):
        backend.estimate_pose(frame, detections=[person()])


@pytest.mark.parametrize("bbox", [(0, 0, 0, 1), (0, 0, 1, float("nan")), (0, 1, 1, 0)])
def test_invalid_geometry_fails_explicitly(bbox):
    from toposync_ext_vision.processing.runtime_backends.rtmpose_halpe26 import prepare_person_crop

    with pytest.raises(ValueError, match="bounding box"):
        prepare_person_crop(np.zeros((50, 80, 3), np.uint8), bbox)
