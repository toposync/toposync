from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api.models import StreamingMseSidecarSettings
from . import GO2RTC_VERSION
from .go2rtc_binary import extract_go2rtc_binary
from .go2rtc_config import Go2RtcResolvedConfig, render_go2rtc_config
from .platform import detect_go2rtc_platform


@dataclass(frozen=True, slots=True)
class Go2RtcSidecarStatus:
    running: bool
    pid: int | None
    uptime_seconds: float | None
    started_at_unix: float | None
    bind_host: str
    api_port: int
    last_error: str | None
    go2rtc_version: str
    platform: str | None
    binary_path: str | None
    config_path: str | None
    log_path: str | None
    warnings: tuple[str, ...]
    restart_count: int
    stream_count: int


@dataclass(frozen=True, slots=True)
class _RuntimeConfig:
    bind_host: str
    api_port: int
    warnings: tuple[str, ...]
    platform_key: str
    binary_path: Path
    config_text: str
    config_hash: str
    stream_count: int


class Go2RtcSidecarManager:
    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._started_at_unix: float | None = None
        self._last_error: str | None = None
        self._bind_host = "127.0.0.1"
        self._api_port = 18764
        self._warnings: tuple[str, ...] = ()
        self._config_hash: str | None = None
        self._platform_key: str | None = None
        self._binary_path: Path | None = None
        self._config_path: Path | None = None
        self._log_path: Path | None = None
        self._log_file: Any = None
        self._go2rtc_version = GO2RTC_VERSION
        self._restart_count = 0
        self._stream_count = 0

    async def ensure_running(
        self,
        sidecar_settings: StreamingMseSidecarSettings,
        *,
        streams: dict[str, str] | None = None,
    ) -> Go2RtcSidecarStatus:
        async with self._lock:
            self._refresh_process_state_locked()
            if not sidecar_settings.enabled:
                await self._stop_locked(clear_error=False)
                return self._status_locked()

            config = self._resolve_runtime_config(sidecar_settings, streams=streams or {})
            if self._is_running_locked() and self._config_hash == config.config_hash:
                return self._status_locked()
            if self._is_running_locked():
                await self._stop_locked(clear_error=False)
            await self._start_locked(config=config)
            return self._status_locked()

    async def restart(
        self,
        sidecar_settings: StreamingMseSidecarSettings,
        *,
        streams: dict[str, str] | None = None,
    ) -> Go2RtcSidecarStatus:
        async with self._lock:
            self._refresh_process_state_locked()
            await self._stop_locked(clear_error=False)
            if not sidecar_settings.enabled:
                return self._status_locked()
            config = self._resolve_runtime_config(sidecar_settings, streams=streams or {})
            await self._start_locked(config=config)
            return self._status_locked()

    async def stop(self) -> Go2RtcSidecarStatus:
        async with self._lock:
            await self._stop_locked(clear_error=False)
            return self._status_locked()

    async def get_status(self) -> Go2RtcSidecarStatus:
        async with self._lock:
            self._refresh_process_state_locked()
            return self._status_locked()

    def _resolve_runtime_config(
        self,
        sidecar_settings: StreamingMseSidecarSettings,
        *,
        streams: dict[str, str],
    ) -> _RuntimeConfig:
        bind_host = "127.0.0.1"
        preferred_port = int(sidecar_settings.api_port)
        if self._is_running_locked() and self._bind_host == bind_host and self._api_port == preferred_port:
            api_port, changed = preferred_port, False
        else:
            api_port, changed = _pick_port(bind_host=bind_host, preferred=preferred_port)
        warnings: list[str] = []
        if changed:
            warnings.append(f"MSE go2rtc API port {preferred_port} unavailable; using {api_port}.")
        version = str(getattr(sidecar_settings, "go2rtc_version", GO2RTC_VERSION) or GO2RTC_VERSION).strip() or GO2RTC_VERSION
        self._go2rtc_version = version
        platform = detect_go2rtc_platform()
        binary = extract_go2rtc_binary(data_dir=self._data_dir, platform=platform, version=version)
        config_text = render_go2rtc_config(
            Go2RtcResolvedConfig(api_bind_host=bind_host, api_port=api_port, streams=dict(streams or {}))
        )
        config_hash = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
        return _RuntimeConfig(
            bind_host=bind_host,
            api_port=api_port,
            warnings=tuple(warnings),
            platform_key=platform.key,
            binary_path=binary,
            config_text=config_text,
            config_hash=config_hash,
            stream_count=len(streams or {}),
        )

    async def _start_locked(self, *, config: _RuntimeConfig) -> None:
        runtime_dir = self._data_dir / "runtime" / "streaming" / "go2rtc"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        config_path = runtime_dir / "go2rtc.yaml"
        log_path = runtime_dir / "go2rtc.log"
        config_path.write_text(config.config_text, encoding="utf-8")
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = log_path.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(
                str(config.binary_path),
                "-c",
                str(config_path),
                cwd=str(runtime_dir),
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise

        self._process = process
        self._started_at_unix = time.time()
        self._last_error = None
        self._bind_host = config.bind_host
        self._api_port = config.api_port
        self._warnings = config.warnings
        self._config_hash = config.config_hash
        self._platform_key = config.platform_key
        self._binary_path = config.binary_path
        self._config_path = config_path
        self._log_path = log_path
        self._stream_count = config.stream_count
        self._restart_count += 1

    async def _stop_locked(self, *, clear_error: bool) -> None:
        process = self._process
        self._process = None
        self._started_at_unix = None
        self._config_hash = None
        self._stream_count = 0
        if clear_error:
            self._last_error = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except Exception:
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=3.0)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _refresh_process_state_locked(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            return
        self._last_error = f"go2rtc exited with code {process.returncode}"
        self._process = None
        self._started_at_unix = None
        self._config_hash = None
        self._stream_count = 0

    def _is_running_locked(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _status_locked(self) -> Go2RtcSidecarStatus:
        running = self._is_running_locked()
        now = time.time()
        return Go2RtcSidecarStatus(
            running=running,
            pid=int(self._process.pid) if running and self._process is not None else None,
            uptime_seconds=(now - self._started_at_unix) if running and self._started_at_unix else None,
            started_at_unix=self._started_at_unix if running else None,
            bind_host=self._bind_host,
            api_port=self._api_port,
            last_error=self._last_error,
            go2rtc_version=self._go2rtc_version,
            platform=self._platform_key,
            binary_path=str(self._binary_path) if self._binary_path else None,
            config_path=str(self._config_path) if self._config_path else None,
            log_path=str(self._log_path) if self._log_path else None,
            warnings=self._warnings,
            restart_count=self._restart_count,
            stream_count=self._stream_count,
        )


def _pick_port(*, bind_host: str, preferred: int) -> tuple[int, bool]:
    normalized = max(1, min(65535, int(preferred)))
    if _can_bind(bind_host, normalized):
        return normalized, False
    for candidate in range(max(1024, normalized + 1), min(65535, normalized + 300)):
        if _can_bind(bind_host, candidate):
            return candidate, True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1]), True


def _can_bind(bind_host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, int(port)))
        except OSError:
            return False
    return True
