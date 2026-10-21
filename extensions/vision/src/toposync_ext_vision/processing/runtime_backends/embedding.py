from __future__ import annotations

import hashlib
import json

import numpy as np

from ...registry.manifests import ModelManifest
from .onnxruntime_backend import _OnnxRuntimeSessionBackend, prepare_onnx_input


class OnnxRuntimeEmbeddingBackend(_OnnxRuntimeSessionBackend):
    """One persistent session for fixed-shape image vectors; inputs are BGR uint8."""

    def __init__(self, manifest: ModelManifest) -> None:
        manifest = manifest.model_copy(deep=True)
        if manifest.artifact_format != "onnx" or manifest.input.dtype != "float32" or manifest.input.layout != "nchw":
            raise ValueError("image embeddings require ONNX with float32 NCHW input")
        dimension = manifest.postprocess.embedding_dimension
        if dimension is None or not manifest.postprocess.output_name:
            raise ValueError("image embeddings require an explicit output name and dimension")
        normalization = manifest.input.normalization
        if (len(normalization.mean) != 3 or len(normalization.std) != 3
                or not np.isfinite([*normalization.mean, *normalization.std]).all()
                or min(normalization.std) < 1e-6 or manifest.input.rescale_factor <= 0):
            raise ValueError("image embeddings require finite three-channel normalization with positive scale")
        if len(manifest.sha256) != 64 or any(character not in "0123456789abcdef" for character in manifest.sha256):
            raise ValueError("image embeddings require a pinned SHA-256 digest")
        path = manifest.resolve_artifact_path()
        maximum_bytes = 512 * 1024 * 1024
        if not 0 < path.stat().st_size <= maximum_bytes:
            raise ValueError("embedding artifact must be between 1 byte and 512 MiB")
        with path.open("rb") as stream:
            data = stream.read(maximum_bytes + 1)
        if len(data) > maximum_bytes or hashlib.sha256(data).hexdigest() != manifest.sha256:
            raise ValueError("embedding artifact digest mismatch")
        # Load the verified bytes so replacement of the file cannot change this session's weights.
        super().__init__(manifest, task="embedding", supported_postprocess={"image_embedding"}, model_data=data)
        inputs, outputs = self._session.get_inputs(), self._session.get_outputs()
        if (len(inputs) != 1 or inputs[0].name != self._input_name or inputs[0].type != "tensor(float)"
                or inputs[0].shape != [1, 3, manifest.input.height, manifest.input.width]):
            raise ValueError("embedding input must match the declared fixed image shape")
        if (len(outputs) != 1 or outputs[0].name != manifest.postprocess.output_name
                or outputs[0].type != "tensor(float)" or outputs[0].shape != [1, dimension]):
            raise ValueError("embedding output must match the declared vector shape")
        self.dimension = dimension
        self.revision = hashlib.sha256(json.dumps({
            "contract": "image_embedding_v1",
            "artifact": manifest.sha256, "input": manifest.input.model_dump(),
            "postprocess": manifest.postprocess.model_dump(),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def embed(self, frame: np.ndarray) -> np.ndarray:
        session = self._session
        if session is None:
            raise RuntimeError("embedding session is closed")
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3
                or frame.shape[2] != 3 or min(frame.shape[:2]) < 1
                or frame.shape[0] * frame.shape[1] > 4_000_000):
            raise ValueError("embedding input must be a nonempty BGR uint8 image of at most four megapixels")
        tensor, _ = prepare_onnx_input(frame, self._manifest)
        if not np.isfinite(tensor).all():
            raise ValueError("embedding input normalization produced nonfinite values")
        output = session.run([self._manifest.postprocess.output_name], {self._input_name: tensor})[0]
        if output.shape != (1, self.dimension) or output.dtype != np.float32 or not np.isfinite(output).all():
            raise ValueError("embedding output has invalid shape, precision or values")
        vector = output[0].astype(np.float64)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm <= 1e-12:
            raise ValueError("embedding output has no usable direction")
        normalized = (vector / norm).astype(np.float32)
        normalized.flags.writeable = False
        return normalized

    def close(self) -> None:
        self._session = None


def build_embedding_backend(manifest: ModelManifest) -> OnnxRuntimeEmbeddingBackend:
    return OnnxRuntimeEmbeddingBackend(manifest)
