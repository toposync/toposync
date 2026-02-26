from __future__ import annotations

import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

import toposync_ext_streaming.streaming.mediamtx_binary as mediamtx_binary
from toposync_ext_streaming.streaming.mediamtx_binary import (
    expected_mediamtx_binary_path,
    extract_mediamtx_binary,
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


def _make_tar_gz(*, exe_name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=f"mediamtx/{exe_name}")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _make_zip(*, exe_name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"mediamtx/{exe_name}", payload)
    return buffer.getvalue()


def test_extract_mediamtx_binary_downloads_and_extracts_tar(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TOPOSYNC_STREAMING_ENGINE_PATH", raising=False)
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_DOWNLOAD_BASE_URL", "https://example.invalid/download")

    platform = MediaMTXPlatform(os="linux", arch="x64", key="linux-x64", exe_name="mediamtx")
    version = "v1.16.2"
    asset_name = f"mediamtx_{version}_linux_amd64.tar.gz"
    archive_bytes = _make_tar_gz(exe_name=platform.exe_name, payload=b"binary-linux")
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    checksums = f"{sha256}  {asset_name}\n".encode("utf-8")

    base = "https://example.invalid/download"
    checksums_url = f"{base}/{version}/checksums.sha256"
    asset_url = f"{base}/{version}/{asset_name}"

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        url = getattr(req, "full_url", None) or str(req)
        if url == checksums_url:
            return _Response(checksums)
        if url == asset_url:
            return _Response(archive_bytes)
        raise AssertionError(f"Unexpected URL in test stub: {url}")

    monkeypatch.setattr(mediamtx_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_mediamtx_binary(data_dir=tmp_path, platform=platform, version=version)
    assert path == expected_mediamtx_binary_path(platform=platform, version=version)
    assert path.is_file()
    assert path.read_bytes() == b"binary-linux"
    if os.name != "nt":
        assert os.access(path, os.X_OK)


def test_extract_mediamtx_binary_downloads_and_extracts_zip(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TOPOSYNC_STREAMING_ENGINE_PATH", raising=False)
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_DOWNLOAD_BASE_URL", "https://example.invalid/download")

    platform = MediaMTXPlatform(os="windows", arch="x64", key="windows-x64", exe_name="mediamtx.exe")
    version = "v1.16.2"
    asset_name = f"mediamtx_{version}_windows_amd64.zip"
    archive_bytes = _make_zip(exe_name=platform.exe_name, payload=b"binary-win")
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    checksums = f"{sha256}  {asset_name}\n".encode("utf-8")

    base = "https://example.invalid/download"
    checksums_url = f"{base}/{version}/checksums.sha256"
    asset_url = f"{base}/{version}/{asset_name}"

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        url = getattr(req, "full_url", None) or str(req)
        if url == checksums_url:
            return _Response(checksums)
        if url == asset_url:
            return _Response(archive_bytes)
        raise AssertionError(f"Unexpected URL in test stub: {url}")

    monkeypatch.setattr(mediamtx_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_mediamtx_binary(data_dir=tmp_path, platform=platform, version=version)
    assert path == expected_mediamtx_binary_path(platform=platform, version=version)
    assert path.is_file()
    assert path.read_bytes() == b"binary-win"


def test_extract_mediamtx_binary_honors_engine_path_env(monkeypatch, tmp_path: Path) -> None:
    platform = MediaMTXPlatform(os="darwin", arch="arm64", key="darwin-arm64", exe_name="mediamtx")
    fake = tmp_path / "mediamtx"
    fake.write_bytes(b"external-binary")
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_PATH", str(fake))

    def urlopen_stub(req, timeout=0):  # noqa: ANN001
        raise AssertionError("urlopen should not be called when TOPOSYNC_STREAMING_ENGINE_PATH is set")

    monkeypatch.setattr(mediamtx_binary.urllib_request, "urlopen", urlopen_stub)

    path = extract_mediamtx_binary(data_dir=tmp_path, platform=platform, version="v1.16.2")
    assert path == fake


def test_extract_mediamtx_binary_accepts_engine_path_dir(monkeypatch, tmp_path: Path) -> None:
    platform = MediaMTXPlatform(os="darwin", arch="arm64", key="darwin-arm64", exe_name="mediamtx")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / platform.exe_name
    fake.write_bytes(b"external-binary-dir")
    monkeypatch.setenv("TOPOSYNC_STREAMING_ENGINE_PATH", str(bin_dir))

    path = extract_mediamtx_binary(data_dir=tmp_path, platform=platform, version="v1.16.2")
    assert path == fake
