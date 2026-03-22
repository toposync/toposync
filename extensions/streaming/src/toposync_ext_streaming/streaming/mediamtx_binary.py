from __future__ import annotations

import hashlib
import os
import tarfile
import shutil
import stat
from pathlib import Path
from typing import Final
from urllib import error as urllib_error
from urllib import request as urllib_request

from . import MEDIAMTX_VERSION
from .platform import MediaMTXPlatform


DEFAULT_DOWNLOAD_BASE_URL: Final[str] = "https://github.com/bluenviron/mediamtx/releases/download"
ENV_ENGINE_PATH: Final[str] = "TOPOSYNC_STREAMING_ENGINE_PATH"
ENV_ENGINE_CACHE_DIR: Final[str] = "TOPOSYNC_STREAMING_ENGINE_CACHE_DIR"
ENV_ENGINE_DOWNLOAD_BASE_URL: Final[str] = "TOPOSYNC_STREAMING_ENGINE_DOWNLOAD_BASE_URL"


def _runtime_root_dir() -> Path:
    override = str(os.getenv(ENV_ENGINE_CACHE_DIR) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".toposync" / "runtime").resolve()


def _install_dir(*, platform: MediaMTXPlatform, version: str) -> Path:
    return _runtime_root_dir() / "streaming" / "mediamtx" / str(version or MEDIAMTX_VERSION) / platform.key


def expected_mediamtx_binary_path(*, platform: MediaMTXPlatform, version: str = MEDIAMTX_VERSION) -> Path:
    """Return the expected install path, without performing any I/O beyond path resolution."""
    return _install_dir(platform=platform, version=version) / platform.exe_name


def _resolve_engine_override_path(*, platform: MediaMTXPlatform) -> Path | None:
    raw = str(os.getenv(ENV_ENGINE_PATH) or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_dir():
        candidate = candidate / platform.exe_name
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"{ENV_ENGINE_PATH} points to a missing MediaMTX binary: {candidate}")
    return candidate


def find_installed_mediamtx_binary(
    *,
    platform: MediaMTXPlatform,
    version: str = MEDIAMTX_VERSION,
) -> Path | None:
    """Return an installed MediaMTX binary path if present, without downloading anything."""
    override = _resolve_engine_override_path(platform=platform)
    if override is not None:
        return override

    expected = expected_mediamtx_binary_path(platform=platform, version=version)
    if expected.is_file():
        return expected
    return None


def extract_mediamtx_binary(
    *,
    data_dir: Path,
    platform: MediaMTXPlatform,
    version: str = MEDIAMTX_VERSION,
) -> Path:
    """Ensure the MediaMTX binary is installed locally and return its path.

    Default behavior downloads the correct release asset from GitHub Releases (or a mirror),
    validates SHA256 against the published `checksums.sha256`, and extracts the binary to:

        ~/.toposync/runtime/streaming/mediamtx/<version>/<platform>/mediamtx(.exe)

    Offline environments can override the engine path with:

        TOPOSYNC_STREAMING_ENGINE_PATH=/path/to/mediamtx
    """
    _ = data_dir  # Kept for backward compatibility with earlier implementations.

    override = _resolve_engine_override_path(platform=platform)
    if override is not None:
        return override

    runtime_dir = _install_dir(platform=platform, version=version)
    target = runtime_dir / platform.exe_name
    if target.is_file():
        return target

    runtime_dir.mkdir(parents=True, exist_ok=True)
    asset_name = _resolve_release_asset_name(platform=platform, version=version)
    checksums_url, asset_url = _resolve_release_urls(version=version, asset_name=asset_name)
    expected_sha256 = _fetch_expected_sha256(checksums_url=checksums_url, asset_name=asset_name)

    # Keep the original extension so extract logic can detect tar.gz vs zip.
    temp_archive = runtime_dir / f".download-{asset_name}"
    if temp_archive.exists():
        try:
            temp_archive.unlink()
        except Exception:
            pass

    _download_asset(url=asset_url, dest_path=temp_archive, expected_sha256=expected_sha256)
    try:
        _extract_archive_binary(
            archive_path=temp_archive,
            target_path=target,
            exe_name=platform.exe_name,
        )
    finally:
        try:
            temp_archive.unlink()
        except Exception:
            pass

    if platform.os != "windows":
        try:
            st_mode = target.stat().st_mode
            os.chmod(target, st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass

    return target


def _download_base_url() -> str:
    override = str(os.getenv(ENV_ENGINE_DOWNLOAD_BASE_URL) or "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_DOWNLOAD_BASE_URL.rstrip("/")


def _resolve_release_urls(*, version: str, asset_name: str) -> tuple[str, str]:
    base = _download_base_url()
    normalized_version = str(version or MEDIAMTX_VERSION).strip() or MEDIAMTX_VERSION
    checksums_url = f"{base}/{normalized_version}/checksums.sha256"
    asset_url = f"{base}/{normalized_version}/{asset_name}"
    return checksums_url, asset_url


def _resolve_release_asset_name(*, platform: MediaMTXPlatform, version: str) -> str:
    normalized_version = str(version or MEDIAMTX_VERSION).strip() or MEDIAMTX_VERSION
    os_part = str(platform.os).strip().lower()
    arch_part = "amd64" if platform.arch == "x64" else "arm64"
    if os_part == "windows":
        return f"mediamtx_{normalized_version}_{os_part}_{arch_part}.zip"
    return f"mediamtx_{normalized_version}_{os_part}_{arch_part}.tar.gz"


def _fetch_expected_sha256(*, checksums_url: str, asset_name: str) -> str:
    text = _download_text(checksums_url)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        sha256 = parts[0].strip()
        filename = parts[-1].lstrip("*").strip()
        if filename == asset_name:
            if len(sha256) != 64:
                raise RuntimeError(f"Invalid SHA256 in checksums file for {asset_name}: {sha256!r}")
            return sha256.lower()
    raise RuntimeError(f"SHA256 for asset not found in checksums file: {asset_name}")


def _download_text(url: str) -> str:
    payload = _download_bytes(url)
    return payload.decode("utf-8", errors="replace")


def _download_bytes(url: str) -> bytes:
    req = urllib_request.Request(
        url=str(url),
        headers={
            "user-agent": "toposync-streaming/0.1",
            "accept": "*/*",
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(req, timeout=30.0) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Failed to download {url} (HTTP {exc.code}): {body}".strip()) from exc
    except urllib_error.URLError as exc:
        reason = str(getattr(exc, "reason", "") or exc)
        raise RuntimeError(f"Failed to download {url}: {reason}") from exc


def _download_asset(*, url: str, dest_path: Path, expected_sha256: str) -> None:
    dest_path = Path(dest_path)
    hasher = hashlib.sha256()
    req = urllib_request.Request(
        url=str(url),
        headers={
            "user-agent": "toposync-streaming/0.1",
            "accept": "*/*",
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(req, timeout=60.0) as response, dest_path.open("wb") as writer:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                writer.write(chunk)
                hasher.update(chunk)
    except Exception:
        try:
            dest_path.unlink()
        except Exception:
            pass
        raise

    digest = hasher.hexdigest().lower()
    if digest != str(expected_sha256 or "").strip().lower():
        try:
            dest_path.unlink()
        except Exception:
            pass
        raise RuntimeError(f"MediaMTX download SHA256 mismatch: expected {expected_sha256}, got {digest}")


def _extract_archive_binary(*, archive_path: Path, target_path: Path, exe_name: str) -> None:
    archive_path = Path(archive_path)
    target_path = Path(target_path)
    tmp_target = target_path.parent / f".{target_path.name}.tmp"

    if str(archive_path).lower().endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(archive_path) as zf:
            members = [name for name in zf.namelist() if Path(name).name == exe_name and not name.endswith("/")]
            if not members:
                raise RuntimeError(f"MediaMTX archive does not contain {exe_name}: {archive_path.name}")
            member = members[0]
            with zf.open(member, "r") as reader, tmp_target.open("wb") as writer:
                shutil.copyfileobj(reader, writer)
        os.replace(tmp_target, target_path)
        return

    with tarfile.open(archive_path, mode="r:*") as tf:
        member = None
        for entry in tf.getmembers():
            if not entry.isfile():
                continue
            if Path(entry.name).name == exe_name:
                member = entry
                break
        if member is None:
            raise RuntimeError(f"MediaMTX archive does not contain {exe_name}: {archive_path.name}")
        fileobj = tf.extractfile(member)
        if fileobj is None:
            raise RuntimeError(f"Failed to extract {exe_name} from archive: {archive_path.name}")
        with fileobj, tmp_target.open("wb") as writer:
            shutil.copyfileobj(fileobj, writer)
    os.replace(tmp_target, target_path)
