"""Evaluation accounting/protocol tests; synthetic rows prove no biometric accuracy."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import math

import pytest
from pydantic import ValidationError

from scripts.identity.evaluate import Photo, Protocol, checked_photo, exact_upper, summarize
from test_identity_store import policy


def manifest():
    return {
        "dataset_authorization": "Explicit authorized local test collection only",
        "source_and_license": "Source/license audited outside this software test",
        "independence_method": "Separate visits collected in independent sessions",
        "profiles": [
            {
                "id": "nominal-person",
                "species": "person",
                "model_id": "opencv_sface_2021dec",
                "policy": policy().model_dump(),
                "nominal_coverage_target": 0.8,
                "nominal_eligibility_rule": "Frontal visible face before model inference",
                "coverage_justification": "Synthetic accounting target, not product calibration",
            }
        ],
        "visits": [
            {
                "id": str(index),
                "session": f"session-{index}",
                "split": split,
                "profile": "nominal-person",
                "identity_label": label,
                "nominal": True,
                "photos": [
                    {
                        "path": f"photo-{index}.jpg",
                        "sha256": hashlib.sha256(str(index).encode()).hexdigest(),
                    }
                ],
            }
            for index, (split, label) in enumerate(
                [
                    ("enrollment", "known"),
                    ("development", "known"),
                    ("development", "unknown-development"),
                    ("holdout", "known"),
                    ("holdout", "unknown-holdout"),
                ]
            )
        ],
    }


def test_freeze_requires_sessions_unknowns_and_nonzero_nominal_target():
    valid = manifest()
    assert len(Protocol.model_validate(valid).visits) == 5
    mutations = [
        lambda item: item["visits"][3].update(session="session-0"),
        lambda item: item["visits"][3]["photos"][0].update(
            sha256=item["visits"][0]["photos"][0]["sha256"]
        ),
        lambda item: item["visits"][4].update(identity_label="known"),
        lambda item: item["visits"][3].update(nominal=False),
        lambda item: item["profiles"][0].update(nominal_coverage_target=0),
        lambda item: item["visits"][3]["photos"][0].update(path="../private-photo.jpg"),
    ]
    for mutate in mutations:
        invalid = deepcopy(valid)
        mutate(invalid)
        with pytest.raises(ValidationError):
            Protocol.model_validate(invalid)


def test_file_hash_and_resolved_root_cannot_be_bypassed(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    photo = root / "photo.jpg"
    photo.write_bytes(b"fixture, not an actual image")
    definition = Photo(path=photo.name, sha256=hashlib.sha256(photo.read_bytes()).hexdigest())
    assert checked_photo(root, definition) == photo.read_bytes()
    photo.write_bytes(b"changed after freeze")
    with pytest.raises(ValueError, match="checksum"):
        checked_photo(root, definition)
    external = tmp_path / "external.jpg"
    external.write_bytes(b"outside authorized root")
    (root / "link.jpg").symlink_to(external)
    with pytest.raises(ValueError, match="outside"):
        checked_photo(
            root, Photo(path="link.jpg", sha256=hashlib.sha256(external.read_bytes()).hexdigest())
        )


def test_exact_bound_matches_closed_forms_and_binomial_inversion():
    assert exact_upper(0, 0) is None
    assert exact_upper(10, 10) == 1
    assert exact_upper(0, 1) == pytest.approx(0.95)
    assert exact_upper(9, 10) == pytest.approx(0.95 ** (1 / 10))
    assert exact_upper(0, 2994) > 0.001
    assert exact_upper(0, 2995) <= 0.001
    bound = exact_upper(3, 20)
    cumulative = sum(
        math.comb(20, index) * bound**index * (1 - bound) ** (20 - index) for index in range(4)
    )
    assert cumulative == pytest.approx(0.05, abs=1e-12)
    with pytest.raises(ValueError):
        exact_upper(2, 1)


def row(*, known, accepted=False, correct=False, nominal=True, switches=0):
    return {
        "known": known,
        "accepted": accepted,
        "correct": correct,
        "nominal": nominal,
        "switches": switches,
        "final_status": "recognized" if accepted else "unknown",
        "closed_correct": correct,
        "inference_unavailable": False,
        "usable_evidence": True,
    }


def test_rejecting_everything_cannot_pass_even_with_many_unknown_visits():
    result = summarize([row(known=False) for _ in range(3000)] + [row(known=True)], 0.8)
    assert result["false_identification_upper_95"] < 0.001
    assert result["nominal_correct_coverage"] == 0
    assert result["known_identification_failure_rate"] == 1
    assert result["accepted_precision"] is None
    assert not all(result["arithmetic_checks_only"].values())
    assert result["release_gate_evaluated"] is False


def test_metrics_include_failures_in_non_nominal_visits_and_small_samples_stay_inconclusive():
    result = summarize(
        [
            row(known=False, accepted=True, nominal=False),
            row(known=False),
            row(known=True, accepted=True, correct=True),
            row(known=True, accepted=True, switches=1),
        ],
        0.8,
    )
    assert result["unknown_visits"] == 2
    assert result["false_identification_rate"] == 0.5
    assert result["accepted_precision"] == pytest.approx(1 / 3)
    assert result["known_identification_failure_rate"] == 0.5
    assert result["accepted_identity_switches"] == 1
    assert not all(result["arithmetic_checks_only"].values())
    assert result["release_gate_evaluated"] is False
    small = summarize([row(known=False), row(known=True, accepted=True, correct=True)], 0.8)
    assert small["false_identification_rate"] == 0
    assert small["false_identification_upper_95"] == pytest.approx(0.95)
    assert small["nominal_correct_coverage"] == 1
    assert not all(small["arithmetic_checks_only"].values())


def test_massive_unknown_unavailability_is_visible_and_never_approves_a_gate():
    unavailable = {
        **row(known=False),
        "final_status": "unavailable",
        "inference_unavailable": True,
        "usable_evidence": False,
    }
    result = summarize(
        [unavailable] * 9998
        + [row(known=False, accepted=True), row(known=True, accepted=True, correct=True)],
        0.8,
    )
    assert result["unknown_visits_with_inference_unavailable"] == 9998
    assert result["unknown_visits_with_usable_evidence"] == 1
    assert result["nominal_unknown_visits"] == 9999
    assert result["accepted_precision"] == 0.5
    assert result["query_evaluation_status"] == "technical_failures_present"
    assert result["release_gate_evaluated"] is False
    assert "quality_criteria_met" not in result


def write_evaluation_photos(content, root):
    from io import BytesIO
    from PIL import Image

    for index, photo in enumerate(photo for visit in content["visits"] for photo in visit["photos"]):
        encoded = BytesIO()
        Image.new("RGB", (128, 128), (index * 10, 10, 10)).save(encoded, format="PNG")
        blob = encoded.getvalue()
        (root / photo["path"]).write_bytes(blob)
        photo["sha256"] = hashlib.sha256(blob).hexdigest()
    return Protocol.model_validate(content)


@pytest.mark.parametrize("failure", ["unobservable", "unavailable"])
def test_failed_enrollment_stays_known_in_real_store_accounting(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    import scripts.identity.evaluate as evaluation
    from toposync_ext_vision.identity.extraction import ExtractionResult
    from test_identity_store import evidence

    content = manifest()
    protocol = write_evaluation_photos(content, tmp_path)

    class AccountingExtractor:
        def __init__(self, *args, **kwargs):
            self.embedding_space = policy().embedding_space

        def extract(self, frame, **values):
            if values["occurrence_id"] == "0":
                return ExtractionResult(failure, "test_bad_enrollment")
            sample = evidence(values["occurrence_id"])
            return ExtractionResult("pending", "evidence_ready", sample)

    monkeypatch.setattr(evaluation, "IdentityExtractor", AccountingExtractor)
    monkeypatch.setattr(
        evaluation,
        "build_default_model_registry",
        lambda: SimpleNamespace(get_manifest=lambda _: SimpleNamespace(sha256="0" * 64)),
    )
    result = evaluation.evaluate(protocol, tmp_path, tmp_path / "output", "holdout")
    profile = result["profiles"][0]
    assert profile["failed_enrollment_identities"] == 1
    assert profile["enrollment_visits_with_inference_unavailable"] == int(failure == "unavailable")
    assert profile["enrollment_visits_with_usable_evidence"] == 0
    assert profile["metrics"]["known_visits"] == 1
    assert profile["metrics"]["unknown_visits"] == 1
    assert profile["metrics"]["known_identification_failure_rate"] == 1
    assert len(profile["visits"]) == 2

@pytest.mark.parametrize(("window", "closed_status"), [(0, "unobservable"), (10, "recognized")])
def test_low_resolution_after_match_updates_closed_gallery(tmp_path, monkeypatch, window, closed_status):
    from types import SimpleNamespace
    import scripts.identity.evaluate as evaluation
    from toposync_ext_vision.identity.extraction import ExtractionResult
    from test_identity_store import evidence

    content = manifest()
    content["profiles"][0]["policy"]["continuity_seconds"] = window
    content["visits"][1]["photos"].append({"path": "low-resolution.png"})
    protocol = write_evaluation_photos(content, tmp_path)

    class Extractor:
        embedding_space = policy().embedding_space

        def extract(self, frame, **values):
            if values["observed_at"] > 0 or values["occurrence_id"] == "2":
                return ExtractionResult("unobservable", "insufficient_resolution")
            sample = evidence(values["occurrence_id"], observed_at=values["observed_at"])
            return ExtractionResult("pending", "evidence_ready", sample)

    monkeypatch.setattr(evaluation, "IdentityExtractor", lambda *args, **kwargs: Extractor())
    monkeypatch.setattr(evaluation, "build_default_model_registry",
                        lambda: SimpleNamespace(get_manifest=lambda _: SimpleNamespace(sha256="0" * 64)))
    profile, = evaluation.evaluate(protocol, tmp_path, tmp_path / "out", "development")["profiles"]
    known, unknown = profile["visits"]
    assert known["correct"] and known["last_extraction_status"] == "unobservable"
    assert known["final_status"] == known["closed_status"] == closed_status
    assert known["closed_correct"] == (window > 0)
    assert unknown["closed_status"] is None and not unknown["closed_correct"]
    assert profile["metrics"]["nominal_closed_correct_coverage"] == float(window > 0)


@pytest.mark.parametrize("split", ["development", "holdout"])
def test_cli_can_keep_the_other_split_offline_without_skipping_input_checks(tmp_path, monkeypatch, split):
    import json
    import sys
    import scripts.identity.evaluate as evaluation

    content = manifest()
    protocol = write_evaluation_photos(content, tmp_path)
    raw = protocol.model_dump_json().encode()
    path = tmp_path / "protocol.json"
    path.write_bytes(raw)
    withheld = "holdout" if split == "development" else "development"
    for visit in protocol.visits:
        if visit.split == withheld:
            (tmp_path / visit.photos[0].path).unlink()
    output = tmp_path / "result"
    args = ["evaluate", "--protocol", str(path), "--expected-protocol-sha256", hashlib.sha256(raw).hexdigest(),
            "--data-root", str(tmp_path), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", [*args, "--validate-only"])
    with pytest.raises(ValueError, match="absent"):
        evaluation.main()
    monkeypatch.setattr(sys, "argv", [*args, "--split", split, "--model-data-dir", str(tmp_path)])
    monkeypatch.setattr(evaluation, "evaluate", lambda *args: {"split": split})
    evaluation.main()
    report = json.loads((output / "result.json").read_text())
    assert report["verified_photo_splits"] == ["enrollment", split]
    first = next(visit for visit in protocol.visits if visit.split == "enrollment")
    (tmp_path / first.photos[0].path).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        evaluation.main()


def test_candidate_lists_measure_rank_and_distractors_once_per_visit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.identity import evaluate as evaluation
    from test_identity_store import evidence
    from toposync_ext_vision.identity.extraction import ExtractionResult

    content = manifest()
    content["profiles"][0]["policy"].update(automatic_enabled=False, continuity_seconds=0)
    content["visits"].append({**content["visits"][0], "id": "rival", "identity_label": "other",
                              "session": "rival-session", "photos": [{"path": "rival.png"}]})
    content["visits"][1]["photos"].extend([{"path": "repeat.png"}, {"path": "blur.png"}])
    protocol = write_evaluation_photos(content, tmp_path)

    class Extractor:
        embedding_space = policy().embedding_space

        def extract(self, frame, *, occurrence_id, observed_at, capture_id, **kwargs):
            if observed_at == 2:
                return ExtractionResult("unobservable", "insufficient_resolution")
            vector = [0.8, 0.6] if occurrence_id == "0" else [1, 0]
            sample = evidence(occurrence_id, capture=capture_id, observed_at=observed_at, vector=vector + [0] * 14)
            return ExtractionResult("pending", "evidence_ready", sample)

    monkeypatch.setattr(evaluation, "IdentityExtractor", lambda *args, **kwargs: Extractor())
    monkeypatch.setattr(evaluation, "build_default_model_registry",
                        lambda: SimpleNamespace(get_manifest=lambda _: SimpleNamespace(sha256="0" * 64)))
    profile, = evaluation.evaluate(protocol, tmp_path, tmp_path / "out", "development")["profiles"]
    known, unknown = profile["visits"]
    assert known["candidate_lists"]["during_visit"] == {"best_correct_rank": 2, "other_candidate": True, "max_candidates": 2}
    assert known["candidate_lists"]["at_close"] == {"best_correct_rank": None, "other_candidate": False, "max_candidates": 0}
    assert unknown["candidate_lists"]["at_close"] == {"best_correct_rank": None, "other_candidate": True, "max_candidates": 2}
    metrics = profile["metrics"]
    assert metrics["candidate_lists"]["during_visit"] == {"known_correct_first": 0, "known_correct_listed": 1,
                                                         "known_with_other_candidate": 1, "unknown_with_candidates": 1}
    assert metrics["candidate_lists"]["at_close"]["known_correct_listed"] == 0
    assert metrics["candidate_lists"]["at_close"]["unknown_with_candidates"] == 1
    assert metrics["false_identifications_of_unknown"] == 0 and not known["accepted"]
