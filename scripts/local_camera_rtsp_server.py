#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMING_SRC = REPO_ROOT / "extensions" / "streaming" / "src"
if str(STREAMING_SRC) not in sys.path:
    sys.path.insert(0, str(STREAMING_SRC))

from toposync_ext_streaming.streaming import MEDIAMTX_VERSION  # noqa: E402
from toposync_ext_streaming.streaming.ffmpeg_binary import resolve_ffmpeg_binary  # noqa: E402
from toposync_ext_streaming.streaming.mediamtx_binary import extract_mediamtx_binary  # noqa: E402
from toposync_ext_streaming.streaming.mediamtx_config import normalize_path_slug  # noqa: E402
from toposync_ext_streaming.streaming.platform import detect_mediamtx_platform  # noqa: E402


DEFAULT_DATA_DIR = REPO_ROOT / ".toposync-data" / "local-camera-rtsp"


@dataclass(frozen=True, slots=True)
class VideoDevice:
    display_index: int
    identifier: str
    name: str
    backend: str


@dataclass(frozen=True, slots=True)
class CaptureCandidate:
    label: str
    args: list[str]


class TailBuffer:
    def __init__(self, *, max_lines: int = 80) -> None:
        self._max_lines = max(1, int(max_lines))
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        text = str(line or "").rstrip()
        if not text:
            return
        with self._lock:
            self._lines.append(text)
            if len(self._lines) > self._max_lines:
                del self._lines[: len(self._lines) - self._max_lines]

    def tail(self, *, max_lines: int = 12) -> str:
        with self._lock:
            lines = self._lines[-max(1, int(max_lines)) :]
        return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose a local camera as an RTSP stream for Toposync development.",
        epilog=(
            "Examples:\n"
            "  npm run dev:camera-rtsp -- --list-only\n"
            "  npm run dev:camera-rtsp\n"
            "  npm run dev:camera-rtsp -- --camera 1\n"
            "  npm run dev:camera-rtsp -- --camera 0 --expose network\n"
            "  npm run dev:camera-rtsp -- --camera \"FaceTime\" --size 1280x720\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--camera",
        help=(
            "Camera selector. Use the printed index, backend identifier, exact name, or unique name "
            "fragment. Defaults to the first detected camera."
        ),
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only print detected cameras and exit.",
    )
    parser.add_argument(
        "--expose",
        choices=("local", "network"),
        default="local",
        help="Bind RTSP only to localhost or expose it to the local network.",
    )
    parser.add_argument(
        "--host",
        help="Advertised host for --expose network. Defaults to the primary LAN address.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8554,
        help="Preferred RTSP port. If busy, the script chooses the next free port unless --strict-port is set.",
    )
    parser.add_argument(
        "--strict-port",
        action="store_true",
        help="Fail instead of choosing another RTSP port when --port is busy.",
    )
    parser.add_argument(
        "--path",
        default="local-camera",
        help="RTSP path slug. It is normalized to letters, numbers, dash, and underscore.",
    )
    parser.add_argument(
        "--fps",
        type=positive_int,
        default=30,
        help="Capture and output frames per second.",
    )
    parser.add_argument(
        "--size",
        help="Optional capture size, for example 1280x720. Omit it to let FFmpeg pick the device default.",
    )
    parser.add_argument(
        "--encoder",
        default="libx264",
        help="FFmpeg video encoder to use. Common values: libx264, h264_videotoolbox.",
    )
    parser.add_argument(
        "--preset",
        default="ultrafast",
        help="Preset used with libx264.",
    )
    parser.add_argument(
        "--ffmpeg-loglevel",
        default="info",
        choices=("quiet", "error", "warning", "info", "verbose", "debug"),
        help="FFmpeg log level.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=positive_int,
        default=12,
        help="Seconds to wait for a readable RTSP frame before trying the next capture mode.",
    )
    parser.add_argument(
        "--restart-delay",
        type=positive_int,
        default=3,
        help="Seconds to wait before retrying after the camera publisher exits.",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Exit if the FFmpeg publisher stops instead of retrying.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Runtime directory for generated MediaMTX config and logs.",
    )
    return parser.parse_args()


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    ffmpeg = resolve_ffmpeg_binary(data_dir=data_dir)
    if ffmpeg.path is None:
        print(ffmpeg.error or "FFmpeg was not found.", file=sys.stderr)
        return 2

    devices = discover_video_devices(Path(ffmpeg.path))
    print_devices(devices, ffmpeg_path=Path(ffmpeg.path), ffmpeg_source=ffmpeg.source or "unknown")

    if args.list_only:
        return 0

    try:
        device = select_device(devices, args.camera)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    bind_host = "127.0.0.1" if args.expose == "local" else "0.0.0.0"
    try:
        rtsp_port = choose_rtsp_port(bind_host=bind_host, preferred=args.port, strict=args.strict_port)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path_slug = normalize_path_slug(args.path, fallback="local-camera")
    publish_url = f"rtsp://127.0.0.1:{rtsp_port}/{path_slug}"
    advertised_host = "127.0.0.1" if args.expose == "local" else (args.host or primary_lan_address())
    public_url = f"rtsp://{advertised_host}:{rtsp_port}/{path_slug}"

    runtime_dir = data_dir / "runtime" / "local-camera-rtsp"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / "mediamtx-local-camera.yml"
    log_path = runtime_dir / "mediamtx-local-camera.log"
    config_path.write_text(
        render_mediamtx_config(bind_host=bind_host, port=rtsp_port, path=path_slug),
        encoding="utf-8",
    )

    try:
        mediamtx_path = extract_mediamtx_binary(
            data_dir=data_dir,
            platform=detect_mediamtx_platform(),
            version=MEDIAMTX_VERSION,
        )
    except Exception as exc:
        print(f"Failed to prepare MediaMTX: {exc}", file=sys.stderr)
        return 2

    ffmpeg_candidates = build_ffmpeg_candidates(
        ffmpeg_path=Path(ffmpeg.path),
        device=device,
        fps=args.fps,
        size=args.size,
        encoder=args.encoder,
        preset=args.preset,
        loglevel=args.ffmpeg_loglevel,
        publish_url=publish_url,
    )

    print("")
    print(f"Selected camera: {device.display_index}: {device.name} [{device.backend}:{device.identifier}]")
    print(f"RTSP URL: {public_url}")
    print("Publish access is restricted to localhost; read access follows --expose.")
    if args.expose == "network":
        print("Network exposure has no viewer password. Use it only on a trusted development LAN.")
    print("")
    print("To test with FFmpeg:")
    print(f"  ffplay -rtsp_transport tcp \"{public_url}\"")
    print("")
    print("Press Ctrl-C to stop.")

    try:
        return run_processes(
            mediamtx_path=Path(mediamtx_path),
            config_path=config_path,
            log_path=log_path,
            rtsp_port=rtsp_port,
            ffmpeg_path=Path(ffmpeg.path),
            verify_url=publish_url,
            ffmpeg_candidates=ffmpeg_candidates,
            startup_timeout_s=float(args.startup_timeout),
            restart_delay_s=float(args.restart_delay),
            restart=not bool(args.no_restart),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def discover_video_devices(ffmpeg_path: Path) -> list[VideoDevice]:
    system = platform.system().strip().lower()
    if system == "darwin":
        return discover_avfoundation_devices(ffmpeg_path)
    if system == "windows":
        return discover_directshow_devices(ffmpeg_path)
    if system == "linux":
        return discover_v4l2_devices()
    return []


def discover_avfoundation_devices(ffmpeg_path: Path) -> list[VideoDevice]:
    proc = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    devices: list[VideoDevice] = []
    in_video_section = False
    for line in output.splitlines():
        if "AVFoundation video devices" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices" in line:
            in_video_section = False
            continue
        if not in_video_section:
            continue
        match = re.search(r"(?:^|\]\s*)\[(?P<index>\d+)\]\s+(?P<name>.+)$", line)
        if match is None:
            continue
        name = match.group("name").strip()
        devices.append(
            VideoDevice(
                display_index=len(devices),
                identifier=match.group("index").strip(),
                name=name,
                backend="avfoundation",
            )
        )
    return devices


def discover_directshow_devices(ffmpeg_path: Path) -> list[VideoDevice]:
    proc = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    devices: list[VideoDevice] = []
    in_video_section = False
    for line in output.splitlines():
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            in_video_section = False
            continue
        if not in_video_section or "Alternative name" in line:
            continue
        match = re.search(r'"(?P<name>.+?)"', line)
        if match is None:
            continue
        name = match.group("name").strip()
        devices.append(
            VideoDevice(
                display_index=len(devices),
                identifier=name,
                name=name,
                backend="dshow",
            )
        )
    return devices


def discover_v4l2_devices() -> list[VideoDevice]:
    v4l2_ctl = shutil.which("v4l2-ctl")
    if v4l2_ctl:
        devices = discover_v4l2_devices_with_v4l2_ctl(Path(v4l2_ctl))
        if devices:
            return devices

    paths = sorted(Path("/dev").glob("video*"), key=lambda item: item.name)
    devices: list[VideoDevice] = []
    for path in paths:
        if not str(path.name).startswith("video"):
            continue
        devices.append(
            VideoDevice(
                display_index=len(devices),
                identifier=str(path),
                name=str(path),
                backend="v4l2",
            )
        )
    return devices


def discover_v4l2_devices_with_v4l2_ctl(v4l2_ctl_path: Path) -> list[VideoDevice]:
    proc = subprocess.run(
        [str(v4l2_ctl_path), "--list-devices"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    devices: list[VideoDevice] = []
    current_name = ""
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            current_name = ""
            continue
        if not line.startswith((" ", "\t")):
            current_name = line.rstrip(":").strip()
            continue
        path = line.strip()
        if not path.startswith("/dev/video"):
            continue
        label = f"{current_name} ({path})" if current_name else path
        devices.append(
            VideoDevice(
                display_index=len(devices),
                identifier=path,
                name=label,
                backend="v4l2",
            )
        )
    return devices


def print_devices(devices: list[VideoDevice], *, ffmpeg_path: Path, ffmpeg_source: str) -> None:
    print(f"FFmpeg: {ffmpeg_path} ({ffmpeg_source})")
    print("Detected local cameras and video sources:")
    if not devices:
        print("  No video devices detected.")
        return
    for device in devices:
        print(f"  {device.display_index}: {device.name} [{device.backend}:{device.identifier}]")


def select_device(devices: list[VideoDevice], selector: str | None) -> VideoDevice:
    if not devices:
        raise ValueError("No video devices were detected.")

    raw = str(selector or "").strip()
    if not raw:
        return devices[0]

    if raw.isdigit():
        index = int(raw)
        for device in devices:
            if device.display_index == index:
                return device

    exact_identifier_matches = [
        device
        for device in devices
        if raw == device.identifier or raw == f"{device.backend}:{device.identifier}"
    ]
    if len(exact_identifier_matches) == 1:
        return exact_identifier_matches[0]

    folded = raw.casefold()
    exact_name_matches = [device for device in devices if device.name.casefold() == folded]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]

    partial_name_matches = [device for device in devices if folded in device.name.casefold()]
    if len(partial_name_matches) == 1:
        return partial_name_matches[0]
    if len(partial_name_matches) > 1:
        matches = ", ".join(f"{device.display_index}:{device.name}" for device in partial_name_matches)
        raise ValueError(f"Camera selector {raw!r} is ambiguous. Matches: {matches}")

    raise ValueError(f"Camera selector {raw!r} did not match any detected video device.")


def choose_rtsp_port(*, bind_host: str, preferred: int, strict: bool) -> int:
    preferred = int(preferred)
    if tcp_port_available(bind_host, preferred):
        return preferred
    if strict:
        raise RuntimeError(f"RTSP port {preferred} is already in use on {bind_host}.")

    for port in range(preferred + 1, min(65535, preferred + 100) + 1):
        if tcp_port_available(bind_host, port):
            print(f"RTSP port {preferred} is busy; using {port}.", file=sys.stderr)
            return port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])


def tcp_port_available(bind_host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, int(port)))
            return True
    except OSError:
        return False


def primary_lan_address() -> str:
    override = os.getenv("TOPOSYNC_LOCAL_CAMERA_RTSP_HOST", "").strip()
    if override:
        return override
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            address = str(sock.getsockname()[0]).strip()
            if address:
                return address
    except OSError:
        pass
    return socket.gethostbyname(socket.gethostname())


def render_mediamtx_config(*, bind_host: str, port: int, path: str) -> str:
    rtsp_address = f":{port}" if bind_host in {"0.0.0.0", ""} else f"{bind_host}:{port}"
    quoted_path = yaml_single_quote(path)
    return "\n".join(
        [
            "logLevel: info",
            "logDestinations: [stdout]",
            "",
            "authMethod: internal",
            "authInternalUsers:",
            "- user: 'any'",
            "  pass: ''",
            "  ips: ['127.0.0.1', '::1']",
            "  permissions:",
            "  - action: publish",
            f"    path: {quoted_path}",
            "- user: 'any'",
            "  pass: ''",
            "  ips: []",
            "  permissions:",
            "  - action: read",
            f"    path: {quoted_path}",
            "  - action: playback",
            f"    path: {quoted_path}",
            "",
            "api: false",
            "metrics: false",
            "pprof: false",
            "",
            "rtsp: true",
            f"rtspAddress: {rtsp_address}",
            "rtspTransports: [tcp]",
            "",
            "rtmp: false",
            "hls: false",
            "webrtc: false",
            "srt: false",
            "playback: false",
            "",
            "paths:",
            f"  {quoted_path}: {{}}",
            "",
        ]
    )


def yaml_single_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def build_ffmpeg_candidates(
    *,
    ffmpeg_path: Path,
    device: VideoDevice,
    fps: int,
    size: str | None,
    encoder: str,
    preset: str,
    loglevel: str,
    publish_url: str,
) -> list[CaptureCandidate]:
    candidates: list[CaptureCandidate] = []
    for label, input_args in build_ffmpeg_input_candidates(device=device, fps=fps, size=size):
        candidates.append(
            CaptureCandidate(
                label=label,
                args=build_ffmpeg_args(
                    ffmpeg_path=ffmpeg_path,
                    input_args=input_args,
                    fps=fps,
                    encoder=encoder,
                    preset=preset,
                    loglevel=loglevel,
                    publish_url=publish_url,
                ),
            )
        )
    return dedupe_capture_candidates(candidates)


def build_ffmpeg_args(
    *,
    ffmpeg_path: Path,
    input_args: list[str],
    fps: int,
    encoder: str,
    preset: str,
    loglevel: str,
    publish_url: str,
) -> list[str]:
    args = [str(ffmpeg_path), "-hide_banner", "-loglevel", loglevel, "-nostats"]
    args.extend(input_args)
    args.extend(["-an", "-sn", "-dn", "-c:v", encoder])
    if encoder == "libx264":
        args.extend(["-preset", preset, "-tune", "zerolatency", "-pix_fmt", "yuv420p"])
    elif encoder.startswith("h264_"):
        args.extend(["-pix_fmt", "yuv420p"])
    args.extend(
        [
            "-r",
            str(fps),
            "-g",
            str(fps),
            "-keyint_min",
            str(fps),
            "-sc_threshold",
            "0",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            publish_url,
        ]
    )
    return args


def build_ffmpeg_input_candidates(
    *,
    device: VideoDevice,
    fps: int,
    size: str | None,
) -> list[tuple[str, list[str]]]:
    if device.backend == "avfoundation":
        return build_avfoundation_input_candidates(device=device, fps=fps, size=size)

    if device.backend == "dshow":
        return build_directshow_input_candidates(device=device, fps=fps, size=size)

    if device.backend == "v4l2":
        return build_v4l2_input_candidates(device=device, fps=fps, size=size)

    raise ValueError(f"Unsupported FFmpeg device backend: {device.backend}")


def build_avfoundation_input_candidates(
    *,
    device: VideoDevice,
    fps: int,
    size: str | None,
) -> list[tuple[str, list[str]]]:
    target = f"{device.identifier}:none"
    requested_size = str(size or "").strip()
    fps_values = unique_strings([str(fps), "30", "15"])
    candidates: list[tuple[str, list[str]]] = []

    def add(label: str, *, candidate_fps: str | None, candidate_size: str | None, pixel_format: str | None) -> None:
        args = ["-f", "avfoundation"]
        if pixel_format:
            args.extend(["-pixel_format", pixel_format])
        if candidate_fps:
            args.extend(["-framerate", candidate_fps])
        if candidate_size:
            args.extend(["-video_size", candidate_size])
        args.extend(["-i", target])
        candidates.append((label, args))

    for candidate_fps in fps_values:
        add(
            f"avfoundation nv12 {candidate_fps}fps"
            + (f" {requested_size}" if requested_size else ""),
            candidate_fps=candidate_fps,
            candidate_size=requested_size or None,
            pixel_format="nv12",
        )
        add(
            f"avfoundation auto-pixel {candidate_fps}fps"
            + (f" {requested_size}" if requested_size else ""),
            candidate_fps=candidate_fps,
            candidate_size=requested_size or None,
            pixel_format=None,
        )

    if requested_size:
        add(
            f"avfoundation nv12 default-fps {requested_size}",
            candidate_fps=None,
            candidate_size=requested_size,
            pixel_format="nv12",
        )
        add(
            f"avfoundation auto-pixel default-fps {requested_size}",
            candidate_fps=None,
            candidate_size=requested_size,
            pixel_format=None,
        )

    add("avfoundation nv12 default-mode", candidate_fps=None, candidate_size=None, pixel_format="nv12")
    add("avfoundation default-mode", candidate_fps=None, candidate_size=None, pixel_format=None)
    return candidates


def build_directshow_input_candidates(
    *,
    device: VideoDevice,
    fps: int,
    size: str | None,
) -> list[tuple[str, list[str]]]:
    target = f"video={device.identifier}"
    requested_size = str(size or "").strip()
    candidates: list[tuple[str, list[str]]] = []

    def add(label: str, *, include_fps: bool, candidate_size: str | None) -> None:
        args = ["-f", "dshow"]
        if include_fps:
            args.extend(["-framerate", str(fps)])
        if candidate_size:
            args.extend(["-video_size", candidate_size])
        args.extend(["-i", target])
        candidates.append((label, args))

    add(
        f"dshow {fps}fps" + (f" {requested_size}" if requested_size else ""),
        include_fps=True,
        candidate_size=requested_size or None,
    )
    if requested_size:
        add(f"dshow default-fps {requested_size}", include_fps=False, candidate_size=requested_size)
    add(f"dshow {fps}fps default-size", include_fps=True, candidate_size=None)
    add("dshow default-mode", include_fps=False, candidate_size=None)
    return candidates


def build_v4l2_input_candidates(
    *,
    device: VideoDevice,
    fps: int,
    size: str | None,
) -> list[tuple[str, list[str]]]:
    requested_size = str(size or "").strip()
    candidates: list[tuple[str, list[str]]] = []

    def add(label: str, *, include_fps: bool, candidate_size: str | None) -> None:
        args = ["-f", "v4l2"]
        if include_fps:
            args.extend(["-framerate", str(fps)])
        if candidate_size:
            args.extend(["-video_size", candidate_size])
        args.extend(["-i", device.identifier])
        candidates.append((label, args))

    add(
        f"v4l2 {fps}fps" + (f" {requested_size}" if requested_size else ""),
        include_fps=True,
        candidate_size=requested_size or None,
    )
    if requested_size:
        add(f"v4l2 default-fps {requested_size}", include_fps=False, candidate_size=requested_size)
    add(f"v4l2 {fps}fps default-size", include_fps=True, candidate_size=None)
    add("v4l2 default-mode", include_fps=False, candidate_size=None)
    return candidates


def dedupe_capture_candidates(candidates: list[CaptureCandidate]) -> list[CaptureCandidate]:
    result: list[CaptureCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate.args)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def run_processes(
    *,
    mediamtx_path: Path,
    config_path: Path,
    log_path: Path,
    rtsp_port: int,
    ffmpeg_path: Path,
    verify_url: str,
    ffmpeg_candidates: list[CaptureCandidate],
    startup_timeout_s: float,
    restart_delay_s: float,
    restart: bool,
) -> int:
    mediamtx_proc: subprocess.Popen[str] | None = None
    ffmpeg_proc: subprocess.Popen[str] | None = None
    stop_event = threading.Event()

    def stop(signum: int | None = None, frame: object | None = None) -> None:
        _ = signum, frame
        stop_event.set()
        terminate_process(ffmpeg_proc)
        terminate_process(mediamtx_proc)

    previous_sigint = signal.signal(signal.SIGINT, stop)
    previous_sigterm = signal.signal(signal.SIGTERM, stop)
    try:
        mediamtx_proc = subprocess.Popen(
            [str(mediamtx_path), str(config_path)],
            cwd=str(config_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_log_thread("mediamtx", mediamtx_proc, log_path)
        wait_for_rtsp_port(port=rtsp_port, process=mediamtx_proc)

        while not stop_event.is_set():
            if mediamtx_proc.poll() is not None:
                raise RuntimeError(f"MediaMTX exited unexpectedly with code {mediamtx_proc.returncode}.")

            started = False
            for candidate in ffmpeg_candidates:
                if stop_event.is_set():
                    break
                tail = TailBuffer()
                print(f"Starting FFmpeg publisher with {candidate.label}.")
                ffmpeg_proc = subprocess.Popen(
                    candidate.args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                start_log_thread("ffmpeg", ffmpeg_proc, log_path, tail=tail)
                ready, detail = wait_for_rtsp_frame(
                    ffmpeg_path=ffmpeg_path,
                    url=verify_url,
                    process=ffmpeg_proc,
                    tail=tail,
                    timeout_s=startup_timeout_s,
                    stop_event=stop_event,
                )
                if not ready:
                    terminate_process(ffmpeg_proc)
                    ffmpeg_proc = None
                    if detail:
                        print(f"FFmpeg capture mode failed ({candidate.label}): {detail}", file=sys.stderr)
                    continue

                print(f"RTSP stream is readable with {candidate.label}.")
                started = True
                return_code = wait_for_process(
                    ffmpeg_proc,
                    stop_event=stop_event,
                    mediamtx_proc=mediamtx_proc,
                )
                ffmpeg_proc = None
                if stop_event.is_set():
                    return 130
                if not restart:
                    return int(return_code)
                print(f"FFmpeg publisher exited with code {return_code}; retrying in {restart_delay_s:g}s.")
                sleep_with_stop(restart_delay_s, stop_event)
                break

            if stop_event.is_set():
                break
            if not started:
                if not restart:
                    return 1
                print(
                    f"All FFmpeg capture modes failed; retrying in {restart_delay_s:g}s.",
                    file=sys.stderr,
                )
                sleep_with_stop(restart_delay_s, stop_event)

        return 130
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        terminate_process(ffmpeg_proc)
        terminate_process(mediamtx_proc)


def start_log_thread(
    prefix: str,
    proc: subprocess.Popen[str],
    log_path: Path,
    *,
    tail: TailBuffer | None = None,
) -> None:
    def loop() -> None:
        stream = proc.stdout
        if stream is None:
            return
        log_file = log_path.open("a", encoding="utf-8")
        try:
            for line in stream:
                text = line.rstrip()
                if text:
                    if tail is not None:
                        tail.append(text)
                    print(f"[{prefix}] {text}")
                    print(text, file=log_file, flush=True)
        finally:
            try:
                log_file.close()
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()


def wait_for_rtsp_frame(
    *,
    ffmpeg_path: Path,
    url: str,
    process: subprocess.Popen[str],
    tail: TailBuffer,
    timeout_s: float,
    stop_event: threading.Event,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    last_error = ""
    while time.monotonic() < deadline and not stop_event.is_set():
        if process.poll() is not None:
            detail = tail.tail()
            if detail:
                return False, detail
            return False, f"publisher exited with code {process.returncode}"
        ok, error = probe_rtsp_frame(ffmpeg_path=ffmpeg_path, url=url)
        if ok:
            return True, ""
        last_error = error
        sleep_with_stop(0.5, stop_event)

    detail = tail.tail()
    if detail:
        return False, detail
    if last_error:
        return False, last_error
    return False, "timed out waiting for a readable RTSP frame"


def probe_rtsp_frame(*, ffmpeg_path: Path, url: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [
                str(ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "error",
                "-timeout",
                "2000000",
                "-rtsp_transport",
                "tcp",
                "-i",
                str(url),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "RTSP frame probe timed out"
    if proc.returncode == 0:
        return True, ""
    error = (proc.stderr or proc.stdout or "").strip()
    return False, error


def wait_for_process(
    proc: subprocess.Popen[str],
    *,
    stop_event: threading.Event,
    mediamtx_proc: subprocess.Popen[str],
) -> int:
    while not stop_event.is_set():
        return_code = proc.poll()
        if return_code is not None:
            return int(return_code)
        if mediamtx_proc.poll() is not None:
            terminate_process(proc)
            raise RuntimeError(f"MediaMTX exited unexpectedly with code {mediamtx_proc.returncode}.")
        sleep_with_stop(0.5, stop_event)
    terminate_process(proc)
    return 130


def sleep_with_stop(seconds: float, stop_event: threading.Event) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline and not stop_event.is_set():
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def wait_for_rtsp_port(*, port: int, process: subprocess.Popen[str], timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MediaMTX exited during startup with code {process.returncode}.")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for MediaMTX on RTSP port {port}.")


def terminate_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=4)


if __name__ == "__main__":
    raise SystemExit(main())
