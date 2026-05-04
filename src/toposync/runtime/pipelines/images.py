from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from .runtime import Artifact, Packet


MAIN_ARTIFACT_NAME = "main"
STORED_IMAGES_PAYLOAD_FIELD = "stored_images"


def normalize_artifact_name(value: str | None, *, default: str = MAIN_ARTIFACT_NAME) -> str:
    normalized = str(value or "").strip()
    return normalized or str(default)


def normalize_image_key(value: str | None) -> str:
    return str(value or "").strip()


def resolve_image_artifact_for_data(
    packet: Packet,
    *,
    input_artifact_name: str | None = None,
) -> tuple[str, Any | None]:
    artifact_name = normalize_artifact_name(input_artifact_name)
    artifact = packet.artifacts.get(artifact_name)
    if artifact is None or artifact.data is None:
        return artifact_name, None
    return artifact_name, artifact.data


def resolve_image_artifact_for_reference(
    packet: Packet,
    *,
    input_artifact_name: str | None = None,
) -> tuple[str, str | None]:
    artifact_name = normalize_artifact_name(input_artifact_name)
    artifact = packet.artifacts.get(artifact_name)
    if artifact is None or not artifact.reference:
        return artifact_name, None
    return artifact_name, str(artifact.reference)


def add_stored_image_entry(
    packet: Packet,
    *,
    key: str,
    artifact: Artifact,
    rel_path: str,
    stored_ts_ms: int,
    confidence: float | None = None,
    max_entries_per_key: int = 64,
) -> Packet:
    normalized_key = normalize_image_key(key)
    if not normalized_key:
        return packet
    payload = dict(packet.payload)
    raw = payload.get(STORED_IMAGES_PAYLOAD_FIELD)
    stored: dict[str, Any] = raw if isinstance(raw, dict) else {}
    entries_raw = stored.get(normalized_key)
    entries = entries_raw if isinstance(entries_raw, list) else []

    next_entries = list(entries)[-max(0, int(max_entries_per_key) - 1) :]
    entry: dict[str, Any] = {
        "rel_path": str(rel_path),
        "artifact_name": str(artifact.name),
        "mime_type": str(artifact.mime_type or "") or None,
        "stored_ts_ms": int(stored_ts_ms),
    }
    if confidence is not None:
        try:
            parsed = float(confidence)
        except Exception:
            parsed = None
        if parsed is not None and math.isfinite(parsed) and parsed >= 0.0:
            entry["confidence"] = float(parsed)

    next_entries.append(entry)
    stored = dict(stored)
    stored[normalized_key] = next_entries
    payload[STORED_IMAGES_PAYLOAD_FIELD] = stored
    return replace(packet, payload=payload)
