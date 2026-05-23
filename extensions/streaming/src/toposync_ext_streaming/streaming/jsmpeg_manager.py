from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import numpy
from fastapi import WebSocket, WebSocketDisconnect

from ..api.models import StreamingJsmpegSettings, StreamingStalePolicySettings, Transmission, TransmissionOutput
from .ffmpeg_binary import ResolvedFFmpegBinary, resolve_ffmpeg_binary
from .placeholder import get_placeholder_frame
from .resize import resize_frame_contain
from .runtime_state import TransmissionRuntimeState


LOGGER = logging.getLogger("toposync.extensions.streaming.jsmpeg")
JSMPEG_MPEG1_FPS = 25.0


@dataclass(frozen=True, slots=True)
class JsmpegTargetDimensions:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class JsmpegStatus:
    enabled: bool
    ffmpeg_path: str | None
    ffmpeg_source: str | None
    ffmpeg_error: str | None
    running_session_count: int
    max_total_sessions: int
    max_sessions_per_transmission: int
    sessions_by_transmission: dict[str, int]
    frames_encoded: int
    bytes_sent: int
    last_error: str | None
    warnings: list[str]


@dataclass(slots=True)
class _JsmpegSession:
    session_id: str
    transmission_id: str
    output_id: str
    started_at_monotonic: float = field(default_factory=time.monotonic)
    frames_encoded: int = 0
    bytes_sent: int = 0
    last_error: str | None = None
    process: asyncio.subprocess.Process | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))


def normalize_jsmpeg_dimensions(
    *,
    output_width: int | None,
    output_height: int | None,
    max_width: int,
    max_height: int,
) -> JsmpegTargetDimensions:
    source_width = max(2, int(output_width or max_width))
    source_height = max(2, int(output_height or max_height))
    target_max_width = _even(max(2, int(max_width)))
    target_max_height = _even(max(2, int(max_height)))

    ratio = min(float(target_max_width) / float(source_width), float(target_max_height) / float(source_height), 1.0)
    width = _even(max(2, int(round(source_width * ratio))))
    height = _even(max(2, int(round(source_height * ratio))))
    return JsmpegTargetDimensions(width=width, height=height)


def build_jsmpeg_ffmpeg_args(
    *,
    ffmpeg_path: Path,
    width: int,
    height: int,
    fps: float,
    bitrate_kbps: int,
) -> list[str]:
    # MPEG-1 only supports a small set of standard frame rates. Keep the input
    # cadence low in the frame pump, while the encoder receives a legal stream.
    encoder_fps = JSMPEG_MPEG1_FPS
    gop = max(1, int(round(encoder_fps)))
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{int(width)}x{int(height)}",
        "-r",
        _format_ffmpeg_number(encoder_fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "mpeg1video",
        "-b:v",
        f"{max(64, int(bitrate_kbps))}k",
        "-bf",
        "0",
        "-g",
        str(gop),
        "-muxdelay",
        "0.001",
        "-f",
        "mpegts",
        "pipe:1",
    ]


class JsmpegSessionManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        runtime_state: TransmissionRuntimeState,
        logger: logging.Logger | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._runtime_state = runtime_state
        self._logger = logger or LOGGER
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _JsmpegSession] = {}
        self._frames_encoded_total = 0
        self._bytes_sent_total = 0
        self._last_error: str | None = None

    async def get_status(self, settings: StreamingJsmpegSettings) -> JsmpegStatus:
        binary = self.resolve_ffmpeg()
        async with self._lock:
            sessions_by_transmission = self._sessions_by_transmission_locked()
            warnings: list[str] = []
            if not settings.enabled:
                warnings.append("JSMpeg is disabled in settings.")
            if binary.path is None:
                warnings.append(binary.error or "FFmpeg is not available for JSMpeg.")
            if len(self._sessions) >= int(settings.max_total_sessions):
                warnings.append("JSMpeg global session limit is reached.")
            return JsmpegStatus(
                enabled=bool(settings.enabled),
                ffmpeg_path=str(binary.path) if binary.path is not None else None,
                ffmpeg_source=binary.source,
                ffmpeg_error=binary.error,
                running_session_count=len(self._sessions),
                max_total_sessions=int(settings.max_total_sessions),
                max_sessions_per_transmission=int(settings.max_sessions_per_transmission),
                sessions_by_transmission=sessions_by_transmission,
                frames_encoded=int(self._frames_encoded_total),
                bytes_sent=int(self._bytes_sent_total),
                last_error=self._last_error,
                warnings=warnings,
            )

    def resolve_ffmpeg(self) -> ResolvedFFmpegBinary:
        return resolve_ffmpeg_binary(data_dir=self._data_dir)

    async def blocking_errors(
        self,
        *,
        settings: StreamingJsmpegSettings,
        transmission_id: str | None = None,
    ) -> list[str]:
        errors: list[str] = []
        if not settings.enabled:
            errors.append("JSMpeg fallback is disabled in streaming settings.")
        binary = self.resolve_ffmpeg()
        if binary.path is None:
            errors.append(binary.error or "FFmpeg is not available for JSMpeg fallback.")
        async with self._lock:
            if len(self._sessions) >= int(settings.max_total_sessions):
                errors.append("JSMpeg global session limit is reached.")
            if transmission_id:
                sessions_for_transmission = sum(
                    1 for session in self._sessions.values() if session.transmission_id == transmission_id
                )
                if sessions_for_transmission >= int(settings.max_sessions_per_transmission):
                    errors.append("JSMpeg session limit for this transmission is reached.")
        return errors

    async def stream(
        self,
        *,
        websocket: WebSocket,
        settings: StreamingJsmpegSettings,
        stale_policy: StreamingStalePolicySettings,
        transmission: Transmission,
        output: TransmissionOutput,
        prime_demand: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        if not settings.enabled:
            await websocket.close(code=1008)
            return

        binary = self.resolve_ffmpeg()
        if binary.path is None:
            await websocket.close(code=1011)
            return

        dimensions = normalize_jsmpeg_dimensions(
            output_width=output.resolution.width if output.resolution is not None else None,
            output_height=output.resolution.height if output.resolution is not None else None,
            max_width=settings.max_width,
            max_height=settings.max_height,
        )
        session_id = f"{transmission.id}:{output.id}:{int(time.time() * 1000)}:{id(websocket)}"
        session = _JsmpegSession(
            session_id=session_id,
            transmission_id=transmission.id,
            output_id=output.id,
        )

        async with self._lock:
            if len(self._sessions) >= int(settings.max_total_sessions):
                await websocket.close(code=1013)
                return
            sessions_for_transmission = sum(
                1 for item in self._sessions.values() if item.transmission_id == transmission.id
            )
            if sessions_for_transmission >= int(settings.max_sessions_per_transmission):
                await websocket.close(code=1013)
                return
            self._sessions[session_id] = session

        process: asyncio.subprocess.Process | None = None
        try:
            if prime_demand is not None:
                with contextlib.suppress(Exception):
                    await prime_demand()
            args = build_jsmpeg_ffmpeg_args(
                ffmpeg_path=binary.path,
                width=dimensions.width,
                height=dimensions.height,
                fps=settings.fps,
                bitrate_kbps=settings.bitrate_kbps,
            )
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            session.process = process
            await websocket.accept()

            tasks = [
                asyncio.create_task(
                    self._pump_frames(
                        process=process,
                        session=session,
                        settings=settings,
                        stale_policy=stale_policy,
                        transmission=transmission,
                        dimensions=dimensions,
                        prime_demand=prime_demand,
                    )
                ),
                asyncio.create_task(self._pump_stdout(process=process, websocket=websocket, session=session)),
                asyncio.create_task(self._pump_stderr(process=process, session=session)),
                asyncio.create_task(self._watch_websocket(websocket)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()
        except Exception as exc:
            session.last_error = str(exc)
            await self._set_last_error(str(exc))
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
        finally:
            if process is not None:
                await _terminate_process(process)
            await self._remove_session(session)

    async def stop_all(self) -> None:
        processes: list[asyncio.subprocess.Process] = []
        async with self._lock:
            processes = [session.process for session in self._sessions.values() if session.process is not None]
            self._sessions.clear()
        for process in processes:
            await _terminate_process(process)

    async def _pump_frames(
        self,
        *,
        process: asyncio.subprocess.Process,
        session: _JsmpegSession,
        settings: StreamingJsmpegSettings,
        stale_policy: StreamingStalePolicySettings,
        transmission: Transmission,
        dimensions: JsmpegTargetDimensions,
        prime_demand: Callable[[], Awaitable[object]] | None,
    ) -> None:
        if process.stdin is None:
            raise RuntimeError("JSMpeg FFmpeg stdin is unavailable")
        frame_interval_s = 1.0 / JSMPEG_MPEG1_FPS
        source_refresh_interval_s = 1.0 / max(1.0, float(settings.fps))
        heartbeat_interval_s = min(
            max(1.0, float(settings.heartbeat_interval_seconds)),
            max(1.0, float(settings.lease_seconds) * 0.8),
        )
        next_heartbeat_monotonic = 0.0
        next_source_refresh_monotonic = 0.0
        current_frame: numpy.ndarray | None = None
        while True:
            if process.returncode is not None:
                raise RuntimeError(f"JSMpeg FFmpeg exited with code {process.returncode}")
            now = time.monotonic()
            if prime_demand is not None and now >= next_heartbeat_monotonic:
                with contextlib.suppress(Exception):
                    await prime_demand()
                next_heartbeat_monotonic = now + heartbeat_interval_s

            if current_frame is None or now >= next_source_refresh_monotonic:
                current_frame = await self._next_frame(
                    transmission=transmission,
                    stale_policy=stale_policy,
                    dimensions=dimensions,
                )
                next_source_refresh_monotonic = now + source_refresh_interval_s

            process.stdin.write(current_frame.tobytes())
            await process.stdin.drain()
            session.frames_encoded += 1
            async with self._lock:
                self._frames_encoded_total += 1
            await asyncio.sleep(frame_interval_s)

    async def _next_frame(
        self,
        *,
        transmission: Transmission,
        stale_policy: StreamingStalePolicySettings,
        dimensions: JsmpegTargetDimensions,
    ) -> numpy.ndarray:
        selected = await self._runtime_state.get_selected_writer_frame(
            transmission.id,
            stale_after_s=stale_policy.stale_after_seconds,
            placeholder_after_s=stale_policy.placeholder_after_seconds,
        )
        if selected.frame is not None and not selected.stale and not selected.placeholder_active:
            frame = selected.frame
        else:
            frame = get_placeholder_frame(dimensions.width, dimensions.height, mode=transmission.placeholder)
        resized = resize_frame_contain(frame, dimensions.width, dimensions.height)
        return numpy.ascontiguousarray(resized, dtype=numpy.uint8)

    async def _pump_stdout(
        self,
        *,
        process: asyncio.subprocess.Process,
        websocket: WebSocket,
        session: _JsmpegSession,
    ) -> None:
        if process.stdout is None:
            raise RuntimeError("JSMpeg FFmpeg stdout is unavailable")
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                if process.returncode is None:
                    continue
                return
            try:
                await websocket.send_bytes(chunk)
            except (RuntimeError, WebSocketDisconnect):
                return
            session.bytes_sent += len(chunk)
            async with self._lock:
                self._bytes_sent_total += len(chunk)

    async def _pump_stderr(
        self,
        *,
        process: asyncio.subprocess.Process,
        session: _JsmpegSession,
    ) -> None:
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = _sanitize_log_line(line.decode("utf-8", errors="replace"))
            if text:
                session.stderr_tail.append(text)
                if _ffmpeg_stderr_line_is_error(text):
                    session.last_error = text
                    await self._set_last_error(text)

    async def _watch_websocket(self, websocket: WebSocket) -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return

    async def _remove_session(self, session: _JsmpegSession) -> None:
        async with self._lock:
            self._sessions.pop(session.session_id, None)

    async def _set_last_error(self, message: str) -> None:
        text = _sanitize_log_line(message)
        if not text:
            return
        async with self._lock:
            self._last_error = text

    def _sessions_by_transmission_locked(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for session in self._sessions.values():
            counts[session.transmission_id] = counts.get(session.transmission_id, 0) + 1
        return counts


def _even(value: int) -> int:
    normalized = int(value)
    if normalized % 2:
        normalized -= 1
    return max(2, normalized)


def _format_ffmpeg_number(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _sanitize_log_line(value: str) -> str:
    return str(value or "").strip().replace("\x00", "")[:1000]


def _ffmpeg_stderr_line_is_error(value: str) -> bool:
    normalized = value.lower()
    return any(marker in normalized for marker in ("error", "invalid", "failed", "exited", "broken"))


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
        with contextlib.suppress(Exception):
            await process.stdin.wait_closed()
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=2.0)
