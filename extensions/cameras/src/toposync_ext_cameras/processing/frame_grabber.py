from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from typing import Any


try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]

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


class FrameGrabber:
    def __init__(
        self,
        rtsp_url: str,
        *,
        target_fps: float = 15.0,
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

        self.cap = self._open_capture(rtsp_url)
        if not self.cap.isOpened():
            fallback = _rtsp_stream2_fallback(rtsp_url)
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
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self._fail_count: int = 0
        self._last_open_ts: float = time.time()
        self._reopen_cooldown_s: float = 2.0
        self._no_frame_reopen_after_s: float = 10.0

        try:
            self.cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        except Exception:
            pass

        self.lock = threading.Lock()
        self.frame: Any | None = None
        self.last_frame_ts: float = 0.0
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
                self.cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        except Exception:
            pass

    def start(self) -> "FrameGrabber":
        if not self.thread.is_alive():
            self.thread.start()
        return self

    def is_opened(self) -> bool:
        try:
            return bool(self.cap.isOpened())
        except Exception:
            return False

    def _reopen_capture(self) -> None:
        try:
            with self.lock:
                try:
                    self.cap.release()
                except Exception:
                    pass
                if cv2 is None:
                    return
                self.cap = self._open_capture(self.rtsp_url)
                self.frame = None
                self.last_frame_ts = 0.0
            self._last_retrieve_ts = 0.0
            self._fail_count = 0
            self._last_open_ts = time.time()
        except Exception:
            self._last_open_ts = time.time()

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
                    _ = self.cap.grab()
                except Exception:
                    self._fail_count += 1
                time.sleep(0.003)
                continue

            try:
                ok_grab = bool(self.cap.grab())
            except Exception:
                ok_grab = False

            if not ok_grab:
                self._fail_count += 1
                if self._fail_count >= 50 or (now - (self.last_frame_ts or 0.0)) > self._no_frame_reopen_after_s:
                    if (now - self._last_open_ts) >= self._reopen_cooldown_s:
                        self._reopen_capture()
                    time.sleep(0.1)
                else:
                    time.sleep(0.02)
                continue

            ok = False
            frame = None
            try:
                ok, frame = self.cap.retrieve()
            except Exception:
                ok = False
                frame = None

            if not ok or frame is None:
                try:
                    ok2, frame2 = self.cap.read()
                except Exception:
                    ok2, frame2 = False, None
                if not ok2 or frame2 is None:
                    self._fail_count += 1
                    if self._fail_count >= 50 or (now - (self.last_frame_ts or 0.0)) > self._no_frame_reopen_after_s:
                        if (now - self._last_open_ts) >= self._reopen_cooldown_s:
                            self._reopen_capture()
                        time.sleep(0.1)
                    else:
                        time.sleep(0.02)
                    continue
                frame = frame2

            self._last_retrieve_ts = now
            self._fail_count = 0

            with self.lock:
                self.frame = frame
                self.last_frame_ts = time.time()

    def get_latest(self) -> tuple[Any | None, float]:
        with self.lock:
            return self.frame, self.last_frame_ts

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)
        try:
            self.cap.release()
        except Exception:
            pass
