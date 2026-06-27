from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import HTTPException

import toposync_ext_cameras.plugin as cameras_plugin


def test_rtsp_snapshot_timeout_bounds_transport_fallback_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    create_calls: list[tuple[str, ...]] = []
    wait_timeouts: list[float] = []
    now = {"value": 1000.0}

    class FakeProcess:
        returncode: int | None = None

        def kill(self) -> None:
            self.returncode = -9

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> FakeProcess:
        create_calls.append(tuple(args))
        return FakeProcess()

    async def fake_wait_for(awaitable: object, timeout: float) -> tuple[bytes, bytes]:
        wait_timeouts.append(timeout)
        now["value"] += timeout
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(cameras_plugin.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cameras_plugin.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(cameras_plugin.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(cameras_plugin.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            cameras_plugin._ffmpeg_snapshot(
                "rtsp://user:secret@camera.local/stream1",
                timeout_ms=9000,
            )
        )

    assert excinfo.value.status_code == 502
    assert len(create_calls) == 7
    assert sum(wait_timeouts) == pytest.approx(9.0, abs=0.1)
    assert max(wait_timeouts) <= 4.6
    assert "/stream2" in " ".join(create_calls[-1])
    assert all(int(call[call.index("-timeout") + 1]) <= 4_600_000 for call in create_calls)
    assert all("-skip_frame" in call for call in create_calls[:4])
    assert any("-skip_frame" not in call for call in create_calls[4:])


def test_rtsp_snapshot_prefers_keyframe_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    create_calls: list[tuple[str, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"jpeg", b""

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> FakeProcess:
        create_calls.append(tuple(args))
        return FakeProcess()

    monkeypatch.setattr(cameras_plugin.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cameras_plugin.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(cameras_plugin._ffmpeg_snapshot("rtsp://camera.local/main", timeout_ms=9000))

    assert result.blob == b"jpeg"
    assert result.capture_mode == "keyframe"
    assert create_calls
    first_call = create_calls[0]
    assert first_call[first_call.index("-skip_frame") + 1] == "nokey"
    assert first_call.index("-skip_frame") < first_call.index("-i")


def test_rtsp_snapshot_falls_back_to_first_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    create_calls: list[tuple[str, ...]] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, index: int) -> None:
            self.index = index

        async def communicate(self) -> tuple[bytes, bytes]:
            if self.index < 2:
                return b"", b""
            return b"jpeg", b""

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> FakeProcess:
        create_calls.append(tuple(args))
        return FakeProcess(len(create_calls) - 1)

    monkeypatch.setattr(cameras_plugin.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cameras_plugin.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(cameras_plugin._ffmpeg_snapshot("rtsp://camera.local/main", timeout_ms=9000))

    assert result.blob == b"jpeg"
    assert result.capture_mode == "first_frame"
    assert len(create_calls) == 3
    assert "-skip_frame" in create_calls[0]
    assert "-skip_frame" in create_calls[1]
    assert "-skip_frame" not in create_calls[2]
