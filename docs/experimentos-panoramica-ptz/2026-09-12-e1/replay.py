"""Run E1 against the recorded Garagem panorama replay without camera access.

The script deliberately calls the production detector, matcher and late-endpoint
path. It only creates a temporary durable baseline required by that path and
writes a small report next to this script.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import tempfile
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from toposync_ext_cameras import panorama_scan as scan
from toposync_ext_cameras.panorama_scan import _match
from toposync_ext_cameras.processing.panorama_stability import VisualStabilityDetector


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_REPLAY_DIRECTORY = Path(
    ".toposync-data/runtime/cameras/source-panorama/jobs/"
    "097e9f406287495f8582423f5f70806c"
)
REPLAY_STEM = "replay-rejected-9b279e2fb08a4018af5ca34053faab31"
CAPTURE_INSTANCE = "e1-replay-capture-instance"
MOVEMENT_ID = "1234567890abcdef1234567890abcdef"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "code",
            "stable",
            "has_motion_transition",
            "evidence",
            "window_frames",
            "window_observation_seconds",
            "recovery_code",
            "recovery_metrics",
        )
    }


def _load(directory: Path) -> tuple[dict[str, Any], list[np.ndarray]]:
    metadata_path = directory / f"{REPLAY_STEM}.json"
    archive_path = directory / f"{REPLAY_STEM}.npz"
    metadata = json.loads(metadata_path.read_text())
    records = metadata.get("frames")
    if not isinstance(records, list) or not records:
        raise ValueError("Replay has no ordered frame metadata")
    with np.load(archive_path, allow_pickle=False) as archive:
        images = [archive[f"frame_{index}"] for index in range(len(records))]
    if len(images) != len(records) or any(image.dtype != np.uint8 for image in images):
        raise ValueError("Replay image sequence is inconsistent")
    return metadata, images


def _observe(
    detector: VisualStabilityDetector,
    image: np.ndarray,
    record: dict[str, Any],
    *,
    generation: int | None = None,
) -> dict[str, Any]:
    return detector.observe(
        image,
        media_time=record["media_time"],
        received_monotonic=record["received_monotonic"],
        sequence=record["sequence"],
        generation=record["generation"] if generation is None else generation,
        pose=record["pose"],
        physical_timestamp_verified=record["physical_timestamp_verified"],
    )


def _normal_replay(metadata: dict[str, Any], images: list[np.ndarray]) -> dict[str, Any]:
    records = metadata["frames"]
    detector = VisualStabilityDetector(allow_observation_timing=True)
    started = float(metadata["started_monotonic"])
    stop_at = started + float(metadata["attempt"]["stop_accepted_seconds"])
    detector.reset(now=started)
    results: list[dict[str, Any]] = []
    stopped = False
    for image, record in zip(images, records, strict=True):
        if not stopped and record["received_monotonic"] >= stop_at:
            detector.arm_stop(now=stop_at)
            stopped = True
        results.append(_observe(detector, image, record))
    actual_counts = dict(Counter(result["code"] for result in results))
    expected_counts = metadata["attempt"]["code_counts"]
    return {
        "last": _compact(results[-1]),
        "code_counts": actual_counts,
        "matches_recorded_code_counts": actual_counts == expected_counts,
        "accepted_any_frame": any(result["stable"] for result in results),
    }


def _frame(image: np.ndarray, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "image": image,
        "capture_instance": CAPTURE_INSTANCE,
        "sequence": record["sequence"],
        "generation": record["generation"],
        "received_monotonic": record["received_monotonic"],
        "media_time": record["media_time"],
        "physical_timestamp_verified": record["physical_timestamp_verified"],
        "source_width": 2880,
        "source_height": 1620,
        "image_representation": "analysis",
    }


async def _late_endpoint(
    metadata: dict[str, Any],
    images: list[np.ndarray],
    *,
    terminal_mutation: str | None = None,
    baseline_image: np.ndarray | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    records = metadata["frames"]
    frames = deque(_frame(image, record) for image, record in zip(images, records, strict=True))
    baseline = frames[0]
    terminal = deque(copy.deepcopy(frame) for frame in list(frames)[-14:])
    if terminal_mutation == "frozen":
        frozen = terminal[0]["image"]
        for frame in terminal:
            frame["image"] = frozen
    elif terminal_mutation == "generation_changed":
        terminal[-1]["generation"] += 1
    elif terminal_mutation == "capture_instance_changed":
        terminal[-1]["capture_instance"] = "replacement-capture-instance"
    elif terminal_mutation is not None:
        raise ValueError(f"Unknown terminal mutation: {terminal_mutation}")
    now = records[-1]["received_monotonic"] + 0.1
    original_time = scan.time
    scan.time = SimpleNamespace(monotonic=lambda: now, time=lambda: now)
    try:
        with tempfile.TemporaryDirectory(prefix="toposync-e1-") as temporary:
            directory = Path(temporary).resolve()
            durable = directory / "baseline.png"
            digest = scan._write_image(durable, images[0] if baseline_image is None else baseline_image)
            transition = {
                "movement_id": MOVEMENT_ID,
                "state": "pending",
                "intent": {"type": "seek_pulse"},
                "baseline": {
                    "path": str(durable),
                    "sha256": digest,
                    **{
                        key: baseline[key]
                        for key in (
                            "capture_instance",
                            "sequence",
                            "generation",
                            "media_time",
                            "received_monotonic",
                        )
                    },
                },
            }
            scanner = object.__new__(scan._Scan)
            scanner.returning = False
            scanner._stop_failed = False
            scanner.directory = directory
            scanner.checkpoint = {"continuous_cursor": {"transition": transition}}
            diagnostic = scan._AttemptDiagnostic("movement")
            diagnostic.value.update(
                movement_id=MOVEMENT_ID,
                command_outcome="accepted",
                stop_command_accepted=True,
            )
            result = await scan._Scan._late_endpoint_capture(
                scanner,
                baseline=baseline,
                frames=frames,
                terminal_frames=terminal,
                transition=transition,
                diagnostic=diagnostic,
                command_finished=True,
                stopped=True,
            )
            return result, {
                key: diagnostic.value.get(key)
                for key in (
                    "late_endpoint_rejection",
                    "late_endpoint_transition",
                    "terminal_scene",
                    "last_comparison",
                )
            }
    finally:
        scan.time = original_time


def _endpoint_gate_controls(matching: dict[str, Any], metadata: dict[str, Any], images: list[np.ndarray]) -> dict[str, Any]:
    tail_images = images[-12:]
    tail_records = metadata["frames"][-12:]
    controls: dict[str, dict[str, Any]] = {}
    for name, evidence in {
        "concentrated_correspondence": {
            **copy.deepcopy(matching),
            "model_candidates": [{"inliers": 42, "occupied_cells": 3, "hull_fraction": 0.2}],
        },
        "missing_homography": {**copy.deepcopy(matching), "homography": None},
    }.items():
        detector = VisualStabilityDetector(allow_observation_timing=True)
        detector.reset(now=tail_records[0]["received_monotonic"])
        detector.arm_stop(now=tail_records[0]["received_monotonic"])
        armed = detector.arm_verified_endpoint_transition(evidence)
        controls[name] = _compact(armed)
    for name, frozen, generation_changed in (
        ("frozen_terminal", True, False),
        ("decoder_generation_changed", False, True),
    ):
        detector = VisualStabilityDetector(allow_observation_timing=True)
        detector.reset(now=tail_records[0]["received_monotonic"])
        detector.arm_stop(now=tail_records[0]["received_monotonic"])
        armed = detector.arm_verified_endpoint_transition(copy.deepcopy(matching))
        result = armed
        for index, (image, record) in enumerate(zip(tail_images, tail_records, strict=True)):
            result = _observe(
                detector,
                tail_images[0] if frozen else image,
                record,
                generation=record["generation"] + 1 if generation_changed and index == 3 else None,
            )
        controls[name] = {"armed": _compact(armed), "terminal": _compact(result)}
    return controls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-directory", type=Path, default=DEFAULT_REPLAY_DIRECTORY)
    parser.add_argument("--output", type=Path, default=SCRIPT_DIRECTORY / "report.json")
    arguments = parser.parse_args()
    replay_directory = arguments.replay_directory.resolve()
    metadata, images = _load(replay_directory)
    cv2.setRNGSeed(20260912)
    cv2.setNumThreads(2)
    normal = _normal_replay(metadata, images)
    matching = _match(images[0], images[-1])
    positive, positive_diagnostic = asyncio.run(_late_endpoint(metadata, images))
    scanner_controls: dict[str, dict[str, Any]] = {}
    for name, mutation, baseline_image in (
        ("frozen_terminal", "frozen", None),
        ("decoder_generation_changed", "generation_changed", None),
        ("capture_instance_changed", "capture_instance_changed", None),
        ("unmatched_durable_anchor", None, np.zeros_like(images[0])),
    ):
        result, diagnostic = asyncio.run(
            _late_endpoint(
                metadata,
                images,
                terminal_mutation=mutation,
                baseline_image=baseline_image,
            )
        )
        scanner_controls[name] = {
            "accepted": result is not None,
            "diagnostic": diagnostic,
        }
    gate_controls = _endpoint_gate_controls(matching, metadata, images)
    controls_rejected = (
        all(not control["accepted"] for control in scanner_controls.values())
        and gate_controls["concentrated_correspondence"]["stable"] is False
        and gate_controls["missing_homography"]["stable"] is False
        and gate_controls["frozen_terminal"]["terminal"]["stable"] is False
        and gate_controls["decoder_generation_changed"]["terminal"]["stable"] is False
    )
    report = {
        "experiment": "E1",
        "inputs": {
            "metadata_sha256": _digest(replay_directory / f"{REPLAY_STEM}.json"),
            "frames_sha256": _digest(replay_directory / f"{REPLAY_STEM}.npz"),
            "frame_count": len(images),
            "timing_basis": "local_observation",
            "physical_camera_access": False,
        },
        "normal_replay": normal,
        "endpoint_match": {
            key: matching.get(key)
            for key in ("verified", "displacement", "overlap", "inliers", "model_candidates")
        },
        "late_endpoint_positive": {
            "accepted": positive is not None,
            "result": _compact(positive["evidence"]) if positive is not None else None,
            "diagnostic": positive_diagnostic,
        },
        "scanner_negative_controls": scanner_controls,
        "detector_negative_controls": gate_controls,
        "decision": {
            "pass": bool(
                normal["matches_recorded_code_counts"]
                and not normal["accepted_any_frame"]
                and positive is not None
                and positive["stable"] is True
                and controls_rejected
            ),
            "scope": "Replay-only proof of the current late-endpoint decision path.",
            "limits": [
                "No PTS or physical exposure timestamp was present in the recording.",
                "No camera command was sent; this cannot prove a new physical trajectory.",
                "Lens identity is held by PanoramaCamera configuration and source-geometry guards, outside this replay adapter.",
            ],
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
