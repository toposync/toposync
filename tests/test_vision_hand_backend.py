"""Real constant-ONNX sessions exercise contracts, not learned accuracy."""

import hashlib
import json

import numpy as np
import pytest

from toposync_ext_vision.registry import ModelManifest


def model(path, side, outputs, *, input_name='input_1', input_shape=None):
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes, declarations = [], []
    for index, array in enumerate(outputs):
        name = 'Identity' + (f'_{index}' if index else '')
        array = np.asarray(array, dtype=np.float32)
        nodes.append(helper.make_node('Constant', [], [name], value=numpy_helper.from_array(array)))
        declarations.append(helper.make_tensor_value_info(name, TensorProto.FLOAT, list(array.shape)))
    graph = helper.make_graph(nodes, 'synthetic_hand_export', [helper.make_tensor_value_info(
        input_name, TensorProto.FLOAT, input_shape or [1, side, side, 3])], declarations)
    fixture = helper.make_model(graph, opset_imports=[helper.make_operatorsetid('', 13)])
    fixture.ir_version = 10
    onnx.save(fixture, path)
    return path


def manifest(path, *, palm):
    side = 192 if palm else 224
    return ModelManifest(model_id='test.palm' if palm else 'test.hand', display_name='Synthetic hand model',
        task='detection' if palm else 'pose', runtime='onnxruntime', artifact_format='onnx',
        artifact_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        input={'width': side, 'height': side, 'dtype': 'float32', 'color_order': 'rgb',
               'layout': 'nhwc', 'tensor_name': 'input_1', 'rescale_factor': 1 / 255,
               'resize_mode': 'letterbox' if palm else 'stretch'},
        postprocess={'type': 'mediapipe_palm_detection' if palm else 'mediapipe_hand_21',
                     'output_name': 'Identity'}, classes={'labels': ['hand']})


def artifacts(tmp_path, *, palm_score=8, confidence=.9, handedness=.7, raw=None):
    regression = np.zeros((1, 2016, 18), np.float32)
    scores = np.full((1, 2016, 1), -100, np.float32)
    regression[0, 0, :4] = [96, 96, 40, 40]
    regression[0, 0, 4:] = np.array([(100, 120), (80, 100), (100, 80),
                                    (110, 90), (120, 100), (80, 110), (85, 115)]).ravel() - 4
    scores[0, 0, 0] = palm_score
    palm = manifest(model(tmp_path / 'palm.onnx', 192, [regression, scores]), palm=True)
    raw = np.tile([112, 112, 0], 21).reshape(1, 63).astype(np.float32) if raw is None else raw
    hand = manifest(model(tmp_path / 'hand.onnx', 224, [raw, [[confidence]], [[handedness]],
                                                     np.zeros((1, 63))]), palm=False)
    return palm, hand


def backend(pair, **options):
    from toposync_ext_vision.processing.runtime_backends.mediapipe_hands import MediaPipeHandsBackend

    return MediaPipeHandsBackend(*pair, **options)


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    monkeypatch.setenv('TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS', 'CPUExecutionProvider')


def test_real_sessions_produce_named_unassigned_hand_without_fictitious_joint_scores(tmp_path):
    runtime = backend(artifacts(tmp_path))
    results = runtime.estimate_hands(np.zeros((192, 192, 3), np.uint8))
    assert len(results) == 1
    assert runtime.providers == {'palm': ['CPUExecutionProvider'], 'hand': ['CPUExecutionProvider']}
    result = results[0].to_dict()
    assert result['skeleton_id'] == 'mediapipe_hand_21'
    assert result['model_id'] == 'test.hand' and result['palm_model_id'] == 'test.palm'
    assert result['landmark_reference'] == 'selected_image' and result['landmark_units'] == 'image_fraction'
    assert result['confidence'] == pytest.approx(.9) and result['handedness_score'] == pytest.approx(.7)
    assert result['association'] == {'status': 'unassigned'}
    assert len(result['landmarks']) == 21
    assert all(p['model_score'] is None for p in result['landmarks'])
    assert result['landmarks'][0]['position'] == pytest.approx([100 / 191, 84 / 191])
    assert 'relative_landmarks_3d' not in result
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('which', ['palm', 'hand'])
def test_low_global_confidence_is_empty_not_fabricated_joints(tmp_path, which):
    pair = artifacts(tmp_path, palm_score=-5 if which == 'palm' else 8,
                     confidence=.3 if which == 'hand' else .9)
    assert backend(pair).estimate_hands(np.zeros((192, 192, 3), np.uint8)) == []


def test_invalid_joint_stays_in_its_slot_without_invalidating_other_joints(tmp_path):
    raw = np.tile([112, 112, 0], 21).reshape(1, 63).astype(np.float32)
    raw[0, 3] = np.nan
    raw[0, 6] = -1000
    result = backend(artifacts(tmp_path, raw=raw)).estimate_hands(np.zeros((192, 192, 3), np.uint8))[0]
    assert result.landmarks[1].position is None
    assert result.landmarks[2].position[0] < 0
    assert result.landmarks[3].position is not None
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize('field', ['confidence', 'handedness'])
@pytest.mark.parametrize('value', [np.nan, np.inf, -1, 2])
def test_invalid_global_model_scores_do_not_become_valid_hands(tmp_path, field, value):
    runtime = backend(artifacts(tmp_path, **{field: value}))
    with pytest.raises(ValueError, match='score'):
        runtime.estimate_hands(np.zeros((192, 192, 3), np.uint8))


@pytest.mark.parametrize('which', [0, 1])
def test_artifact_checksum_is_verified_for_both_sessions(tmp_path, which):
    pair = artifacts(tmp_path)
    pair[which].sha256 = '0' * 64
    with pytest.raises(ValueError, match='checksum'):
        backend(pair)


@pytest.mark.parametrize('which', [0, 1])
def test_wrong_preprocessing_manifest_is_not_silently_accepted(tmp_path, which):
    pair = artifacts(tmp_path)
    pair[which].input.color_order = 'bgr'
    with pytest.raises(ValueError, match='manifest'):
        backend(pair)


def test_actual_onnx_interface_is_checked_not_only_manifest(tmp_path):
    pair = artifacts(tmp_path)
    path = model(tmp_path / 'wrong.onnx', 224, [np.zeros((1, 63)), [[.9]], [[.5]],
                                             np.zeros((1, 63))], input_name='wrong')
    pair = pair[0], manifest(path, palm=False)
    with pytest.raises(ValueError, match='input'):
        backend(pair)


@pytest.mark.parametrize('session,index,replacement', [
    ('_hand', 1, np.array([[.9, np.nan]], np.float32)),
    ('_hand', 2, np.array([[True]], bool)),
    ('_hand', 0, np.zeros((1, 63), np.float64)),
    ('_palm', 1, np.zeros((1, 2016, 1), np.float64)),
])
def test_actual_returned_tensor_shape_and_dtype_are_checked(tmp_path, session, index, replacement):
    runtime = backend(artifacts(tmp_path))
    owner = getattr(runtime, session)
    real = owner._session

    class AlteredSession:
        def run(self, names, inputs):
            outputs = real.run(names, inputs)
            outputs[index] = replacement
            return outputs

    owner._session = AlteredSession()
    with pytest.raises(ValueError, match='tensor'):
        runtime.estimate_hands(np.zeros((192, 192, 3), np.uint8))


def test_capacity_is_checked_before_any_hand_inference(tmp_path):
    runtime = backend(artifacts(tmp_path), maximum_palms=1)
    real = runtime._palm._session

    class TwoPalms:
        def run(self, names, inputs):
            regression, scores = real.run(names, inputs)
            regression[0, 1] = regression[0, 0]
            regression[0, 1, 0] -= 60
            regression[0, 1, 4::2] -= 60
            scores[0, 1, 0] = 6
            return regression, scores

    class NoHandInference:
        def run(self, names, inputs):
            pytest.fail('Capacity must fail before any hand inference')

    runtime._palm._session = TwoPalms()
    runtime._hand._session = NoHandInference()
    with pytest.raises(ValueError, match='capacity'):
        runtime.estimate_hands(np.zeros((192, 192, 3), np.uint8))
