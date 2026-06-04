from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_vision.processing.tasks import (
    VisionDetectRuntime,
    VisionTrackRuntime,
)
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
TRACKING_BENCHMARK_TRACK_COLORS_RGB: tuple[tuple[int, int, int], ...] = (
    (230, 57, 70),
    (42, 157, 143),
    (244, 162, 97),
    (69, 123, 157),
    (131, 56, 236),
    (255, 183, 3),
    (0, 150, 199),
    (214, 40, 40),
    (6, 214, 160),
    (255, 0, 110),
    (58, 134, 255),
    (128, 185, 24),
    (251, 86, 7),
    (114, 9, 183),
    (46, 196, 182),
    (255, 202, 58),
)


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
    raw_tracklet_count: int
    event_ids: tuple[str, ...]
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
            "close_after_seconds": close_after_seconds,
            "default_interval_seconds": default_interval_seconds,
        },
        dependencies,
    )
    context = _Context()
    outputs: list[Packet] = []
    raw_tracklet_ids: set[str] = set()
    detected_frames = 0
    detection_count = 0
    for packet in detected_packets:
        detections = packet.payload.get("vision", {}).get("detections", [])
        if isinstance(detections, list) and detections:
            detected_frames += 1
            detection_count += len(detections)
        track_packets = await track.process_packet(packet, context)
        for track_packet in track_packets:
            vision = track_packet.payload.get("vision")
            raw_tracks = vision.get("tracks") if isinstance(vision, dict) else None
            if isinstance(raw_tracks, list):
                for raw_track in raw_tracks:
                    if not isinstance(raw_track, dict):
                        continue
                    tracklet_id = str(
                        raw_track.get("tracklet_id") or raw_track.get("tracking_id") or ""
                    ).strip()
                    if tracklet_id:
                        raw_tracklet_ids.add(tracklet_id)
            outputs.append(track_packet)

    lifecycle_counts = Counter(str(packet.lifecycle.value) for packet in outputs)
    event_ids = sorted(
        {
            str(packet.payload.get("event_id") or "").strip()
            for packet in outputs
            if str(packet.payload.get("event_id") or "").strip()
        }
    )
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
        raw_tracklet_count=len(raw_tracklet_ids),
        event_ids=tuple(event_ids),
        tracking_ids=tuple(tracking_ids),
    )


async def _run_tracker_annotations(
    *,
    tracker_id: str,
    detected_packets: list[Packet],
    close_after_seconds: float,
) -> list[tuple[Any, ...]]:
    dependencies = PipelineRuntimeDependencies(vision_model_registry=build_default_model_registry())
    track = VisionTrackRuntime(
        {
            "tracker_id": tracker_id,
            "close_after_seconds": close_after_seconds,
        },
        dependencies,
    )
    context = _Context()
    annotations: list[tuple[Any, ...]] = []
    for packet in detected_packets:
        outputs = await track.process_packet(packet, context)
        if not outputs:
            annotations.append(())
            continue
        vision = outputs[0].payload.get("vision")
        raw_tracks = vision.get("tracks") if isinstance(vision, dict) else None
        annotations.append(tuple(raw_tracks) if isinstance(raw_tracks, list) else ())
    return annotations


async def _run_trackers_annotations(
    *,
    tracker_ids: list[str],
    detected_packets: list[Packet],
    close_after_seconds: float,
) -> dict[str, list[tuple[Any, ...]]]:
    annotations: dict[str, list[tuple[Any, ...]]] = {}
    for tracker_id in tracker_ids:
        annotations[tracker_id] = await _run_tracker_annotations(
            tracker_id=tracker_id,
            detected_packets=detected_packets,
            close_after_seconds=close_after_seconds,
        )
    return annotations


def _value_from_object(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _bbox01_from_object(item: Any) -> tuple[float, float, float, float] | None:
    raw = _value_from_object(item, "bbox01")
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        x1, y1, x2, y2 = [float(value) for value in raw[:4]]
    except Exception:
        return None
    values = (x1, y1, x2, y2)
    if not all(math.isfinite(value) for value in values):
        return None
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


def _bbox01_to_pixels(
    bbox01: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox01
    max_x = max(0, int(width) - 1)
    max_y = max(0, int(height) - 1)
    return (
        max(0, min(max_x, int(round(x1 * max_x)))),
        max(0, min(max_y, int(round(y1 * max_y)))),
        max(0, min(max_x, int(round(x2 * max_x)))),
        max(0, min(max_y, int(round(y2 * max_y)))),
    )


def _detections_from_packet(packet: Packet) -> tuple[Any, ...]:
    vision = packet.payload.get("vision")
    raw_detections = vision.get("detections") if isinstance(vision, dict) else None
    return tuple(raw_detections) if isinstance(raw_detections, list) else ()


def _track_color_bgr(tracker_id: str, tracking_id: str) -> tuple[int, int, int]:
    key = f"{tracker_id}:{tracking_id}"
    index = sum((position + 1) * ord(char) for position, char in enumerate(key))
    red, green, blue = TRACKING_BENCHMARK_TRACK_COLORS_RGB[
        index % len(TRACKING_BENCHMARK_TRACK_COLORS_RGB)
    ]
    return (blue, green, red)


def _label_text_color_bgr(background_bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    blue, green, red = background_bgr
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return (20, 20, 20) if luminance >= 150.0 else (255, 255, 255)


def _short_tracking_id(tracking_id: str) -> str:
    text = str(tracking_id or "").strip()
    if not text:
        return "?"
    return text.rsplit(":", 1)[-1] or text


def _draw_label(
    cv2: Any,
    frame: Any,
    *,
    text: str,
    x: int,
    y: int,
    background_bgr: tuple[int, int, int],
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.42
    thickness = 1
    label = str(text or "").strip()
    if not label:
        return
    (text_width, text_height), baseline = cv2.getTextSize(label, font, font_scale, thickness)
    frame_height, frame_width = frame.shape[:2]
    label_x = max(0, min(int(x), max(0, frame_width - text_width - 8)))
    label_y = max(text_height + baseline + 4, int(y))
    label_y = min(label_y, max(text_height + baseline + 4, frame_height - 2))
    top_left = (label_x, label_y - text_height - baseline - 4)
    bottom_right = (label_x + text_width + 8, label_y + baseline)
    cv2.rectangle(frame, top_left, bottom_right, background_bgr, -1)
    cv2.putText(
        frame,
        label,
        (label_x + 4, label_y - 3),
        font,
        font_scale,
        _label_text_color_bgr(background_bgr),
        thickness,
        cv2.LINE_AA,
    )


def _draw_detections(cv2: Any, frame: Any, detections: tuple[Any, ...]) -> None:
    height, width = frame.shape[:2]
    for detection in detections:
        bbox01 = _bbox01_from_object(detection)
        if bbox01 is None:
            continue
        x1, y1, x2, y2 = _bbox01_to_pixels(bbox01, width=width, height=height)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (175, 175, 175), 1, cv2.LINE_AA)


def _draw_tracks(
    cv2: Any,
    frame: Any,
    *,
    tracker_id: str,
    tracks: tuple[Any, ...],
) -> None:
    height, width = frame.shape[:2]
    for track in tracks:
        bbox01 = _bbox01_from_object(track)
        if bbox01 is None:
            continue
        tracking_id = str(_value_from_object(track, "tracking_id") or "").strip()
        color = _track_color_bgr(tracker_id, tracking_id)
        x1, y1, x2, y2 = _bbox01_to_pixels(bbox01, width=width, height=height)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
        label = str(_value_from_object(track, "label") or _value_from_object(track, "category") or "track")
        score = _value_from_object(track, "score")
        if score is None:
            score = _value_from_object(track, "confidence")
        try:
            score_text = f" {float(score):.2f}"
        except Exception:
            score_text = ""
        _draw_label(
            cv2,
            frame,
            text=f"#{_short_tracking_id(tracking_id)} {label}{score_text}",
            x=x1,
            y=max(0, y1 - 4),
            background_bgr=color,
        )


def _frame_from_packet(cv2: Any, packet: Packet) -> Any:
    artifact = packet.artifacts.get("main")
    if artifact is None and packet.artifacts:
        artifact = next(iter(packet.artifacts.values()))
    frame = getattr(artifact, "data", None) if artifact is not None else None
    if frame is None or not hasattr(frame, "copy"):
        raise ValueError("benchmark packet does not contain a writable frame artifact")
    copied = frame.copy()
    shape = getattr(copied, "shape", ())
    if len(shape) == 2:
        return cv2.cvtColor(copied, cv2.COLOR_GRAY2BGR)
    if len(shape) == 3 and shape[2] == 4:
        return cv2.cvtColor(copied, cv2.COLOR_BGRA2BGR)
    return copied


def _draw_panel_header(
    cv2: Any,
    frame: Any,
    *,
    tracker_id: str,
    detection_count: int,
    track_count: int,
    frame_index: int,
) -> None:
    width = int(frame.shape[1])
    cv2.rectangle(frame, (0, 0), (width, 26), (25, 25, 25), -1)
    cv2.putText(
        frame,
        f"{tracker_id} | frame {frame_index + 1} | det {detection_count} | tracks {track_count}",
        (7, 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (245, 245, 245),
        1,
        cv2.LINE_AA,
    )


def _pad_even_frame(cv2: Any, frame: Any) -> Any:
    height, width = frame.shape[:2]
    pad_right = int(width % 2)
    pad_bottom = int(height % 2)
    if not pad_right and not pad_bottom:
        return frame
    return cv2.copyMakeBorder(
        frame,
        0,
        pad_bottom,
        0,
        pad_right,
        cv2.BORDER_CONSTANT,
        value=(0, 0, 0),
    )


def _write_tracking_overlay_video(
    *,
    output_path: Path,
    detected_packets: list[Packet],
    tracker_ids: list[str],
    annotations_by_tracker: dict[str, list[tuple[Any, ...]]],
    fps: float,
) -> Path:
    cv2 = _import_cv2()
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not detected_packets:
        raise ValueError("no packets are available for tracking overlay video")
    if not tracker_ids:
        raise ValueError("at least one tracker is required for tracking overlay video")

    suffix = output_path.suffix.lower()
    fourcc_text = "MJPG" if suffix == ".avi" else "mp4v"
    writer = None
    try:
        for frame_index, packet in enumerate(detected_packets):
            detections = _detections_from_packet(packet)
            panels = []
            for tracker_id in tracker_ids:
                frame = _frame_from_packet(cv2, packet)
                tracks = ()
                tracker_annotations = annotations_by_tracker.get(tracker_id)
                if tracker_annotations is not None and frame_index < len(tracker_annotations):
                    tracks = tracker_annotations[frame_index]
                _draw_detections(cv2, frame, detections)
                _draw_tracks(cv2, frame, tracker_id=tracker_id, tracks=tracks)
                _draw_panel_header(
                    cv2,
                    frame,
                    tracker_id=tracker_id,
                    detection_count=len(detections),
                    track_count=len(tracks),
                    frame_index=frame_index,
                )
                panels.append(frame)
            combined = panels[0] if len(panels) == 1 else cv2.hconcat(panels)
            combined = _pad_even_frame(cv2, combined)
            if writer is None:
                height, width = combined.shape[:2]
                writer = cv2.VideoWriter(
                    str(output_path),
                    cv2.VideoWriter_fourcc(*fourcc_text),
                    float(max(0.5, fps)),
                    (int(width), int(height)),
                )
                if not writer.isOpened():
                    raise ValueError(f"OpenCV could not open output video writer: {output_path}")
            writer.write(combined)
    finally:
        if writer is not None:
            writer.release()

    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise ValueError(f"tracking overlay video was not written: {output_path}")
    return output_path


def _safe_output_suffix(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "subject_v1"


def _postfixed_output_video_path(path: Path) -> Path:
    suffix = _safe_output_suffix(
        _env_text("TOPOSYNC_TRACKING_BENCHMARK_OUTPUT_SUFFIX", "subject_v1")
    )
    target = path.expanduser()
    if not target.stem.endswith(f"__{suffix}"):
        target = target.with_name(f"{target.stem}__{suffix}{target.suffix or '.mp4'}")
    if not target.exists():
        return target
    for index in range(2, 10_000):
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not find a non-existing output video path near {target}")


def _tracker_ids_from_env() -> list[str]:
    raw = str(
        os.getenv("TOPOSYNC_TRACKING_BENCHMARK_TRACKERS")
        or "byte_world,simple_iou_kalman,norfair"
    ).strip()
    trackers = [item.strip() for item in raw.split(",") if item.strip()]
    return trackers or ["byte_world", "simple_iou_kalman", "norfair"]


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
    sample_fps = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_SAMPLE_FPS",
        5.0,
        minimum=0.5,
        maximum=30.0,
    )
    frames = _sample_video_frames(
        video_path,
        sample_fps=sample_fps,
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
        0.25,
        minimum=0.0,
        maximum=1.0,
    )
    close_after_seconds = _env_float(
        "TOPOSYNC_TRACKING_BENCHMARK_CLOSE_AFTER_SECONDS",
        10.0,
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

    tracker_ids = _tracker_ids_from_env()
    results: list[TrackingBenchmarkResult] = []
    for tracker_id in tracker_ids:
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

    artifacts: dict[str, str] = {}
    output_video = _env_text("TOPOSYNC_TRACKING_BENCHMARK_OUTPUT_VIDEO")
    if output_video:
        try:
            annotations_by_tracker = asyncio.run(
                _run_trackers_annotations(
                    tracker_ids=tracker_ids,
                    detected_packets=detected_packets,
                    close_after_seconds=close_after_seconds,
                )
            )
            written_video = _write_tracking_overlay_video(
                output_path=_postfixed_output_video_path(Path(output_video)),
                detected_packets=detected_packets,
                tracker_ids=tracker_ids,
                annotations_by_tracker=annotations_by_tracker,
                fps=sample_fps,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Tracking benchmark output video failed: {exc}")
        artifacts["output_video"] = str(written_video)

    report = {
        "artifacts": artifacts,
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
            "sample_fps": sample_fps,
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
