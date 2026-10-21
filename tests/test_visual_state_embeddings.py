from __future__ import annotations

import hashlib

import numpy as np
import pytest

from toposync_ext_vision.processing.runtime_backends import build_embedding_backend, prepare_onnx_input
from toposync_ext_vision.processing.runtime_backends import onnxruntime_backend
from toposync_ext_vision.registry.manifests import ModelManifest


def model_manifest(tmp_path, values=(3.0, 4.0), *, output_shape=None):
    import onnx
    from onnx import TensorProto, helper

    path = tmp_path / "embedding.onnx"
    shape = output_shape or [1, len(values)]
    graph = helper.make_graph([
        helper.make_node("Constant", [], ["embedding"],
                         value=helper.make_tensor("vector", TensorProto.FLOAT, shape, values)),
    ], "embedding", [helper.make_tensor_value_info("pixels", TensorProto.FLOAT, [1, 3, 4, 4])],
        [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, shape)])
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    model.ir_version = 10
    onnx.save(model, path)
    return ModelManifest(
        model_id="test.embedding", display_name="Controlled embedding", task="embedding",
        runtime="onnxruntime", artifact_format="onnx", artifact_path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        input={"width": 4, "height": 4, "tensor_name": "pixels", "resize_mode": "letterbox",
               "normalization": {"mean": [0, 0, 0], "std": [1, 1, 1]}},
        postprocess={"type": "image_embedding", "output_name": "embedding", "embedding_dimension": len(values)},
    )


def test_persistent_session_normalizes_vectors_and_close_releases_it(tmp_path):
    backend = build_embedding_backend(model_manifest(tmp_path))
    session = backend._session
    frame = np.ones((3, 7, 3), dtype=np.uint8)
    for _ in range(3):
        vector = backend.embed(frame)
        assert vector.shape == (2,) and vector.dtype == np.float32
        np.testing.assert_allclose(vector, [0.6, 0.8])
        assert not vector.flags.writeable
        assert backend._session is session
    assert "CPUExecutionProvider" in backend.providers
    options = session.get_session_options()
    assert options.intra_op_num_threads == 2 and options.inter_op_num_threads == 1
    backend.close()
    assert backend._session is None
    with pytest.raises(RuntimeError, match="closed"):
        backend.embed(frame)


def test_digest_checked_before_runtime_and_verified_bytes_loaded(tmp_path, monkeypatch):
    manifest = model_manifest(tmp_path)
    original = onnxruntime_backend._import_onnxruntime
    monkeypatch.setattr(onnxruntime_backend, "_import_onnxruntime", lambda: pytest.fail("must reject before loading"))
    with pytest.raises(ValueError, match="digest mismatch"):
        build_embedding_backend(manifest.model_copy(update={"sha256": "0" * 64}))
    with pytest.raises(ValueError, match="pinned"):
        build_embedding_backend(manifest.model_copy(update={"sha256": ""}))
    monkeypatch.setattr(onnxruntime_backend, "_import_onnxruntime", original)
    import onnxruntime as ort
    real_session = ort.InferenceSession
    received = []
    def create_session(data, **options):
        assert isinstance(data, bytes)
        assert hashlib.sha256(data).hexdigest() == manifest.sha256
        received.append(data)
        return real_session(data, **options)
    monkeypatch.setattr(ort, "InferenceSession", create_session)
    backend = build_embedding_backend(manifest)
    backend.embed(np.zeros((4, 4, 3), dtype=np.uint8))
    assert len(received) == 1
    backend.close()


@pytest.mark.parametrize("values", [(0.0, 0.0), (float("nan"), 1), (float("inf"), 1)])
def test_invalid_vectors_never_become_references(tmp_path, values):
    backend = build_embedding_backend(model_manifest(tmp_path, values))
    with pytest.raises(ValueError, match="embedding output"):
        backend.embed(np.ones((4, 4, 3), dtype=np.uint8))
    backend.close()


@pytest.mark.parametrize("field,change,reason", [
    ("postprocess", {"embedding_dimension": None}, "explicit"),
    ("postprocess", {"embedding_dimension": 3}, "vector shape"),
    ("postprocess", {"output_name": "wrong"}, "vector shape"),
    ("input", {"width": 5}, "fixed image shape"),
    ("input", {"tensor_name": "wrong"}, "fixed image shape"),
    ("input", {"dtype": "float16"}, "float32 NCHW"),
    ("input", {"normalization": {"mean": [0, 0, 0], "std": [0, 1, 1]}}, "normalization"),
])
def test_declared_input_and_output_contracts_fail_closed(tmp_path, field, change, reason):
    payload = model_manifest(tmp_path).model_dump()
    payload[field].update(change)
    with pytest.raises(ValueError, match=reason):
        build_embedding_backend(ModelManifest.model_validate(payload))


@pytest.mark.parametrize("frame", [
    np.zeros((4, 4), dtype=np.uint8), np.zeros((4, 4, 4), dtype=np.uint8),
    np.zeros((0, 4, 3), dtype=np.uint8), np.zeros((4, 4, 3), dtype=np.float32),
])
def test_input_is_not_silently_converted_or_truncated(tmp_path, frame):
    backend = build_embedding_backend(model_manifest(tmp_path))
    with pytest.raises(ValueError, match="nonempty BGR"):
        backend.embed(frame)
    backend.close()


def test_letterbox_retains_both_edges_and_declared_channel_order(tmp_path):
    manifest = model_manifest(tmp_path)
    image = np.zeros((2, 4, 3), dtype=np.uint8)
    image[:, 0] = (1, 2, 3)
    image[:, -1] = (4, 5, 6)
    tensor, geometry = prepare_onnx_input(image, manifest)
    np.testing.assert_array_equal(tensor[0, :, 1, 0], [3, 2, 1])
    np.testing.assert_array_equal(tensor[0, :, 1, -1], [6, 5, 4])
    assert geometry["offset_y"] == 1 and geometry["resized_width"] == 4


def test_revision_tracks_preparation_and_manifest_mutation_cannot_change_session(tmp_path):
    manifest = model_manifest(tmp_path)
    first = build_embedding_backend(manifest)
    manifest.input.pad_value = 114
    second = build_embedding_backend(manifest)
    assert first.revision != second.revision
    assert first._manifest.input.pad_value == 0
    first.close()
    second.close()
