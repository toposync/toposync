from __future__ import annotations

import argparse
import os
import re
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


DEFAULT_SIZE = "1280x720"
DEFAULT_FPS = 15


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


def add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_path: str,
    default_data_dir: Path,
) -> None:
    parser.add_argument(
        "--expose",
        choices=("local", "docker", "network"),
        default="local",
        help=(
            "How to expose the RTSP reader URL. Use local for host-only use, docker for Docker Desktop "
            "containers, or network for another host/container on the LAN."
        ),
    )
    parser.add_argument(
        "--host",
        help="Advertised host for --expose network. Defaults to the primary LAN address.",
    )
    parser.add_argument(
        "--port",
        type=positive_int,
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
        default=default_path,
        help="RTSP path slug. It is normalized to letters, numbers, dash, and underscore.",
    )
    parser.add_argument(
        "--fps",
        type=positive_int,
        default=DEFAULT_FPS,
        help="Output frames per second.",
    )
    parser.add_argument(
        "--size",
        type=normalize_size,
        default=DEFAULT_SIZE,
        help="Output size, for example 1280x720. Use an even width and height.",
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
        help="Seconds to wait for a readable RTSP frame before failing startup.",
    )
    parser.add_argument(
        "--restart-delay",
        type=positive_int,
        default=3,
        help="Seconds to wait before restarting if the FFmpeg publisher exits.",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Exit if the FFmpeg publisher stops instead of retrying.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir,
        help="Runtime directory for generated MediaMTX config and logs.",
    )


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def serve_rtsp_sample(
    *,
    input_label: str,
    input_args: list[str],
    args: argparse.Namespace,
) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    size = str(args.size)
    fps = int(args.fps)

    ffmpeg = resolve_ffmpeg_binary(data_dir=data_dir)
    if ffmpeg.path is None:
        print(ffmpeg.error or "FFmpeg was not found.", file=sys.stderr)
        return 2

    bind_host = bind_host_for_exposure(str(args.expose))
    try:
        rtsp_port = choose_rtsp_port(bind_host=bind_host, preferred=int(args.port), strict=bool(args.strict_port))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    path_slug = normalize_path_slug(args.path, fallback="toposync-sample")
    publish_url = f"rtsp://127.0.0.1:{rtsp_port}/{path_slug}"
    public_url = f"rtsp://{advertised_host_for_exposure(str(args.expose), args.host)}:{rtsp_port}/{path_slug}"

    runtime_dir = data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / f"mediamtx-{path_slug}.yml"
    log_path = runtime_dir / f"rtsp-{path_slug}.log"
    config_path.write_text(render_mediamtx_config(bind_host=bind_host, port=rtsp_port, path=path_slug), encoding="utf-8")

    try:
        mediamtx_path = extract_mediamtx_binary(
            data_dir=data_dir,
            platform=detect_mediamtx_platform(),
            version=MEDIAMTX_VERSION,
        )
    except Exception as exc:
        print(f"Failed to prepare MediaMTX: {exc}", file=sys.stderr)
        return 2

    ffmpeg_args = build_ffmpeg_args(
        ffmpeg_path=Path(ffmpeg.path),
        input_args=input_args,
        fps=fps,
        size=size,
        encoder=str(args.encoder),
        preset=str(args.preset),
        loglevel=str(args.ffmpeg_loglevel),
        publish_url=publish_url,
    )
    print_startup_summary(
        public_url=public_url,
        input_label=input_label,
        expose=str(args.expose),
        ffmpeg_path=Path(ffmpeg.path),
        ffmpeg_source=ffmpeg.source or "unknown",
        size=size,
        fps=fps,
    )

    try:
        return run_processes(
            mediamtx_path=Path(mediamtx_path),
            config_path=config_path,
            log_path=log_path,
            rtsp_port=rtsp_port,
            ffmpeg_path=Path(ffmpeg.path),
            verify_url=publish_url,
            ffmpeg_candidate=CaptureCandidate(label=input_label, args=ffmpeg_args),
            startup_timeout_s=float(args.startup_timeout),
            restart_delay_s=float(args.restart_delay),
            restart=not bool(args.no_restart),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def normalize_size(value: str) -> str:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(?P<width>\d+)x(?P<height>\d+)", text)
    if match is None:
        raise argparse.ArgumentTypeError(f"expected size like 1280x720, got {value!r}")
    width = int(match.group("width"))
    height = int(match.group("height"))
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError(f"expected positive even dimensions, got {value!r}")
    return f"{width}x{height}"


def bind_host_for_exposure(expose: str) -> str:
    if expose == "local":
        return "127.0.0.1"
    return "0.0.0.0"


def advertised_host_for_exposure(expose: str, host: str | None) -> str:
    if expose == "local":
        return "127.0.0.1"
    if expose == "docker":
        return "host.docker.internal"
    override = str(host or "").strip()
    if override:
        return override
    return primary_lan_address()


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
    override = os.getenv("TOPOSYNC_RTSP_SAMPLE_HOST", "").strip()
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


def build_ffmpeg_args(
    *,
    ffmpeg_path: Path,
    input_args: list[str],
    fps: int,
    size: str,
    encoder: str,
    preset: str,
    loglevel: str,
    publish_url: str,
) -> list[str]:
    width, height = size.split("x", 1)
    video_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,format=yuv420p"
    args = [str(ffmpeg_path), "-hide_banner", "-loglevel", loglevel, "-nostats"]
    args.extend(input_args)
    args.extend(["-vf", video_filter, "-an", "-sn", "-dn", "-c:v", encoder])
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


def print_startup_summary(
    *,
    public_url: str,
    input_label: str,
    expose: str,
    ffmpeg_path: Path,
    ffmpeg_source: str,
    size: str,
    fps: int,
) -> None:
    print(f"FFmpeg: {ffmpeg_path} ({ffmpeg_source})")
    print(f"Source: {input_label}")
    print(f"Output: H.264 RTSP, {size}, {fps} fps")
    print(f"RTSP URL for Toposync: {public_url}")
    if expose == "docker":
        print("Exposure: Docker Desktop. Use the printed host.docker.internal URL inside Toposync running in Docker.")
    elif expose == "network":
        print("Exposure: local network. Use only on a trusted development LAN.")
    else:
        print("Exposure: host only. Use this when Toposync runs directly on this machine.")
    print("")
    print("Toposync UI path:")
    print("  Settings -> Cameras -> Add manual -> Add RTSP")
    print("  Paste the RTSP URL, run Probe/Snapshot, enable Transmit this source, then open Live cameras.")
    print("")
    print("To test with FFmpeg:")
    print(f"  ffplay -rtsp_transport tcp \"{public_url}\"")
    print("")
    print("Press Ctrl-C to stop.")
    print("")


def run_processes(
    *,
    mediamtx_path: Path,
    config_path: Path,
    log_path: Path,
    rtsp_port: int,
    ffmpeg_path: Path,
    verify_url: str,
    ffmpeg_candidate: CaptureCandidate,
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

            tail = TailBuffer()
            print(f"Starting FFmpeg publisher with {ffmpeg_candidate.label}.")
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_candidate.args,
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
                if not restart:
                    if detail:
                        print(f"FFmpeg publisher failed: {detail}", file=sys.stderr)
                    return 1
                if detail:
                    print(f"FFmpeg publisher failed: {detail}", file=sys.stderr)
                print(f"Retrying in {restart_delay_s:g}s.", file=sys.stderr)
                sleep_with_stop(restart_delay_s, stop_event)
                continue

            print(f"RTSP stream is readable with {ffmpeg_candidate.label}.")
            return_code = wait_for_process(ffmpeg_proc, stop_event=stop_event, mediamtx_proc=mediamtx_proc)
            ffmpeg_proc = None
            if stop_event.is_set():
                return 130
            if not restart:
                return int(return_code)
            print(f"FFmpeg publisher exited with code {return_code}; retrying in {restart_delay_s:g}s.")
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
