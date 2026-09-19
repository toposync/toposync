"""Offline E11 comparison of Garagem panorama runs 8, 9 and 10.

The experiment compares stored source photographs through the production matcher,
then distinguishes that capture-level evidence from an aligned coverage mask. It
does not contact or move a camera and it never changes which artifact is active.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import cv2

from toposync_ext_cameras.panorama_scan import _match
from toposync_ext_cameras.source_panorama import _promotion_candidate_reason


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
JOBS_DIRECTORY = Path(".toposync-data/runtime/cameras/source-panorama/jobs")
ARTIFACTS_DIRECTORY = Path(".toposync-data/runtime/cameras/source-panorama/artifacts")
RUNS = {
    "8": {
        "job": "5f9d75d35b914b22b09d5c8007a59fb0",
        "artifact": "7dc4c4354b5042e891242ede7151f0d2",
    },
    "9": {
        "job": "4ad36fcbd4b8463da825725aca1b739e",
        "artifact": "a6bef107861d43fc9c7ed40f53e27c7d",
    },
    "10": {
        "job": "097e9f406287495f8582423f5f70806c",
        "artifact": "2aad02871bc641e8bbaac66ae1172581",
    },
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_set(job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = JOBS_DIRECTORY / job_id / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    captures = {}
    for path in sorted(manifest_path.parent.glob("capture-*.jpg")):
        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError(f"Cannot read {path}")
        captures[path.stem] = {"image": image, "sha256": _digest(path)}
    if not captures:
        raise RuntimeError(f"No captures for {job_id}")
    return manifest, captures


def _artifact(artifact_id: str) -> dict[str, Any]:
    return json.loads((ARTIFACTS_DIRECTORY / artifact_id / "artifact.json").read_text())


def _summary(match: dict[str, Any]) -> dict[str, Any]:
    return {
        key: match.get(key)
        for key in ("verified", "code", "overlap", "displacement", "inliers", "model_candidates")
    }


def _cross_run(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    accepted: list[tuple[str, str, dict[str, Any]]] = []
    best_for_left: dict[str, tuple[str, dict[str, Any]]] = {}
    for left_id, left_capture in left.items():
        for right_id, right_capture in right.items():
            match = _match(left_capture["image"], right_capture["image"])
            if match.get("verified") is not True:
                continue
            accepted.append((left_id, right_id, match))
            previous = best_for_left.get(left_id)
            score = (float(match.get("overlap") or 0), -float(match.get("displacement") or 0))
            if previous is None or score > (
                float(previous[1].get("overlap") or 0),
                -float(previous[1].get("displacement") or 0),
            ):
                best_for_left[left_id] = (right_id, match)
    left_covered = {left_id for left_id, _, _ in accepted}
    right_covered = {right_id for _, right_id, _ in accepted}
    return {
        "verified_pair_count": len(accepted),
        "left_capture_count": len(left),
        "right_capture_count": len(right),
        "left_covered_count": len(left_covered),
        "right_covered_count": len(right_covered),
        "left_unmatched": sorted(set(left) - left_covered),
        "right_unmatched": sorted(set(right) - right_covered),
        "best_matches_by_left": {
            left_id: {"right": right_id, **_summary(match)}
            for left_id, (right_id, match) in sorted(best_for_left.items())
        },
    }


def _coverage_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    coverage = artifact["coverage"]
    return {
        "status": artifact["status"],
        "pixel_ratio": coverage["pixel_ratio"],
        "solid_angle_ratio": coverage["solid_angle_ratio"],
        "acquisition_complete": coverage["acquisition_complete"],
        "progress": coverage["acquisition"]["progress"],
        "quality_status": artifact["quality"]["status"],
    }


def _control_reasons(active: dict[str, Any]) -> dict[str, str | None]:
    reduced = copy.deepcopy(active)
    reduced["coverage"]["pixel_ratio"] -= 0.02
    reduced["coverage"]["solid_angle_ratio"] -= 0.02
    incompatible = copy.deepcopy(active)
    incompatible["coverage"]["provenance"] = "other_geometry"
    missing = copy.deepcopy(active)
    missing["coverage"].pop("solid_angle_ratio")
    return {
        "coverage_reduced": _promotion_candidate_reason(reduced, active, active_is_current=True),
        "geometry_incompatible": _promotion_candidate_reason(
            incompatible, active, active_is_current=True
        ),
        "coverage_evidence_missing": _promotion_candidate_reason(
            missing, active, active_is_current=True
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    manifests: dict[str, dict[str, Any]] = {}
    captures: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    for run, metadata in RUNS.items():
        manifests[run], captures[run] = _image_set(metadata["job"])
        artifacts[run] = _artifact(metadata["artifact"])
    comparisons: dict[str, Any] = {}
    for active_run, candidate_run in (("8", "9"), ("8", "10"), ("9", "10")):
        key = f"{active_run}_to_{candidate_run}"
        capture_evidence = _cross_run(captures[active_run], captures[candidate_run])
        panorama_match = _match(
            cv2.imread(str(ARTIFACTS_DIRECTORY / RUNS[active_run]["artifact"] / "panorama.png")),
            cv2.imread(str(ARTIFACTS_DIRECTORY / RUNS[candidate_run]["artifact"] / "panorama.png")),
        )
        structural_reason = _promotion_candidate_reason(
            artifacts[candidate_run], artifacts[active_run], active_is_current=True
        )
        aligned_global_coverage = panorama_match.get("verified") is True
        safe_decision = (
            "eligible_for_spatial_mask_comparison"
            if aligned_global_coverage
            and not capture_evidence["left_unmatched"]
            and not capture_evidence["right_unmatched"]
            else "preserve_active_store_candidate"
        )
        comparisons[key] = {
            "active_coverage": _coverage_summary(artifacts[active_run]),
            "candidate_coverage": _coverage_summary(artifacts[candidate_run]),
            "capture_level_evidence": capture_evidence,
            "panorama_alignment": _summary(panorama_match),
            "aligned_global_coverage": aligned_global_coverage,
            "current_structural_promotion_reason": structural_reason,
            "safe_policy_decision": safe_decision,
            "policy_gap_observed": structural_reason is None
            and safe_decision == "preserve_active_store_candidate",
        }
    controls = _control_reasons(artifacts["8"])
    report = {
        "experiment": "E11",
        "inputs": {
            run: {
                "job_id": RUNS[run]["job"],
                "artifact_id": RUNS[run]["artifact"],
                "scan_manifest_sha256": _digest(JOBS_DIRECTORY / RUNS[run]["job"] / "scan-manifest.json"),
                "artifact_sha256": _digest(ARTIFACTS_DIRECTORY / RUNS[run]["artifact"] / "artifact.json"),
                "capture_count": len(captures[run]),
            }
            for run in RUNS
        },
        "comparisons": comparisons,
        "negative_controls": controls,
        "decision": {
            "pass": all(
                comparison["safe_policy_decision"] == "preserve_active_store_candidate"
                for comparison in comparisons.values()
            )
            and controls == {
                "coverage_reduced": "coverage_regressed",
                "geometry_incompatible": "comparison_incompatible",
                "coverage_evidence_missing": "coverage_evidence_missing",
            },
            "current_implementation_gap": any(
                comparison["policy_gap_observed"] for comparison in comparisons.values()
            ),
            "required_change": (
                "Persist an alignable coverage frame or cross-run coverage-mask relation before "
                "automatic promotion; scalar area metrics and structural acquisition evidence alone "
                "cannot prove bilateral preservation."
            ),
            "scope_limit": (
                "Cross-run photograph matches demonstrate shared views but not the union or loss "
                "of solid angle; the panorama images did not yield a valid global alignment."
            ),
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
