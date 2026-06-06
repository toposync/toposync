#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


HOST = "127.0.0.1"
DEFAULT_IMAGE = "ghcr.io/toposync/toposync:0.7.7"


def _run(
    command: list[str | Path],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    printable = [str(part) for part in command]
    print(f"+ {shlex.join(printable)}", flush=True)
    return subprocess.run(
        printable,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _read_json(url: str, *, timeout: float = 5.0) -> object:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _read_text(url: str, *, timeout: float = 5.0) -> str:
    request = urllib.request.Request(url, headers={"accept": "text/html, text/plain;q=0.9, */*;q=0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, *, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = "server did not become healthy"
    while time.monotonic() < deadline:
        try:
            payload = _read_json(f"{base_url}/api/health", timeout=3.0)
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return
            last_error = f"unexpected health payload: {payload!r}"
        except (
            ConnectionError,
            TimeoutError,
            socket.timeout,
            urllib.error.URLError,
            json.JSONDecodeError,
            http.client.HTTPException,
        ) as exc:
            last_error = str(exc)
        time.sleep(2.0)
    raise RuntimeError(f"Timed out waiting for {base_url}/api/health: {last_error}")


def _assert_frontend(base_url: str) -> None:
    html = _read_text(f"{base_url}/")
    if "<script" not in html and "main.js" not in html:
        raise RuntimeError("Frontend host did not return the bundled application shell")


def _assert_extensions(base_url: str) -> None:
    payload = _read_json(f"{base_url}/api/extensions")
    raw = json.dumps(payload)
    for extension_id in (
        "com.toposync.cameras",
        "com.toposync.vision",
        "com.toposync.home_assistant",
        "com.toposync.streaming",
    ):
        if extension_id not in raw:
            raise RuntimeError(f"/api/extensions does not include {extension_id}")


def _assert_binary(container_id: str, binary_name: str, *, required: bool = True) -> None:
    result = _run(
        ["docker", "exec", container_id, "sh", "-lc", f"command -v {shlex.quote(binary_name)}"],
        capture=True,
        check=False,
    )
    status = "found" if result.returncode == 0 else "missing"
    print(f"{binary_name}: {status}", flush=True)
    if required and result.returncode != 0:
        raise RuntimeError(f"Container is missing required binary: {binary_name}")


def _assert_python_distribution(container_id: str, distribution_name: str) -> None:
    script = (
        "import importlib.metadata as metadata; "
        f"print('{distribution_name}=' + metadata.version('{distribution_name}'))"
    )
    _run(["docker", "exec", container_id, "python", "-c", script])


def _assert_import(container_id: str, module_name: str) -> None:
    script = f"import {module_name}; print('{module_name}=ok')"
    _run(["docker", "exec", container_id, "python", "-c", script])


def _run_container(args: argparse.Namespace) -> None:
    port = _find_free_port()
    volume_name = f"toposync-registry-smoke-{uuid.uuid4().hex}"
    _run(["docker", "volume", "create", volume_name])
    try:
        container_id = _run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--platform",
                args.platform,
                "-e",
                "TOPOSYNC_AUTH_MODE=bypass",
                "-p",
                f"{HOST}:{port}:8000",
                "-v",
                f"{volume_name}:/data",
                args.image,
            ],
            capture=True,
        ).stdout.strip()
        try:
            base_url = f"http://{HOST}:{port}"
            print(f"Container URL: {base_url}", flush=True)
            _wait_for_health(base_url, timeout_s=args.timeout)
            _assert_frontend(base_url)
            _assert_extensions(base_url)
            for binary_name in args.required_binary:
                _assert_binary(container_id, binary_name)
            if args.expect_cuda:
                _assert_python_distribution(container_id, "toposync-vision-cuda")
                _assert_python_distribution(container_id, "toposync-ext-streaming")
            else:
                _assert_python_distribution(container_id, "toposync-streaming")
            for module_name in ("toposync", "toposync_ext_streaming.plugin", "toposync_ext_vision"):
                _assert_import(container_id, module_name)
        finally:
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
                    container_id,
                ],
                check=False,
            )
            subprocess.run(["docker", "logs", "--tail", "160", container_id], check=False)
            subprocess.run(["docker", "stop", container_id], check=False)
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume_name], check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a published Toposync container image.")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--expect-cuda", action="store_true")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--required-binary",
        action="append",
        default=["ffmpeg", "go2rtc", "cmake", "gcc", "g++", "make", "ninja", "git", "pkg-config"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.skip_pull:
        _run(["docker", "pull", "--platform", args.platform, args.image])
    _run_container(args)
    print("Docker registry image smoke passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
