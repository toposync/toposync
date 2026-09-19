"""Pixel provenance for the image operators.

Matrices map pixel centres to the original image. Unknown operators
must omit this metadata; equal output dimensions alone do not prove identity.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


def image_geometry(width: int, height: int, evidence: dict) -> dict:
    import numpy as np

    return {
        "source_size": [width, height],
        "image_size": [width, height],
        "to_source": np.eye(3).tolist(),
        "capture_evidence": dict(evidence),
    }


def geometry_matrix(geometry: Any, width: int, height: int) -> np.ndarray:
    import numpy as np

    if not isinstance(geometry, dict) or geometry.get("image_size") != [width, height]:
        raise ValueError("Unknown image geometry")
    matrix = np.asarray(geometry.get("to_source"), dtype=float)
    size = geometry.get("source_size")
    if (
        matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
        or abs(np.linalg.det(matrix)) < 1e-12
        or not isinstance(size, list)
        or len(size) != 2
        or any(type(value) is not int or value < 2 for value in size)
    ):
        raise ValueError("Invalid image geometry")
    return matrix


def transformed_geometry(artifact: Any, forward: Any, width: int, height: int) -> dict:
    """Compose an actual OpenCV transform, never inferred from output shape."""
    import numpy as np

    geometry = artifact.metadata.get("image_geometry") if artifact is not None else None
    shape = getattr(getattr(artifact, "data", None), "shape", ())
    if not geometry or len(shape) < 2:
        return {}
    matrix = geometry_matrix(geometry, shape[1], shape[0])
    step = np.asarray(forward, dtype=float)
    if step.shape != (3, 3) or not np.isfinite(step).all():
        return {}
    return {
        "image_geometry": {
            **geometry,
            "image_size": [width, height],
            "to_source": (matrix @ np.linalg.inv(step)).tolist(),
        }
    }


def source_pixels(points: Any, matrix: np.ndarray) -> np.ndarray:
    import numpy as np

    pixels = np.asarray(points, dtype=float)
    homogeneous = np.column_stack((pixels, np.ones(len(pixels)))) @ matrix.T
    if not np.isfinite(homogeneous).all() or np.any(np.abs(homogeneous[:, 2]) < 1e-8):
        raise ValueError("Pixel outside projective support")
    return homogeneous[:, :2] / homogeneous[:, 2:]
