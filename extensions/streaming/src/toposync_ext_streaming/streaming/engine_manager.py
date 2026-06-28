from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import os
import socket
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..api.models import StreamingEngineSettings
from . import MEDIAMTX_VERSION
from .mediamtx_binary import extract_mediamtx_binary
from .mediamtx_config import MediaMTXPathAuth, MediaMTXResolvedPorts, render_mediamtx_config
from .mediamtx_processes import kill_mediamtx_processes_for_config_path
from .platform import detect_mediamtx_platform

PortResolutionPolicy = Literal["stable", "flexible"]

PORT_RELEASE_TIMEOUT_SECONDS = 2.0
PORT_RELEASE_POLL_SECONDS = 0.1


def _split_env_list(name: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in str(os.getenv(name) or "").replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _merge_string_lists(*values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def _env_address(name: str, *, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip()


def _hls_public_mode() -> str:
    raw = str(os.getenv("TOPOSYNC_STREAMING_HLS_PUBLIC_MODE") or "").strip().lower()
    return "proxy" if raw == "proxy" else "direct"


def _hls_should_bind_loopback(engine_settings: StreamingEngineSettings) -> bool:
    media_auth = getattr(getattr(engine_settings, "media_auth", None), "mode", "signed_proxy")
    return (
        str(media_auth or "signed_proxy") == "signed_proxy"
        or _hls_public_mode() == "proxy"
    )


@dataclass(frozen=True, slots=True)
class MediaMtxPorts:
    rtsp: int
    hls: int
    webrtc: int
    api: int
    # MediaMTX uses these ports when RTSP over UDP is enabled.
    # They must be consecutive (RTP/RTCP).
    rtp: int
    rtcp: int
    metrics: int = 9998
    webrtc_udp: int = 18762


@dataclass(frozen=True, slots=True)
class MediaMtxEngineStatus:
    running: bool
    pid: int | None
    uptime_seconds: float | None
    started_at_unix: float | None
    bind_host: str
    ports: MediaMtxPorts
    last_error: str | None
    mediamtx_version: str
    platform: str | None
    binary_path: str | None
    config_path: str | None
    log_path: str | None
    test_path: str
    warnings: tuple[str, ...]
    restart_count: int
    metrics_enabled: bool = True
    port_policy: str = "flexible"
    port_resolution: str = "preferred"
    port_blocking_errors: tuple[str, ...] = ()


class MediaMtxEngineManager:
    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._lock = asyncio.Lock()

        self._process: asyncio.subprocess.Process | None = None
        self._started_at_unix: float | None = None
        self._last_error: str | None = None

        self._bind_host = "127.0.0.1"
        # Defaults are fallback values; actual ports are resolved on start.
        self._ports = MediaMtxPorts(
            rtsp=8554,
            hls=8888,
            webrtc=8889,
            webrtc_udp=18762,
            api=9997,
            metrics=9998,
            rtp=50000,
            rtcp=50001,
        )
        self._metrics_enabled = True
        self._warnings: tuple[str, ...] = ()
        self._port_policy = "flexible"
        self._port_resolution = "preferred"
        self._port_blocking_errors: tuple[str, ...] = ()

        self._config_hash: str | None = None
        self._platform_key: str | None = None
        self._binary_path: Path | None = None
        self._config_path: Path | None = None
        self._log_path: Path | None = None
        self._log_file = None

        self._test_path = "test"
        self._engine_paths: tuple[str, ...] = (self._test_path,)
        self._path_configs_by_path: dict[str, dict[str, object]] = {}
        self._mediamtx_version: str = MEDIAMTX_VERSION

        self._publish_credentials_by_path: dict[str, tuple[str, str]] = {}
        self._read_auth_by_path: dict[str, tuple[str, str]] = {}
        self._restart_count = 0
        self._restart_attempts_monotonic: deque[float] = deque(maxlen=128)
        self._next_start_attempt_monotonic = 0.0
        self._start_backoff_seconds = 0.0
        self._max_restarts_per_minute = 8
        self._publish_secret: bytes | None = None

    async def ensure_running(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        engine_paths: list[str] | None = None,
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] | None = None,
        path_configs: dict[str, dict[str, object]] | None = None,
    ) -> MediaMtxEngineStatus:
        async with self._lock:
            now_monotonic = time.monotonic()
            self._refresh_process_state_locked(now_monotonic)
            self._update_engine_paths_locked(engine_paths)
            self._update_path_configs_locked(path_configs)
            if not engine_settings.enabled:
                await self._stop_locked(clear_error=False)
                self._reset_restart_backoff_locked()
                return self._status_locked()

            bind_host = _bind_host_for_engine_settings(engine_settings)
            running = self._is_running_locked()
            stable_ports = self._ports if running and bind_host == self._bind_host else None
            port_policy = self._port_policy_for_start_locked(
                engine_settings,
                running=running,
            )
            if running and stable_ports is None:
                previous_ports = self._ports
                previous_bind_host = self._bind_host
                await self._stop_locked(clear_error=False)
                await self._wait_for_ports_available_locked(
                    bind_host=previous_bind_host,
                    ports=previous_ports,
                    timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                )

            config = await self._resolve_runtime_config_with_reclaim_locked(
                engine_settings,
                path_auth=path_auth,
                port_policy=port_policy,
                stable_ports=stable_ports,
            )
            if self._is_running_locked() and self._config_hash == config.config_hash:
                return self._status_locked()

            if self._is_running_locked():
                previous_ports = self._ports
                previous_bind_host = self._bind_host
                await self._stop_locked(clear_error=False)
                if stable_ports is not None:
                    await self._ensure_ports_available_after_stop_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                    )
                else:
                    await self._wait_for_ports_available_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                        timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                    )

            if not self._can_attempt_restart_locked(now_monotonic):
                return self._status_locked()

            try:
                await self._start_locked(config=config)
            except Exception as exc:
                self._record_restart_failure_locked(now_monotonic, reason=str(exc))
                raise
            self._reset_restart_backoff_locked()
            return self._status_locked()

    async def apply_settings(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        previous_engine_settings: StreamingEngineSettings | None = None,
        engine_paths: list[str] | None = None,
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] | None = None,
        path_configs: dict[str, dict[str, object]] | None = None,
    ) -> MediaMtxEngineStatus:
        _ = previous_engine_settings
        async with self._lock:
            now_monotonic = time.monotonic()
            self._refresh_process_state_locked(now_monotonic)
            self._update_engine_paths_locked(engine_paths)
            self._update_path_configs_locked(path_configs)
            if not engine_settings.enabled:
                await self._stop_locked(clear_error=False)
                self._reset_restart_backoff_locked()
                return self._status_locked()

            bind_host = _bind_host_for_engine_settings(engine_settings)
            running = self._is_running_locked()
            stable_ports = self._ports if running and bind_host == self._bind_host else None
            port_policy = "stable" if running else self._port_policy_for_start_locked(
                engine_settings,
                running=running,
            )
            if running and stable_ports is None:
                previous_ports = self._ports
                previous_bind_host = self._bind_host
                await self._stop_locked(clear_error=False)
                await self._wait_for_ports_available_locked(
                    bind_host=previous_bind_host,
                    ports=previous_ports,
                    timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                )

            config = await self._resolve_runtime_config_with_reclaim_locked(
                engine_settings,
                path_auth=path_auth,
                port_policy=port_policy,
                stable_ports=stable_ports,
            )
            if self._is_running_locked() and self._config_hash == config.config_hash:
                return self._status_locked()

            if self._is_running_locked():
                previous_ports = self._ports
                previous_bind_host = self._bind_host
                await self._stop_locked(clear_error=False)
                if stable_ports is not None:
                    await self._ensure_ports_available_after_stop_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                    )
                else:
                    await self._wait_for_ports_available_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                        timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                    )

            if not self._can_attempt_restart_locked(now_monotonic):
                return self._status_locked()

            try:
                await self._start_locked(config=config)
            except Exception as exc:
                self._record_restart_failure_locked(now_monotonic, reason=str(exc))
                raise
            self._reset_restart_backoff_locked()
            return self._status_locked()

    async def restart(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        engine_paths: list[str] | None = None,
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] | None = None,
        path_configs: dict[str, dict[str, object]] | None = None,
    ) -> MediaMtxEngineStatus:
        async with self._lock:
            now_monotonic = time.monotonic()
            self._refresh_process_state_locked(now_monotonic)
            self._update_engine_paths_locked(engine_paths)
            self._update_path_configs_locked(path_configs)
            if not engine_settings.enabled:
                await self._stop_locked(clear_error=False)
                self._reset_restart_backoff_locked()
                return self._status_locked()

            previous_ports = self._ports
            previous_bind_host = self._bind_host
            was_running = self._is_running_locked()
            await self._stop_locked(clear_error=False)
            if was_running:
                if _bind_host_for_engine_settings(engine_settings) == previous_bind_host:
                    await self._ensure_ports_available_after_stop_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                    )
                else:
                    await self._wait_for_ports_available_locked(
                        bind_host=previous_bind_host,
                        ports=previous_ports,
                        timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                    )
            if not self._can_attempt_restart_locked(now_monotonic):
                return self._status_locked()

            bind_host = _bind_host_for_engine_settings(engine_settings)
            stable_ports = previous_ports if was_running and bind_host == previous_bind_host else None
            config = await self._resolve_runtime_config_with_reclaim_locked(
                engine_settings,
                path_auth=path_auth,
                port_policy="stable",
                stable_ports=stable_ports,
            )
            try:
                await self._start_locked(config=config)
            except Exception as exc:
                self._record_restart_failure_locked(now_monotonic, reason=str(exc))
                raise
            self._reset_restart_backoff_locked()
            return self._status_locked()

    async def stop(self) -> MediaMtxEngineStatus:
        async with self._lock:
            await self._stop_locked(clear_error=False)
            self._reset_restart_backoff_locked()
            return self._status_locked()

    async def get_status(self) -> MediaMtxEngineStatus:
        async with self._lock:
            self._refresh_process_state_locked(time.monotonic())
            return self._status_locked()

    async def get_urls_for_path(self, path_slug: str, *, host: str | None = None) -> dict[str, str]:
        async with self._lock:
            return self._urls_for_path_locked(path_slug=path_slug, host=host)

    async def get_publish_url_for_path(self, path_slug: str, *, host: str | None = None) -> str:
        async with self._lock:
            normalized_path = _normalize_path(path_slug)
            if self._bind_host == "127.0.0.1":
                resolved_host = "127.0.0.1"
            else:
                candidate = str(host or "").strip()
                resolved_host = candidate if candidate and candidate != "0.0.0.0" else "127.0.0.1"
            username, password = self._publish_credentials_by_path.get(normalized_path, ("", ""))
            if username and password:
                return f"rtsp://{username}:{password}@{resolved_host}:{self._ports.rtsp}/{normalized_path}"
            return f"rtsp://{resolved_host}:{self._ports.rtsp}/{normalized_path}"

    async def get_read_url_for_path(self, path_slug: str, *, host: str | None = None) -> str:
        async with self._lock:
            normalized_path = _normalize_path(path_slug)
            if self._bind_host == "127.0.0.1":
                resolved_host = "127.0.0.1"
            else:
                candidate = str(host or "").strip()
                resolved_host = candidate if candidate and candidate != "0.0.0.0" else "127.0.0.1"
            username, password = self._read_auth_by_path.get(normalized_path, ("", ""))
            if username and password:
                return f"rtsp://{username}:{password}@{resolved_host}:{self._ports.rtsp}/{normalized_path}"
            return f"rtsp://{resolved_host}:{self._ports.rtsp}/{normalized_path}"

    async def status_payload(self, *, host: str | None = None) -> dict[str, Any]:
        async with self._lock:
            self._refresh_process_state_locked(time.monotonic())
            status = self._status_locked()
            urls = self._urls_for_path_locked(path_slug=status.test_path, host=host)
            return {
                "running": status.running,
                "metrics_enabled": status.metrics_enabled,
                "pid": status.pid,
                "uptime_seconds": status.uptime_seconds,
                "started_at_unix": status.started_at_unix,
                "bind_host": status.bind_host,
                "ports": {
                    "rtsp": status.ports.rtsp,
                    "hls": status.ports.hls,
                    "webrtc": status.ports.webrtc,
                    "webrtc_udp": status.ports.webrtc_udp,
                    "api": status.ports.api,
                    "metrics": status.ports.metrics,
                },
                "last_error": status.last_error,
                "mediamtx_version": status.mediamtx_version,
                "platform": status.platform,
                "binary_path": status.binary_path,
                "config_path": status.config_path,
                "logs": {
                    "stdout": str(status.log_path or ""),
                    "stderr": str(status.log_path or ""),
                },
                "test_path": status.test_path,
                "urls": urls,
                "warnings": list(status.warnings),
                "restart_count": status.restart_count,
                "port_policy": status.port_policy,
                "port_resolution": status.port_resolution,
                "port_blocking_errors": list(status.port_blocking_errors),
            }

    def _is_running_locked(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _status_locked(self) -> MediaMtxEngineStatus:
        running = self._is_running_locked()
        pid = self._process.pid if running and self._process is not None else None
        uptime = (time.time() - self._started_at_unix) if running and self._started_at_unix is not None else None
        warnings = list(self._warnings)
        backoff_s = self._restart_backoff_remaining_locked(time.monotonic())
        if backoff_s > 0.0 and not running:
            warnings.append(f"MediaMTX auto-restart backoff active for {backoff_s:.1f}s.")

        return MediaMtxEngineStatus(
            running=running,
            metrics_enabled=bool(self._metrics_enabled),
            pid=pid,
            uptime_seconds=uptime,
            started_at_unix=self._started_at_unix,
            bind_host=self._bind_host,
            ports=self._ports,
            last_error=self._last_error,
            mediamtx_version=self._mediamtx_version,
            platform=self._platform_key,
            binary_path=str(self._binary_path) if self._binary_path else None,
            config_path=str(self._config_path) if self._config_path else None,
            log_path=str(self._log_path) if self._log_path else None,
            test_path=self._test_path,
            warnings=tuple(warnings),
            restart_count=int(self._restart_count),
            port_policy=str(self._port_policy or "flexible"),
            port_resolution=str(self._port_resolution or "preferred"),
            port_blocking_errors=tuple(self._port_blocking_errors),
        )

    def _urls_for_path_locked(self, *, path_slug: str, host: str | None) -> dict[str, str]:
        normalized_path = _normalize_path(path_slug)

        if self._bind_host == "127.0.0.1":
            resolved_host = "127.0.0.1"
        else:
            candidate = str(host or "").strip()
            resolved_host = candidate if candidate and candidate != "0.0.0.0" else "127.0.0.1"

        return {
            "rtsp_url": f"rtsp://{resolved_host}:{self._ports.rtsp}/{normalized_path}",
            "hls_url": f"http://{resolved_host}:{self._ports.hls}/{normalized_path}/index.m3u8",
            "webrtc_url": f"http://{resolved_host}:{self._ports.webrtc}/{normalized_path}/whep",
        }

    async def _start_locked(self, *, config: _RuntimeConfig) -> None:
        runtime_dir = self._data_dir / "runtime" / "streaming"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        logs_dir = runtime_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._prune_logs(logs_dir, keep=8)

        config_path = runtime_dir / "mediamtx.yml"
        config_path.write_text(config.config_text, encoding="utf-8")
        extra_warnings: list[str] = []

        for attempt in range(2):
            log_name = time.strftime("mediamtx-%Y%m%d-%H%M%S.log")
            log_path = logs_dir / log_name
            log_file = log_path.open("ab")

            try:
                process = await asyncio.create_subprocess_exec(
                    str(config.binary_path),
                    str(config_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(runtime_dir),
                )
            except Exception:
                log_file.close()
                raise

            self._process = process
            self._started_at_unix = time.time()
            self._bind_host = config.bind_host
            self._ports = config.ports
            self._warnings = config.warnings + tuple(extra_warnings)
            self._port_policy = config.port_policy
            self._port_resolution = "reclaimed" if extra_warnings else config.port_resolution
            self._port_blocking_errors = config.port_blocking_errors
            self._config_hash = config.config_hash
            self._platform_key = config.platform_key
            self._binary_path = config.binary_path
            self._config_path = config_path
            self._log_path = log_path
            self._log_file = log_file
            self._publish_credentials_by_path = dict(config.publish_credentials_by_path)
            self._read_auth_by_path = dict(config.read_auth_by_path)
            self._last_error = None

            try:
                await self._wait_until_ready_locked(timeout_s=8.0)
                return
            except Exception as exc:
                log_hint = _mediamtx_log_error_hint(self._log_path)
                failed_pid = process.pid

                if attempt == 0 and _should_attempt_auto_reclaim(exc=exc, log_hint=log_hint):
                    await self._stop_locked(clear_error=False)
                    killed_pids = await asyncio.to_thread(
                        kill_mediamtx_processes_for_config_path,
                        str(config_path),
                        exclude_pids={failed_pid} if failed_pid else None,
                    )
                    if killed_pids:
                        extra_warnings.append(_format_auto_reclaim_warning(killed_pids))
                        await asyncio.sleep(0.4)
                        continue

                self._last_error = str(exc)
                if log_hint:
                    self._last_error = f"{self._last_error}. {log_hint}"
                await self._stop_locked(clear_error=False)
                raise

    async def _wait_until_ready_locked(self, *, timeout_s: float) -> None:
        deadline = time.monotonic() + max(1.0, float(timeout_s))

        while time.monotonic() < deadline:
            if self._process is None:
                raise RuntimeError("MediaMTX process is not running")
            if self._process.returncode is not None:
                raise RuntimeError(f"MediaMTX exited during startup (code={self._process.returncode})")
            if await _tcp_reachable("127.0.0.1", self._ports.api):
                return
            await asyncio.sleep(0.2)

        raise RuntimeError(f"MediaMTX startup timed out on API port {self._ports.api}")

    async def _stop_locked(self, *, clear_error: bool) -> None:
        process = self._process
        self._process = None
        self._started_at_unix = None
        self._config_hash = None
        self._warnings = ()
        self._port_blocking_errors = ()
        self._publish_credentials_by_path.clear()
        self._read_auth_by_path.clear()
        if clear_error:
            self._last_error = None

        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except Exception:
                    pass

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _port_policy_for_start_locked(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        running: bool,
    ) -> PortResolutionPolicy:
        if running:
            return "stable"
        if _strict_port_contract_active(engine_settings):
            return "stable"
        return "flexible"

    async def _resolve_runtime_config_with_reclaim_locked(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] | None = None,
        port_policy: PortResolutionPolicy,
        stable_ports: MediaMtxPorts | None,
    ) -> _RuntimeConfig:
        try:
            config = self._resolve_runtime_config(
                engine_settings,
                path_auth=path_auth,
                port_policy=port_policy,
                stable_ports=stable_ports,
            )
        except MediaMtxPortResolutionError as exc:
            killed_pids = await self._reclaim_stale_mediamtx_locked()
            if killed_pids:
                await self._wait_for_preferred_ports_available_locked(
                    engine_settings,
                    stable_ports=stable_ports,
                    timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                )
                try:
                    config = self._resolve_runtime_config(
                        engine_settings,
                        path_auth=path_auth,
                        port_policy=port_policy,
                        stable_ports=stable_ports,
                        extra_warnings=(_format_auto_reclaim_warning(killed_pids),),
                        forced_resolution="reclaimed",
                    )
                except MediaMtxPortResolutionError as retry_exc:
                    self._mark_port_blocked_locked(retry_exc.blocking_errors)
                    raise
                return config
            self._mark_port_blocked_locked(exc.blocking_errors)
            raise

        if port_policy == "flexible" and config.warnings:
            killed_pids = await self._reclaim_stale_mediamtx_locked()
            if killed_pids:
                await self._wait_for_preferred_ports_available_locked(
                    engine_settings,
                    stable_ports=stable_ports,
                    timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                )
                config = self._resolve_runtime_config(
                    engine_settings,
                    path_auth=path_auth,
                    port_policy=port_policy,
                    stable_ports=stable_ports,
                    extra_warnings=(_format_auto_reclaim_warning(killed_pids),),
                    forced_resolution="reclaimed",
                )
        return config

    async def _wait_for_ports_available_locked(
        self,
        *,
        bind_host: str,
        ports: MediaMtxPorts,
        timeout_s: float,
    ) -> tuple[str, ...]:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        errors: tuple[str, ...] = ()
        while True:
            errors = tuple(_port_blocking_errors(bind_host=bind_host, ports=ports))
            if not errors or time.monotonic() >= deadline:
                return errors
            await asyncio.sleep(PORT_RELEASE_POLL_SECONDS)

    async def _wait_for_preferred_ports_available_locked(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        stable_ports: MediaMtxPorts | None,
        timeout_s: float,
    ) -> tuple[str, ...]:
        bind_host = _bind_host_for_engine_settings(engine_settings)
        ports = stable_ports or _preferred_ports_for_engine_settings(engine_settings)
        return await self._wait_for_ports_available_locked(
            bind_host=bind_host,
            ports=ports,
            timeout_s=timeout_s,
        )

    async def _ensure_ports_available_after_stop_locked(
        self,
        *,
        bind_host: str,
        ports: MediaMtxPorts,
    ) -> None:
        errors = await self._wait_for_ports_available_locked(
            bind_host=bind_host,
            ports=ports,
            timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
        )
        if errors:
            killed_pids = await self._reclaim_stale_mediamtx_locked()
            if killed_pids:
                errors = await self._wait_for_ports_available_locked(
                    bind_host=bind_host,
                    ports=ports,
                    timeout_s=PORT_RELEASE_TIMEOUT_SECONDS,
                )
        if errors:
            exc = MediaMtxPortResolutionError(tuple(errors))
            self._mark_port_blocked_locked(exc.blocking_errors)
            raise exc

    async def _reclaim_stale_mediamtx_locked(self) -> list[int]:
        config_path = self._data_dir / "runtime" / "streaming" / "mediamtx.yml"
        if not config_path.exists():
            return []
        current_pid = self._process.pid if self._process is not None and self._process.pid else None
        killed_pids = await asyncio.to_thread(
            kill_mediamtx_processes_for_config_path,
            str(config_path),
            exclude_pids={int(current_pid)} if current_pid else None,
        )
        if killed_pids:
            await asyncio.sleep(0.4)
        return killed_pids

    def _mark_port_blocked_locked(self, blocking_errors: tuple[str, ...]) -> None:
        errors = tuple(str(item or "").strip() for item in blocking_errors if str(item or "").strip())
        self._port_policy = "stable"
        self._port_resolution = "blocked"
        self._port_blocking_errors = errors
        if errors:
            self._last_error = errors[0]

    def _resolve_runtime_config(
        self,
        engine_settings: StreamingEngineSettings,
        *,
        path_auth: dict[str, tuple[str, str] | MediaMTXPathAuth] | None = None,
        port_policy: PortResolutionPolicy,
        stable_ports: MediaMtxPorts | None = None,
        extra_warnings: tuple[str, ...] = (),
        forced_resolution: str | None = None,
    ) -> _RuntimeConfig:
        bind_host = _bind_host_for_engine_settings(engine_settings)

        if stable_ports is not None:
            if not (self._is_running_locked() and bind_host == self._bind_host):
                errors = tuple(_port_blocking_errors(bind_host=bind_host, ports=stable_ports))
                if errors:
                    raise MediaMtxPortResolutionError(errors)
            ports = stable_ports
            warnings: tuple[str, ...] = ()
            resolution = "preserved"
        else:
            preferred = engine_settings.preferred_ports
            ports, warnings = _resolve_ports(
                bind_host=bind_host,
                preferred_rtsp=int(preferred.rtsp),
                preferred_hls=int(preferred.hls),
                preferred_webrtc=int(preferred.webrtc),
                preferred_webrtc_udp=int(getattr(preferred, "webrtc_udp", 18762)),
                preferred_api=int(preferred.api),
                preferred_metrics=int(getattr(preferred, "metrics", 9998)),
                allow_fallback=port_policy == "flexible",
            )
            resolution = "fallback" if warnings else "preferred"
        if extra_warnings:
            warnings = tuple([*warnings, *extra_warnings])
        if forced_resolution:
            resolution = str(forced_resolution)

        version = (
            str(getattr(engine_settings, "mediamtx_version", MEDIAMTX_VERSION) or MEDIAMTX_VERSION).strip()
            or MEDIAMTX_VERSION
        )
        self._mediamtx_version = version
        self._metrics_enabled = bool(getattr(engine_settings, "metrics_enabled", True))

        platform = detect_mediamtx_platform()
        binary = extract_mediamtx_binary(data_dir=self._data_dir, platform=platform, version=version)

        path_auth_entries: list[MediaMTXPathAuth] = []
        publish_credentials_by_path: dict[str, tuple[str, str]] = {}
        read_auth_by_path: dict[str, tuple[str, str]] = {}
        path_auth_source = path_auth or {}
        for path in self._engine_paths:
            normalized_path = _normalize_path(path)
            publish_username, publish_password = self._derive_publish_credentials(normalized_path)
            publish_credentials_by_path[normalized_path] = (publish_username, publish_password)

            read_ips: tuple[str, ...] = ()
            publish_enabled = True
            auth_rule = path_auth_source.get(normalized_path)
            if isinstance(auth_rule, MediaMTXPathAuth):
                read_username = str(auth_rule.read_username or "").strip()
                read_password = str(auth_rule.read_password or "").strip()
                read_ips = tuple(str(ip or "").strip() for ip in (auth_rule.read_ips or ()) if str(ip or "").strip())
                publish_enabled = bool(auth_rule.publish_enabled)
                if auth_rule.publish_username and auth_rule.publish_password:
                    publish_username = str(auth_rule.publish_username).strip()
                    publish_password = str(auth_rule.publish_password).strip()
            else:
                read_pair = auth_rule or ("", "")
                read_username = str(read_pair[0] or "").strip()
                read_password = str(read_pair[1] or "").strip()
            if read_username and read_password:
                read_auth_by_path[normalized_path] = (read_username, read_password)
            if not publish_enabled:
                publish_credentials_by_path.pop(normalized_path, None)

            path_auth_entries.append(
                MediaMTXPathAuth(
                    path=normalized_path,
                    read_username=read_username or None,
                    read_password=read_password or None,
                    read_ips=read_ips,
                    publish_username=publish_username,
                    publish_password=publish_password,
                    publish_enabled=publish_enabled,
                )
            )

        config_text = render_mediamtx_config(
            bind_host=bind_host,
            hls_bind_host=(
                "127.0.0.1" if _hls_should_bind_loopback(engine_settings) else bind_host
            ),
            ports=MediaMTXResolvedPorts(
                rtsp=ports.rtsp,
                hls=ports.hls,
                api=ports.api,
                webrtc=ports.webrtc,
                webrtc_udp=ports.webrtc_udp,
                metrics=ports.metrics,
                rtp=ports.rtp,
                rtcp=ports.rtcp,
            ),
            paths=list(self._engine_paths),
            enable_webrtc=True,
            webrtc_ice_servers=list(getattr(engine_settings, "webrtc_ice_servers", []) or []),
            webrtc_additional_hosts=_merge_string_lists(
                list(getattr(engine_settings, "webrtc_additional_hosts", []) or []),
                _split_env_list("TOPOSYNC_STREAMING_WEBRTC_ADDITIONAL_HOSTS"),
            ),
            path_auth=path_auth_entries,
            path_configs=dict(self._path_configs_by_path),
            api_allow_origins=["*"],
            hls_allow_origins=["*"],
            webrtc_allow_origins=["*"],
            webrtc_local_udp_address=_env_address(
                "TOPOSYNC_STREAMING_WEBRTC_LOCAL_UDP_ADDRESS",
                default=f":{ports.webrtc_udp}",
            ),
            webrtc_local_tcp_address=_env_address(
                "TOPOSYNC_STREAMING_WEBRTC_LOCAL_TCP_ADDRESS",
                default="",
            ),
            metrics_enabled=self._metrics_enabled,
        )

        config_hash = hashlib.sha256(config_text.encode("utf-8")).hexdigest()

        return _RuntimeConfig(
            bind_host=bind_host,
            ports=ports,
            warnings=warnings,
            port_policy=port_policy,
            port_resolution=resolution,
            port_blocking_errors=(),
            platform_key=platform.key,
            binary_path=binary,
            config_text=config_text,
            config_hash=config_hash,
            publish_credentials_by_path=publish_credentials_by_path,
            read_auth_by_path=read_auth_by_path,
        )

    def _update_engine_paths_locked(self, engine_paths: list[str] | None) -> None:
        if engine_paths is None:
            return

        seen: set[str] = set()
        normalized: list[str] = []
        for item in engine_paths:
            slug = _normalize_path(str(item or ""))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            normalized.append(slug)

        if self._test_path not in seen:
            normalized.insert(0, self._test_path)

        if not normalized:
            normalized = [self._test_path]

        self._engine_paths = tuple(normalized)

    def _update_path_configs_locked(self, path_configs: dict[str, dict[str, object]] | None) -> None:
        if path_configs is None:
            return
        normalized: dict[str, dict[str, object]] = {}
        if isinstance(path_configs, dict):
            for raw_path, raw_config in path_configs.items():
                path_slug = _normalize_path(str(raw_path or ""))
                if not path_slug:
                    continue
                cfg = raw_config if isinstance(raw_config, dict) else {}
                normalized[path_slug] = dict(cfg)
        self._path_configs_by_path = normalized

    def _prune_logs(self, logs_dir: Path, *, keep: int) -> None:
        try:
            logs = sorted(logs_dir.glob("mediamtx-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
        except Exception:
            return

        for old in logs[max(0, int(keep)) :]:
            try:
                old.unlink()
            except Exception:
                continue

    def _refresh_process_state_locked(self, now_monotonic: float) -> None:
        process = self._process
        if process is None or process.returncode is None:
            return
        return_code = int(process.returncode)
        log_hint = _mediamtx_log_error_hint(self._log_path)
        self._process = None
        self._started_at_unix = None
        self._config_hash = None
        self._publish_credentials_by_path.clear()
        self._read_auth_by_path.clear()
        self._last_error = f"MediaMTX exited unexpectedly (code={return_code})"
        if log_hint:
            self._last_error = f"{self._last_error}. {log_hint}"
        self._record_restart_failure_locked(now_monotonic, reason=self._last_error)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _record_restart_failure_locked(self, now_monotonic: float, *, reason: str) -> None:
        self._restart_count += 1
        self._last_error = str(reason or "").strip() or self._last_error
        self._restart_attempts_monotonic.append(float(now_monotonic))
        cutoff = float(now_monotonic) - 60.0
        while self._restart_attempts_monotonic and self._restart_attempts_monotonic[0] < cutoff:
            self._restart_attempts_monotonic.popleft()
        attempts = len(self._restart_attempts_monotonic)
        if attempts > self._max_restarts_per_minute:
            self._start_backoff_seconds = min(
                30.0,
                max(5.0, self._start_backoff_seconds * 1.7 if self._start_backoff_seconds else 5.0),
            )
        else:
            self._start_backoff_seconds = min(
                20.0,
                max(1.0, self._start_backoff_seconds * 1.8 if self._start_backoff_seconds else 1.0),
            )
        self._next_start_attempt_monotonic = float(now_monotonic) + float(self._start_backoff_seconds)

    def _reset_restart_backoff_locked(self) -> None:
        self._start_backoff_seconds = 0.0
        self._next_start_attempt_monotonic = 0.0
        self._restart_attempts_monotonic.clear()

    def _can_attempt_restart_locked(self, now_monotonic: float) -> bool:
        return float(now_monotonic) >= float(self._next_start_attempt_monotonic or 0.0)

    def _restart_backoff_remaining_locked(self, now_monotonic: float) -> float:
        remaining = float(self._next_start_attempt_monotonic or 0.0) - float(now_monotonic)
        return max(0.0, remaining)

    def _derive_publish_credentials(self, path_slug: str) -> tuple[str, str]:
        normalized_path = _normalize_path(path_slug)
        secret = self._load_or_create_publish_secret()
        digest = hmac.new(secret, normalized_path.encode("utf-8"), hashlib.sha256).hexdigest()
        username = f"pub_{digest[:12]}"
        password = digest[12:44]
        return username, password

    def _load_or_create_publish_secret(self) -> bytes:
        if self._publish_secret is not None:
            return self._publish_secret

        runtime_dir = self._data_dir / "runtime" / "streaming"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        key_path = runtime_dir / "publish-secret.key"

        if key_path.is_file():
            try:
                payload = key_path.read_bytes()
                if payload:
                    self._publish_secret = payload
                    return payload
            except Exception:
                pass

        payload = os.urandom(32)
        try:
            key_path.write_bytes(payload)
        except Exception:
            # If I/O fails, fall back to an in-memory secret.
            pass
        self._publish_secret = payload
        return payload


@dataclass(frozen=True, slots=True)
class _RuntimeConfig:
    bind_host: str
    ports: MediaMtxPorts
    warnings: tuple[str, ...]
    port_policy: PortResolutionPolicy
    port_resolution: str
    port_blocking_errors: tuple[str, ...]
    platform_key: str
    binary_path: Path
    config_text: str
    config_hash: str
    publish_credentials_by_path: dict[str, tuple[str, str]]
    read_auth_by_path: dict[str, tuple[str, str]]


class MediaMtxPortResolutionError(RuntimeError):
    def __init__(self, blocking_errors: tuple[str, ...]) -> None:
        self.blocking_errors = tuple(
            str(item or "").strip() for item in blocking_errors if str(item or "").strip()
        )
        super().__init__(" ".join(self.blocking_errors) or "MediaMTX port contract is blocked.")


def _bind_host_for_engine_settings(engine_settings: StreamingEngineSettings) -> str:
    return "0.0.0.0" if bool(engine_settings.expose_to_lan) else "127.0.0.1"


def _preferred_ports_for_engine_settings(engine_settings: StreamingEngineSettings) -> MediaMtxPorts:
    preferred = engine_settings.preferred_ports
    return MediaMtxPorts(
        rtsp=int(preferred.rtsp),
        hls=int(preferred.hls),
        webrtc=int(preferred.webrtc),
        webrtc_udp=int(getattr(preferred, "webrtc_udp", 18762)),
        api=int(preferred.api),
        metrics=int(getattr(preferred, "metrics", 9998)),
        rtp=50000,
        rtcp=50001,
    )


def _env_port_contract_names() -> tuple[str, ...]:
    return (
        "TOPOSYNC_EXPECTED_RTSP_PORT",
        "TOPOSYNC_EXPECTED_HLS_PORT",
        "TOPOSYNC_EXPECTED_WEBRTC_PORT",
        "TOPOSYNC_EXPECTED_WEBRTC_UDP_PORT",
        "TOPOSYNC_EXPECTED_MEDIAMTX_API_PORT",
    )


def _strict_port_contract_active(engine_settings: StreamingEngineSettings) -> bool:
    target = str(os.getenv("TOPOSYNC_DEPLOYMENT_TARGET") or "").strip().lower()
    if target == "home_assistant_addon":
        return True
    if any(str(os.getenv(name) or "").strip() for name in _env_port_contract_names()):
        return True
    if str(os.getenv("TOPOSYNC_HOME_ASSISTANT_RTSP_HOST") or "").strip():
        return True
    strict_env = str(os.getenv("TOPOSYNC_STREAMING_STRICT_PORTS") or "").strip().lower()
    if bool(engine_settings.expose_to_lan) and strict_env in {"1", "true", "yes", "on"}:
        return True
    return False


def _port_unavailable_message(
    *,
    label: str,
    bind_host: str,
    port: int,
    suffix: str = "",
) -> str:
    target = suffix or f" port {int(port)}"
    return (
        f"Port contract blocked: {label}{target} is unavailable on {bind_host}. "
        "Stop the external process using it, run /api/streams/engine/reclaim if it is a stale "
        "Toposync MediaMTX process, or change streaming preferred_ports explicitly."
    )


def _port_reserved_message(*, label: str, port: int) -> str:
    return (
        f"Port contract blocked: {label} port {int(port)} is already assigned to another "
        "MediaMTX listener in this configuration. Change streaming preferred_ports explicitly."
    )


def _port_blocking_errors(*, bind_host: str, ports: MediaMtxPorts) -> list[str]:
    checks: list[tuple[str, str, int, bool]] = [
        ("RTSP", bind_host, int(ports.rtsp), False),
        ("HLS", bind_host, int(ports.hls), False),
        ("WebRTC", bind_host, int(ports.webrtc), False),
        ("API", bind_host, int(ports.api), False),
        ("Metrics", "127.0.0.1", int(ports.metrics), False),
        ("WebRTC UDP", bind_host, int(ports.webrtc_udp), True),
        ("RTP", bind_host, int(ports.rtp), True),
        ("RTCP", bind_host, int(ports.rtcp), True),
    ]
    errors: list[str] = []
    seen: set[tuple[str, int, bool]] = set()
    for label, host, port, udp in checks:
        key = (host, int(port), udp)
        if key in seen:
            errors.append(_port_reserved_message(label=label, port=port))
            continue
        seen.add(key)
        available = _can_bind_udp(host, port) if udp else _can_bind(host, port)
        if not available:
            errors.append(_port_unavailable_message(label=label, bind_host=host, port=port))
    return errors


def _resolve_ports(
    *,
    bind_host: str,
    preferred_rtsp: int,
    preferred_hls: int,
    preferred_webrtc: int,
    preferred_webrtc_udp: int,
    preferred_api: int,
    preferred_metrics: int,
    allow_fallback: bool = True,
) -> tuple[MediaMtxPorts, tuple[str, ...]]:
    used: set[int] = set()
    warnings: list[str] = []

    rtsp, changed = _pick_port(
        bind_host=bind_host,
        preferred=preferred_rtsp,
        used=used,
        label="RTSP",
        allow_fallback=allow_fallback,
    )
    used.add(rtsp)
    if changed:
        warnings.append(f"RTSP port {preferred_rtsp} unavailable; using {rtsp}.")

    hls, changed = _pick_port(
        bind_host=bind_host,
        preferred=preferred_hls,
        used=used,
        label="HLS",
        allow_fallback=allow_fallback,
    )
    used.add(hls)
    if changed:
        warnings.append(f"HLS port {preferred_hls} unavailable; using {hls}.")

    webrtc, changed = _pick_port(
        bind_host=bind_host,
        preferred=preferred_webrtc,
        used=used,
        label="WebRTC",
        allow_fallback=allow_fallback,
    )
    used.add(webrtc)
    if changed:
        warnings.append(f"WebRTC port {preferred_webrtc} unavailable; using {webrtc}.")

    webrtc_udp, udp_changed = _pick_udp_port(
        bind_host=bind_host,
        preferred=preferred_webrtc_udp,
        used=used,
        label="WebRTC UDP",
        allow_fallback=allow_fallback,
    )
    used.add(webrtc_udp)
    if udp_changed:
        warnings.append(f"WebRTC UDP port {preferred_webrtc_udp} unavailable; using {webrtc_udp}.")

    api, changed = _pick_port(
        bind_host=bind_host,
        preferred=preferred_api,
        used=used,
        label="API",
        allow_fallback=allow_fallback,
    )
    used.add(api)
    if changed:
        warnings.append(f"API port {preferred_api} unavailable; using {api}.")

    metrics, changed = _pick_port(
        bind_host="127.0.0.1",
        preferred=preferred_metrics,
        used=used,
        label="Metrics",
        allow_fallback=allow_fallback,
    )
    used.add(metrics)
    if changed:
        warnings.append(f"Metrics port {preferred_metrics} unavailable; using {metrics}.")

    # MediaMTX defaults to RTP/RTCP (UDP) on 8000/8001 and fails to start if they're already in use.
    # Since 8000 is commonly taken by dev servers, we automatically pick a free consecutive pair.
    preferred_rtp = 50000
    rtp, rtcp, udp_changed = _pick_udp_ports_pair(
        bind_host=bind_host,
        preferred=preferred_rtp,
        used=used,
        allow_fallback=allow_fallback,
    )
    if udp_changed:
        warnings.append(f"RTP/RTCP port pair {preferred_rtp}/{preferred_rtp + 1} unavailable; using {rtp}/{rtcp}.")

    return MediaMtxPorts(
        rtsp=rtsp,
        hls=hls,
        webrtc=webrtc,
        webrtc_udp=webrtc_udp,
        api=api,
        metrics=metrics,
        rtp=rtp,
        rtcp=rtcp,
    ), tuple(warnings)


def _pick_port(
    *,
    bind_host: str,
    preferred: int,
    used: set[int],
    label: str,
    allow_fallback: bool,
) -> tuple[int, bool]:
    normalized = max(1, min(65535, int(preferred)))
    if normalized in used:
        if not allow_fallback:
            raise MediaMtxPortResolutionError(
                (_port_reserved_message(label=label, port=normalized),)
            )
    elif _can_bind(bind_host, normalized):
        return normalized, False
    elif not allow_fallback:
        raise MediaMtxPortResolutionError(
            (_port_unavailable_message(label=label, bind_host=bind_host, port=normalized),)
        )

    for candidate in range(max(1024, normalized + 1), min(65535, normalized + 300)):
        if candidate in used:
            continue
        if _can_bind(bind_host, candidate):
            return candidate, True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        dynamic = int(sock.getsockname()[1])
    if dynamic in used:
        raise RuntimeError("Failed to find free TCP port for MediaMTX")
    return dynamic, True


def _pick_udp_port(
    *,
    bind_host: str,
    preferred: int,
    used: set[int],
    label: str,
    allow_fallback: bool,
) -> tuple[int, bool]:
    normalized = max(1, min(65535, int(preferred)))
    if normalized in used:
        if not allow_fallback:
            raise MediaMtxPortResolutionError(
                (_port_reserved_message(label=label, port=normalized),)
            )
    elif _can_bind_udp(bind_host, normalized):
        return normalized, False
    elif not allow_fallback:
        raise MediaMtxPortResolutionError(
            (_port_unavailable_message(label=label, bind_host=bind_host, port=normalized),)
        )

    for candidate in range(max(1024, normalized + 1), min(65535, normalized + 300)):
        if candidate in used:
            continue
        if _can_bind_udp(bind_host, candidate):
            return candidate, True

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_host, 0))
        dynamic = int(sock.getsockname()[1])
    if dynamic in used:
        raise RuntimeError("Failed to find free UDP port for MediaMTX WebRTC")
    return dynamic, True


def _can_bind(bind_host: str, port: int) -> bool:
    normalized_host = str(bind_host or "").strip() or "127.0.0.1"
    candidates: list[tuple[int, str]] = [(socket.AF_INET, normalized_host)]

    # Avoid subtle collisions between IPv4/IPv6 listeners on the same port.
    if normalized_host == "127.0.0.1":
        candidates.append((socket.AF_INET6, "::1"))
    elif normalized_host == "0.0.0.0":
        candidates.append((socket.AF_INET6, "::"))

    for family, host in candidates:
        bind_payload: tuple[str, int] | tuple[str, int, int, int]
        if family == socket.AF_INET6:
            bind_payload = (host, int(port), 0, 0)
        else:
            bind_payload = (host, int(port))

        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6") and hasattr(socket, "IPV6_V6ONLY"):
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(bind_payload)
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EINVAL}:
                continue
            return False
    return True


def _pick_udp_ports_pair(
    *,
    bind_host: str,
    preferred: int,
    used: set[int],
    allow_fallback: bool,
) -> tuple[int, int, bool]:
    """Pick a free consecutive RTP/RTCP UDP port pair.

    MediaMTX requires RTP/RTCP ports to be consecutive.
    """
    normalized = max(1024, min(65534, int(preferred)))
    if normalized % 2 != 0:
        normalized += 1
    if normalized >= 65534:
        normalized = 65532

    def ok(candidate: int) -> bool:
        if candidate in used or (candidate + 1) in used:
            return False
        if candidate <= 0 or (candidate + 1) >= 65535:
            return False
        return _can_bind_udp(bind_host, candidate) and _can_bind_udp(bind_host, candidate + 1)

    if ok(normalized):
        used.add(normalized)
        used.add(normalized + 1)
        return normalized, normalized + 1, False

    if not allow_fallback:
        raise MediaMtxPortResolutionError(
            (
                _port_unavailable_message(
                    label="RTP/RTCP",
                    bind_host=bind_host,
                    port=normalized,
                    suffix=f" pair {normalized}/{normalized + 1}",
                ),
            )
        )

    for candidate in range(normalized + 2, min(65534, normalized + 2000), 2):
        if ok(candidate):
            used.add(candidate)
            used.add(candidate + 1)
            return candidate, candidate + 1, True

    for candidate in range(10000, 65000, 2):
        if ok(candidate):
            used.add(candidate)
            used.add(candidate + 1)
            return candidate, candidate + 1, True

    raise RuntimeError("Failed to find free UDP RTP/RTCP port pair for MediaMTX")


def _can_bind_udp(bind_host: str, port: int) -> bool:
    normalized_host = str(bind_host or "").strip() or "127.0.0.1"
    candidates: list[tuple[int, str]] = [(socket.AF_INET, normalized_host)]

    # Avoid subtle collisions between IPv4/IPv6 listeners on the same port.
    if normalized_host == "127.0.0.1":
        candidates.append((socket.AF_INET6, "::1"))
    elif normalized_host == "0.0.0.0":
        candidates.append((socket.AF_INET6, "::"))

    for family, host in candidates:
        bind_payload: tuple[str, int] | tuple[str, int, int, int]
        if family == socket.AF_INET6:
            bind_payload = (host, int(port), 0, 0)
        else:
            bind_payload = (host, int(port))

        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6") and hasattr(socket, "IPV6_V6ONLY"):
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(bind_payload)
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EINVAL}:
                continue
            return False
    return True


async def _tcp_reachable(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=0.35)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    _ = reader
    return True


def _normalize_path(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "test"
    filtered = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "-" for ch in raw)
    cleaned = filtered.strip("-_")
    return cleaned or "test"


def _mediamtx_log_error_hint(log_path: Path | None, *, max_bytes: int = 4096) -> str:
    """Extract the last error reason from the MediaMTX log tail, if available.

    MediaMTX can exit with a generic code, while the real failure reason is printed to stdout/stderr.
    We capture stdout/stderr into a file, so we can surface a short, user-facing hint.
    """
    if log_path is None:
        return ""

    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = int(handle.tell())
            if size <= 0:
                return ""
            handle.seek(max(0, size - int(max_bytes)))
            chunk = handle.read(int(max_bytes))
    except Exception:
        return ""

    if not chunk:
        return ""

    text = chunk.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    # Prefer the last error-level line in the tail. Example:
    # "2026/02/28 16:51:22 ERR listen udp :50000: bind: address already in use"
    for line in reversed(lines[-30:]):
        if " ERR " in line:
            detail = line.split(" ERR ", 1)[1].strip()
            if detail:
                return f"MediaMTX log: {detail}"
        if " FTL " in line:
            detail = line.split(" FTL ", 1)[1].strip()
            if detail:
                return f"MediaMTX log: {detail}"
    return ""


def _should_attempt_auto_reclaim(*, exc: Exception, log_hint: str) -> bool:
    text = f"{exc} {log_hint}".strip().lower()
    return "address already in use" in text


def _format_auto_reclaim_warning(killed_pids: list[int]) -> str:
    count = len(killed_pids)
    suffix = f" (pid: {killed_pids[0]})" if count == 1 else f" (pids: {', '.join(str(pid) for pid in killed_pids)})"
    return f"Automatically recovered {count} stale MediaMTX process(es) for this data directory{suffix}."
