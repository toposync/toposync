"""E15: compare the production visual matcher at capture and control scales.

No network or PTZ action is performed.  The input is the retained Garagem
capture set from the earlier physical scan.  Pixels remain in their existing
private job directory; the report keeps only scalar correspondence evidence.
"""

from __future__ import annotations

import json
import argparse
import hashlib
import inspect
import math
from pathlib import Path
from typing import Any

import cv2

from toposync_ext_cameras.panorama_scan import ANALYSIS_WIDTH, _match


DIRECTORY = Path(__file__).resolve().parent
JOB_MANIFEST = (
    Path(".toposync-data/runtime/cameras/source-panorama/jobs")
    / "097e9f406287495f8582423f5f70806c"
    / "scan-manifest.json"
)
ANALYSIS_SIZE = (ANALYSIS_WIDTH, 540)


def _summary(match: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: match.get(key)
        for key in ("verified", "code", "inliers", "overlap", "shift_x", "shift_y", "displacement", "analysis_size")
    }
    if match.get("verified") is True:
        # _match already returns pixels in its analysis image, regardless of
        # input resolution. A second source/analysis division was the E15 bug.
        result["displacement_at_960"] = float(match["displacement"])
        result["shift_at_960"] = [
            float(match["shift_x"]),
            float(match["shift_y"]),
        ]
    return result


def _same_measurement(canonical: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
    if canonical.get("verified") is not True or legacy.get("verified") is not True:
        return {
            "same_verification": canonical.get("verified") is legacy.get("verified"),
            "same_direction": None,
            "same_displacement": None,
            "consistent": canonical.get("verified") is legacy.get("verified") is False,
        }
    if canonical.get("analysis_size") != list(ANALYSIS_SIZE) or legacy.get("analysis_size") != list(ANALYSIS_SIZE):
        return {"consistent": False, "code": "analysis_units_unverified"}
    canonical_shift = [float(canonical["shift_x"]), float(canonical["shift_y"])]
    legacy_shift = [float(legacy["shift_x"]), float(legacy["shift_y"])]
    dot = canonical_shift[0] * legacy_shift[0] + canonical_shift[1] * legacy_shift[1]
    canonical_displacement = float(canonical["displacement"])
    legacy_displacement = float(legacy["displacement"])
    difference = abs(canonical_displacement - legacy_displacement)
    tolerance = max(2.0, max(canonical_displacement, legacy_displacement) * 0.35)
    return {
        "same_verification": True,
        "same_direction": dot >= 0.0,
        "same_displacement": difference <= tolerance,
        "normalized_displacement_difference": difference,
        "normalized_displacement_tolerance": tolerance,
        "consistent": dot >= 0.0 and difference <= tolerance,
    }


def _pair(identifier: str, first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    left = cv2.imread(first["path"], cv2.IMREAD_COLOR)
    right = cv2.imread(second["path"], cv2.IMREAD_COLOR)
    if left is None or right is None:
        return {"pair": identifier, "status": "source_image_unavailable"}
    if left.shape != right.shape:
        return {
            "pair": identifier,
            "status": "dimensions_changed",
            "left_size": [left.shape[1], left.shape[0]],
            "right_size": [right.shape[1], right.shape[0]],
        }
    scale = left.shape[1] / ANALYSIS_SIZE[0]
    if not math.isclose(left.shape[0] / ANALYSIS_SIZE[1], scale, rel_tol=0, abs_tol=1e-9):
        return {"pair": identifier, "status": "non_uniform_analysis_scale"}
    canonical = _match(left, right)
    legacy_color_preresized = _match(
        cv2.resize(left, ANALYSIS_SIZE, interpolation=cv2.INTER_AREA),
        cv2.resize(right, ANALYSIS_SIZE, interpolation=cv2.INTER_AREA),
    )
    agreement = _same_measurement(canonical, legacy_color_preresized)
    return {
        "pair": identifier,
        "status": "compared",
        "source_size": [left.shape[1], left.shape[0]],
        "scale_to_960": scale,
        "production_canonical": _summary(canonical),
        "legacy_color_preresized": _summary(legacy_color_preresized),
        "agreement_with_legacy": agreement,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DIRECTORY / "report-v2.json")
    arguments = parser.parse_args()
    # Refuse to overwrite evidence from either revision, including custom paths.
    if arguments.output.exists():
        parser.error("The output already exists; choose a new report path.")
    manifest_bytes = JOB_MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    captures = manifest["captures"]
    adjacent = [
        _pair(f"{first['id']}->{second['id']}", first, second)
        for first, second in zip(captures, captures[1:])
    ]
    self_control = _pair(f"{captures[0]['id']}->{captures[0]['id']}", captures[0], captures[0])
    compared = [item for item in adjacent if item["status"] == "compared"]
    consistent = [item for item in compared if item["agreement_with_legacy"]["consistent"]]
    report = {
        "experiment_id": "E15",
        "instrument_revision": 2,
        "provenance": {
            "instrument_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "matcher_module_sha256": hashlib.sha256(Path(inspect.getfile(_match)).read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "opencv_version": cv2.__version__,
            "analysis_size": list(ANALYSIS_SIZE),
            "displacement_units": "analysis_pixels_no_additional_normalization",
            "inputs": [{"id": capture["id"], "sha256": hashlib.sha256(Path(capture["path"]).read_bytes()).hexdigest()}
                       for capture in captures],
        },
        "title": "Consistency of full and reduced visual correspondence",
        "scope": {
            "camera_network_access": False,
            "ptz_commands_issued": 0,
            "images_persisted": False,
            "input_capture_count": len(captures),
            "adjacent_pair_count": len(adjacent),
        },
        "acceptance": {
            "control": "The identical-image control must be verified and have zero displacement in both code paths.",
            "decision": "The legacy color pre-resize is not admissible if it changes verification, shift direction or normalized displacement beyond the stated tolerance. The controller-owned transform is canonical.",
        },
        "control": self_control,
        "summary": {
            "compared_pairs": len(compared),
            "consistent_pairs": len(consistent),
            "inconsistent_pairs": len(compared) - len(consistent),
            "all_compared_pairs_consistent": len(compared) == len(adjacent) == len(consistent),
            "verified_consistent_pairs": sum(item["production_canonical"].get("verified") is True
                                             and item["legacy_color_preresized"].get("verified") is True for item in consistent),
            "both_rejected_pairs": sum(item["production_canonical"].get("verified") is False
                                       and item["legacy_color_preresized"].get("verified") is False for item in compared),
        },
        "pairs": adjacent,
    }
    with arguments.output.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
