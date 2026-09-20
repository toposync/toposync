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


@pytest.mark.parametrize("failure", ["unobservable", "unavailable"])
def test_failed_enrollment_stays_known_in_real_store_accounting(tmp_path, monkeypatch, failure):
    from io import BytesIO
    from types import SimpleNamespace
    from PIL import Image
    import scripts.identity.evaluate as evaluation
    from toposync_ext_vision.identity.extraction import ExtractionResult
    from test_identity_store import evidence

    content = manifest()
    for index, visit in enumerate(content["visits"]):
        encoded = BytesIO()
        Image.new("RGB", (128, 128), (index * 10, 10, 10)).save(encoded, format="PNG")
        blob = encoded.getvalue()
        (tmp_path / visit["photos"][0]["path"]).write_bytes(blob)
        visit["photos"][0]["sha256"] = hashlib.sha256(blob).hexdigest()
    protocol = Protocol.model_validate(content)

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
