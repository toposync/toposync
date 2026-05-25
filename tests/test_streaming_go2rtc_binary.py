from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import toposync_ext_streaming.streaming.go2rtc_binary as go2rtc_binary
from toposync_ext_streaming.streaming.go2rtc_binary import (
    ENV_GO2RTC_PATH,
    expected_go2rtc_binary_path,
    extract_go2rtc_binary,
)
from toposync_ext_streaming.streaming.platform import MediaMTXPlatform


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False


def _make_zip(*, exe_name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"go2rtc/{exe_name}", payload)
    return buffer.getvalue()


def test_extract_go2rtc_binary_downloads_linux_asset(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.delenv(ENV_GO2RTC_PATH, raising=False)
    monkeypatch.setenv("TOPOSYNC_STREAMING_GO2RTC_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_STREAMING_GO2RTC_DOWNLOAD_BASE_URL", "https://example.invalid/download")

    platform = MediaMTXPlatform(os="linux", arch="x64", key="linux-x64", exe_name="go2rtc")
    version = "v1.test"
    asset_url = f"https://example.invalid/download/{version}/go2rtc_linux_amd64"

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        url = getattr(req, "full_url", None) or str(req)
        if url == asset_url:
            return _Response(b"binary-linux")
        raise AssertionError(f"Unexpected URL in test stub: {url}")

    monkeypatch.setattr(go2rtc_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_go2rtc_binary(data_dir=tmp_path, platform=platform, version=version)

    assert path == expected_go2rtc_binary_path(platform=platform, version=version)
    assert path.read_bytes() == b"binary-linux"
    if os.name != "nt":
        assert os.access(path, os.X_OK)


def test_extract_go2rtc_binary_downloads_windows_zip(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    monkeypatch.delenv(ENV_GO2RTC_PATH, raising=False)
    monkeypatch.setenv("TOPOSYNC_STREAMING_GO2RTC_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_STREAMING_GO2RTC_DOWNLOAD_BASE_URL", "https://example.invalid/download")

    platform = MediaMTXPlatform(os="windows", arch="x64", key="windows-x64", exe_name="go2rtc.exe")
    version = "v1.test"
    asset_url = f"https://example.invalid/download/{version}/go2rtc_win64.zip"
    archive_bytes = _make_zip(exe_name=platform.exe_name, payload=b"binary-windows")

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        url = getattr(req, "full_url", None) or str(req)
        if url == asset_url:
            return _Response(archive_bytes)
        raise AssertionError(f"Unexpected URL in test stub: {url}")

    monkeypatch.setattr(go2rtc_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_go2rtc_binary(data_dir=tmp_path, platform=platform, version=version)

    assert path == expected_go2rtc_binary_path(platform=platform, version=version)
    assert path.read_bytes() == b"binary-windows"


def test_extract_go2rtc_binary_honors_override_path(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    platform = MediaMTXPlatform(os="darwin", arch="arm64", key="darwin-arm64", exe_name="go2rtc")
    fake = tmp_path / "go2rtc"
    fake.write_bytes(b"external-binary")
    monkeypatch.setenv(ENV_GO2RTC_PATH, str(fake))

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        raise AssertionError("urlopen should not be called when TOPOSYNC_STREAMING_GO2RTC_PATH is set")

    monkeypatch.setattr(go2rtc_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_go2rtc_binary(data_dir=tmp_path, platform=platform, version="v1.test")

    assert path == fake
