from __future__ import annotations

import io
import threading
from typing import Any

import pytest

from toposync_ext_cameras.processing import frame_grabber


def test_ffmpeg_failed_restarts_are_spaced_even_when_process_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = frame_grabber.FfmpegFrameGrabber.__new__(frame_grabber.FfmpegFrameGrabber)
    reader._frames_stop = threading.Event()
    clock = [0.0]
    restarts: list[float] = []
    stopped: list[bool] = []

    def advance(seconds: float) -> bool:
        clock[0] += seconds
        return reader._frames_stop.is_set()

    def restart() -> None:
        restarts.append(clock[0])
        if len(restarts) == 1:
            raise OSError("Process creation failed")
        if len(restarts) == 3:
            reader._frames_stop.set()

    monkeypatch.setattr(reader._frames_stop, "wait", advance)
    monkeypatch.setattr(frame_grabber.time, "sleep", advance)
    monkeypatch.setattr(reader, "is_opened", lambda: False)
    monkeypatch.setattr(reader, "_restart_process", restart)
    monkeypatch.setattr(reader, "_stop_process", lambda: stopped.append(True))

    reader._reader_loop()

    assert len(restarts) == 3
    assert restarts[0] >= 2.0
    assert all(later - earlier >= 2.0 for earlier, later in zip(restarts, restarts[1:]))
    assert stopped == [True]


def test_ffmpeg_stop_interrupts_reopen_wait_without_starting_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = frame_grabber.FfmpegFrameGrabber.__new__(frame_grabber.FfmpegFrameGrabber)
    reader._frames_stop = threading.Event()
    restarts: list[bool] = []
    stopped: list[bool] = []

    def stop_during_wait(_seconds: float) -> bool:
        reader._frames_stop.set()
        return True

    monkeypatch.setattr(reader._frames_stop, "wait", stop_during_wait)
    monkeypatch.setattr(frame_grabber.time, "sleep", stop_during_wait)
    monkeypatch.setattr(reader, "is_opened", lambda: False)
    monkeypatch.setattr(reader, "_restart_process", lambda: restarts.append(True))
    monkeypatch.setattr(reader, "_stop_process", lambda: stopped.append(True))

    reader._reader_loop()

    assert restarts == []
    assert stopped == [True]


@pytest.mark.parametrize(
    "source, is_rtsp",
    [
        ("rtsp://127.0.0.1:18554/pose-fixture", True),
        ("/tmp/fixture.mp4", False),
        ("/dev/video0", False),
        ("http://localhost/fixture.mjpg", False),
    ],
)
def test_ffmpeg_startup_options_are_input_scoped_and_rtsp_only(
    monkeypatch: pytest.MonkeyPatch, source: str, is_rtsp: bool
) -> None:
    commands: list[list[str]] = []

    class Process:
        stderr = io.BytesIO()
        stdout = io.BytesIO()

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, **_kwargs: Any) -> int:
            return 0

    def popen(args: list[str], **_kwargs: Any) -> Process:
        commands.append(args)
        return Process()

    monkeypatch.setattr(frame_grabber.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(frame_grabber, "_jpeg_decoder_available", lambda: True)
    monkeypatch.setattr(frame_grabber.subprocess, "Popen", popen)
    grabber = frame_grabber.FfmpegFrameGrabber(
        source, target_fps=4, open_timeout_ms=4321, read_timeout_ms=8765
    )
    try:
        grabber._start_process()
        assert len(commands) == 1
        command = commands[0]
        input_index = command.index("-i")
        assert command[input_index + 1] == source
        options = {"-analyzeduration": "100000", "-probesize": "32768", "-threads": "1"}
        for option, value in options.items():
            if is_rtsp:
                assert command.count(option) == 1
                index = command.index(option)
                assert index < input_index
                assert command[index + 1] == value
            else:
                assert option not in command
        if is_rtsp:
            assert command[command.index("-timeout") + 1] == "8765000"
            assert command[command.index("-rtsp_transport") + 1] == "tcp"
        else:
            assert "-timeout" not in command
            assert "-rtsp_transport" not in command
        assert command[input_index + 2 :] == [
            "-an",
            "-sn",
            "-dn",
            "-vf",
            "fps=4.0",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        assert grabber._open_timeout_ms == 4321
        assert grabber._read_timeout_ms == 8765
    finally:
        grabber.stop()
