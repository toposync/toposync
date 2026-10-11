"""Exercise changing pixels: a repeated constant input hides the KleidiAI cache leak."""

from __future__ import annotations

import json
import platform
import subprocess
import sys

import pytest


def _probe() -> dict:
    import resource

    import numpy as np
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper

    random = np.random.default_rng(10710)
    weights = random.normal(size=(8, 3, 3, 3)).astype(np.float32)
    model = helper.make_model(
        helper.make_graph(
            [helper.make_node("Conv", ["image", "weights"], ["output"])],
            "changing_pixels",
            [helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 192, 192])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 8, 190, 190])],
            [numpy_helper.from_array(weights, "weights")],
        ),
        opset_imports=[helper.make_operatorsetid("", 13)],
    )
    model.ir_version = 10
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        model.SerializeToString(), options, providers=["CPUExecutionProvider"]
    )
    warm_bytes = 0
    for iteration in range(288):
        image = random.normal(size=(1, 3, 192, 192)).astype(np.float32)
        output = session.run(None, {"image": image})[0]
        if iteration in (0, 287):
            # Independent numerical oracle; memory reduction must preserve computation.
            expected = np.einsum("ocij,cij->o", weights, image[0, :, :3, :3])
            np.testing.assert_allclose(output[0, :, 0, 0], expected, rtol=1e-4, atol=1e-4)
        if iteration == 31:
            warm_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "version": ort.__version__,
        "growth_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - warm_bytes,
    }


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="Apple Silicon regression; ru_maxrss is measured in bytes on macOS",
)
def test_changing_convolution_inputs_do_not_accumulate_native_memory():
    # A fresh process isolates the native allocator from other tests' high-water marks.
    result = subprocess.run(
        [sys.executable, __file__], capture_output=True, text=True, check=True, timeout=60
    )
    measurement = json.loads(result.stdout)
    assert measurement["growth_bytes"] < 64 * 1024 * 1024, measurement


if __name__ == "__main__":
    print(json.dumps(_probe()))
