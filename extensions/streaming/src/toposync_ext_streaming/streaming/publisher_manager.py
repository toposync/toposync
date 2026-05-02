from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy

from . import FFMPEG_VERSION
from .ffmpeg_binary import resolve_ffmpeg_binary


LOGGER = logging.getLogger("toposync.extensions.streaming.publisher")


@dataclass(frozen=True, slots=True)
class PublisherOutput:
    output_id: str
    transmission_id: str
    protocol: str


@dataclass(frozen=True, slots=True)
class PublisherInputSettings:
    mode: Literal["rawvideo_pipe", "rtsp_pull"] = "rawvideo_pipe"
    rtsp_url: str | None = None
    source_fps: float | None = None


@dataclass(frozen=True, slots=True)
class PublisherEncodingSettings:
    width: int
    height: int
    fps: float
    preset: str = "veryfast"
    tune: str = "zerolatency"
    video_codec: str = "auto"
    bitrate_kbps: int | None = None
    latency_profile: Literal["normal", "low", "ultra_low"] = "normal"
    prefer_hardware: bool = False


@dataclass(frozen=True, slots=True)
class PublisherRuntimeConfig:
    output: PublisherOutput
    engine_path: str
    publish_url: str
    encoding: PublisherEncodingSettings
    input_settings: PublisherInputSettings


@dataclass(frozen=True, slots=True)
class PublisherStatus:
    output_id: str
    running: bool
    pid: int | None
    publish_url: str
    engine_path: str
    width: int
    height: int
    fps: float
    ffmpeg_path: str | None
    ffmpeg_source: str | None
    frames_sent: int
    restart_count: int
    last_frame_at_unix: float | None
    last_error: str | None
    active_codec: str | None
    hardware_accelerated: bool
    log_path: str | None
    stderr_tail: list[str]


class _LatestFrameSlot:
    def __init__(self) -> None:
        self._frame: numpy.ndarray | None = None
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def set(self, frame: numpy.ndarray) -> None:
        async with self._lock:
            self._frame = frame
            self._event.set()

    async def get(self, *, timeout_s: float = 1.0) -> numpy.ndarray | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.01, float(timeout_s)))
        except TimeoutError:
            return None

        async with self._lock:
            frame = self._frame
            self._event.clear()
            return frame


class _PublisherRuntime:
    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffmpeg_source: str,
        supported_encoders: set[str],
        config: PublisherRuntimeConfig,
        logs_dir: Path,
        max_restarts_per_minute: int = 12,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._ffmpeg_source = ffmpeg_source
        self._supported_encoders = {str(item).strip() for item in supported_encoders if str(item).strip()}
        self._config = config
        self._latest_frame = _LatestFrameSlot()
        self._run_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stop_event = asyncio.Event()
        self._logs_dir = logs_dir
        self._log_file = None
        self._log_path: Path | None = None
        self._max_restarts_per_minute = max(1, int(max_restarts_per_minute))
        self._restart_times: deque[float] = deque(maxlen=max(64, self._max_restarts_per_minute * 3))
        self._consecutive_failures = 0

        self.frames_sent = 0
        self.restart_count = 0
        self.last_frame_at_unix: float | None = None
        self.last_error: str | None = None
        self.active_codec: str | None = None
        self.hardware_accelerated = False
        self._stderr_tail: deque[str] = deque(maxlen=160)

    @property
    def output_id(self) -> str:
        return self._config.output.output_id

    @property
    def config(self) -> PublisherRuntimeConfig:
        return self._config

    def update_config(self, config: PublisherRuntimeConfig) -> None:
        self._config = config

    async def start(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            return
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run_loop(), name=f"streaming.publisher.{self.output_id}")

    async def stop(self) -> None:
        self._stop_event.set()

        task = self._run_task
        self._run_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        await self._shutdown_process()

    async def submit_frame(self, frame: numpy.ndarray) -> None:
        if self._config.input_settings.mode != "rawvideo_pipe":
            return
        normalized = numpy.asarray(frame)
        if normalized.dtype != numpy.uint8:
            normalized = numpy.clip(normalized, 0, 255).astype(numpy.uint8)
        if normalized.ndim != 3 or normalized.shape[2] != 3:
            raise ValueError("Publisher expects BGR frame with 3 channels")
        await self._latest_frame.set(numpy.ascontiguousarray(normalized))

    async def status(self) -> PublisherStatus:
        running = self._process is not None and self._process.returncode is None
        pid = self._process.pid if running and self._process is not None else None
        return PublisherStatus(
            output_id=self.output_id,
            running=running,
            pid=pid,
            publish_url=self._config.publish_url,
            engine_path=self._config.engine_path,
            width=int(self._config.encoding.width),
            height=int(self._config.encoding.height),
            fps=float(self._config.encoding.fps),
            ffmpeg_path=self._ffmpeg_path,
            ffmpeg_source=self._ffmpeg_source,
            frames_sent=int(self.frames_sent),
            restart_count=int(self.restart_count),
            last_frame_at_unix=float(self.last_frame_at_unix) if self.last_frame_at_unix else None,
            last_error=self.last_error,
            active_codec=self.active_codec,
            hardware_accelerated=bool(self.hardware_accelerated),
            log_path=str(self._log_path) if self._log_path else None,
            stderr_tail=list(self._stderr_tail),
        )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._spawn_process()
                await self._pump_frames()
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self._consecutive_failures += 1
            finally:
                await self._shutdown_process()

            if self._stop_event.is_set():
                break

            if not self._register_restart_attempt():
                self.last_error = (
                    f"Publisher restart limit reached for output '{self.output_id}'. "
                    "Waiting for next bridge tick to retry."
                )
                break

            self.restart_count += 1
            delay_s = min(0.6 * (2 ** max(0, self._consecutive_failures - 1)), 12.0)
            await asyncio.sleep(delay_s)

    def _register_restart_attempt(self) -> bool:
        now = time.monotonic()
        self._restart_times.append(now)
        cutoff = now - 60.0
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()
        return len(self._restart_times) <= self._max_restarts_per_minute

    async def _spawn_process(self) -> None:
        args, codec, hardware_accelerated = self._build_ffmpeg_args()
        self.active_codec = codec
        self.hardware_accelerated = hardware_accelerated
        self._open_log_file()

        stdout_target = self._log_file if self._log_file is not None else asyncio.subprocess.DEVNULL
        stderr_stream = asyncio.subprocess.PIPE
        stdin_target = asyncio.subprocess.PIPE if self._config.input_settings.mode == "rawvideo_pipe" else asyncio.subprocess.DEVNULL

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=stdin_target,
            stdout=stdout_target,
            stderr=stderr_stream,
        )

        self._stderr_task = None
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._consume_stderr(self._process.stderr))

    async def _pump_frames(self) -> None:
        if self._config.input_settings.mode == "rtsp_pull":
            await self._pump_rtsp_pull()
            return
        await self._pump_raw_frames()

    async def _pump_rtsp_pull(self) -> None:
        while not self._stop_event.is_set():
            if self._process is None:
                raise RuntimeError("FFmpeg process is not available")
            if self._process.returncode is not None:
                raise RuntimeError(f"FFmpeg exited unexpectedly (code={self._process.returncode})")
            await asyncio.sleep(0.4)

    async def _pump_raw_frames(self) -> None:
        while not self._stop_event.is_set():
            if self._process is None:
                raise RuntimeError("FFmpeg process is not available")
            if self._process.returncode is not None:
                raise RuntimeError(f"FFmpeg exited unexpectedly (code={self._process.returncode})")

            frame = await self._latest_frame.get(timeout_s=1.0)
            if frame is None:
                continue

            expected_width = int(self._config.encoding.width)
            expected_height = int(self._config.encoding.height)
            if frame.shape[1] != expected_width or frame.shape[0] != expected_height:
                raise RuntimeError(
                    "Frame shape mismatch for publisher "
                    f"{self.output_id}: got {frame.shape[1]}x{frame.shape[0]}, expected {expected_width}x{expected_height}"
                )

            stdin = self._process.stdin
            if stdin is None:
                raise RuntimeError("FFmpeg stdin is not available")

            stdin.write(frame.tobytes(order="C"))
            await stdin.drain()
            self.frames_sent += 1
            self.last_frame_at_unix = time.time()

    async def _shutdown_process(self) -> None:
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task

        process = self._process
        self._process = None
        if process is not None:
            stdin = process.stdin
            if stdin is not None:
                with contextlib.suppress(Exception):
                    stdin.close()

            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    async def _consume_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if self._log_file is not None:
                try:
                    self._log_file.write((line + "\n").encode("utf-8", errors="ignore"))
                    self._log_file.flush()
                except Exception:
                    pass

    def _build_ffmpeg_args(self) -> tuple[list[str], str, bool]:
        width = int(self._config.encoding.width)
        height = int(self._config.encoding.height)
        fps = max(1.0, float(self._config.encoding.fps))
        codec = self._pick_video_codec()
        hardware_accelerated = codec in {"h264_nvenc", "h264_vaapi", "h264_videotoolbox"}
        preset, tune = self._resolve_latency_profile()
        bitrate_kbps = self._config.encoding.bitrate_kbps
        ffmpeg_loglevel = str(os.getenv("TOPOSYNC_STREAMING_FFMPEG_LOGLEVEL", "warning") or "warning").strip() or "warning"

        args: list[str] = [self._ffmpeg_path, "-hide_banner", "-loglevel", ffmpeg_loglevel, "-nostats"]

        if self._config.input_settings.mode == "rawvideo_pipe":
            args.extend(
                [
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "bgr24",
                    "-s",
                    f"{width}x{height}",
                    "-r",
                    f"{fps:.3f}",
                    "-i",
                    "pipe:0",
                ]
            )
        else:
            source_url = str(self._config.input_settings.rtsp_url or "").strip()
            if not source_url:
                raise RuntimeError("Bypass publisher requires a non-empty RTSP source URL")
            args.extend(
                [
                    "-rtsp_transport",
                    "tcp",
                    "-fflags",
                    "nobuffer",
                    "-i",
                    source_url,
                ]
            )

        args.extend(["-an", "-c:v", codec])

        if codec == "libx264":
            args.extend(["-preset", preset])
            if tune:
                args.extend(["-tune", tune])
            args.extend(["-pix_fmt", "yuv420p"])
        elif codec == "h264_nvenc":
            args.extend(["-preset", _nvenc_preset_for_latency(self._config.encoding.latency_profile)])
            if self._config.encoding.latency_profile in {"low", "ultra_low"}:
                args.extend(["-tune", "ll"])
            args.extend(["-pix_fmt", "yuv420p"])
        elif codec == "h264_videotoolbox":
            if self._config.encoding.latency_profile in {"low", "ultra_low"}:
                args.extend(["-realtime", "true"])
            args.extend(["-pix_fmt", "yuv420p"])

        filters: list[str] = []
        if self._config.input_settings.mode == "rtsp_pull":
            filters.append(f"fps={fps:.3f}")
            filters.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
            filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black")
        if codec == "h264_vaapi":
            filters.append("format=nv12")
            filters.append("hwupload")
        if filters:
            args.extend(["-vf", ",".join(filters)])

        if bitrate_kbps is not None and int(bitrate_kbps) > 0:
            bitrate = int(bitrate_kbps)
            maxrate = int(round(bitrate * 1.1))
            bufsize = max(2 * bitrate, 512)
            args.extend(["-b:v", f"{bitrate}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"])

        gop = max(1, int(round(fps)))
        args.extend(
            [
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-sc_threshold",
                "0",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                self._config.publish_url,
            ]
        )
        return args, codec, hardware_accelerated

    def _pick_video_codec(self) -> str:
        requested = str(self._config.encoding.video_codec or "").strip().lower()
        if requested and requested != "auto":
            return requested

        if not self._config.encoding.prefer_hardware:
            return "libx264"

        available = self._supported_encoders
        platform = sys.platform.lower()
        ordered_candidates: list[str] = []
        if platform.startswith("darwin"):
            ordered_candidates.extend(["h264_videotoolbox", "h264_nvenc", "h264_vaapi"])
        elif platform.startswith("win"):
            ordered_candidates.extend(["h264_nvenc", "h264_qsv", "h264_amf"])
        else:
            ordered_candidates.extend(["h264_nvenc", "h264_vaapi", "h264_videotoolbox"])

        for candidate in ordered_candidates:
            if candidate in available:
                return candidate
        return "libx264"

    def _resolve_latency_profile(self) -> tuple[str, str]:
        profile = str(self._config.encoding.latency_profile or "normal").strip().lower()
        if profile == "ultra_low":
            default_preset = "ultrafast"
            default_tune = "zerolatency"
        elif profile == "low":
            default_preset = "faster"
            default_tune = "zerolatency"
        else:
            default_preset = "veryfast"
            default_tune = "zerolatency"

        preset = str(self._config.encoding.preset or "").strip() or default_preset
        tune = str(self._config.encoding.tune or "").strip() or default_tune
        return preset, tune

    def _open_log_file(self) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._prune_logs(keep=20)
        safe_id = _sanitize_component(self.output_id, fallback="output")
        name = time.strftime(f"ffmpeg-{safe_id}-%Y%m%d-%H%M%S.log")
        self._log_path = self._logs_dir / name
        self._log_file = self._log_path.open("ab")

    def _prune_logs(self, *, keep: int) -> None:
        try:
            logs = sorted(self._logs_dir.glob("ffmpeg-*.log"), key=lambda item: item.stat().st_mtime, reverse=True)
        except Exception:
            return
        for old in logs[max(0, int(keep)) :]:
            try:
                old.unlink()
            except Exception:
                continue


class PublisherManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        logger: logging.Logger | None = None,
        ffmpeg_version: str = FFMPEG_VERSION,
    ) -> None:
        self._lock = asyncio.Lock()
        self._publishers: dict[str, _PublisherRuntime] = {}
        self._ffmpeg_path: str | None = None
        self._ffmpeg_source: str | None = None
        self._ffmpeg_last_probe_error: str | None = None
        self._ffmpeg_supported_encoders: set[str] = set()
        self._ffmpeg_version = str(ffmpeg_version or FFMPEG_VERSION).strip() or FFMPEG_VERSION
        self._data_dir = Path(data_dir)
        self._logger = logger or LOGGER

    def ffmpeg_path(self) -> str | None:
        return self._ffmpeg_path

    def ffmpeg_source(self) -> str | None:
        return self._ffmpeg_source

    def ffmpeg_last_probe_error(self) -> str | None:
        return self._ffmpeg_last_probe_error

    def probe_ffmpeg(self) -> str | None:
        resolved = resolve_ffmpeg_binary(data_dir=self._data_dir, version=self._ffmpeg_version)
        self._ffmpeg_path = str(resolved.path).strip() if resolved.path is not None else None
        self._ffmpeg_source = str(resolved.source).strip() if resolved.source else None

        if self._ffmpeg_path is None:
            self._ffmpeg_last_probe_error = resolved.error
            self._ffmpeg_supported_encoders.clear()
            return None

        self._ffmpeg_last_probe_error = None
        self._ffmpeg_supported_encoders = _probe_ffmpeg_encoders(self._ffmpeg_path, logger=self._logger)
        return self._ffmpeg_path

    async def start_publisher(
        self,
        *,
        output: PublisherOutput,
        engine_path: str,
        publish_url: str,
        encoding_settings: PublisherEncodingSettings,
        input_settings: PublisherInputSettings | None = None,
    ) -> PublisherStatus:
        async with self._lock:
            ffmpeg_path = self._ffmpeg_path or self.probe_ffmpeg()
            if not ffmpeg_path:
                return PublisherStatus(
                    output_id=str(output.output_id),
                    running=False,
                    pid=None,
                    publish_url=str(publish_url),
                    engine_path=str(engine_path),
                    width=int(encoding_settings.width),
                    height=int(encoding_settings.height),
                    fps=float(encoding_settings.fps),
                    ffmpeg_path=None,
                    ffmpeg_source=None,
                    frames_sent=0,
                    restart_count=0,
                    last_frame_at_unix=None,
                    last_error=self._ffmpeg_last_probe_error,
                    active_codec=None,
                    hardware_accelerated=False,
                    log_path=None,
                    stderr_tail=[],
                )

            config = PublisherRuntimeConfig(
                output=output,
                engine_path=str(engine_path),
                publish_url=str(publish_url),
                encoding=encoding_settings,
                input_settings=input_settings or PublisherInputSettings(),
            )

            existing = self._publishers.get(output.output_id)
            if existing is not None and existing.config != config:
                await existing.stop()
                self._publishers.pop(output.output_id, None)
                existing = None

            if existing is None:
                runtime_logs_dir = self._data_dir / "runtime" / "streaming" / "logs"
                existing = _PublisherRuntime(
                    ffmpeg_path=ffmpeg_path,
                    ffmpeg_source=str(self._ffmpeg_source or "system"),
                    supported_encoders=set(self._ffmpeg_supported_encoders),
                    config=config,
                    logs_dir=runtime_logs_dir,
                )
                self._publishers[output.output_id] = existing
            else:
                existing.update_config(config)

            await existing.start()
            return await existing.status()

    async def submit_frame(self, output_id: str, frame: numpy.ndarray) -> None:
        async with self._lock:
            runtime = self._publishers.get(str(output_id))
        if runtime is None:
            return
        await runtime.submit_frame(frame)

    async def stop_publisher(self, output_id: str) -> None:
        async with self._lock:
            runtime = self._publishers.pop(str(output_id), None)
        if runtime is not None:
            await runtime.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            runtimes = list(self._publishers.values())
            self._publishers.clear()
        for runtime in runtimes:
            await runtime.stop()

    async def stop_missing(self, desired_output_ids: set[str]) -> None:
        desired = {str(item) for item in desired_output_ids}
        async with self._lock:
            to_stop = [key for key in self._publishers.keys() if key not in desired]
        for output_id in to_stop:
            await self.stop_publisher(output_id)

    async def get_publisher_status(self, output_id: str) -> PublisherStatus | None:
        async with self._lock:
            runtime = self._publishers.get(str(output_id))
        if runtime is None:
            return None
        return await runtime.status()

    async def list_status(self) -> dict[str, PublisherStatus]:
        async with self._lock:
            items = list(self._publishers.items())
        out: dict[str, PublisherStatus] = {}
        for output_id, runtime in items:
            out[output_id] = await runtime.status()
        return out

    async def snapshot(self) -> dict[str, Any]:
        statuses = await self.list_status()
        return {
            "ffmpeg_path": self._ffmpeg_path,
            "ffmpeg_source": self._ffmpeg_source,
            "ffmpeg_supported_encoders": sorted(self._ffmpeg_supported_encoders),
            "ffmpeg_last_probe_error": self._ffmpeg_last_probe_error,
            "outputs": {
                key: {
                    "running": status.running,
                    "pid": status.pid,
                    "publish_url": status.publish_url,
                    "engine_path": status.engine_path,
                    "width": status.width,
                    "height": status.height,
                    "fps": status.fps,
                    "frames_sent": status.frames_sent,
                    "restart_count": status.restart_count,
                    "last_frame_at_unix": status.last_frame_at_unix,
                    "last_error": status.last_error,
                    "active_codec": status.active_codec,
                    "hardware_accelerated": status.hardware_accelerated,
                    "log_path": status.log_path,
                    "stderr_tail": list(status.stderr_tail),
                }
                for key, status in statuses.items()
            },
        }


def _nvenc_preset_for_latency(profile: Literal["normal", "low", "ultra_low"]) -> str:
    if profile == "ultra_low":
        return "p1"
    if profile == "low":
        return "p3"
    return "p5"


def _sanitize_component(value: str, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in text)
    out = out.strip("-_")
    return out or fallback


def _probe_ffmpeg_encoders(path: str, *, logger: logging.Logger) -> set[str]:
    try:
        completed = subprocess.run(
            [path, "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning("Failed to probe FFmpeg encoders: %s", exc)
        return set()

    payload = "\n".join([completed.stdout or "", completed.stderr or ""])
    encoders: set[str] = set()
    for raw_line in payload.splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line.startswith("------") or line.startswith("Encoders:"):
            continue
        if len(line) < 8:
            continue
        flags = line[:6]
        if "V" not in flags:
            continue
        parts = line[6:].strip().split()
        if not parts:
            continue
        encoder_name = str(parts[0] or "").strip()
        if not encoder_name:
            continue
        encoders.add(encoder_name)
    return encoders
