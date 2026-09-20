"""Ephemeral image construction never reads references or writes image files."""

import base64
import copy
from dataclasses import replace
import io
import json

import numpy as np
from PIL import Image
import pytest

from toposync.runtime.pipelines import notification_image
from toposync.runtime.pipelines.operators_distributed import _deserialize_packet, _serialize_packet
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(notification_image.time, "time", lambda: 1000.1)


def packet(width=64, height=48):
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[..., 2] = 240  # Existing encoder interprets arrays as BGR, not RGB.
    evidence = {"capture_instance": "decoder-a", "generation": 2, "sequence": 7,
                "published_at": 1000.0, "received_monotonic": 10.5,
                "physical_timestamp_verified": False, "captured_monotonic": None}
    return Packet.create(packet_id="event-packet", parent_packet_id="image-packet",
        stream_id="event-stream", lifecycle=Lifecycle.UPDATE,
        payload={"camera_id": "camera-a", "source_stream_id": "camera-source",
                 "media": {"ts": 42.5, "width": width, "height": height},
                 "capture_evidence": evidence},
        artifacts={"main": Artifact(name="main", data=pixels, mime_type="image/raw",
            metadata={"width": width, "height": height,
                      "image_geometry": image_geometry(width, height, evidence)})})


def build(value, name=""):
    return notification_image.build_ephemeral_notification_image(value, name)


@pytest.mark.parametrize("mime", [None, "image/raw", "image/png"])
def test_distributed_roundtrip_preserves_image_mime_and_preview_decision(mime):
    original = packet()
    original = original.with_artifact(replace(original.artifacts["main"], mime_type=mime))
    restored = _deserialize_packet(json.loads(json.dumps(_serialize_packet(original))))
    assert restored.artifacts["main"].mime_type == mime
    np.testing.assert_array_equal(restored.artifacts["main"].data, original.artifacts["main"].data)
    assert restored.artifacts["main"].metadata == original.artifacts["main"].metadata
    assert build(restored) == build(original)
    assert build(restored)[1] == ("image_mime_mismatch" if mime == "image/png" else "ready")


def test_exact_artifact_encodes_decodable_jpeg_with_original_binding_without_mutation(tmp_path):
    value = packet()
    before_payload = copy.deepcopy(value.payload)
    before_metadata = copy.deepcopy(value.artifacts["main"].metadata)
    before_pixels = value.artifacts["main"].data.copy()
    descriptor, reason = build(value)
    assert reason == "ready"
    assert set(descriptor) == {"schemaVersion", "packetId", "parentPacketId", "cameraId",
        "sourceStreamId", "artifactName", "mediaTimestamp", "captureEvidence", "imageGeometry",
        "width", "height", "mimeType", "dataBase64", "expiresAt"}
    assert descriptor["schemaVersion"] == 1
    assert descriptor["packetId"] == "event-packet"
    assert descriptor["parentPacketId"] == "image-packet"
    assert descriptor["cameraId"] == "camera-a"
    assert descriptor["sourceStreamId"] == "camera-source"
    assert descriptor["artifactName"] == "main"
    assert descriptor["mediaTimestamp"] == 42.5
    assert descriptor["expiresAt"] == 1000750
    assert descriptor["captureEvidence"] == before_payload["capture_evidence"]
    assert descriptor["imageGeometry"] == before_metadata["image_geometry"]
    assert descriptor["mimeType"] == "image/jpeg"
    blob = base64.b64decode(descriptor["dataBase64"], validate=True)
    assert len(blob) <= 256 * 1024
    with Image.open(io.BytesIO(blob)) as decoded:
        decoded.load()
        assert decoded.size == (64, 48)
        assert decoded.getpixel((20, 20))[0] > 230
        assert decoded.getpixel((20, 20))[2] < 10
    assert value.payload == before_payload
    assert value.artifacts["main"].metadata == before_metadata
    np.testing.assert_array_equal(value.artifacts["main"].data, before_pixels)
    descriptor["captureEvidence"]["sequence"] = 99
    descriptor["imageGeometry"]["to_source"][0][0] = 9
    assert value.payload == before_payload
    assert value.artifacts["main"].metadata == before_metadata
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_encoded_images_require_real_decode_and_preserve_permitted_mime(fmt, mime):
    value = packet()
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (12, 30, 40)).save(buffer, format=fmt)
    blob = buffer.getvalue()
    value = value.with_artifact(replace(value.artifacts["main"], data=blob, mime_type=mime))
    descriptor, reason = build(value)
    assert reason == "ready"
    assert descriptor["mimeType"] == mime
    assert base64.b64decode(descriptor["dataBase64"]) == blob


def test_explicit_artifact_only_no_fallback_to_other_pixels_or_file_reference():
    value = packet()
    assert build(value, "missing") == (None, "image_artifact_missing")
    original = replace(value.artifacts["main"], name="original")
    value = replace(value, artifacts={"original": original, "main": Artifact(name="main", reference="/not/read.png")})
    assert build(value) == (None, "image_pixels_missing")
    assert build(value, "original")[0]["artifactName"] == "original"


@pytest.mark.parametrize("mutate", [
    lambda p: p.payload.pop("capture_evidence"),
    lambda p: p.payload["capture_evidence"].update(published_at=999.0),
    lambda p: p.payload["capture_evidence"].update(published_at=1000.2),
    lambda p: p.payload["capture_evidence"].update(published_at=float("nan")),
    lambda p: p.payload["capture_evidence"].update(published_at=True),
    lambda p: p.payload["capture_evidence"].update(sequence=True),
    lambda p: p.payload["capture_evidence"].update(generation=2.0),
    lambda p: p.payload["capture_evidence"].update(capture_instance=""),
    lambda p: p.payload["capture_evidence"].update(physical_timestamp_verified=1),
    lambda p: p.payload["capture_evidence"].update(received_monotonic=float("inf")),
    lambda p: p.payload["capture_evidence"].update(received_monotonic=True),
    lambda p: p.artifacts["main"].metadata["image_geometry"]["capture_evidence"].update(sequence=8),
    lambda p: p.artifacts["main"].metadata["image_geometry"].update(image_size=[63, 48]),
    lambda p: p.artifacts["main"].metadata["image_geometry"].update(source_size=[128, 96]),
    lambda p: p.artifacts["main"].metadata["image_geometry"].update(to_source=[[1, 0, 1], [0, 1, 0], [0, 0, 1]]),
    lambda p: p.artifacts["main"].metadata["image_geometry"].update(to_source=[[True, 0, 0], [0, 1, 0], [0, 0, 1]]),
    lambda p: p.artifacts["main"].metadata["image_geometry"].update(to_source=[[1, 0, 0], [0, float("nan"), 0], [0, 0, 1]]),
    lambda p: p.artifacts["main"].metadata.update(width=True),
    lambda p: p.artifacts["main"].metadata.update(height=47),
    lambda p: p.artifacts["main"].metadata.pop("image_geometry"),
    lambda p: p.payload["media"].update(width=63),
    lambda p: p.payload["media"].update(ts=True),
    lambda p: p.payload["media"].update(ts=float("nan")),
    lambda p: p.payload.pop("media"),
    lambda p: p.payload.update(camera_id=""),
    lambda p: p.payload.update(source_stream_id=False),
    lambda p: p.payload.pop("source_stream_id"),
    lambda p: p.payload.update(frame_crop={"x": 0, "y": 0, "w": 1, "h": 1}),
    lambda p: p.payload.update(frame_warp={"matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}),
])
def test_unavailable_for_invalid_or_conflicting_evidence(mutate):
    value = packet()
    mutate(value)
    descriptor, reason = build(value)
    assert descriptor is None
    assert reason and reason != "ready"


def test_close_abstains_before_inspecting_pixels():
    class Explosive(dict):
        def get(self, *_args):
            raise AssertionError("CLOSE cannot inspect artifacts")
    value = replace(packet(), lifecycle=Lifecycle.CLOSE, artifacts=Explosive())
    assert build(value) == (None, "closed")


@pytest.mark.parametrize("data", [b"\xff\xd8\xffgarbage", b"\x89PNG\r\n\x1a\ngarbage", b"<svg/>",
    np.zeros((48, 64, 3), dtype=np.float32), np.zeros((48, 64, 2), dtype=np.uint8)])
def test_encoded_malformed_and_unsupported_pixel_types_fail_closed(data):
    value = packet().with_artifact(replace(packet().artifacts["main"], data=data))
    assert build(value)[0] is None


def test_pixel_and_encoded_limits_do_not_resize_or_reencode_to_fit(monkeypatch):
    assert build(packet(width=2049, height=1024)) == (None, "image_pixel_limit")
    assert build(packet(width=2048, height=1024))[1] == "ready"
    value = packet().with_artifact(replace(packet().artifacts["main"], data=b"x" * (256 * 1024 + 1)))
    assert build(value) == (None, "image_byte_limit")
    from toposync.runtime.pipelines import operators_sinks
    monkeypatch.setattr(operators_sinks, "_encode_image_bytes", lambda *_a, **_k: (b"x" * (256 * 1024 + 1), ".jpg", "image/jpeg"))
    assert build(packet()) == (None, "image_byte_limit")


def test_rechecks_deadline_after_encoding_without_renewal(monkeypatch):
    from toposync.runtime.pipelines import operators_sinks
    original = operators_sinks._encode_image_bytes
    now = [1000.1]
    monkeypatch.setattr(notification_image.time, "time", lambda: now[0])

    def slow_encoder(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 1000.75
        return result

    monkeypatch.setattr(operators_sinks, "_encode_image_bytes", slow_encoder)
    assert build(packet()) == (None, "image_expired")


def test_output_encoder_failure_and_corrupt_output_are_unavailable(monkeypatch):
    from toposync.runtime.pipelines import operators_sinks
    monkeypatch.setattr(operators_sinks, "_encode_image_bytes", lambda *_a, **_k: (b"\xff\xd8\xffbad", ".jpg", "image/jpeg"))
    assert build(packet()) == (None, "image_encoding_invalid")


def test_encoded_pixel_limit_is_checked_before_raster_load(monkeypatch):
    buffer = io.BytesIO()
    Image.new("RGB", (2049, 1024)).save(buffer, format="PNG")
    value = packet().with_artifact(replace(packet().artifacts["main"], data=buffer.getvalue(), mime_type="image/png"))

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("Oversized raster must not be decompressed")

    monkeypatch.setattr(Image.Image, "load", forbidden_load)
    assert build(value) == (None, "image_pixel_limit")


def test_encoded_orientation_and_animation_are_not_silently_normalized():
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (64, 48)).save(buffer, format="JPEG", exif=exif)
    value = packet().with_artifact(replace(packet().artifacts["main"], data=buffer.getvalue(), mime_type="image/jpeg"))
    assert build(value) == (None, "image_orientation_unsupported")
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buffer, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (64, 48), "blue")], duration=100, loop=0)
    value = packet().with_artifact(replace(packet().artifacts["main"], data=buffer.getvalue(), mime_type="image/webp"))
    assert build(value) == (None, "image_encoding_invalid")


def test_encoding_errors_do_not_expose_internal_details(monkeypatch):
    from toposync.runtime.pipelines import operators_sinks

    def broken_encoder(*_args, **_kwargs):
        raise RuntimeError("private encoder details")

    monkeypatch.setattr(operators_sinks, "_encode_image_bytes", broken_encoder)
    assert build(packet()) == (None, "image_encoding_invalid")


def test_post_encoding_wall_clock_rollback_cannot_extend_lifetime(monkeypatch):
    from toposync.runtime.pipelines import operators_sinks
    original = operators_sinks._encode_image_bytes
    now = [1000.1]
    monkeypatch.setattr(notification_image.time, "time", lambda: now[0])

    def rollback(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 999.9
        return result

    monkeypatch.setattr(operators_sinks, "_encode_image_bytes", rollback)
    assert build(packet()) == (None, "image_clock_skew")


def test_direct_source_and_explicit_legacy_media_timestamp_are_supported():
    value = packet()
    value.payload.pop("media")
    value.payload["frame_ts"] = 0.0  # Valid media origin, not a Unix freshness clock.
    value = replace(value, parent_packet_id=None, stream_id="camera-source")
    descriptor, reason = build(value)
    assert reason == "ready"
    assert descriptor["parentPacketId"] is None
    assert descriptor["sourceStreamId"] == "camera-source"
    assert descriptor["mediaTimestamp"] == 0
    json.dumps(descriptor, allow_nan=False)
