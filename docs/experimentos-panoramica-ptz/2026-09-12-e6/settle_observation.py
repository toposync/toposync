"""Observe the post-E6 view without issuing PTZ commands or retaining images."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bounded_motion import _safe_frame_summary, _snapshot, _visual_match
from toposync_ext_cameras.processing.panorama_stability import VisualStabilityDetector


DIRECTORY = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--frames", type=int, default=8)
    arguments = parser.parse_args()
    if not 5 <= arguments.frames <= 12:
        raise SystemExit("--frames must be between 5 and 12")

    detector = VisualStabilityDetector(allow_observation_timing=True)
    detector.reset(require_motion_transition=False, now=time.monotonic())
    frames = []
    observations = []
    pairs = []
    for _ in range(arguments.frames):
        started = time.monotonic()
        frame = _snapshot(arguments.base_url)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        frames.append(frame)
        observation = None
        if frame.get("ok"):
            observation = detector.observe(
                frame["image"],
                media_time=None,
                received_monotonic=float(frame["received_monotonic"]),
                sequence=int(frame["sequence"]),
                generation=int(frame["generation"]),
                physical_timestamp_verified=False,
            )
        observations.append({"elapsed_ms": elapsed_ms, "result": observation})
        if len(frames) > 1:
            pairs.append(_visual_match(frames[-2], frames[-1]))
        time.sleep(0.12)

    valid_pairs = [pair for pair in pairs if isinstance(pair, dict) and pair.get("verified")]
    max_pair_displacement = max(
        (float(pair["displacement"]) for pair in valid_pairs if isinstance(pair.get("displacement"), (int, float))),
        default=None,
    )
    terminal = _visual_match(frames[0], frames[-1]) if len(frames) >= 2 else None
    report = {
        "experiment_id": "E6",
        "attempt": "post_return_observation_only",
        "ptz_commands_issued": 0,
        "images_persisted": False,
        "frames": [_safe_frame_summary(frame) for frame in frames],
        "observations": observations,
        "consecutive_visual_matches": pairs,
        "first_to_last_visual_match": terminal,
        "max_consecutive_displacement_analysis_pixels": max_pair_displacement,
        "interpretation": (
            "No physical-timestamp claim is made. This only distinguishes an actively changing decoded view from a view that is visually quiet after the return."
        ),
    }
    (DIRECTORY / "report-post-return-settle.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "frames_observed": len(frames),
        "max_consecutive_displacement_analysis_pixels": max_pair_displacement,
        "first_to_last": terminal,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
