"""Optional contextual correspondences, measured against original photographs.

DISK and LightGlue weights: Apache-2.0, https://github.com/cvg/LightGlue.
Pinned ONNX export: https://github.com/fabio-sim/LightGlue-ONNX/releases/tag/v0.1.0.
No image leaves this process. Scores propose pairs; geometry decides eligibility.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

import cv2
import numpy as np


MODELS = {
    "disk_1024.onnx": (4405578, "f1242f4af52a649310cf00cb345ca8c1cae974b853e62ffaac01cac1791fa9c1"),
    "disk_lightglue.onnx": (46596282, "025c36dddb245d70cf80674b75eebbbac7e20479a82b3ef3897c36b36d095406"),
}


def matching_model_files(directory: Path) -> list[Path]:
    """Bounded, checksum-verified acquisition into the camera's private runtime."""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name, (size, checksum) in MODELS.items():
        destination = directory / name
        if not destination.exists():
            temporary = None
            try:
                with urllib.request.urlopen(
                    "https://github.com/fabio-sim/LightGlue-ONNX/releases/download/v0.1.0/" + name,
                    timeout=10,
                ) as response, tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                    temporary = Path(output.name)
                    remaining = size + 1
                    while remaining:
                        chunk = response.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                if temporary.stat().st_size != size or hashlib.sha256(temporary.read_bytes()).hexdigest() != checksum:
                    raise ValueError("Contextual matching model checksum mismatch")
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if destination.stat().st_size != size or hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
            raise ValueError("Contextual matching model checksum mismatch")
        files.append(destination)
    return files


class ContextualMatcher:
    def __init__(self, directory: Path, width: int, height: int):
        import onnxruntime as ort

        extractor, matcher = matching_model_files(directory)
        self.width, self.height = width, height
        self.padded_width, self.padded_height = (width + 15) // 16 * 16, (height + 15) // 16 * 16
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_free_dimension_override_by_name("width", self.padded_width)
        options.add_free_dimension_override_by_name("height", self.padded_height)
        providers: list = ["CPUExecutionProvider"]
        if "CoreMLExecutionProvider" in ort.get_available_providers():
            providers.insert(0, ("CoreMLExecutionProvider", {
                "ModelFormat": "MLProgram", "MLComputeUnits": "ALL", "RequireStaticInputShapes": "1",
            }))
        try:
            self.extractor = ort.InferenceSession(str(extractor), options, providers=providers)
        except Exception:
            self.extractor = ort.InferenceSession(str(extractor), options, providers=["CPUExecutionProvider"])
        matching_options = ort.SessionOptions()
        matching_options.intra_op_num_threads = 2
        matching_options.inter_op_num_threads = 1
        self.matcher = ort.InferenceSession(str(matcher), matching_options, providers=["CPUExecutionProvider"])

    def features(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB if image.ndim == 2 else cv2.COLOR_BGR2RGB)
        image = cv2.copyMakeBorder(image, 0, self.padded_height - self.height,
                                  0, self.padded_width - self.width, cv2.BORDER_CONSTANT)
        tensor = np.ascontiguousarray(image.transpose(2, 0, 1)[None], dtype=np.float32) / 255
        points, _, descriptors = self.extractor.run(None, {"image": tensor})
        valid = (points[0, :, 0] < self.width) & (points[0, :, 1] < self.height)
        return points[:, valid].astype(np.float32), descriptors[:, valid]

    def correspondences(self, first: tuple, second: tuple) -> tuple[np.ndarray, np.ndarray]:
        shift = np.array([self.padded_width, self.padded_height], np.float32) / 2
        scale = max(self.padded_width, self.padded_height) / 2
        forward, reverse, scores, _ = self.matcher.run(None, {
            "kpts0": (first[0] - shift) / scale, "kpts1": (second[0] - shift) / scale,
            "desc0": first[1], "desc1": second[1],
        })
        indices = np.flatnonzero((forward[0] >= 0) & (scores[0] >= 0.5))
        targets = forward[0, indices]
        mutual = reverse[0, targets] == indices
        return first[0][0, indices[mutual]], second[0][0, targets[mutual]]
