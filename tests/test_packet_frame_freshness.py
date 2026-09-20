"""Timing-contract tests without cameras, network access, or wall-clock sleeps."""

from dataclasses import replace

import pytest

from toposync.runtime.pipelines.operators_distributed import _deserialize_packet, _serialize_packet
from toposync.runtime.pipelines.packet_contract import resolve_frame_freshness
from toposync.runtime.pipelines.runtime import Packet


def packet(*, media_timestamp=0.2, evidence=None):
    if evidence is None:
        evidence = {
            "capture_instance": "decoder-test",
            "generation": 1,
            "sequence": 3,
            "published_at": 990.0,
            "received_monotonic": 90.0,
            "physical_timestamp_verified": False,
            "captured_monotonic": None,
        }
    return Packet.create(
        stream_id="camera:test",
        payload={
            "capture_evidence": evidence,
            "media": {"ts": media_timestamp},
            "source": {"clock_domain": "device:test"},
        },
    )


def test_recreated_envelope_and_remote_roundtrip_do_not_renew_source_sample():
    original = packet()
    # Mirrors finite event construction: new ID, new created_at and monotonic.
    recreated = Packet.create(
        stream_id="person:test", payload=dict(original.payload), parent_packet_id=original.packet_id
    )
    remote = _deserialize_packet(_serialize_packet(recreated))
    for value in (original, recreated, remote):
        result = resolve_frame_freshness(value, now_unix=1000.0)
        assert result.age_seconds == 10.0
        assert result.reason == "age_available" and result.basis == "source_publication"
        assert value.age_ms() < 1000  # Envelope age is unrelated to this sample.


@pytest.mark.parametrize("media_timestamp", [0.0, 0.2, 1_700_000_000.0, -40.0])
def test_replay_media_clock_is_not_interpreted_as_epoch(media_timestamp):
    source = packet(media_timestamp=media_timestamp)
    source.payload["capture_evidence"]["published_at"] = 999.9
    result = resolve_frame_freshness(source, now_unix=1000)
    assert result.age_seconds == pytest.approx(0.1)
    assert source.payload["media"]["ts"] == media_timestamp


def test_absent_evidence_is_explicit_and_never_uses_envelope_media_or_artifact_fallback():
    source = packet(media_timestamp=1000)
    source.payload.pop("capture_evidence")
    result = resolve_frame_freshness(source, now_unix=1000)
    assert result.age_seconds is None and result.reason == "capture_evidence_missing"
    source.payload["capture_evidence"] = []
    assert resolve_frame_freshness(source, now_unix=1000).reason == "capture_evidence_invalid"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"capture_instance": ""}, "capture_identity_invalid"),
        ({"generation": True}, "capture_identity_invalid"),
        ({"sequence": 0}, "capture_identity_invalid"),
        ({"sequence": 1.5}, "capture_identity_invalid"),
        ({"physical_timestamp_verified": "yes"}, "capture_verification_invalid"),
        ({"published_at": float("nan")}, "capture_published_at_invalid"),
        ({"published_at": float("inf")}, "capture_published_at_invalid"),
        ({"published_at": "990.0"}, "capture_published_at_invalid"),
        ({"published_at": True}, "capture_published_at_invalid"),
        ({"published_at": 0}, "capture_published_at_invalid"),
        ({"published_at": 1000.1}, "capture_clock_skew"),
    ],
)
def test_invalid_authoritative_capture_fields_do_not_fall_back(change, reason):
    source = packet(media_timestamp=1000)
    source.payload["capture_evidence"].update(change)
    result = resolve_frame_freshness(source, now_unix=1000)
    assert result.age_seconds is None and result.reason == reason


def test_monotonic_age_requires_explicit_same_clock_knowledge():
    source = packet()
    source.payload["capture_evidence"]["received_monotonic"] = 1_000_000  # Another host's uptime.
    remote = replace(source, metadata={"dist_target": {"node_id": "consumer"}})
    default = resolve_frame_freshness(remote, now_unix=1000, now_monotonic=100)
    assert default.age_seconds == 10.0 and default.basis == "source_publication"
    # Explicit local callers may use the decoder receive boundary, not exposure.
    source.payload["capture_evidence"]["received_monotonic"] = 99.5
    local = resolve_frame_freshness(source, local_monotonic_clock=True, now_monotonic=100)
    assert local.age_seconds == 0.5 and local.basis == "local_receive"


def test_verified_physical_timestamp_is_used_only_in_shared_local_clock():
    source = packet()
    source.payload["capture_evidence"].update(
        physical_timestamp_verified=True, captured_monotonic=89.0
    )
    local = resolve_frame_freshness(source, local_monotonic_clock=True, now_monotonic=100)
    assert local.age_seconds == 11 and local.basis == "physical_capture"
    remote = resolve_frame_freshness(source, now_unix=1000, now_monotonic=100)
    assert remote.age_seconds == 10 and remote.basis == "source_publication"
    source.payload["capture_evidence"]["captured_monotonic"] = None
    invalid = resolve_frame_freshness(source, local_monotonic_clock=True, now_monotonic=100)
    assert invalid.age_seconds is None and invalid.reason == "capture_captured_monotonic_invalid"


def test_unverified_physical_values_cannot_override_receive_evidence():
    source = packet()
    source.payload["capture_evidence"]["captured_monotonic"] = 99.9
    result = resolve_frame_freshness(source, local_monotonic_clock=True, now_monotonic=100)
    assert result.age_seconds == 10 and result.basis == "local_receive"


def test_remote_future_publication_is_unknown_not_clamped_to_fresh():
    source = packet()
    source.payload["capture_evidence"]["published_at"] = 1001
    result = resolve_frame_freshness(source, now_unix=1000)
    assert result.age_seconds is None and result.reason == "capture_clock_skew"
    # A past publication is only source-wall-clock age. It does not certify
    # clock synchronization or measure buffering before the decoder boundary.
    source.payload["capture_evidence"]["published_at"] = 999
    result = resolve_frame_freshness(source, now_unix=1000)
    assert result.age_seconds == 1 and result.basis == "source_publication"


def test_future_local_monotonic_and_invalid_reference_clock_fail_closed():
    source = packet()
    source.payload["capture_evidence"]["received_monotonic"] = 101
    result = resolve_frame_freshness(source, local_monotonic_clock=True, now_monotonic=100)
    assert result.age_seconds is None and result.reason == "capture_clock_skew"
    assert (
        resolve_frame_freshness(source, now_unix=float("nan")).reason
        == "freshness_reference_clock_invalid"
    )
