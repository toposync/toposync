"""E5 uses saved Garagem captures to isolate loss caused by resolution only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2

from toposync_ext_cameras.panorama_scan import _match


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_CAPTURE_DIRECTORY = Path(
    ".toposync-data/runtime/cameras/source-panorama/jobs/097e9f406287495f8582423f5f70806c"
)


def _summary(match: dict[str, Any]) -> dict[str, Any]:
    return {
        key: match.get(key)
        for key in ("verified", "code", "inliers", "overlap", "displacement", "model_candidates")
    }


def _analyze(images: dict[str, Any], size: tuple[int, int]) -> dict[str, Any]:
    normalized = {
        identifier: cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        for identifier, image in images.items()
    }
    identifiers = sorted(normalized)
    links: dict[str, dict[str, Any]] = {}
    for first, second in zip(identifiers, identifiers[1:]):
        links[f"{first}->{second}"] = _summary(_match(normalized[first], normalized[second]))
    accepted = [identifier for identifier, result in links.items() if result["verified"]]
    rejected = {identifier: result.get("code") for identifier, result in links.items() if not result["verified"]}
    inliers = [result["inliers"] for result in links.values() if isinstance(result.get("inliers"), int)]
    supports = [
        candidate["occupied_cells"]
        for result in links.values() if result.get("verified")
        for candidate in result.get("model_candidates") or []
        if isinstance(candidate, dict) and isinstance(candidate.get("occupied_cells"), int)
    ]
    return {
        "analysis_size": list(size),
        "pair_count": len(links),
        "accepted_pairs": accepted,
        "rejected_pairs": rejected,
        "accepted_count": len(accepted),
        "mean_inliers_on_verified_pairs": round(sum(inliers) / len(inliers), 3) if inliers else None,
        "minimum_observed_occupied_cells": min(supports) if supports else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-directory", type=Path, default=DEFAULT_CAPTURE_DIRECTORY)
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    capture_directory = arguments.capture_directory.resolve()
    paths = sorted(capture_directory.glob("capture-*.jpg"))
    images = {path.stem: cv2.imread(str(path), cv2.IMREAD_COLOR) for path in paths}
    if len(images) < 2 or any(image is None for image in images.values()):
        raise RuntimeError("Saved Garagem capture set is incomplete")
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    high = _analyze(images, (960, 540))
    low = _analyze(images, (640, 360))
    retained = sorted(set(high["accepted_pairs"]) & set(low["accepted_pairs"]))
    report = {
        "experiment_id": "E5",
        "camera_network_access": False,
        "ptz_commands_issued": 0,
        "capture_count": len(images),
        "offline_reduction": {"high": high, "low": low, "links_retained_at_low": retained},
        "native_substream": {
            "status": "deferred",
            "reason": "E2 and E3 recorded no decodable profile_2 frame: shared decoder had no fresh frame, centralized ingest returned 400, and configured origin returned 401.",
        },
        "decision": {
            "offline_low_resolution_retains_all_high_resolution_links": len(retained) == len(high["accepted_pairs"]),
            "native_substream_qualified": False,
        },
    }
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
