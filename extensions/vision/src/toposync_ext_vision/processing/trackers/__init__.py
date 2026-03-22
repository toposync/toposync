from __future__ import annotations

from typing import Any

from .norfair_tracker import NorfairTrackerBackend
from .simple_iou_kalman import SimpleIouKalmanTrackerBackend


def build_tracker_backend(
    tracker_id: str,
    *,
    close_after_seconds: float,
):
    normalized = str(tracker_id or "").strip().lower() or "simple_iou_kalman"
    if normalized == "simple_iou_kalman":
        return SimpleIouKalmanTrackerBackend(close_after_seconds=close_after_seconds)
    if normalized == "norfair":
        return NorfairTrackerBackend(close_after_seconds=close_after_seconds)
    raise ValueError(f"Unknown tracker backend: {normalized}")


def available_tracker_backends() -> list[dict[str, Any]]:
    trackers: list[dict[str, Any]] = [
        {
            "id": "simple_iou_kalman",
            "available": True,
            "description": "Toposync lightweight IoU + Kalman tracker for CPU-first deployments.",
        }
    ]
    try:
        import norfair  # type: ignore

        trackers.append(
            {
                "id": "norfair",
                "available": True,
                "version": str(getattr(norfair, "__version__", "") or ""),
                "description": "Detector-agnostic tracker adapter based on Norfair.",
            }
        )
    except Exception as exc:  # noqa: BLE001
        trackers.append(
            {
                "id": "norfair",
                "available": False,
                "version": "",
                "description": "Detector-agnostic tracker adapter based on Norfair.",
                "error": str(exc),
            }
        )
    return trackers


__all__ = [
    "NorfairTrackerBackend",
    "SimpleIouKalmanTrackerBackend",
    "available_tracker_backends",
    "build_tracker_backend",
]
