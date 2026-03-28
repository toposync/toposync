from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

from . import FFMPEG_VERSION
from .platform import MediaMTXPlatform, detect_ffmpeg_platform


PACKAGE_NAME: Final[str] = "toposync_ext_streaming"
ENV_FFMPEG_PATH: Final[str] = "TOPOSYNC_STREAMING_FFMPEG_PATH"


@dataclass(frozen=True, slots=True)
class ResolvedFFmpegBinary:
    path: Path | None
    source: str | None
    error: str | None = None


def find_packaged_ffmpeg_binary(platform: MediaMTXPlatform) -> resources.abc.Traversable | None:
    root = resources.files(PACKAGE_NAME)
    path = root.joinpath("bin", "ffmpeg", platform.key, platform.exe_name)
    if not path.is_file():
        return None
    return path


def _resolve_ffmpeg_override_path(*, platform: MediaMTXPlatform) -> Path | None:
    raw = str(os.getenv(ENV_FFMPEG_PATH) or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_dir():
        candidate = candidate / platform.exe_name
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"{ENV_FFMPEG_PATH} points to a missing FFmpeg binary: {candidate}")
    return candidate


def packaged_ffmpeg_binary(platform: MediaMTXPlatform) -> resources.abc.Traversable:
    path = find_packaged_ffmpeg_binary(platform)
    if path is None:
        raise FileNotFoundError(f"Packaged FFmpeg binary not found: {platform.key}/{platform.exe_name}")
    return path


def extract_ffmpeg_binary(
    *,
    data_dir: Path,
    platform: MediaMTXPlatform,
    version: str = FFMPEG_VERSION,
) -> Path:
    """Extract the bundled FFmpeg binary into a writable directory.

    The packaged file can be read-only; the runtime needs a stable path and an executable bit.
    """
    runtime_dir = data_dir / "runtime" / "streaming" / "ffmpeg" / version / platform.key
    runtime_dir.mkdir(parents=True, exist_ok=True)

    source = packaged_ffmpeg_binary(platform)
    target = runtime_dir / platform.exe_name

    needs_copy = True
    if target.is_file():
        try:
            needs_copy = source.stat().st_size != target.stat().st_size
        except Exception:
            needs_copy = True

    if needs_copy:
        temp = runtime_dir / f".{platform.exe_name}.tmp"
        if temp.exists():
            try:
                temp.unlink()
            except Exception:
                pass
        with source.open("rb") as reader, temp.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
        os.replace(temp, target)

    if platform.os != "windows":
        try:
            st_mode = target.stat().st_mode
            os.chmod(target, st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass

    return target


def resolve_ffmpeg_binary(
    *,
    data_dir: Path,
    version: str = FFMPEG_VERSION,
) -> ResolvedFFmpegBinary:
    try:
        platform = detect_ffmpeg_platform()
    except Exception as exc:
        system_path = shutil.which("ffmpeg")
        if system_path:
            return ResolvedFFmpegBinary(path=Path(system_path), source="system")
        return ResolvedFFmpegBinary(
            path=None,
            source=None,
            error=_missing_ffmpeg_error(platform_error=str(exc)),
        )

    try:
        override_path = _resolve_ffmpeg_override_path(platform=platform)
    except FileNotFoundError as exc:
        return ResolvedFFmpegBinary(path=None, source=None, error=str(exc))
    if override_path is not None:
        return ResolvedFFmpegBinary(path=override_path, source="override")

    system_path = shutil.which("ffmpeg")
    if system_path:
        return ResolvedFFmpegBinary(path=Path(system_path), source="system")

    packaged = find_packaged_ffmpeg_binary(platform)
    if packaged is None:
        return ResolvedFFmpegBinary(path=None, source=None, error=_missing_ffmpeg_error())

    try:
        extracted_path = extract_ffmpeg_binary(data_dir=data_dir, platform=platform, version=version)
    except Exception as exc:
        return ResolvedFFmpegBinary(path=None, source=None, error=_missing_ffmpeg_error(packaged_error=str(exc)))
    if not extracted_path.is_file():
        return ResolvedFFmpegBinary(
            path=None,
            source=None,
            error=_missing_ffmpeg_error(packaged_error="packaged FFmpeg binary is not a file after extraction"),
        )
    return ResolvedFFmpegBinary(path=extracted_path, source="packaged")


def _missing_ffmpeg_error(*, packaged_error: str | None = None, platform_error: str | None = None) -> str:
    message = (
        f"ffmpeg executable not found. Install FFmpeg and ensure it is available in PATH, "
        f"or set {ENV_FFMPEG_PATH} to the binary path."
    )
    if platform_error:
        return f"{message} platform_error={platform_error}"
    if packaged_error:
        return f"{message} packaged_error={packaged_error}"
    return message
