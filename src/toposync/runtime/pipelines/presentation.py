"""Capture provenance carried with published pixels, never inferred from a player clock."""

from __future__ import annotations

from typing import Any

from .image_geometry import geometry_matrix
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
    result = {"frame": reference, "image_size": [shape[1], shape[0]], "geometry": None}
    geometry = artifact.metadata.get("image_geometry")
    if not isinstance(geometry, dict):
        return result
    evidence = geometry.get("capture_evidence")
    if not isinstance(evidence, dict) or any(
        evidence.get(key) != reference[key] for key in ("capture_instance", "generation", "sequence")
    ):
        return result
    try:
        matrix = geometry_matrix(geometry, shape[1], shape[0])
        # Only declared affine/projective support is carried. No implicit lens correction.
        domains = geometry.get("valid_domains", [])
        if not isinstance(domains, list) or len(domains) > 64:
            return result
        validated_domains = []
        for domain in domains:
            domain_matrix = geometry_matrix(
                {"image_size": [shape[1], shape[0]], "source_size": domain["size"],
                 "to_source": domain["matrix"]}, shape[1], shape[0]
            )
            validated_domains.append({"size": list(domain["size"]), "matrix": domain_matrix.tolist()})
        result["geometry"] = {
            "image_size": [shape[1], shape[0]], "source_size": list(geometry["source_size"]),
            "to_source": matrix.tolist(), "valid_domains": validated_domains,
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    return result
