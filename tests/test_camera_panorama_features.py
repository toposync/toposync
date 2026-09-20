"""Integrity and correspondence contracts for optional contextual matching."""
import hashlib
import io

import numpy as np
import pytest

from toposync_ext_cameras.processing import panorama_features as features


@pytest.mark.parametrize("payload", [b"verified", b"corrupt!", b"verified-extra"])
def test_model_download_publishes_only_exact_verified_bytes(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(features, "MODELS", {
        "model.onnx": (8, hashlib.sha256(b"verified").hexdigest()),
    })
    monkeypatch.setattr(features.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    if payload == b"verified":
        assert features.matching_model_files(tmp_path) == [tmp_path / "model.onnx"]
        assert (tmp_path / "model.onnx").read_bytes() == payload
    else:
        with pytest.raises(ValueError, match="checksum"):
            features.matching_model_files(tmp_path)
        assert list(tmp_path.iterdir()) == []


def test_changed_cached_model_is_rejected_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MODELS", {
        "model.onnx": (8, hashlib.sha256(b"verified").hexdigest()),
    })
    (tmp_path / "model.onnx").write_bytes(b"corrupt!")
    def forbidden(*args, **kwargs):
        pytest.fail("Existing corrupt bytes must not trigger another download")
    monkeypatch.setattr(features.urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="checksum"):
        features.matching_model_files(tmp_path)
    assert (tmp_path / "model.onnx").read_bytes() == b"corrupt!"


def test_contextual_proposals_require_confidence_and_reciprocity():
    matcher = object.__new__(features.ContextualMatcher)
    matcher.padded_width, matcher.padded_height = 960, 544
    class Inference:
        def run(self, outputs, inputs):
            assert inputs["kpts0"].dtype == np.float32
            return (np.array([[0, 1, 2, -1]]), np.array([[0, 3, 2, -1]]),
                    np.array([[0.5, 0.9, 0.49, 1]]), None)
    matcher.matcher = Inference()
    points = np.float32([[[10, 20], [30, 40], [50, 60], [70, 80]]])
    first, second = matcher.correspondences((points, None), (points + 1, None))
    assert np.array_equal(first, [[10, 20]])
    assert np.array_equal(second, [[11, 21]])
