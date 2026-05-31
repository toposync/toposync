from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, cwd: Path = ROOT, capture: bool = False) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"+ {printable}", flush=True)
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _url_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def _url_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def _wait_for_health(base_url: str, *, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = _url_json(f"{base_url}/api/health")
            if isinstance(payload, dict):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2.0)
    raise RuntimeError(f"Timed out waiting for {base_url}/api/health: {last_error}")


def _container_base_url(container_id: str) -> str:
    output = _run(["docker", "port", container_id, "8000/tcp"], capture=True).stdout.strip()
    if not output:
        raise RuntimeError("Docker did not report a mapped 8000/tcp port")
    host, port = output.rsplit(":", 1)
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _assert_frontend(base_url: str) -> None:
    html = _url_text(f"{base_url}/")
    if "<script" not in html and "main.js" not in html:
        raise RuntimeError("Frontend host did not return the bundled application shell")


def _assert_extensions(base_url: str) -> None:
    payload = _url_json(f"{base_url}/api/extensions")
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("/api/extensions did not return JSON")
    raw = json.dumps(payload)
    for extension_id in ("com.toposync.cameras", "com.toposync.vision", "com.toposync.home_assistant"):
        if extension_id not in raw:
            raise RuntimeError(f"/api/extensions does not include {extension_id}")


def _assert_binary(container_id: str, binary_name: str, *, required: bool) -> None:
    result = subprocess.run(
        ["docker", "exec", container_id, "sh", "-lc", f"command -v {binary_name}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if required and result.returncode != 0:
        raise RuntimeError(f"Container is missing required binary: {binary_name}")
    status = "found" if result.returncode == 0 else "missing"
    print(f"{binary_name}: {status}", flush=True)


def _pip_dry_run(package_spec: str, platform: str) -> None:
    command = (
        "python -m pip install --upgrade pip >/dev/null && "
        f"python -m pip install --dry-run {package_spec}"
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            "python:3.12-slim-bookworm",
            "sh",
            "-lc",
            command,
        ]
    )


def _build_image(args: argparse.Namespace) -> None:
    build_args = [
        "--build-arg",
        f"TOPOSYNC_INSTALL_WHEEL={args.install_wheel}",
        "--build-arg",
        f"TOPOSYNC_APT_PACKAGES={args.apt_packages}",
    ]
    _run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            args.platform,
            "--target",
            "runtime-cpu",
            "-t",
            args.image_tag,
            "--load",
            *build_args,
            ".",
        ]
    )


def _run_container(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="toposync-arm64-data-") as data_dir:
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
                "127.0.0.1::8000",
                "-v",
                f"{data_dir}:/data",
                args.image_tag,
            ],
            capture=True,
        ).stdout.strip()
        try:
            base_url = _container_base_url(container_id)
            print(f"Container URL: {base_url}", flush=True)
            _wait_for_health(base_url)
            _assert_extensions(base_url)
            _assert_frontend(base_url)
            _assert_binary(container_id, "go2rtc", required=True)
            _assert_binary(container_id, "ffmpeg", required=args.expect_ffmpeg)
        finally:
            subprocess.run(["docker", "logs", "--tail", "120", container_id], check=False)
            subprocess.run(["docker", "stop", container_id], check=False)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Toposync distribution on linux/arm64 with Docker/QEMU.")
    parser.add_argument("--platform", default="linux/arm64")
    parser.add_argument("--package-spec", default="toposync-streaming==0.7.3")
    parser.add_argument("--image-tag", default="toposync:arm64-test")
    parser.add_argument("--install-wheel", default="/wheelhouse/toposync_streaming-*.whl")
    parser.add_argument("--apt-packages", default="ffmpeg")
    parser.add_argument("--skip-pip-dry-run", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--expect-ffmpeg", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    os.environ.setdefault("DOCKER_BUILDKIT", "1")
    if not args.skip_pip_dry_run:
        _pip_dry_run(args.package_spec, args.platform)
    if not args.skip_build:
        _build_image(args)
    if not args.skip_run:
        _run_container(args)
    print("ARM64 distribution smoke passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
