from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from toposync_ext_streaming.api.models import StreamingEngineSettings
from toposync_ext_streaming.streaming.engine_manager import (
    MediaMtxEngineManager,
    MediaMtxPortResolutionError,
    MediaMtxPorts,
)


class _FakeLogHandle:
    def __enter__(self) -> "_FakeLogHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        del exc_type, exc, tb
        return False

    def write(self, payload) -> int:  # type: ignore[no-untyped-def]
        try:
            return len(payload)
        except Exception:
            return 0

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, *, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


def _patch_engine_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_extract_mediamtx_binary(*, data_dir: Path, platform, version: str) -> Path:  # type: ignore[no-untyped-def]
        del data_dir, platform, version
        return tmp_path / "mediamtx"

    def fake_detect_mediamtx_platform():  # type: ignore[no-untyped-def]
        return SimpleNamespace(key="darwin-arm64")

    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.extract_mediamtx_binary",
        fake_extract_mediamtx_binary,
    )
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.detect_mediamtx_platform",
        fake_detect_mediamtx_platform,
    )


def _install_fake_start(
    manager: MediaMtxEngineManager,
    events: list[tuple[str, object]],
    *,
    tmp_path: Path,
) -> None:
    async def fake_start_locked(*, config):  # type: ignore[no-untyped-def]
        events.append(("start", config.ports))
        manager._process = _FakeProcess(pid=9202, returncode=None)  # noqa: SLF001
        manager._started_at_unix = 1.0  # noqa: SLF001
        manager._bind_host = config.bind_host  # noqa: SLF001
        manager._ports = config.ports  # noqa: SLF001
        manager._warnings = config.warnings  # noqa: SLF001
        manager._port_policy = config.port_policy  # noqa: SLF001
        manager._port_resolution = config.port_resolution  # noqa: SLF001
        manager._port_blocking_errors = config.port_blocking_errors  # noqa: SLF001
        manager._config_hash = config.config_hash  # noqa: SLF001
        manager._platform_key = config.platform_key  # noqa: SLF001
        manager._binary_path = config.binary_path  # noqa: SLF001
        manager._config_path = tmp_path / "runtime" / "streaming" / "mediamtx.yml"  # noqa: SLF001
        manager._log_path = tmp_path / "runtime" / "streaming" / "mediamtx.log"  # noqa: SLF001
        manager._last_error = None  # noqa: SLF001

    manager._start_locked = fake_start_locked  # type: ignore[method-assign]  # noqa: SLF001


def test_engine_restart_stops_before_resolving_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = MediaMtxEngineManager(data_dir=tmp_path)
    settings = StreamingEngineSettings(enabled=True)
    previous_ports = MediaMtxPorts(
        rtsp=8554,
        hls=8888,
        webrtc=8889,
        api=9997,
        rtp=50000,
        rtcp=50001,
    )
    events: list[tuple[str, object]] = []

    class RestartProcess(_FakeProcess):
        def terminate(self) -> None:
            events.append(("terminate", self.pid))
            super().terminate()

    manager._process = RestartProcess(pid=9201, returncode=None)  # noqa: SLF001
    manager._started_at_unix = 1.0  # noqa: SLF001
    manager._bind_host = "127.0.0.1"  # noqa: SLF001
    manager._ports = previous_ports  # noqa: SLF001
    _patch_engine_binary(monkeypatch, tmp_path)

    def fake_can_bind(_host: str, port: int) -> bool:
        events.append(("can_bind", port))
        return True

    def fake_can_bind_udp(_host: str, port: int) -> bool:
        events.append(("can_bind_udp", port))
        return True

    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind", fake_can_bind)
    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind_udp", fake_can_bind_udp)
    _install_fake_start(manager, events, tmp_path=tmp_path)

    status = asyncio.run(manager.restart(settings, engine_paths=["camera-main"]))

    first_bind_index = next(index for index, item in enumerate(events) if item[0] == "can_bind")
    terminate_index = next(index for index, item in enumerate(events) if item[0] == "terminate")
    assert terminate_index < first_bind_index
    assert status.ports.rtsp == previous_ports.rtsp
    assert status.port_policy == "stable"
    assert status.port_resolution == "preserved"


def test_engine_stable_start_fails_when_contracted_port_is_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MediaMtxEngineManager(data_dir=tmp_path)
    settings = StreamingEngineSettings(enabled=True)
    monkeypatch.setenv("TOPOSYNC_EXPECTED_RTSP_PORT", "8554")

    def fake_can_bind(_host: str, port: int) -> bool:
        return int(port) != 8554

    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind", fake_can_bind)
    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind_udp", lambda _host, _port: True)

    with pytest.raises(MediaMtxPortResolutionError) as exc_info:
        asyncio.run(manager.ensure_running(settings, engine_paths=["camera-main"]))

    assert "RTSP port 8554 is unavailable" in str(exc_info.value)
    status = asyncio.run(manager.get_status())
    assert status.port_policy == "stable"
    assert status.port_resolution == "blocked"
    assert any("engine/reclaim" in item for item in status.port_blocking_errors)


def test_engine_flexible_initial_start_can_fallback_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MediaMtxEngineManager(data_dir=tmp_path)
    settings = StreamingEngineSettings(enabled=True)
    events: list[tuple[str, object]] = []
    _patch_engine_binary(monkeypatch, tmp_path)

    def fake_can_bind(_host: str, port: int) -> bool:
        return int(port) != 8554

    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind", fake_can_bind)
    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind_udp", lambda _host, _port: True)
    _install_fake_start(manager, events, tmp_path=tmp_path)

    status = asyncio.run(manager.ensure_running(settings, engine_paths=["camera-main"]))

    assert status.running is True
    assert status.ports.rtsp == 8555
    assert status.port_policy == "flexible"
    assert status.port_resolution == "fallback"
    assert any("RTSP port 8554 unavailable; using 8555." in item for item in status.warnings)


def test_engine_reclaims_same_config_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MediaMtxEngineManager(data_dir=tmp_path)
    settings = StreamingEngineSettings(enabled=True)
    events: list[tuple[str, object]] = []
    reclaimed = {"done": False}
    config_path = tmp_path / "runtime" / "streaming" / "mediamtx.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("paths: {}\n", encoding="utf-8")
    monkeypatch.setenv("TOPOSYNC_EXPECTED_RTSP_PORT", "8554")
    _patch_engine_binary(monkeypatch, tmp_path)

    def fake_can_bind(_host: str, port: int) -> bool:
        return bool(reclaimed["done"]) or int(port) != 8554

    def fake_kill_for_config(_config_path: str, *, exclude_pids: set[int] | None = None) -> list[int]:
        del _config_path, exclude_pids
        reclaimed["done"] = True
        return [72120]

    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind", fake_can_bind)
    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._can_bind_udp", lambda _host, _port: True)
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.kill_mediamtx_processes_for_config_path",
        fake_kill_for_config,
    )
    _install_fake_start(manager, events, tmp_path=tmp_path)

    status = asyncio.run(manager.ensure_running(settings, engine_paths=["camera-main"]))

    assert status.running is True
    assert status.ports.rtsp == 8554
    assert status.port_policy == "stable"
    assert status.port_resolution == "reclaimed"
    assert any("Automatically recovered 1 stale MediaMTX process" in item for item in status.warnings)


def test_engine_manager_auto_recovers_stale_mediamtx_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = MediaMtxEngineManager(data_dir=tmp_path)
    settings = StreamingEngineSettings(enabled=True)

    create_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [
        _FakeProcess(pid=9101, returncode=1),
        _FakeProcess(pid=9102, returncode=None),
    ]

    async def fake_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        create_calls.append((args, kwargs))
        return processes.pop(0)

    async def fake_sleep(_seconds: float) -> None:
        return None

    tcp_results = iter([True])

    async def fake_tcp_reachable(_host: str, _port: int) -> bool:
        return next(tcp_results)

    def fake_extract_mediamtx_binary(*, data_dir: Path, platform, version: str) -> Path:  # type: ignore[no-untyped-def]
        del data_dir, platform, version
        return tmp_path / "mediamtx"

    def fake_detect_mediamtx_platform():  # type: ignore[no-untyped-def]
        return SimpleNamespace(key="darwin-arm64")

    def fake_log_hint(_path: Path | None, *, max_bytes: int = 4096) -> str:
        del max_bytes
        return "MediaMTX log: listen udp :50000: bind: address already in use"

    killed_calls: list[tuple[str, set[int] | None]] = []

    def fake_kill_for_config(config_path: str, *, exclude_pids: set[int] | None = None) -> list[int]:
        killed_calls.append((config_path, exclude_pids))
        return [72120]

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr("toposync_ext_streaming.streaming.engine_manager._tcp_reachable", fake_tcp_reachable)
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.extract_mediamtx_binary",
        fake_extract_mediamtx_binary,
    )
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.detect_mediamtx_platform",
        fake_detect_mediamtx_platform,
    )
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager._mediamtx_log_error_hint",
        fake_log_hint,
    )
    monkeypatch.setattr(
        "toposync_ext_streaming.streaming.engine_manager.kill_mediamtx_processes_for_config_path",
        fake_kill_for_config,
    )
    monkeypatch.setattr(Path, "open", lambda self, mode="r", *args, **kwargs: _FakeLogHandle())

    status = asyncio.run(manager.ensure_running(settings, engine_paths=["camera-main"]))

    assert status.running is True
    assert status.pid == 9102
    assert len(create_calls) == 2
    assert len(killed_calls) == 1
    assert killed_calls[0][1] == {9101}
    assert any("Automatically recovered 1 stale MediaMTX process" in item for item in status.warnings)
