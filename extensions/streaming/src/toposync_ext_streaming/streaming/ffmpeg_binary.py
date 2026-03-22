from __future__ import annotations

import os
import shutil
import stat
from importlib import resources
from pathlib import Path
from typing import Final

from . import FFMPEG_VERSION
from .platform import MediaMTXPlatform


PACKAGE_NAME: Final[str] = "toposync_ext_streaming"


def packaged_ffmpeg_binary(platform: MediaMTXPlatform) -> resources.abc.Traversable:
    root = resources.files(PACKAGE_NAME)
    path = root.joinpath("bin", "ffmpeg", platform.key, platform.exe_name)
    if not path.is_file():
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
