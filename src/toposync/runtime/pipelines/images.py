from __future__ import annotations

from dataclasses import replace
from typing import Any

from .runtime import Artifact, Packet


IMAGE_KEYS_PAYLOAD_FIELD = "images"
STORED_IMAGES_PAYLOAD_FIELD = "stored_images"

# Semantic image keys used in the pipeline UX.
DEFAULT_IMAGE_KEY_TO_ARTIFACT_NAME: dict[str, str] = {
    "original": "frame_original",
    "treated": "frame",
}


def normalize_image_key(value: str) -> str:
    return str(value or "").strip()


def parse_fallback_keys(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list):
        parts = [str(p or "").strip() for p in value]
    else:
        parts = [str(value).strip()]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = normalize_image_key(part)
        if not key or key in seen:
            continue
        out.append(key)
        seen.add(key)
    return out


def _get_images_mapping(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get(IMAGE_KEYS_PAYLOAD_FIELD)
    if isinstance(raw, dict):
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = normalize_image_key(str(k))
            name = normalize_image_key(str(v))
            if key and name:
                out[key] = name
        return out
    return {}


def ensure_packet_image_keys(packet: Packet) -> Packet:
    payload = dict(packet.payload)
    images = _get_images_mapping(payload)
    changed = False
    for key, artifact_name in DEFAULT_IMAGE_KEY_TO_ARTIFACT_NAME.items():
        if key in images:
            continue
        if artifact_name in packet.artifacts:
            images[key] = artifact_name
            changed = True
    if not changed and IMAGE_KEYS_PAYLOAD_FIELD in payload:
        return packet
    payload[IMAGE_KEYS_PAYLOAD_FIELD] = images
    return replace(packet, payload=payload)


def resolve_image_artifact_name(packet: Packet, key_or_name: str) -> tuple[str, str] | None:
    key = normalize_image_key(key_or_name)
    if not key:
        return None
    images = _get_images_mapping(packet.payload)
    mapped = normalize_image_key(images.get(key, ""))
    if mapped:
        return key, mapped
    if key in DEFAULT_IMAGE_KEY_TO_ARTIFACT_NAME:
        return key, DEFAULT_IMAGE_KEY_TO_ARTIFACT_NAME[key]
    return key, key


def resolve_image_artifact_for_data(
    packet: Packet,
    *,
    input_with_fallback: str | list[str] | None,
    fallback_to_stream_frame: bool = True,
) -> tuple[str | None, str | None, Any | None]:
    packet = ensure_packet_image_keys(packet)
    keys = parse_fallback_keys(input_with_fallback)
    for candidate in keys:
        resolved = resolve_image_artifact_name(packet, candidate)
        if resolved is None:
            continue
        key, artifact_name = resolved
        artifact = packet.artifacts.get(artifact_name)
        if artifact is None or artifact.data is None:
            continue
        return key, artifact_name, artifact.data
    if fallback_to_stream_frame:
        for artifact_name in ("frame", "frame_original"):
            artifact = packet.artifacts.get(artifact_name)
            if artifact is None or artifact.data is None:
                continue
            return None, artifact_name, artifact.data
    return None, None, None


def resolve_image_artifact_for_reference(
    packet: Packet,
    *,
    input_with_fallback: str | list[str] | None,
) -> tuple[str | None, str | None, str | None]:
    packet = ensure_packet_image_keys(packet)
    keys = parse_fallback_keys(input_with_fallback)
    for candidate in keys:
        resolved = resolve_image_artifact_name(packet, candidate)
        if resolved is None:
            continue
        key, artifact_name = resolved
        artifact = packet.artifacts.get(artifact_name)
        if artifact is None or not artifact.reference:
            continue
        return key, artifact_name, str(artifact.reference)
    return None, None, None


def set_image_key(packet: Packet, *, key: str, artifact_name: str) -> Packet:
    normalized_key = normalize_image_key(key)
    normalized_name = normalize_image_key(artifact_name)
    if not normalized_key or not normalized_name:
        return packet
    packet = ensure_packet_image_keys(packet)
    payload = dict(packet.payload)
    images = _get_images_mapping(payload)
    images[normalized_key] = normalized_name
    payload[IMAGE_KEYS_PAYLOAD_FIELD] = images
    return replace(packet, payload=payload)


def add_stored_image_entry(
    packet: Packet,
    *,
    key: str,
    artifact: Artifact,
    rel_path: str,
    stored_ts_ms: int,
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
    next_entries.append(
        {
            "rel_path": str(rel_path),
            "artifact_name": str(artifact.name),
            "mime_type": str(artifact.mime_type or "") or None,
            "stored_ts_ms": int(stored_ts_ms),
        },
    )
    stored = dict(stored)
    stored[normalized_key] = next_entries
    payload[STORED_IMAGES_PAYLOAD_FIELD] = stored
    return replace(packet, payload=payload)

