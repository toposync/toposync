from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from toposync_ext_streaming.api.models import StreamingEngineSettings
from toposync_ext_streaming.streaming.engine_manager import MediaMtxEngineManager


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
