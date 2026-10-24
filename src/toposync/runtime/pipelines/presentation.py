"""Capture provenance carried with published pixels, never inferred from a player clock."""

from __future__ import annotations

from typing import Any

from .image_geometry import inspection_geometry
from .inspection import frame_reference
from .runtime import Packet


def frame_presentation(packet: Packet, artifact_name: str) -> dict[str, Any] | None:
    reference = frame_reference(packet)
    artifact = packet.artifacts.get(artifact_name)
    if reference is None or not reference["source_id"] or artifact is None or artifact.private:
        return None
    shape = getattr(artifact.data, "shape", ())
    if len(shape) not in (2, 3) or not all(0 < size <= 8192 for size in shape[:2]):
        return None
    return {"frame": reference, "image_size": [shape[1], shape[0]],
            "geometry": inspection_geometry(artifact.metadata.get("image_geometry"), shape[1], shape[0], reference)}
