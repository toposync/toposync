"""Readiness command regression tests; never start a camera or listener."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def sample_server(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "rtsp_sample_server.py"
    spec = importlib.util.spec_from_file_location("rtsp_sample_server_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_readiness_bounds_analysis_and_decoder_buffering_before_input(sample_server, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sample_server.subprocess, "run", run)
    assert sample_server.probe_rtsp_frame(
        ffmpeg_path=Path("/test/ffmpeg"), url="rtsp://127.0.0.1:18554/fixture"
    ) == (True, "")
    assert len(calls) == 1
    command, options = calls[0]
    input_index = command.index("-i")
    for flag, value in (
        ("-analyzeduration", "100000"),
        ("-probesize", "32768"),
        ("-threads", "1"),
        ("-timeout", "2000000"),
        ("-rtsp_transport", "tcp"),
    ):
        assert command.index(flag) < input_index
        assert command[command.index(flag) + 1] == value
    assert command[command.index("-frames:v") + 1] == "1"
    assert options["timeout"] == 4
    assert options["check"] is False


def test_readiness_timeout_remains_bounded_and_does_not_retry(sample_server, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(sample_server.subprocess, "run", run)
    assert sample_server.probe_rtsp_frame(
        ffmpeg_path=Path("/test/ffmpeg"), url="rtsp://127.0.0.1:18554/fixture"
    ) == (False, "RTSP frame probe timed out")
    assert len(calls) == 1


def test_readiness_preserves_decoder_error(sample_server, monkeypatch):
    monkeypatch.setattr(
        sample_server.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "decode failed\n"),
    )
    assert sample_server.probe_rtsp_frame(
        ffmpeg_path=Path("/test/ffmpeg"), url="rtsp://127.0.0.1:18554/fixture"
    ) == (False, "decode failed")
