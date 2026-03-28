from __future__ import annotations

from pathlib import Path

import pytest

import toposync_ext_streaming.streaming.ffmpeg_binary as ffmpeg_binary
from toposync_ext_streaming.streaming.ffmpeg_binary import ENV_FFMPEG_PATH, resolve_ffmpeg_binary
from toposync_ext_streaming.streaming.platform import MediaMTXPlatform


def test_resolve_ffmpeg_binary_prefers_override_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    platform = MediaMTXPlatform(os="linux", arch="x64", key="linux-x64", exe_name="ffmpeg")
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    custom_ffmpeg = custom_dir / platform.exe_name
    custom_ffmpeg.write_text("override", encoding="utf-8")

    monkeypatch.setenv(ENV_FFMPEG_PATH, str(custom_dir))
    monkeypatch.setattr(ffmpeg_binary, "detect_ffmpeg_platform", lambda: platform)
    monkeypatch.setattr(ffmpeg_binary.shutil, "which", lambda _name: None)

    resolved = resolve_ffmpeg_binary(data_dir=tmp_path)

    assert resolved.path == custom_ffmpeg.resolve()
    assert resolved.source == "override"
    assert resolved.error is None


def test_resolve_ffmpeg_binary_prefers_system_path_before_packaged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(ENV_FFMPEG_PATH, raising=False)
    monkeypatch.setattr(ffmpeg_binary.shutil, "which", lambda _name: "/usr/local/bin/ffmpeg")
    monkeypatch.setattr(
        ffmpeg_binary,
        "find_packaged_ffmpeg_binary",
        lambda _platform: pytest.fail("packaged FFmpeg lookup should not happen when PATH has ffmpeg"),
    )

    resolved = resolve_ffmpeg_binary(data_dir=tmp_path)

    assert resolved.path == Path("/usr/local/bin/ffmpeg")
    assert resolved.source == "system"
    assert resolved.error is None


def test_resolve_ffmpeg_binary_uses_packaged_binary_as_last_resort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = MediaMTXPlatform(os="linux", arch="x64", key="linux-x64", exe_name="ffmpeg")
    packaged_ffmpeg = tmp_path / "runtime" / "ffmpeg"
    packaged_ffmpeg.parent.mkdir(parents=True)
    packaged_ffmpeg.write_text("packaged", encoding="utf-8")

    monkeypatch.delenv(ENV_FFMPEG_PATH, raising=False)
    monkeypatch.setattr(ffmpeg_binary, "detect_ffmpeg_platform", lambda: platform)
    monkeypatch.setattr(ffmpeg_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_binary, "find_packaged_ffmpeg_binary", lambda _platform: object())
    monkeypatch.setattr(
        ffmpeg_binary,
        "extract_ffmpeg_binary",
        lambda **_kwargs: packaged_ffmpeg,
    )

    resolved = resolve_ffmpeg_binary(data_dir=tmp_path)

    assert resolved.path == packaged_ffmpeg
    assert resolved.source == "packaged"
    assert resolved.error is None


def test_resolve_ffmpeg_binary_reports_actionable_error_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    platform = MediaMTXPlatform(os="linux", arch="x64", key="linux-x64", exe_name="ffmpeg")

    monkeypatch.delenv(ENV_FFMPEG_PATH, raising=False)
    monkeypatch.setattr(ffmpeg_binary, "detect_ffmpeg_platform", lambda: platform)
    monkeypatch.setattr(ffmpeg_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_binary, "find_packaged_ffmpeg_binary", lambda _platform: None)

    resolved = resolve_ffmpeg_binary(data_dir=tmp_path)

    assert resolved.path is None
    assert resolved.source is None
    assert ENV_FFMPEG_PATH in str(resolved.error)
    assert "PATH" in str(resolved.error)
