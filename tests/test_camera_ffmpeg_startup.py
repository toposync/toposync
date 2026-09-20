from __future__ import annotations

import io
from typing import Any

import pytest

from toposync_ext_cameras.processing import frame_grabber


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
