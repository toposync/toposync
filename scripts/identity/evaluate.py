"""Offline evaluation of real identity models against a frozen, authorized manifest.

One row is one independently collected visit, never one frame. This runner does
not select thresholds, infer consent, certify independent collection or measure
pipeline latency. Keep manifests, images and reports outside the repository.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.metadata
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import time
from typing import Literal

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from toposync_ext_vision.identity.contracts import RecognitionPolicy, Species
from toposync_ext_vision.identity.extraction import IdentityExtractor
from toposync_ext_vision.identity.store import IdentityStore
from toposync_ext_vision.registry import build_default_model_registry


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Photo(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    region: tuple[float, float, float, float] = (0, 0, 1, 1)


class Visit(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    session: str = Field(min_length=1, max_length=120)
    split: Literal["enrollment", "development", "holdout"]
    profile: str = Field(min_length=1, max_length=120)
    identity_label: str = Field(min_length=1, max_length=120)
    nominal: StrictBool
    photos: list[Photo] = Field(min_length=1, max_length=16)


class Profile(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    species: Species
    model_id: str
    face_detector_model_id: str = "opencv_yunet_2023mar"
    policy: RecognitionPolicy
    nominal_coverage_target: float = Field(gt=0, le=1, allow_inf_nan=False)
    nominal_eligibility_rule: str = Field(min_length=20, max_length=4000)
    coverage_justification: str = Field(min_length=20, max_length=4000)

    @model_validator(mode="after")
    def species_matches(self):
        if self.policy.species != self.species:
            raise ValueError("policy species differs from profile")
        return self


class Protocol(StrictModel):
    schema_version: Literal[1] = 1
    dataset_authorization: str = Field(min_length=20, max_length=8000)
    source_and_license: str = Field(min_length=20, max_length=8000)
    independence_method: str = Field(min_length=20, max_length=8000)
    profiles: list[Profile] = Field(min_length=1, max_length=16)
    visits: list[Visit] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def disjoint_visits(self):
        profiles = {profile.id for profile in self.profiles}
        if len(profiles) != len(self.profiles):
            raise ValueError("duplicate profile")
        ids, sessions, hashes, split_by_session = set(), {}, {}, {}
        for visit in self.visits:
            if visit.id in ids or visit.profile not in profiles:
                raise ValueError("duplicate visit or unknown profile")
            ids.add(visit.id)
            # Repeated visits from one session cannot inflate independent denominators.
            key = (visit.profile, visit.session)
            if key in sessions:
                raise ValueError("one visit per independent session and profile is required")
            sessions[key] = visit.split
            if visit.session in split_by_session and split_by_session[visit.session] != visit.split:
                raise ValueError("a collection session cannot cross evaluation splits")
            split_by_session[visit.session] = visit.split
            for photo in visit.photos:
                path = Path(photo.path)
                if path.is_absolute() or ".." in path.parts or "://" in photo.path:
                    raise ValueError("photos must stay inside the authorized local data root")
                previous = hashes.get(photo.sha256)
                if previous is not None and previous != visit.id:
                    raise ValueError("identical photos cannot be separate visits")
                hashes[photo.sha256] = visit.id
                x1, y1, x2, y2 = photo.region
                if (
                    not all(math.isfinite(x) and 0 <= x <= 1 for x in photo.region)
                    or x2 <= x1
                    or y2 <= y1
                ):
                    raise ValueError("invalid photo region")
        for profile in self.profiles:
            visits = [visit for visit in self.visits if visit.profile == profile.id]
            enrollment = {visit.identity_label for visit in visits if visit.split == "enrollment"}
            for split in ("development", "holdout"):
                if (
                    sum(
                        len(visit.photos)
                        for visit in visits
                        if visit.split in {"enrollment", split}
                    )
                    > 10000
                ):
                    raise ValueError("evaluation exceeds the actual per-gallery observation budget")
            if not enrollment:
                raise ValueError("each profile requires enrollment")
            for split in ("development", "holdout"):
                selected = [visit for visit in visits if visit.split == split]
                if not any(
                    visit.identity_label in enrollment and visit.nominal for visit in selected
                ):
                    raise ValueError("each evaluation split requires nominal known visits")
                if not any(visit.identity_label not in enrollment for visit in selected):
                    raise ValueError("each evaluation split requires unknown visits")
        return self


def exact_upper(failures: int, trials: int, *, alpha: float = 0.05) -> float | None:
    """One-sided exact binomial upper bound by inversion, conditional on IID trials.

    Source: https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm
    Zero observations are unavailable, not zero risk. No optional stopping claim.
    """
    if trials < 0 or not 0 <= failures <= trials or not 0 < alpha < 1:
        raise ValueError("invalid binomial counts or confidence")
    if trials == 0:
        return None
    if failures == trials:
        return 1.0
    if failures == 0:
        return -math.expm1(math.log(alpha) / trials)
    coefficients = [
        math.lgamma(trials + 1) - math.lgamma(index + 1) - math.lgamma(trials - index + 1)
        for index in range(failures + 1)
    ]
    low, high = failures / trials, 1.0
    for _ in range(60):
        probability = (low + high) / 2
        values = [
            coefficient
            + index * math.log(probability)
            + (trials - index) * math.log1p(-probability)
            for index, coefficient in enumerate(coefficients)
        ]
        maximum = max(values)
        log_cumulative = maximum + math.log(sum(math.exp(value - maximum) for value in values))
        if log_cumulative > math.log(alpha):
            low = probability
        else:
            high = probability
    return high


def summarize(rows: list[dict], coverage_target: float) -> dict:
    unknown = [row for row in rows if not row["known"]]
    known = [row for row in rows if row["known"]]
    nominal = [row for row in known if row["nominal"]]
    false_unknown = sum(row["accepted"] for row in unknown)
    nominal_unknown = [row for row in unknown if row["nominal"]]
    nominal_errors = sum(row["accepted"] for row in nominal_unknown)
    unavailable = sum(row["inference_unavailable"] for row in rows)
    upper = exact_upper(false_unknown, len(unknown))
    correct = sum(row["correct"] for row in rows)
    accepted = sum(row["accepted"] for row in rows)
    coverage = sum(row["correct"] for row in nominal) / len(nominal) if nominal else None
    return {
        "visits": len(rows),
        "known_visits": len(known),
        "unknown_visits": len(unknown),
        "false_identifications_of_unknown": false_unknown,
        "false_identification_rate": false_unknown / len(unknown) if unknown else None,
        "false_identification_upper_95": upper,
        "known_identification_failure_rate": sum(not row["correct"] for row in known) / len(known)
        if known
        else None,
        "accepted_precision": correct / accepted if accepted else None,
        "accepted_coverage_all_visits": accepted / len(rows) if rows else None,
        "nominal_known_visits": len(nominal),
        "nominal_correct_coverage": coverage,
        "nominal_coverage_target": coverage_target,
        "accepted_identity_switches": sum(row["switches"] for row in rows),
        "final_states": dict(Counter(row["final_status"] for row in rows)),
        "nominal_unknown_visits": len(nominal_unknown),
        "nominal_unknown_false_identification_rate": nominal_errors / len(nominal_unknown)
        if nominal_unknown
        else None,
        "nominal_unknown_false_identification_upper_95": exact_upper(
            nominal_errors, len(nominal_unknown)
        ),
        "unknown_visits_with_usable_evidence": sum(row["usable_evidence"] for row in unknown),
        "nominal_unknown_visits_with_usable_evidence": sum(
            row["usable_evidence"] for row in nominal_unknown
        ),
        "visits_with_inference_unavailable": unavailable,
        "unknown_visits_with_inference_unavailable": sum(
            row["inference_unavailable"] for row in unknown
        ),
        "arithmetic_checks_only": {
            "total_unknown_upper_within_target": upper is not None and upper <= 0.001,
            "nominal_known_coverage_reached": coverage is not None and coverage >= coverage_target,
        },
        "query_evaluation_status": "technical_failures_present"
        if unavailable
        else "metrics_computed",
        "release_gate_evaluated": False,
        "scope": "Operational rates include every visit, including unavailable and unobservable cases. Nominal eligibility was declared before inference. Usable evidence is an observed outcome, never a retrospective exclusion. No aggregate quality approval is issued: collection, independent-data audit, enrollment, known errors, availability, licensing, clustering, contamination and latency still need review.",
    }


def checked_photo(root: Path, photo: Photo) -> bytes:
    path = (root / photo.path).resolve()
    if (
        not path.is_relative_to(root.resolve())
        or not path.is_file()
        or path.stat().st_size > 32 * 1024 * 1024
    ):
        raise ValueError("photo outside authorized root, absent or over budget")
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != photo.sha256:
        raise ValueError("photo checksum changed after protocol freeze")
    return blob


def evaluate(protocol: Protocol, root: Path, output: Path, split: str) -> dict:
    registry = build_default_model_registry()
    results = []
    for profile in protocol.profiles:
        manifest = registry.get_manifest(profile.model_id)
        if manifest is None:
            raise ValueError("model is not in catalog")
        extractor = IdentityExtractor(
            manifest,
            face_detector=registry.get_manifest(profile.face_detector_model_id)
            if profile.species == "person"
            else None,
        )
        if extractor.embedding_space != profile.policy.embedding_space:
            raise ValueError("frozen policy does not match the selected model space")
        profile_dir = output / hashlib.sha256(profile.id.encode()).hexdigest()[:16]
        gallery = IdentityStore(profile_dir, scope="offline-evaluation")
        identities, rows = {}, []
        enrollment_rows = []
        try:
            selected = [
                visit
                for visit in protocol.visits
                if visit.profile == profile.id and visit.split in {"enrollment", split}
            ]
            selected.sort(key=lambda visit: visit.split != "enrollment")
            for visit in selected:
                decisions, useful = [], []
                has_usable_evidence = False
                inference_unavailable = False
                started = time.perf_counter()
                for index, photo in enumerate(visit.photos):
                    blob = checked_photo(root, photo)
                    with Image.open(BytesIO(blob)) as image:
                        if image.width * image.height > 12_000_000:
                            raise ValueError("photo exceeds frozen input pixel budget")
                        frame = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
                    result = extractor.extract(
                        frame,
                        species=profile.species,
                        occurrence_id=visit.id,
                        source_id="offline-evaluation",
                        camera_id="offline-evaluation",
                        capture_id=photo.sha256,
                        observed_at=float(index),
                        region=photo.region,
                        color_order="rgb",
                    )
                    inference_unavailable |= result.status == "unavailable"
                    has_usable_evidence |= (
                        result.evidence is not None and result.evidence.status == "ready"
                    )
                    if result.evidence is None:
                        decisions.append({"status": result.status, "identity_id": None})
                        continue
                    decision = gallery.observe(
                        result.evidence,
                        None if visit.split == "enrollment" else profile.policy,
                        crop=result.crop if visit.split == "enrollment" else None,
                    )
                    decisions.append(decision.model_dump())
                    if result.evidence.reference_eligible:
                        useful.append(decision.observation_id)
                if visit.split == "enrollment":
                    enrollment_rows.append(
                        {
                            "usable_evidence": has_usable_evidence,
                            "inference_unavailable": inference_unavailable,
                        }
                    )
                    if useful:
                        values = {"observation_ids": useful, "use_as_reference": True}
                        if visit.identity_label in identities:
                            values["identity_id"] = identities[visit.identity_label]
                        else:
                            values.update(name=visit.identity_label, species=profile.species)
                        identified = gallery.curate(
                            action="identify",
                            values=values,
                            actor="authorized-offline-evaluation",
                            request_key=visit.id,
                            expected_revision=gallery.revision,
                        )
                        identities[visit.identity_label] = identified["identity_id"]
                else:
                    # Known means planned enrollment, even if every enrollment photo failed.
                    enrolled_labels = {
                        item.identity_label for item in selected if item.split == "enrollment"
                    }
                    known = visit.identity_label in enrolled_labels
                    accepted = [
                        item["identity_id"] for item in decisions if item["status"] == "recognized"
                    ]
                    expected = identities.get(visit.identity_label)
                    rows.append(
                        {
                            "visit": visit.id,
                            "known": known,
                            "nominal": visit.nominal,
                            "accepted": bool(accepted),
                            "correct": bool(accepted)
                            and known
                            and all(value == expected for value in accepted),
                            "switches": sum(
                                left != right for left, right in zip(accepted, accepted[1:])
                            ),
                            "final_status": decisions[-1]["status"],
                            "usable_evidence": has_usable_evidence,
                            "inference_unavailable": inference_unavailable,
                            "processing_ms": (time.perf_counter() - started) * 1000,
                        }
                    )
                gallery.close_occurrence(visit.id)
            declared = {visit.identity_label for visit in selected if visit.split == "enrollment"}
            results.append(
                {
                    "profile": profile.id,
                    "species": profile.species,
                    "embedding_space": extractor.embedding_space,
                    "model_sha256": manifest.sha256,
                    "failed_enrollment_identities": len(declared - identities.keys()),
                    "enrollment_visits": len(enrollment_rows),
                    "enrollment_visits_with_usable_evidence": sum(
                        item["usable_evidence"] for item in enrollment_rows
                    ),
                    "enrollment_visits_with_inference_unavailable": sum(
                        item["inference_unavailable"] for item in enrollment_rows
                    ),
                    "metrics": summarize(rows, profile.nominal_coverage_target),
                    "visits": rows,
                }
            )
        finally:
            gallery.close()
    return {"split": split, "profiles": results, "pipeline_performance_evaluated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--model-data-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-protocol-sha256", required=True)
    parser.add_argument("--split", choices=["development", "holdout"])
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    raw = args.protocol.read_bytes()
    if (
        len(raw) > 8 * 1024 * 1024
        or hashlib.sha256(raw).hexdigest() != args.expected_protocol_sha256
    ):
        parser.error("Protocol differs from the externally frozen SHA-256 or exceeds budget")
    protocol = Protocol.model_validate_json(raw)
    for visit in protocol.visits:
        for photo in visit.photos:
            checked_photo(args.data_root, photo)
    if not args.validate_only and (args.split is None or args.model_data_dir is None):
        parser.error("An evaluation split and installed model data directory are required")
    args.output.mkdir(mode=0o700, parents=False)
    os.umask(0o077)
    if args.model_data_dir:
        os.environ["TOPOSYNC_DATA_DIR"] = str(args.model_data_dir.resolve())
    report = {
        "protocol_sha256": args.expected_protocol_sha256,
        "validated_only": args.validate_only,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": time.time(),
        "input_preparation": "EXIF transpose, RGB, original resolution bounded by 12 million pixels",
        "statistical_assumption": "One independent visit per declared session/profile; declarations require external audit. Exact binomial bounds assume independent identically distributed visits within each profile; pooled frames are forbidden.",
    }
    report["dependencies"] = {
        name: importlib.metadata.version(name)
        for name in ("numpy", "Pillow", "onnxruntime", "cryptography")
    }
    source_root = Path(__file__).resolve().parents[2]
    report["implementation_sha256"] = {
        str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            Path(__file__).resolve(),
            *sorted(
                (source_root / "extensions/vision/src/toposync_ext_vision/identity").glob("*.py")
            ),
        ]
    }
    (args.output / "protocol.json").write_bytes(raw)
    if not args.validate_only:
        report.update(evaluate(protocol, args.data_root, args.output, args.split))
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(args.output / "result.json"),
                "validated_only": args.validate_only,
                "profile_count": len(protocol.profiles),
            }
        )
    )


if __name__ == "__main__":
    main()
