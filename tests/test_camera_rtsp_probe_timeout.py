from __future__ import annotations

import asyncio
import inspect

import pytest

import toposync_ext_cameras.plugin as cameras_plugin


def test_rtsp_probe_timeout_uses_total_operation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    create_calls: list[tuple[str, ...]] = []
    wait_timeouts: list[float] = []

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
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(cameras_plugin.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cameras_plugin.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(cameras_plugin.asyncio, "wait_for", fake_wait_for)

    response = asyncio.run(
        cameras_plugin._ffmpeg_rtsp_probe("rtsp://user:secret@camera.local/stream1", timeout_ms=5000)
    )

    assert response.status == "timeout"
    assert response.url == "rtsp://***@camera.local/stream1"
    assert response.transports_tested == ["configured:tcp"]
    assert len(create_calls) == 1
    assert wait_timeouts == pytest.approx([5.0], abs=0.1)
    timeout_flag_index = create_calls[0].index("-timeout")
    assert int(create_calls[0][timeout_flag_index + 1]) <= 5_000_000
