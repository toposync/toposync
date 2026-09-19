"""E14R: pair low/high decoder frames only when visual and temporal evidence agree."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from toposync_ext_cameras.panorama_scan import _match


CAMERA_ID = "camera_3_177980"
SOURCES = ("profile_1", "profile_2")
ANALYSIS_SIZE = (640, 360)
MAXIMUM_TIMESTAMP_DELTA_SECONDS = 1.0


def _fetch(base_url: str, source_id: str) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/api/cameras/cameras/{CAMERA_ID}/snapshot?source_id={source_id}&fresh=true&freshness=decoder"
    started = time.time()
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=18.0) as response:
            blob = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            status = int(response.status)
    except urllib.error.HTTPError as error:
        return {"obtained": False, "status": int(error.code), "error": "fresh_snapshot_rejected"}
    except Exception as error:
        return {"obtained": False, "error": type(error).__name__}
    image = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return {"obtained": False, "status": status, "error": "undecodable_snapshot"}
    frame_timestamp = headers.get("x-toposync-snapshot-frame-timestamp")
    try:
        published_at = float(frame_timestamp) if frame_timestamp is not None else None
    except ValueError:
        published_at = None
    return {
        "obtained": True,
        "status": status,
        "response_seconds": round(time.time() - started, 4),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "dimensions": [int(image.shape[1]), int(image.shape[0])],
        "published_at": published_at,
        "frame_generation": headers.get("x-toposync-snapshot-frame-generation"),
        "frame_sequence": headers.get("x-toposync-snapshot-frame-sequence"),
        "freshness": headers.get("x-toposync-snapshot-freshness"),
        "image": image,
    }


def _summary(sample: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sample.items() if key != "image"}


def _pair(main: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
    if not main.get("obtained") or not sub.get("obtained"):
        return {"accepted": False, "reason": "fresh_pair_unavailable"}
    main_at, sub_at = main.get("published_at"), sub.get("published_at")
    temporal_delta = abs(float(main_at) - float(sub_at)) if isinstance(main_at, float) and isinstance(sub_at, float) else None
    first = cv2.resize(main["image"], ANALYSIS_SIZE, interpolation=cv2.INTER_AREA)
    second = cv2.resize(sub["image"], ANALYSIS_SIZE, interpolation=cv2.INTER_AREA)
    visual = _match(first, second)
    visual_summary = {key: visual.get(key) for key in ("verified", "code", "inliers", "overlap", "displacement", "shift_x", "shift_y", "model_candidates")}
    temporal_verified = isinstance(temporal_delta, float) and temporal_delta <= MAXIMUM_TIMESTAMP_DELTA_SECONDS
    return {
        "accepted": bool(visual.get("verified") is True and temporal_verified),
        "reason": "visual_and_temporal_verified" if visual.get("verified") is True and temporal_verified else ("temporal_alignment_unverified" if visual.get("verified") is True else "visual_alignment_unverified"),
        "temporal_delta_seconds": round(temporal_delta, 6) if temporal_delta is not None else None,
        "temporal_verified": temporal_verified,
        "visual": visual_summary,
    }


def _concurrent_pair(base_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        main_future = executor.submit(_fetch, base_url, "profile_1")
        sub_future = executor.submit(_fetch, base_url, "profile_2")
        return main_future.result(), sub_future.result()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("report-low-high-frame-association.json"))
    arguments = parser.parse_args()
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    accepted_pairs: list[dict[str, Any]] = []
    for _index in range(3):
        main_sample, sub_sample = _concurrent_pair(arguments.base_url)
        accepted_pairs.append({"main": _summary(main_sample), "sub": _summary(sub_sample), "association": _pair(main_sample, sub_sample)})
    delayed_low = _fetch(arguments.base_url, "profile_2")
    time.sleep(2.0)
    delayed_high = _fetch(arguments.base_url, "profile_1")
    negative = _pair(delayed_high, delayed_low)
    report = {
        "experiment_id": "E14R",
        "attempt": "stationary_low_high_frame_association",
        "camera": {"id": CAMERA_ID, "label": "Garagem", "sources": list(SOURCES)},
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "maximum_timestamp_delta_seconds": MAXIMUM_TIMESTAMP_DELTA_SECONDS,
        "concurrent_pairs": accepted_pairs,
        "negative_temporal_control": {"low": _summary(delayed_low), "high": _summary(delayed_high), "association": negative},
        "accepted_concurrent_pairs": sum(1 for item in accepted_pairs if item["association"].get("accepted") is True),
    }
    report["outcome"] = (
        "low_high_association_verified_with_temporal_negative_control"
        if report["accepted_concurrent_pairs"] >= 2 and negative.get("accepted") is False and negative.get("reason") == "temporal_alignment_unverified"
        else "low_high_association_inconclusive"
    )
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in ("outcome", "accepted_concurrent_pairs", "ptz_commands_issued")}, sort_keys=True))


if __name__ == "__main__":
    main()
