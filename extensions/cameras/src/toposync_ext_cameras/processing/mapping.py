from __future__ import annotations

from dataclasses import dataclass
from typing import Any


try:
    import numpy as np  # type: ignore
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ControlPointPair:
    image_u: float
    image_v: float
    world_x: float
    world_z: float


class ControlPointMapper:
    def __init__(self, pairs: list[ControlPointPair]) -> None:
        if np is None:
            raise RuntimeError("numpy is required for control point mapping")
        if len(pairs) < 4:
            raise ValueError("At least 4 control points are required")

        src = np.array([[p.image_u, p.image_v] for p in pairs], dtype=np.float64)
        dst = np.array([[p.world_x, p.world_z] for p in pairs], dtype=np.float64)

        # Homography from normalized image (u,v) -> world plane (x,z)
        # Use DLT via OpenCV if available, otherwise fall back to NumPy solve.
        H = None
        try:
            import cv2  # type: ignore

            H, _mask = cv2.findHomography(src, dst, method=0)
        except Exception:
            H = None

        if H is None:
            H = _solve_homography(src, dst)

        self._H = H
        self._H_inv: Any | None = None

    def map(self, u: float, v: float) -> tuple[float, float] | None:
        if np is None:
            return None
        p = np.array([float(u), float(v), 1.0], dtype=np.float64)
        out = self._H @ p
        w = float(out[2]) if out.shape[0] >= 3 else 0.0
        if abs(w) < 1e-9:
            return None
        x = float(out[0]) / w
        z = float(out[1]) / w
        if not (x == x and z == z):
            return None
        return x, z

    def map_image_to_world(self, u: float, v: float) -> tuple[float, float] | None:
        return self.map(u, v)

    def map_world_to_image(self, x: float, z: float) -> tuple[float, float] | None:
        if np is None:
            return None

        inv = self._H_inv
        if inv is None:
            try:
                inv = np.linalg.inv(self._H)
            except Exception:
                return None
            self._H_inv = inv

        p = np.array([float(x), float(z), 1.0], dtype=np.float64)
        out = inv @ p
        w = float(out[2]) if out.shape[0] >= 3 else 0.0
        if abs(w) < 1e-9:
            return None
        u = float(out[0]) / w
        v = float(out[1]) / w
        if not (u == u and v == v):
            return None
        return u, v


def _solve_homography(src: Any, dst: Any) -> Any:
    if np is None:
        raise RuntimeError("numpy is required for control point mapping")

    # Direct Linear Transform for homography. Returns H such that dst ~ H * src.
    A = []
    for (u, v), (x, z) in zip(src, dst, strict=False):
        A.append([-u, -v, -1, 0, 0, 0, u * x, v * x, x])
        A.append([0, 0, 0, -u, -v, -1, u * z, v * z, z])
    A = np.array(A, dtype=np.float64)
    _u, _s, vh = np.linalg.svd(A)
    h = vh[-1, :]
    H = h.reshape((3, 3))
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
    return H
