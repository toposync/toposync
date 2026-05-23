from __future__ import annotations

from pathlib import Path
import asyncio

import pytest

from toposync_ext_streaming.api.models import StreamingJsmpegSettings
from toposync_ext_streaming.streaming import jsmpeg_manager as manager_module
from toposync_ext_streaming.streaming.ffmpeg_binary import ResolvedFFmpegBinary
from toposync_ext_streaming.streaming.jsmpeg_manager import (
    JsmpegSessionManager,
    _JsmpegSession,
    build_jsmpeg_ffmpeg_args,
    normalize_jsmpeg_dimensions,
)
from toposync_ext_streaming.streaming.runtime_state import TransmissionRuntimeState


def test_jsmpeg_dimensions_are_even_and_contained() -> None:
    dimensions = normalize_jsmpeg_dimensions(
        output_width=1920,
        output_height=1080,
        max_width=854,
        max_height=480,
    )

    assert dimensions.width == 852
    assert dimensions.height == 480
    assert dimensions.width % 2 == 0
    assert dimensions.height % 2 == 0


def test_jsmpeg_ffmpeg_args_use_mpegts_mpeg1_without_audio() -> None:
    args = build_jsmpeg_ffmpeg_args(
        ffmpeg_path=Path("/usr/bin/ffmpeg"),
        width=854,
        height=480,
        fps=8,
        bitrate_kbps=700,
    )

    assert args[:2] == ["/usr/bin/ffmpeg", "-hide_banner"]
    assert ["-f", "rawvideo"] == args[args.index("-f") : args.index("-f") + 2]
    assert "bgr24" in args
    rate_indexes = [index for index, value in enumerate(args) if value == "-r"]
    assert rate_indexes == [args.index("-r")]
    assert args[rate_indexes[0] + 1] == "25"
    assert ["-an", "-c:v", "mpeg1video"] == args[args.index("-an") : args.index("-an") + 3]
    assert args[-2:] == ["mpegts", "pipe:1"]


def test_jsmpeg_blocking_errors_include_ffmpeg_and_session_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JsmpegSessionManager(
        data_dir=tmp_path,
        runtime_state=TransmissionRuntimeState(),
    )
    settings = StreamingJsmpegSettings(max_total_sessions=1, max_sessions_per_transmission=1)

    monkeypatch.setattr(
        manager_module,
        "resolve_ffmpeg_binary",
        lambda *, data_dir: ResolvedFFmpegBinary(path=None, source=None, error="ffmpeg missing"),
    )
    manager._sessions["session-1"] = _JsmpegSession(  # noqa: SLF001
        session_id="session-1",
        transmission_id="tx-1",
        output_id="hls-main",
    )

    errors = asyncio.run(manager.blocking_errors(settings=settings, transmission_id="tx-1"))

    assert "ffmpeg missing" in errors
    assert "JSMpeg global session limit is reached." in errors
    assert "JSMpeg session limit for this transmission is reached." in errors
