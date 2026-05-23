from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Final
from urllib import error as urllib_error
from urllib import request as urllib_request

from . import GO2RTC_VERSION
from .platform import MediaMTXPlatform


DEFAULT_GO2RTC_DOWNLOAD_BASE_URL: Final[str] = "https://github.com/AlexxIT/go2rtc/releases/download"
ENV_GO2RTC_PATH: Final[str] = "TOPOSYNC_STREAMING_GO2RTC_PATH"
ENV_GO2RTC_CACHE_DIR: Final[str] = "TOPOSYNC_STREAMING_GO2RTC_CACHE_DIR"
ENV_GO2RTC_DOWNLOAD_BASE_URL: Final[str] = "TOPOSYNC_STREAMING_GO2RTC_DOWNLOAD_BASE_URL"


def _runtime_root_dir() -> Path:
    override = str(os.getenv(ENV_GO2RTC_CACHE_DIR) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".toposync" / "runtime").resolve()


def _install_dir(*, platform: MediaMTXPlatform, version: str) -> Path:
    return _runtime_root_dir() / "streaming" / "go2rtc" / str(version or GO2RTC_VERSION) / platform.key


def expected_go2rtc_binary_path(*, platform: MediaMTXPlatform, version: str = GO2RTC_VERSION) -> Path:
    return _install_dir(platform=platform, version=version) / platform.exe_name


def _resolve_go2rtc_override_path(*, platform: MediaMTXPlatform) -> Path | None:
    raw = str(os.getenv(ENV_GO2RTC_PATH) or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_dir():
        candidate = candidate / platform.exe_name
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"{ENV_GO2RTC_PATH} points to a missing go2rtc binary: {candidate}")
    return candidate


def find_installed_go2rtc_binary(
    *,
    platform: MediaMTXPlatform,
    version: str = GO2RTC_VERSION,
) -> Path | None:
    override = _resolve_go2rtc_override_path(platform=platform)
    if override is not None:
        return override
    expected = expected_go2rtc_binary_path(platform=platform, version=version)
    if expected.is_file():
        return expected
    return None


def extract_go2rtc_binary(
    *,
    data_dir: Path,
    platform: MediaMTXPlatform,
    version: str = GO2RTC_VERSION,
) -> Path:
    _ = data_dir
    override = _resolve_go2rtc_override_path(platform=platform)
    if override is not None:
        return override

    runtime_dir = _install_dir(platform=platform, version=version)
    target = runtime_dir / platform.exe_name
    if target.is_file():
        return target

    runtime_dir.mkdir(parents=True, exist_ok=True)
    asset_name = _resolve_release_asset_name(platform=platform)
    asset_url = f"{_download_base_url()}/{str(version or GO2RTC_VERSION).strip() or GO2RTC_VERSION}/{asset_name}"
    temp_path = runtime_dir / f".download-{asset_name}"
    if temp_path.exists():
        try:
            temp_path.unlink()
        except Exception:
            pass
    _download_asset(url=asset_url, dest_path=temp_path)
    try:
        if asset_name.lower().endswith(".zip"):
            _extract_zip_binary(archive_path=temp_path, target_path=target, exe_name=platform.exe_name)
        else:
            os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
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
    override = str(os.getenv(ENV_GO2RTC_DOWNLOAD_BASE_URL) or "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_GO2RTC_DOWNLOAD_BASE_URL.rstrip("/")


def _resolve_release_asset_name(*, platform: MediaMTXPlatform) -> str:
    arch = "amd64" if platform.arch == "x64" else "arm64"
    if platform.os == "darwin":
        return f"go2rtc_mac_{arch}.zip"
    if platform.os == "windows":
        return "go2rtc_win64.zip" if platform.arch == "x64" else "go2rtc_win_arm64.zip"
    return f"go2rtc_linux_{arch}"


def _download_asset(*, url: str, dest_path: Path) -> None:
    req = urllib_request.Request(
        url=str(url),
        headers={"user-agent": "toposync-streaming/0.1", "accept": "*/*"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(req, timeout=60.0) as response, Path(dest_path).open("wb") as writer:
            shutil.copyfileobj(response, writer)
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
    except Exception:
        try:
            Path(dest_path).unlink()
        except Exception:
            pass
        raise


def _extract_zip_binary(*, archive_path: Path, target_path: Path, exe_name: str) -> None:
    archive_path = Path(archive_path)
    target_path = Path(target_path)
    tmp_target = target_path.parent / f".{target_path.name}.tmp"
    with zipfile.ZipFile(archive_path) as zf:
        members = [name for name in zf.namelist() if Path(name).name == exe_name and not name.endswith("/")]
        if not members:
            raise RuntimeError(f"go2rtc archive does not contain {exe_name}: {archive_path.name}")
        with zf.open(members[0], "r") as reader, tmp_target.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
    os.replace(tmp_target, target_path)
