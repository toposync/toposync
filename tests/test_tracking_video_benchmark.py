from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.processing.tasks import VisionDetectRuntime, VisionTrackRuntime
from toposync_ext_vision.registry import build_default_model_registry


# Public-domain CCTV video. Do not vendor it in the repo; keep it as an external fixture.
# Source: https://commons.wikimedia.org/wiki/File:Seeking_Information_Pipe_Bombs_in_Washington_D.C._wfo-poi-010521.webm
# License: public domain, fixed CCTV / automated camera recording.
TRACKING_BENCHMARK_VIDEO_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/transcoded/1/19/"
    "Seeking_Information_Pipe_Bombs_in_Washington_D.C._wfo-poi-010521.webm/"
    "Seeking_Information_Pipe_Bombs_in_Washington_D.C._wfo-poi-010521.webm.480p.vp9.webm"
)
TRACKING_BENCHMARK_VIDEO_PAGE = (
    "https://commons.wikimedia.org/wiki/"
    "File:Seeking_Information_Pipe_Bombs_in_Washington_D.C._wfo-poi-010521.webm"
)
TRACKING_BENCHMARK_DEFAULT_START_SECONDS = 20.0
TRACKING_BENCHMARK_DEFAULT_END_SECONDS = 55.0


@dataclass(frozen=True)
class TrackingBenchmarkResult:
    tracker_id: str
    frames_processed: int
    detected_frames: int
    detection_count: int
    event_packet_count: int
    open_event_count: int
    update_event_count: int
    close_event_count: int
    tracking_ids: tuple[str, ...]


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        kwargs.pop("concurrency_key", None)
        return func(*args, **kwargs)

    def observe_telemetry_numeric(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None


def _env_int(name: str, fallback: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(fallback)
    try:
        value = int(raw)
    except Exception:
        return int(fallback)
    return max(int(minimum), min(int(maximum), value))


def _env_float(name: str, fallback: float, *, minimum: float, maximum: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(fallback)
    try:
        value = float(raw)
    except Exception:
        return float(fallback)
    return max(float(minimum), min(float(maximum), value))


def _benchmark_video_path() -> Path:
    raw = str(os.getenv("TOPOSYNC_TRACKING_BENCHMARK_VIDEO") or "").strip()
    if not raw:
        pytest.skip(
            "Set TOPOSYNC_TRACKING_BENCHMARK_VIDEO to a local video path to run the "
            f"tracking benchmark. Suggested CC0 fixture: {TRACKING_BENCHMARK_VIDEO_URL}"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"TOPOSYNC_TRACKING_BENCHMARK_VIDEO does not exist: {path}")
    return path


def _env_text(name: str, fallback: str = "") -> str:
    return str(os.getenv(name) or fallback).strip()


def _import_cv2():
    try:
        import cv2  # type: ignore
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"OpenCV is required to decode the tracking benchmark video: {exc}")
    return cv2


def _sample_video_frames(
    path: Path,
    *,
    sample_fps: float,
    max_frames: int,
    start_seconds: float = 0.0,
    end_seconds: float = 0.0,
) -> list[tuple[float, Any]]:
    cv2 = _import_cv2()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        pytest.skip(f"OpenCV could not open tracking benchmark video: {path}")

    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if source_fps <= 0.0:
        source_fps = 30.0
    stride = max(1, int(round(source_fps / max(0.1, float(sample_fps)))))
    if start_seconds > 0.0:
        capture.set(cv2.CAP_PROP_POS_MSEC, float(start_seconds) * 1000.0)
    frames: list[tuple[float, Any]] = []
    index = int(round(max(0.0, float(start_seconds)) * source_fps))
    while len(frames) < int(max_frames):
        ok, frame = capture.read()
        if not ok:
            break
        ts_s = index / source_fps
        if end_seconds > 0.0 and ts_s > end_seconds:
            break
        if index % stride == 0:
            frames.append((float(ts_s), frame))
        index += 1
    capture.release()

    if not frames:
        pytest.skip(f"No frames could be decoded from tracking benchmark video: {path}")
    return frames


async def _detect_video_frames(
    *,
    frames: list[tuple[float, Any]],
    model_id: str,
    confidence_threshold: float,
) -> list[Packet]:
    dependencies = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
    detect = VisionDetectRuntime(
        {
            "model_id": model_id,
            "categories": ["person"],
            "confidence_threshold": confidence_threshold,
            "emit_mode": "annotate",
        },
        dependencies,
    )
    context = _Context()
    out: list[Packet] = []
    for frame_ts, frame in frames:
        packet = Packet.create(
            stream_id="camera:benchmark:big-city-life",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "camera_id": "benchmark-camera",
                "frame_ts": frame_ts,
                "frame_width": int(getattr(frame, "shape", [0, 0])[1]),
                "frame_height": int(getattr(frame, "shape", [0, 0])[0]),
            },
            artifacts={"main": Artifact(name="main", data=frame, mime_type="image/raw")},
            metadata={"motion_gate_open": True},
        )
        detected = await detect.process_packet(packet, context)
        if detected:
            out.append(detected[0])
    return out


async def _run_tracker(
    *,
    tracker_id: str,
    detected_packets: list[Packet],
    close_after_seconds: float,
    default_interval_seconds: float,
) -> TrackingBenchmarkResult:
    dependencies = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
    track = VisionTrackRuntime(
        {
            "tracker_id": tracker_id,
            "emit_mode": "events",
            "close_after_seconds": close_after_seconds,
            "default_interval_seconds": default_interval_seconds,
        },
        dependencies,
    )
    context = _Context()
    outputs: list[Packet] = []
    detected_frames = 0
    detection_count = 0
    for packet in detected_packets:
        detections = packet.payload.get("vision", {}).get("detections", [])
        if isinstance(detections, list) and detections:
            detected_frames += 1
            detection_count += len(detections)
        outputs.extend(await track.process_packet(packet, context))

    lifecycle_counts = Counter(str(packet.lifecycle.value) for packet in outputs)
    tracking_ids = sorted(
        {
            str(packet.payload.get("tracking_id") or "").strip()
            for packet in outputs
            if str(packet.payload.get("tracking_id") or "").strip()
        }
    )
    return TrackingBenchmarkResult(
        tracker_id=tracker_id,
        frames_processed=len(detected_packets),
        detected_frames=detected_frames,
        detection_count=detection_count,
        event_packet_count=len(outputs),
        open_event_count=int(lifecycle_counts[Lifecycle.OPEN.value]),
        update_event_count=int(lifecycle_counts[Lifecycle.UPDATE.value]),
        close_event_count=int(lifecycle_counts[Lifecycle.CLOSE.value]),
        tracking_ids=tuple(tracking_ids),
    )


def _tracker_ids_from_env() -> list[str]:
    raw = str(
        os.getenv("TOPOSYNC_TRACKING_BENCHMARK_TRACKERS")
        or "simple_iou_kalman,norfair"
    ).strip()
    trackers = [item.strip() for item in raw.split(",") if item.strip()]
    return trackers or ["simple_iou_kalman", "norfair"]


def _max_open_events_for_tracker(tracker_id: str) -> int | None:
    env_key = f"TOPOSYNC_TRACKING_BENCHMARK_MAX_OPEN_EVENTS_{tracker_id.upper()}"
    raw = str(os.getenv(env_key) or os.getenv("TOPOSYNC_TRACKING_BENCHMARK_MAX_OPEN_EVENTS") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    return max(0, value)


@pytest.mark.integration
def test_tracking_video_benchmark_counts_event_fragmentation() -> None:
    video_path = _benchmark_video_path()
    start_seconds = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_START_SECONDS",
        TRACKING_BENCHMARK_DEFAULT_START_SECONDS,
        minimum=0.0,
        maximum=86_400.0,
    )
    end_seconds = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_END_SECONDS",
        TRACKING_BENCHMARK_DEFAULT_END_SECONDS,
        minimum=0.0,
        maximum=86_400.0,
    )
    frames = _sample_video_frames(
        video_path,
        sample_fps=_env_float(
            "TOPOSYNC_TRACKING_BENCHMARK_SAMPLE_FPS",
            5.0,
            minimum=0.5,
            maximum=30.0,
        ),
        max_frames=_env_int(
            "TOPOSYNC_TRACKING_BENCHMARK_MAX_FRAMES",
            80,
            minimum=1,
            maximum=1_000,
        ),
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    model_id = str(os.getenv("TOPOSYNC_TRACKING_BENCHMARK_MODEL_ID") or "rfdetr_det_medium").strip()
    confidence_threshold = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_CONFIDENCE",
        0.55,
        minimum=0.0,
        maximum=1.0,
    )
    close_after_seconds = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_CLOSE_AFTER_SECONDS",
        5.0,
        minimum=0.05,
        maximum=300.0,
    )
    default_interval_seconds = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_DEFAULT_INTERVAL_SECONDS",
        0.25,
        minimum=0.0,
        maximum=120.0,
    )

    try:
        detected_packets = asyncio.run(
            _detect_video_frames(
                frames=frames,
                model_id=model_id,
                confidence_threshold=confidence_threshold,
            )
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Tracking benchmark detector is not available for model {model_id!r}: {exc}")

    assert detected_packets, "benchmark detector produced no packets"

    results: list[TrackingBenchmarkResult] = []
    for tracker_id in _tracker_ids_from_env():
        try:
            results.append(
                asyncio.run(
                    _run_tracker(
                        tracker_id=tracker_id,
                        detected_packets=detected_packets,
                        close_after_seconds=close_after_seconds,
                        default_interval_seconds=default_interval_seconds,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Tracking benchmark failed for tracker {tracker_id!r}: {exc}")

    report = {
        "fixture": {
            "name": _env_text("TOPOSYNC_TRACKING_BENCHMARK_FIXTURE_NAME", video_path.stem),
            "path": str(video_path),
            "source_url": _env_text("TOPOSYNC_TRACKING_BENCHMARK_SOURCE_URL", TRACKING_BENCHMARK_VIDEO_URL),
            "source_page": _env_text("TOPOSYNC_TRACKING_BENCHMARK_SOURCE_PAGE", TRACKING_BENCHMARK_VIDEO_PAGE),
            "license": _env_text(
                "TOPOSYNC_TRACKING_BENCHMARK_LICENSE",
                "Public domain fixed CCTV / automated camera recording",
            ),
        },
        "config": {
            "model_id": model_id,
            "confidence_threshold": confidence_threshold,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "sample_fps": _env_float(
                "TOPOSYNC_TRACKING_BENCHMARK_SAMPLE_FPS",
                5.0,
                minimum=0.5,
                maximum=30.0,
            ),
            "close_after_seconds": close_after_seconds,
            "default_interval_seconds": default_interval_seconds,
        },
        "results": [result.__dict__ for result in results],
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    report_path = str(os.getenv("TOPOSYNC_TRACKING_BENCHMARK_REPORT") or "").strip()
    if report_path:
        Path(report_path).expanduser().write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    assert any(result.detected_frames > 0 for result in results), "benchmark had no detected person frames"
    for result in results:
        assert result.open_event_count > 0, f"{result.tracker_id} did not open any tracked events"
        max_open_events = _max_open_events_for_tracker(result.tracker_id)
        if max_open_events is not None:
            assert result.open_event_count <= max_open_events, (
                f"{result.tracker_id} opened {result.open_event_count} events, "
                f"expected at most {max_open_events}"
            )
