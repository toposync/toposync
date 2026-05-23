from __future__ import annotations

import platform as py_platform
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class MediaMTXPlatform:
    os: Literal["linux", "darwin", "windows"]
    arch: Literal["x64", "arm64"]
    key: str
    exe_name: str


def detect_mediamtx_platform() -> MediaMTXPlatform:
    os_key, arch_key = _detect_platform_tuple()
    key = f"{os_key}-{arch_key}"
    exe_name = "mediamtx.exe" if os_key == "windows" else "mediamtx"
    return MediaMTXPlatform(os=os_key, arch=arch_key, key=key, exe_name=exe_name)


def detect_ffmpeg_platform() -> MediaMTXPlatform:
    os_key, arch_key = _detect_platform_tuple()
    key = f"{os_key}-{arch_key}"
    exe_name = "ffmpeg.exe" if os_key == "windows" else "ffmpeg"
    return MediaMTXPlatform(os=os_key, arch=arch_key, key=key, exe_name=exe_name)


def detect_go2rtc_platform() -> MediaMTXPlatform:
    os_key, arch_key = _detect_platform_tuple()
    key = f"{os_key}-{arch_key}"
    exe_name = "go2rtc.exe" if os_key == "windows" else "go2rtc"
    return MediaMTXPlatform(os=os_key, arch=arch_key, key=key, exe_name=exe_name)


def _detect_platform_tuple() -> tuple[Literal["linux", "darwin", "windows"], Literal["x64", "arm64"]]:
    system = py_platform.system().strip().lower()
    machine = py_platform.machine().strip().lower()

    if system.startswith("linux"):
        os_key: Literal["linux", "darwin", "windows"] = "linux"
    elif system.startswith("darwin"):
        os_key = "darwin"
    elif system.startswith("windows"):
        os_key = "windows"
    else:
        raise RuntimeError(f"Unsupported platform.system(): {system!r}")

    if machine in {"x86_64", "amd64"}:
        arch_key: Literal["x64", "arm64"] = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch_key = "arm64"
    else:
        raise RuntimeError(f"Unsupported platform.machine(): {machine!r}")

    return os_key, arch_key
