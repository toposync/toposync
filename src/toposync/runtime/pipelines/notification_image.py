"""Construct a bounded, current, in-memory image descriptor; never persist it.

The caller must keep this descriptor out of notification storage. Publication
freshness is not proof of physical exposure timing. This first contract accepts
only integral images with explicit identity geometry, not crop/warp guesses.
"""

import base64
import io
import math
import time
from typing import Any

from .images import normalize_artifact_name
from .packet_contract import resolve_frame_freshness, resolve_media_ts
from .runtime import Lifecycle, Packet


MAX_IMAGE_BYTES = 256 * 1024
MAX_IMAGE_PIXELS = 2_097_152
MAX_IMAGE_AGE_SECONDS = 0.75
_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _dimensions(width: Any, height: Any) -> None:
    if type(width) is not int or type(height) is not int or width < 2 or height < 2:
        raise ValueError("image_dimensions_invalid")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image_pixel_limit")


def _decode_dimensions(blob: bytes) -> tuple[int, int, str]:
    from PIL import Image

    if not blob or len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("image_byte_limit")
    with Image.open(io.BytesIO(blob)) as image:
        mime = _MIME_BY_FORMAT.get(image.format or "")
        if mime is None or getattr(image, "n_frames", 1) != 1:
            raise ValueError("image_encoding_invalid")
        width, height = image.size
        _dimensions(width, height)  # Must run before raster decompression.
        image.verify()
    with Image.open(io.BytesIO(blob)) as image:
        image.load()  # A valid header alone does not establish valid pixels.
        if image.getexif().get(274, 1) != 1:
            raise ValueError("image_orientation_unsupported")
    return width, height, mime


def _pixels_and_dimensions(data: Any) -> tuple[Any, int, int, str | None]:
    import numpy as np
    from PIL import Image

    if isinstance(data, (bytes, bytearray, memoryview)):
        # Check size before copying/decoding, including memoryview item sizes.
        size = data.nbytes if isinstance(data, memoryview) else len(data)
        if size > MAX_IMAGE_BYTES:
            raise ValueError("image_byte_limit")
        pixels = bytes(data)
        width, height, mime = _decode_dimensions(pixels)
        return pixels, width, height, mime
    if isinstance(data, np.ndarray):
        if data.dtype != np.uint8 or not (data.ndim == 2 or (data.ndim == 3 and data.shape[2] == 3)):
            raise ValueError("image_pixels_unsupported")
        height, width = data.shape[:2]
    elif isinstance(data, Image.Image):
        if data.mode not in {"RGB", "L"} or getattr(data, "n_frames", 1) != 1:
            raise ValueError("image_pixels_unsupported")
        if data.getexif().get(274, 1) != 1:
            raise ValueError("image_orientation_unsupported")
        width, height = data.size
    else:
        raise ValueError("image_pixels_unsupported")
    _dimensions(width, height)
    # Snapshot only the explicitly selected pixels, preserving channel order.
    return data.copy(), width, height, None


def _validated_metadata(packet: Packet, artifact: Any, width: int, height: int, name: str) -> tuple[dict, dict]:
    # This existing copier supplies finite strict JSON, node/depth/byte bounds,
    # and independent dictionaries without importing a domain/pose contract.
    from .operators_sinks import _copy_notification_projection

    metadata = artifact.metadata
    if not isinstance(metadata, dict) or not isinstance(metadata.get("image_geometry"), dict):
        raise ValueError("image_geometry_missing")
    copied = _copy_notification_projection({
        "capture": packet.payload.get("capture_evidence"), "geometry": metadata["image_geometry"],
    })
    evidence, geometry = copied["capture"], copied["geometry"]
    if not isinstance(evidence, dict) or geometry.get("capture_evidence") != evidence:
        raise ValueError("image_capture_mismatch")
    for value in (evidence, geometry["capture_evidence"]):
        for key in ("received_monotonic", "captured_monotonic"):
            item = value.get(key)
            if item is not None and (not _number(item) or item < 0):
                raise ValueError("image_capture_invalid")
        if value.get("physical_timestamp_verified") is True:
            if not _number(value.get("captured_monotonic")) or value["captured_monotonic"] <= 0:
                raise ValueError("image_capture_invalid")
        if type(value.get("generation")) is not int or type(value.get("sequence")) is not int:
            raise ValueError("image_capture_invalid")
        if not _number(value.get("published_at")):
            raise ValueError("image_capture_invalid")
        if "physical_timestamp_verified" in value and type(value["physical_timestamp_verified"]) is not bool:
            raise ValueError("image_capture_invalid")
    for key in ("image_size", "source_size"):
        size = geometry.get(key)
        if not isinstance(size, list) or len(size) != 2 or any(type(item) is not int for item in size) or size != [width, height]:
            raise ValueError("image_geometry_mismatch")
    matrix = geometry.get("to_source")
    if not isinstance(matrix, list) or len(matrix) != 3:
        raise ValueError("image_geometry_invalid")
    for row, values in enumerate(matrix):
        if not isinstance(values, list) or len(values) != 3 or any(
            not _number(item) or item != (1 if row == column else 0)
            for column, item in enumerate(values)
        ):
            raise ValueError("image_transform_unsupported")
    for key, expected in (("width", width), ("height", height)):
        if key in metadata and (type(metadata[key]) is not int or metadata[key] != expected):
            raise ValueError("image_dimensions_mismatch")
    if name == "main":
        if packet.payload.get("frame_crop") or packet.payload.get("frame_warp"):
            raise ValueError("image_transform_unsupported")
        media = packet.payload.get("media")
        for key, expected in (("width", width), ("height", height)):
            if isinstance(media, dict) and key in media and (type(media[key]) is not int or media[key] != expected):
                raise ValueError("image_dimensions_mismatch")
    return evidence, geometry


def build_ephemeral_notification_image(packet: Packet, input_artifact_name: str = "") -> tuple[dict | None, str]:
    """Return a camelCase descriptor or a stable unavailability reason.

    No image selection fallback, disk reference dereference, resize, dtype
    coercion or automatic storage. Requires explicit source_stream_id because a
    tracker child stream is not the camera's technical source stream identity.
    """
    if packet.lifecycle == Lifecycle.CLOSE:
        return None, "closed"
    payload = packet.payload
    if not isinstance(payload, dict):
        return None, "image_identity_invalid"
    camera = payload.get("camera_id")
    source = payload.get("source_stream_id")
    if not all(_text(value) for value in (packet.packet_id, camera, source)) or (
        packet.parent_packet_id is not None and not _text(packet.parent_packet_id)
    ):
        return None, "image_identity_invalid"
    media = payload.get("media")
    raw_timestamp = media.get("ts") if isinstance(media, dict) and "ts" in media else next(
        (payload[key] for key in ("frame_ts", "ts") if key in payload), None
    )
    if not _number(raw_timestamp):
        return None, "image_media_timestamp_required"
    timestamp = resolve_media_ts(packet)
    initial_now = time.time()
    freshness = resolve_frame_freshness(packet, now_unix=initial_now)
    if freshness.age_seconds is None:
        return None, freshness.reason
    if freshness.age_seconds >= MAX_IMAGE_AGE_SECONDS:
        return None, "image_expired"
    name = normalize_artifact_name(input_artifact_name)
    artifact = packet.artifacts.get(name)
    if artifact is None:
        return None, "image_artifact_missing"
    if artifact.data is None:
        return None, "image_pixels_missing"
    try:
        pixels, width, height, encoded_mime = _pixels_and_dimensions(artifact.data)
        evidence, geometry = _validated_metadata(packet, artifact, width, height, name)
        if artifact.name != name:
            return None, "image_artifact_identity_mismatch"
        if artifact.mime_type not in (None, "image/raw", encoded_mime):
            return None, "image_mime_mismatch"
        from .operators_sinks import _encode_image_bytes

        blob, _extension, mime = _encode_image_bytes(pixels, fmt="jpg", jpeg_quality=80)
        if not isinstance(blob, bytes) or len(blob) > MAX_IMAGE_BYTES:
            return None, "image_byte_limit"
        decoded_width, decoded_height, decoded_mime = _decode_dimensions(blob)
        if (decoded_width, decoded_height, decoded_mime) != (width, height, mime):
            return None, "image_encoding_invalid"
        expires_at = evidence["published_at"] * 1000 + MAX_IMAGE_AGE_SECONDS * 1000
        now = time.time()
        if not _number(now) or now < initial_now:
            return None, "image_clock_skew"
        if now * 1000 >= expires_at:
            return None, "image_expired"
        return {"schemaVersion": 1, "packetId": packet.packet_id, "parentPacketId": packet.parent_packet_id,
            "cameraId": camera, "sourceStreamId": source, "artifactName": name,
            "mediaTimestamp": timestamp, "captureEvidence": evidence, "imageGeometry": geometry,
            "width": width, "height": height, "mimeType": mime,
            "dataBase64": base64.b64encode(blob).decode("ascii"), "expiresAt": expires_at}, "ready"
    except ValueError as error:
        reason = str(error)
        return None, reason if reason.startswith("image_") and len(reason) < 64 else "image_encoding_invalid"
    except Exception:
        return None, "image_encoding_invalid"
