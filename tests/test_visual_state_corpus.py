from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from visual_state_corpus import CorpusManifest, CorpusSequence, Decision, evaluate_sequence, load_corpus


@pytest.fixture
def sequence() -> dict:
    return {
        "sequence_id": "door-day", "source_id": "synthetic", "session_id": "session-1",
        "partition": "test", "evidence": "synthetic", "label_origin": "synthetic",
        "illumination": "day", "preparation_revision": "p1", "asset": "sequence.json",
        "sha256": "a" * 64, "duration_ms": 4000,
        "truth": [
            {"start_ms": 0, "end_ms": 1000, "class_id": "closed"},
            {"start_ms": 1000, "end_ms": 1100, "class_id": None, "uncertainty": "transition"},
            {"start_ms": 1100, "end_ms": 3000, "class_id": "open"},
            {"start_ms": 3000, "end_ms": 4000, "class_id": None, "uncertainty": "unobservable"},
        ],
        "transitions": [{"from_class": "closed", "to_class": "open", "earliest_ms": 1000, "latest_ms": 1100}],
    }


def manifest(*sequences: dict) -> dict:
    return {"corpus_id": "test", "protocol_revision": "v1", "sequences": list(sequences)}


@pytest.mark.parametrize("alteration,match", [
    ({"sha256": "b" * 64}, "session"),
    ({"session_id": "session-2"}, "asset"),
    ({"session_id": "session-2", "sha256": "b" * 64, "ancestor_hashes": ["a" * 64]}, "asset"),
])
def test_rejects_session_original_and_derivative_leakage(sequence: dict, alteration: dict, match: str) -> None:
    other = {**sequence, "sequence_id": "other", "partition": "calibration", **alteration}
    with pytest.raises(ValidationError, match=match):
        CorpusManifest.model_validate(manifest(sequence, other))


def test_shared_ancestor_cannot_leak_between_distinct_derivatives(sequence: dict) -> None:
    sequence["ancestor_hashes"] = ["c" * 64]
    other = {**sequence, "sequence_id": "other", "session_id": "session-2",
             "partition": "calibration", "sha256": "b" * 64}
    with pytest.raises(ValidationError, match="asset"):
        CorpusManifest.model_validate(manifest(sequence, other))


def test_same_partition_can_reuse_frames_without_independent_session_claim(sequence: dict) -> None:
    other = {**sequence, "sequence_id": "other"}
    result = CorpusManifest.model_validate(manifest(sequence, other))
    assert {item.session_id for item in result.sequences} == {"session-1"}


def test_integrity_and_root_boundary(sequence: dict, tmp_path: Path) -> None:
    asset = tmp_path / "sequence.json"
    asset.write_text("synthetic pixels only", encoding="utf-8")
    sequence["sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(sequence)), encoding="utf-8")
    assert load_corpus(path, asset_root=tmp_path).corpus_id == "test"
    asset.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_corpus(path, asset_root=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sequence.json").symlink_to(asset)
    with pytest.raises(ValueError, match="escapes"):
        load_corpus(path, asset_root=outside)


@pytest.mark.parametrize("changes", [
    {"asset": "../secret"}, {"asset": "/private/image.jpg"},
    {"evidence": "recorded", "label_origin": "synthetic"},
    {"label_origin": "model"}, {"duration_ms": 4001},
])
def test_manifest_rejects_unsafe_or_misrepresented_evidence(sequence: dict, changes: dict) -> None:
    with pytest.raises(ValidationError):
        CorpusSequence.model_validate({**sequence, **changes})


def test_time_weighted_metrics_expiry_and_uncertain_latency(sequence: dict) -> None:
    result = evaluate_sequence(CorpusSequence.model_validate(sequence), [
        Decision(at_ms=0, class_id="closed", valid_until_ms=1000),
        Decision(at_ms=1250, class_id="open", valid_until_ms=2000),
        Decision(at_ms=2000, class_id="open", valid_until_ms=3000),
    ])
    assert result["observable_ms"] == 2900
    assert result["conclusive_ms"] == 2750
    assert result["abstention_ms"] == 150
    assert result["uncertain_ms"] == 1100
    assert result["wrong_state_ms"] == 0
    assert result["expected_transitions"] == result["matched_transitions"] == 1
    assert result["latency_intervals"] == [
        {"from_class": "closed", "to_class": "open", "minimum_ms": 150, "maximum_ms": 250},
    ]


def test_reconfirmation_frames_do_not_inflate_transition_denominator(sequence: dict) -> None:
    decisions = [Decision(at_ms=0, class_id="closed", valid_until_ms=1000)]
    decisions += [Decision(at_ms=at, class_id="open", valid_until_ms=3000) for at in range(1250, 3000, 10)]
    result = evaluate_sequence(CorpusSequence.model_validate(sequence), decisions)
    assert result["expected_transitions"] == result["matched_transitions"] == 1
    assert result["false_transitions"] == 0


def test_oscillation_and_unobservable_claim_are_visible(sequence: dict) -> None:
    result = evaluate_sequence(CorpusSequence.model_validate(sequence), [
        Decision(at_ms=0, class_id="closed", valid_until_ms=1000),
        Decision(at_ms=200, class_id="open", valid_until_ms=1000),
        Decision(at_ms=400, class_id="closed", valid_until_ms=1000),
        Decision(at_ms=1250, class_id="open", valid_until_ms=4000),
    ])
    assert result["false_transitions"] == 1
    assert result["wrong_state_ms"] == 200
    assert result["unobservable_claim_ms"] == 1000
    assert result["confusion_ms"]["closed"]["open"] == 200


def test_total_abstention_is_not_quality_success(sequence: dict) -> None:
    result = evaluate_sequence(CorpusSequence.model_validate(sequence), [])
    assert result["coverage"] == 0
    assert result["missed_transitions"] == 1
    assert result["wrong_state_ms"] == 0


def test_timestamp_order_is_not_silently_repaired(sequence: dict) -> None:
    with pytest.raises(ValueError, match="increasing"):
        evaluate_sequence(CorpusSequence.model_validate(sequence), [
            Decision(at_ms=10, class_id=None), Decision(at_ms=10, class_id=None),
        ])


def test_missing_transition_annotation_cannot_reduce_denominator(sequence: dict) -> None:
    sequence["transitions"] = []
    with pytest.raises(ValidationError, match="every known change"):
        CorpusSequence.model_validate(sequence)


def test_checked_in_manifest_is_synthetic_and_integral() -> None:
    root = Path(__file__).parent / "fixtures" / "visual_state"
    corpus = load_corpus(root / "manifest.json", asset_root=root)
    assert {item.partition for item in corpus.sequences} == {"references", "calibration", "test"}
    assert all(item.evidence == "synthetic" for item in corpus.sequences)
