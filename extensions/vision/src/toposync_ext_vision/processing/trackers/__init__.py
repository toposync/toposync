from __future__ import annotations

from typing import Any

from .byte_world import ByteWorldTrackerBackend
from .norfair_tracker import NorfairTrackerBackend
from .simple_iou_kalman import SimpleIouKalmanTrackerBackend


def build_tracker_backend(
    tracker_id: str,
    *,
    close_after_seconds: float,
    open_confidence_threshold: float = 0.50,
    continue_confidence_threshold: float = 0.25,
    use_world_anchor: str = "auto",
    world_match_distance_meters: float = 3.0,
):
    normalized = str(tracker_id or "").strip().lower() or "byte_world"
    if normalized == "byte_world":
        return ByteWorldTrackerBackend(
            close_after_seconds=close_after_seconds,
            open_confidence_threshold=open_confidence_threshold,
            continue_confidence_threshold=continue_confidence_threshold,
            use_world_anchor=use_world_anchor,
            world_match_distance_meters=world_match_distance_meters,
        )
    if normalized == "simple_iou_kalman":
        return SimpleIouKalmanTrackerBackend(close_after_seconds=close_after_seconds)
    if normalized == "norfair":
        return NorfairTrackerBackend(close_after_seconds=close_after_seconds)
    raise ValueError(f"Unknown tracker backend: {normalized}")


def available_tracker_backends() -> list[dict[str, Any]]:
    trackers: list[dict[str, Any]] = [
        {
            "id": "byte_world",
            "available": True,
            "description": "Toposync primary tracker with ByteTrack-style confidence bands and world-anchor matching.",
        },
        {
            "id": "simple_iou_kalman",
            "available": True,
            "description": "Toposync lightweight IoU + Kalman tracker kept as an internal benchmark baseline.",
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
    "ByteWorldTrackerBackend",
    "NorfairTrackerBackend",
    "SimpleIouKalmanTrackerBackend",
    "available_tracker_backends",
    "build_tracker_backend",
]
