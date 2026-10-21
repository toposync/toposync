"""Offline corpus integrity and event/time metrics for visual sensor qualification.

This runner consumes explicit manifests and decisions, never camera connections.
Synthetic corpus acceptance is not evidence of vision quality.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from toposync_ext_vision.visual_state.schemas import Identifier, VisualStateModel


Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Time = Annotated[int, Field(strict=True, ge=0)]


class TruthInterval(VisualStateModel):
    start_ms: Time
    end_ms: Time
    class_id: Identifier | None
    uncertainty: Literal["none", "transition", "unobservable"] = "none"

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("truth intervals must have positive duration")
        if (self.class_id is None) != (self.uncertainty != "none"):
            raise ValueError("unknown ground truth requires an explicit uncertainty reason")
        return self


class TruthTransition(VisualStateModel):
    from_class: Identifier
    to_class: Identifier
    earliest_ms: Time
    latest_ms: Time

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if self.from_class == self.to_class or self.latest_ms < self.earliest_ms:
            raise ValueError("transition requires different classes and an ordered time window")
        return self


class CorpusSequence(VisualStateModel):
    sequence_id: Identifier
    source_id: Identifier
    session_id: Identifier
    partition: Literal["references", "calibration", "test"]
    evidence: Literal["synthetic", "recorded"]
    label_origin: Literal["synthetic", "human", "instrument"]
    illumination: Literal["day", "twilight", "infrared", "artificial", "unknown"]
    preparation_revision: Identifier
    asset: str = Field(min_length=1, max_length=1000)
    sha256: Digest
    ancestor_hashes: tuple[Digest, ...] = ()
    duration_ms: int = Field(strict=True, gt=0)
    truth: tuple[TruthInterval, ...] = Field(min_length=1)
    transitions: tuple[TruthTransition, ...] = ()

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        if self.evidence == "recorded" and self.label_origin == "synthetic":
            raise ValueError("recorded evidence needs independent human or instrument labels")
        asset = Path(self.asset)
        if asset.is_absolute() or ".." in asset.parts:
            raise ValueError("asset must stay relative to the explicit corpus root")
        expected_start = 0
        for interval in self.truth:
            if interval.start_ms != expected_start:
                raise ValueError("truth must cover the sequence without gaps or overlaps")
            expected_start = interval.end_ms
        if expected_start != self.duration_ms:
            raise ValueError("truth must cover the complete duration")
        expected_transitions = []
        previous_known = None
        for interval in self.truth:
            if interval.uncertainty == "unobservable":
                previous_known = None
            elif interval.class_id is not None:
                if previous_known is not None and previous_known.class_id != interval.class_id:
                    expected_transitions.append((previous_known.class_id, interval.class_id,
                                                 previous_known.end_ms, interval.start_ms))
                previous_known = interval
        annotated = [(item.from_class, item.to_class, item.earliest_ms, item.latest_ms)
                     for item in self.transitions]
        if annotated != expected_transitions:
            raise ValueError("transition annotations must cover every known change and its uncertainty")
        previous_end = -1
        for transition in self.transitions:
            if transition.earliest_ms <= previous_end or transition.latest_ms >= self.duration_ms:
                raise ValueError("transition windows must be ordered, disjoint and within duration")
            before = [span for span in self.truth if span.end_ms <= transition.earliest_ms]
            after = [span for span in self.truth if span.start_ms <= transition.latest_ms < span.end_ms]
            if not before or not after or before[-1].class_id != transition.from_class:
                raise ValueError("transition source must agree with preceding ground truth")
            if after[0].class_id != transition.to_class:
                raise ValueError("transition target must agree with subsequent ground truth")
            previous_end = transition.latest_ms
        return self


class CorpusManifest(VisualStateModel):
    schema_version: Literal[1] = 1
    corpus_id: Identifier
    protocol_revision: Identifier
    sequences: tuple[CorpusSequence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_leakage(self) -> Self:
        identifiers: set[str] = set()
        sessions: dict[tuple[str, str], str] = {}
        hashes: dict[str, str] = {}
        for sequence in self.sequences:
            if sequence.sequence_id in identifiers:
                raise ValueError("sequence identifiers must be unique")
            identifiers.add(sequence.sequence_id)
            session = (sequence.source_id, sequence.session_id)
            if sessions.setdefault(session, sequence.partition) != sequence.partition:
                raise ValueError("a recording session cannot cross partitions")
            for digest in (sequence.sha256, *sequence.ancestor_hashes):
                if hashes.setdefault(digest, sequence.partition) != sequence.partition:
                    raise ValueError("an original or derived asset cannot cross partitions")
        return self


def load_corpus(manifest_path: Path, *, asset_root: Path) -> CorpusManifest:
    manifest = CorpusManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    root = asset_root.resolve(strict=True)
    for sequence in manifest.sequences:
        asset = (root / sequence.asset).resolve(strict=True)
        if not asset.is_relative_to(root) or not asset.is_file():
            raise ValueError("asset escapes corpus root or is not a file")
        with asset.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != sequence.sha256:
            raise ValueError(f"asset digest mismatch for sequence {sequence.sequence_id}")
    return manifest


class Decision(VisualStateModel):
    at_ms: Time
    class_id: Identifier | None
    valid_until_ms: Time | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.class_id is None:
            if self.valid_until_ms is not None:
                raise ValueError("unknown decision cannot have validity")
        elif self.valid_until_ms is None or self.valid_until_ms <= self.at_ms:
            raise ValueError("confirmed decision must have a future expiry")
        return self


def evaluate_sequence(sequence: CorpusSequence, decisions: list[Decision]) -> dict:
    """Integrate time coverage and match each annotated transition at most once.

    A transition matched after its annotation window has latency measured as a range.
    Adjacent frames are never counted as independent trials.
    """
    if any(item.at_ms >= sequence.duration_ms for item in decisions):
        raise ValueError("decisions must fall inside the sequence")
    if any(left.at_ms >= right.at_ms for left, right in zip(decisions, decisions[1:])):
        raise ValueError("decisions must have strictly increasing observation times")
    known_ms = sum(span.end_ms - span.start_ms for span in sequence.truth if span.class_id is not None)
    conclusive_ms = wrong_ms = unobservable_claim_ms = 0
    confusion_ms: dict[str, dict[str, int]] = {}
    for index, decision in enumerate(decisions):
        if decision.class_id is None:
            continue
        end = min(decision.valid_until_ms, sequence.duration_ms,
                  decisions[index + 1].at_ms if index + 1 < len(decisions) else sequence.duration_ms)
        for span in sequence.truth:
            overlap = max(0, min(end, span.end_ms) - max(decision.at_ms, span.start_ms))
            if span.class_id is not None:
                conclusive_ms += overlap
                wrong_ms += overlap if span.class_id != decision.class_id else 0
                if overlap:
                    row = confusion_ms.setdefault(span.class_id, {})
                    row[decision.class_id] = row.get(decision.class_id, 0) + overlap
            elif span.uncertainty == "unobservable":
                unobservable_claim_ms += overlap

    changes: list[Decision] = []
    previous_class = None
    for decision in decisions:
        if decision.class_id is not None and decision.class_id != previous_class:
            changes.append(decision)
            previous_class = decision.class_id
    matched: set[int] = set()
    latencies = []
    for index, transition in enumerate(sequence.transitions):
        next_start = (sequence.transitions[index + 1].earliest_ms
                      if index + 1 < len(sequence.transitions) else sequence.duration_ms)
        for change_index, change in enumerate(changes):
            if (change_index not in matched and change.class_id == transition.to_class
                    and transition.earliest_ms <= change.at_ms < next_start):
                matched.add(change_index)
                latencies.append({
                    "from_class": transition.from_class, "to_class": transition.to_class,
                    "minimum_ms": max(0, change.at_ms - transition.latest_ms),
                    "maximum_ms": change.at_ms - transition.earliest_ms,
                })
                break
    false_transitions = 0
    for index, change in enumerate(changes):
        if index in matched:
            continue
        span = next(item for item in sequence.truth if item.start_ms <= change.at_ms < item.end_ms)
        if span.class_id is not None and span.class_id != change.class_id:
            false_transitions += 1
    return {
        "sequence_id": sequence.sequence_id, "evidence": sequence.evidence,
        "session_id": sequence.session_id, "illumination": sequence.illumination,
        "observable_ms": known_ms, "conclusive_ms": conclusive_ms, "wrong_state_ms": wrong_ms,
        "abstention_ms": known_ms - conclusive_ms, "confusion_ms": confusion_ms,
        "uncertain_ms": sequence.duration_ms - known_ms,
        "unobservable_claim_ms": unobservable_claim_ms,
        "coverage": conclusive_ms / known_ms if known_ms else None,
        "expected_transitions": len(sequence.transitions), "matched_transitions": len(matched),
        "missed_transitions": len(sequence.transitions) - len(matched),
        "false_transitions": false_transitions,
        "false_transitions_per_hour": false_transitions * 3_600_000 / known_ms if known_ms else None,
        "latency_intervals": latencies,
    }
