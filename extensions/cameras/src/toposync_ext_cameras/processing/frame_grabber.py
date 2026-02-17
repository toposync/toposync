from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from typing import Any
from typing import Protocol


try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]

import numpy as np

logger = logging.getLogger(__name__)


def _read_env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except Exception:
        return fallback


def _clamp_int(v: int, *, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, int(v)))


def _rtsp_stream2_fallback(rtsp_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(rtsp_url)
    except Exception:
        return None

    path = parsed.path or ""
    trailing = "/" if path.endswith("/") else ""
    stripped = path.rstrip("/")
    if not stripped.endswith("/stream1"):
        return None

    base = stripped[: -len("/stream1")]
    new_path = f"{base}/stream2{trailing}"
    return urllib.parse.urlunsplit(parsed._replace(path=new_path))


@dataclasses.dataclass(frozen=True, slots=True)
class CaptureBackendMetrics:
    backend: str
    target_fps: float
    opened: bool
    frames_captured: int
    decode_failures: int
    restarts: int
    last_frame_ts: float
    fps: float
    last_error: str | None


class CaptureBackend(Protocol):
    backend_name: str

    @property
    def target_fps(self) -> float: ...

    def set_target_fps(self, fps: float) -> None: ...

    def start(self) -> "CaptureBackend": ...

    def is_opened(self) -> bool: ...

    def get_latest(self) -> tuple[Any | None, float]: ...

    def metrics_snapshot(self) -> CaptureBackendMetrics: ...

    def stop(self) -> None: ...


class _LatestFrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Any | None = None
        self._ts: float = 0.0

    def set(self, frame: Any, ts: float) -> None:
        with self._lock:
            self._frame = frame
            self._ts = float(ts)

    def clear(self) -> None:
        with self._lock:
            self._frame = None
            self._ts = 0.0

    def get(self) -> tuple[Any | None, float]:
        with self._lock:
            return self._frame, self._ts


class OpenCvFrameGrabber:
    backend_name = "opencv"

    def __init__(
        self,
        rtsp_url: str,
        *,
        target_fps: float = 15.0,
        backend: str | None = None,  # noqa: ARG002 - kept for API parity
        open_timeout_ms: int | None = None,
        read_timeout_ms: int | None = None,
    ) -> None:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is required for camera processing. Install with: "
                "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
            )

        self.original_rtsp_url = rtsp_url
        self.rtsp_url = rtsp_url

        self._target_fps: float = min(60.0, max(1.0, float(target_fps or 15.0)))
        self._min_interval: float = 1.0 / self._target_fps
        self._last_retrieve_ts: float = 0.0

        default_open_timeout = _read_env_int("TOPOSYNC_RTSP_OPEN_TIMEOUT_MS", 8000)
        default_read_timeout = _read_env_int("TOPOSYNC_RTSP_READ_TIMEOUT_MS", 8000)
        self._open_timeout_ms = _clamp_int(
            int(open_timeout_ms) if open_timeout_ms is not None else default_open_timeout,
            min_value=1000,
            max_value=120_000,
        )
        self._read_timeout_ms = _clamp_int(
            int(read_timeout_ms) if read_timeout_ms is not None else default_read_timeout,
            min_value=1000,
            max_value=120_000,
        )

        self.cap: Any | None = None
        self._fail_count: int = 0
        self._last_open_ts: float = time.time()
        self._reopen_cooldown_s: float = 2.0
        self._no_frame_reopen_after_s: float = 10.0
        self._restarts: int = 0
        self._decode_failures: int = 0
        self._frames_captured: int = 0
        self._last_error: str | None = None
        self._frame_buffer = _LatestFrameBuffer()
        self._fps_samples = deque[float](maxlen=120)

        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)

    def _open_capture(self, url: str) -> Any:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is required for camera processing. Install with: "
                "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
            )

        cap = cv2.VideoCapture()

        open_timeout_prop = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
        read_timeout_prop = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
        try:
            if open_timeout_prop is not None:
                cap.set(open_timeout_prop, float(self._open_timeout_ms))
            if read_timeout_prop is not None:
                cap.set(read_timeout_prop, float(self._read_timeout_ms))
        except Exception:
            pass

        try:
            cap.open(url, cv2.CAP_FFMPEG)
        except Exception:
            cap.open(url)

        # Post-open tuning (safe to ignore if unsupported).
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        try:
            cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        except Exception:
            pass

        return cap

    @property
    def target_fps(self) -> float:
        return self._target_fps

    def set_target_fps(self, fps: float) -> None:
        new_fps = 15.0
        try:
            new_fps = float(fps)
        except Exception:
            new_fps = 15.0

        self._target_fps = min(60.0, max(1.0, new_fps))
        self._min_interval = 1.0 / self._target_fps
        try:
            if cv2 is not None:
                if self.cap is not None:
                    self.cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        except Exception:
            pass

    def start(self) -> "OpenCvFrameGrabber":
        if self.cap is None:
            self.cap = self._open_capture(self.rtsp_url)
            if not self.is_opened():
                fallback = _rtsp_stream2_fallback(self.rtsp_url)
                if fallback:
                    cap2 = self._open_capture(fallback)
                    if cap2.isOpened():
                        try:
                            self.cap.release()
                        except Exception:
                            pass
                        self.cap = cap2
                        self.rtsp_url = fallback
                        logger.warning(
                            "RTSP stream1 failed to open; falling back to stream2 (substream). "
                            "Update the camera rtsp_url to /stream2 if you want this permanently."
                        )
                    else:
                        try:
                            cap2.release()
                        except Exception:
                            pass

            try:
                if cv2 is not None and self.cap is not None:
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            try:
                if cv2 is not None and self.cap is not None:
                    self.cap.set(cv2.CAP_PROP_FPS, self._target_fps)
            except Exception:
                pass

        if not self.thread.is_alive():
            self.thread.start()
        return self

    def is_opened(self) -> bool:
        try:
            if self.cap is None:
                return False
            return bool(self.cap.isOpened())
        except Exception:
            return False

    def _reopen_capture(self) -> None:
        try:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
            if cv2 is None:
                self.cap = None
                return
            self.cap = self._open_capture(self.rtsp_url)
            self._frame_buffer.clear()
            self._last_retrieve_ts = 0.0
            self._fail_count = 0
            self._last_open_ts = time.time()
            self._restarts += 1
        except Exception:
            self._last_open_ts = time.time()
            self._restarts += 1

    def _reader_loop(self) -> None:
        while not self.stopped.is_set():
            now = time.time()
            opened = self.is_opened()
            if not opened:
                if (now - self._last_open_ts) >= self._reopen_cooldown_s:
                    self._reopen_capture()
                time.sleep(0.1)
                continue

            if self._last_retrieve_ts and (now - self._last_retrieve_ts) < self._min_interval:
                try:
                    if self.cap is not None:
                        _ = self.cap.grab()
                except Exception:
                    self._fail_count += 1
                time.sleep(0.003)
                continue

            try:
                ok_grab = bool(self.cap.grab() if self.cap is not None else False)
            except Exception:
                ok_grab = False

            if not ok_grab:
                self._fail_count += 1
                _, last_frame_ts = self._frame_buffer.get()
                if self._fail_count >= 50 or (now - (last_frame_ts or 0.0)) > self._no_frame_reopen_after_s:
                    if (now - self._last_open_ts) >= self._reopen_cooldown_s:
                        self._reopen_capture()
                    time.sleep(0.1)
                else:
                    time.sleep(0.02)
                continue

            ok = False
            frame = None
            try:
                if self.cap is not None:
                    ok, frame = self.cap.retrieve()
                else:
                    ok, frame = False, None
            except Exception:
                ok = False
                frame = None

            if not ok or frame is None:
                try:
                    ok2, frame2 = self.cap.read() if self.cap is not None else (False, None)
                except Exception:
                    ok2, frame2 = False, None
                if not ok2 or frame2 is None:
                    self._fail_count += 1
                    _, last_frame_ts = self._frame_buffer.get()
                    if self._fail_count >= 50 or (now - (last_frame_ts or 0.0)) > self._no_frame_reopen_after_s:
                        if (now - self._last_open_ts) >= self._reopen_cooldown_s:
                            self._reopen_capture()
                        time.sleep(0.1)
                    else:
                        time.sleep(0.02)
                    continue
                frame = frame2

            self._last_retrieve_ts = now
            self._fail_count = 0
            self._frames_captured += 1
            self._fps_samples.append(time.monotonic())

            self._frame_buffer.set(frame, time.time())

    def get_latest(self) -> tuple[Any | None, float]:
        return self._frame_buffer.get()

    def _effective_fps(self) -> float:
        if len(self._fps_samples) < 2:
            return 0.0
        elapsed = self._fps_samples[-1] - self._fps_samples[0]
        if elapsed <= 0:
            return 0.0
        return max(0.0, float(len(self._fps_samples) - 1) / elapsed)

    def metrics_snapshot(self) -> CaptureBackendMetrics:
        frame, frame_ts = self._frame_buffer.get()
        _ = frame
        return CaptureBackendMetrics(
            backend=self.backend_name,
            target_fps=float(self._target_fps),
            opened=self.is_opened(),
            frames_captured=int(self._frames_captured),
            decode_failures=int(self._decode_failures),
            restarts=int(self._restarts),
            last_frame_ts=float(frame_ts),
            fps=float(self._effective_fps()),
            last_error=self._last_error,
        )

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self._frame_buffer.clear()


class FfmpegFrameGrabber:
    backend_name = "ffmpeg"

    def __init__(
        self,
        rtsp_url: str,
        *,
        target_fps: float = 15.0,
        backend: str | None = None,  # noqa: ARG002 - kept for API parity
        open_timeout_ms: int | None = None,
        read_timeout_ms: int | None = None,
    ) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            raise RuntimeError("ffmpeg is required for the ffmpeg capture backend, but was not found in PATH.")

        self._ffmpeg_path = ffmpeg_path
        self.original_rtsp_url = rtsp_url
        self.rtsp_url = rtsp_url

        self._target_fps: float = min(60.0, max(1.0, float(target_fps or 15.0)))
        default_open_timeout = _read_env_int("TOPOSYNC_RTSP_OPEN_TIMEOUT_MS", 8000)
        default_read_timeout = _read_env_int("TOPOSYNC_RTSP_READ_TIMEOUT_MS", 8000)
        self._open_timeout_ms = _clamp_int(
            int(open_timeout_ms) if open_timeout_ms is not None else default_open_timeout,
            min_value=1000,
            max_value=120_000,
        )
        self._read_timeout_ms = _clamp_int(
            int(read_timeout_ms) if read_timeout_ms is not None else default_read_timeout,
            min_value=1000,
            max_value=120_000,
        )

        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._frames_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._stderr_stop = threading.Event()
        self._frames_stop = threading.Event()
        self._frame_buffer = _LatestFrameBuffer()

        self._restarts: int = 0
        self._decode_failures: int = 0
        self._frames_captured: int = 0
        self._last_error: str | None = None
        self._fps_samples = deque[float](maxlen=120)

    @property
    def target_fps(self) -> float:
        return self._target_fps

    def set_target_fps(self, fps: float) -> None:
        new_fps = 15.0
        try:
            new_fps = float(fps)
        except Exception:
            new_fps = 15.0
        self._target_fps = min(60.0, max(1.0, new_fps))

    def start(self) -> "CaptureBackend":
        if self._proc is None:
            self._start_process()
        if not self._frames_thread.is_alive():
            self._frames_thread.start()
        return self

    def is_opened(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def get_latest(self) -> tuple[Any | None, float]:
        return self._frame_buffer.get()

    def _effective_fps(self) -> float:
        if len(self._fps_samples) < 2:
            return 0.0
        elapsed = self._fps_samples[-1] - self._fps_samples[0]
        if elapsed <= 0:
            return 0.0
        return max(0.0, float(len(self._fps_samples) - 1) / elapsed)

    def metrics_snapshot(self) -> CaptureBackendMetrics:
        frame, frame_ts = self._frame_buffer.get()
        _ = frame
        return CaptureBackendMetrics(
            backend=self.backend_name,
            target_fps=float(self._target_fps),
            opened=self.is_opened(),
            frames_captured=int(self._frames_captured),
            decode_failures=int(self._decode_failures),
            restarts=int(self._restarts),
            last_frame_ts=float(frame_ts),
            fps=float(self._effective_fps()),
            last_error=self._last_error,
        )

    def _start_process(self) -> None:
        timeout_us = int(max(0, self._read_timeout_ms) * 1000)
        args: list[str] = [self._ffmpeg_path, "-hide_banner", "-loglevel", "error"]

        if str(self.rtsp_url).startswith("rtsp://"):
            args += [
                "-timeout",
                str(timeout_us),
                "-rtsp_transport",
                "tcp",
                "-allowed_media_types",
                "video",
            ]

        args += [
            "-i",
            self.rtsp_url,
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"fps={self._target_fps}",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._proc = proc
        self._stderr_stop.clear()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while not self._stderr_stop.is_set():
                line = proc.stderr.readline()
                if not line:
                    break
                message = line.decode("utf-8", errors="ignore").strip()
                if message:
                    self._last_error = message
        except Exception:
            return

    def _restart_process(self) -> None:
        self._restarts += 1
        self._stop_process()
        self._frame_buffer.clear()
        self._start_process()

    def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        self._stderr_stop.set()
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.8)
            except Exception:
                pass

        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:
            pass

    def _read_chunk(self, size: int) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return proc.stdout.read(size) or b""
        except Exception:
            return b""

    def _reader_loop(self) -> None:
        buffer = bytearray()
        while not self._frames_stop.is_set():
            if not self.is_opened():
                time.sleep(0.15)
                try:
                    self._restart_process()
                except Exception:
                    time.sleep(0.8)
                continue

            chunk = self._read_chunk(8192)
            if not chunk:
                time.sleep(0.05)
                continue
            buffer.extend(chunk)

            # MJPEG framing: find JPEG SOI/EOI markers.
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        buffer[:] = buffer[-2:]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break

                jpg = bytes(buffer[start : end + 2])
                del buffer[: end + 2]

                try:
                    arr = np.frombuffer(jpg, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR) if cv2 is not None else None
                except Exception:
                    frame = None

                if frame is None:
                    self._decode_failures += 1
                    continue

                self._frames_captured += 1
                self._fps_samples.append(time.monotonic())
                self._frame_buffer.set(frame, time.time())

        self._stop_process()

    def stop(self) -> None:
        self._frames_stop.set()
        if self._frames_thread.is_alive():
            self._frames_thread.join(timeout=1.0)
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.2)
        self._stop_process()
        self._frame_buffer.clear()


class FrameGrabber:
    def __init__(
        self,
        rtsp_url: str,
        *,
        target_fps: float = 15.0,
        backend: str = "opencv",
        open_timeout_ms: int | None = None,
        read_timeout_ms: int | None = None,
    ) -> None:
        backend_key = str(backend or "").strip().lower() or "opencv"
        if backend_key not in {"auto", "opencv", "ffmpeg"}:
            backend_key = "opencv"

        preferred: list[str]
        if backend_key == "ffmpeg":
            preferred = ["ffmpeg", "opencv"]
        elif backend_key == "opencv":
            preferred = ["opencv", "ffmpeg"]
        else:
            preferred = ["opencv", "ffmpeg"]

        self._backend: CaptureBackend | None = None
        last_error: Exception | None = None
        for candidate in preferred:
            if candidate == "opencv":
                if cv2 is None:
                    continue
                try:
                    self._backend = OpenCvFrameGrabber(
                        rtsp_url,
                        target_fps=target_fps,
                        open_timeout_ms=open_timeout_ms,
                        read_timeout_ms=read_timeout_ms,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
            if candidate == "ffmpeg":
                try:
                    self._backend = FfmpegFrameGrabber(
                        rtsp_url,
                        target_fps=target_fps,
                        open_timeout_ms=open_timeout_ms,
                        read_timeout_ms=read_timeout_ms,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue

        if self._backend is None:
            if last_error is not None:
                raise RuntimeError(f"Failed to initialize any capture backend: {last_error}") from last_error
            raise RuntimeError("No capture backend available (requires OpenCV and/or ffmpeg).")

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "backend_name", "") or "")

    @property
    def target_fps(self) -> float:
        if self._backend is None:
            return 0.0
        return float(self._backend.target_fps)

    def set_target_fps(self, fps: float) -> None:
        if self._backend is None:
            return
        self._backend.set_target_fps(fps)

    def start(self) -> "FrameGrabber":
        if self._backend is not None:
            self._backend.start()
        return self

    def is_opened(self) -> bool:
        if self._backend is None:
            return False
        return bool(self._backend.is_opened())

    def get_latest(self) -> tuple[Any | None, float]:
        if self._backend is None:
            return None, 0.0
        return self._backend.get_latest()

    def metrics_snapshot(self) -> CaptureBackendMetrics:
        if self._backend is None:
            return CaptureBackendMetrics(
                backend="none",
                target_fps=0.0,
                opened=False,
                frames_captured=0,
                decode_failures=0,
                restarts=0,
                last_frame_ts=0.0,
                fps=0.0,
                last_error="Backend not initialized",
            )
        return self._backend.metrics_snapshot()

    def stop(self) -> None:
        if self._backend is not None:
            self._backend.stop()
