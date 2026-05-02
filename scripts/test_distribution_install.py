#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
DEFAULT_EXTENSION_NAMES = ("structural", "models", "images", "home_assistant", "cameras", "vision")
DEFAULT_EXTENSION_UI_WORKSPACES = (
    "@toposync/extension-structural-ui",
    "@toposync/extension-models-ui",
    "@toposync/extension-images-ui",
    "@toposync/extension-home-assistant-ui",
    "@toposync/extension-cameras-ui",
)


def _load_project(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]


CORE_PROJECT = _load_project(ROOT / "pyproject.toml")
APP_PROJECT = _load_project(ROOT / "packages" / "toposync" / "pyproject.toml")
APP_VERSION = str(APP_PROJECT["version"])


def _load_extension_manifest(extension_name: str) -> dict[str, object]:
    manifest_path = ROOT / "extensions" / extension_name / "src" / f"toposync_ext_{extension_name}" / "extension.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


DEFAULT_EXTENSION_MANIFESTS = {
    extension_name: _load_extension_manifest(extension_name) for extension_name in DEFAULT_EXTENSION_NAMES
}
EXPECTED_EXTENSION_IDS = tuple(
    str(DEFAULT_EXTENSION_MANIFESTS[extension_name]["id"]) for extension_name in DEFAULT_EXTENSION_NAMES
)
EXPECTED_FRONTEND_REMOTES = {
    str(manifest["id"]): f"/extensions/{manifest['id']}/{manifest['frontend']['remote_entry']}"
    for manifest in DEFAULT_EXTENSION_MANIFESTS.values()
    if isinstance(manifest.get("frontend"), dict)
}


def _print_step(message: str) -> None:
    print(f"\n== {message} ==")


def _run(
    command: list[str | Path],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    printable = [str(part) for part in command]
    print(f"+ {shlex.join(printable)}")
    subprocess.run(printable, cwd=cwd, env=env, check=True)


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv_dir: Path) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    return _venv_bin_dir(venv_dir) / executable


def _venv_toposync(venv_dir: Path) -> Path:
    executable = "toposync.exe" if os.name == "nt" else "toposync"
    return _venv_bin_dir(venv_dir) / executable


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _read_json(url: str, *, timeout: float = 5.0) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_text(url: str, *, timeout: float = 5.0) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/html, text/plain;q=0.9, */*;q=0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _tail_lines(path: Path, *, limit: int = 120) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def _wait_for_server(*, base_url: str, process: subprocess.Popen[str], log_path: Path, timeout_s: float = 120.0) -> None:
    deadline = time.time() + timeout_s
    last_error = "server did not become healthy"
    while time.time() < deadline:
        if process.poll() is not None:
            tail = _tail_lines(log_path)
            raise RuntimeError(
                "Toposync server exited before becoming healthy.\n"
                f"Exit code: {process.returncode}\n"
                f"Recent log output:\n{tail}"
            )
        try:
            payload = _read_json(f"{base_url}/api/health", timeout=3.0)
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return
            last_error = f"unexpected health payload: {payload!r}"
        except urllib.error.URLError as exc:
            last_error = str(exc)
        except TimeoutError as exc:
            last_error = str(exc)
        time.sleep(1.0)

    tail = _tail_lines(log_path)
    raise RuntimeError(
        "Timed out waiting for Toposync health endpoint.\n"
        f"Last error: {last_error}\n"
        f"Recent log output:\n{tail}"
    )


def _build_frontends() -> None:
    _print_step("Building bundled frontend assets")
    _run(["npm", "run", "build:frontend"])
    for workspace in DEFAULT_EXTENSION_UI_WORKSPACES:
        _run(["npm", "--workspace", workspace, "run", "build"])


def _build_wheelhouse(wheelhouse_dir: Path) -> None:
    _print_step("Building wheelhouse")
    targets = [
        ROOT,
        ROOT / "packages" / "toposync",
        *(ROOT / "extensions" / extension_name for extension_name in DEFAULT_EXTENSION_NAMES),
    ]
    for target in targets:
        _run(["uv", "build", "--wheel", "--out-dir", wheelhouse_dir, target])
    _assert_core_wheel_has_bundled_frontend(wheelhouse_dir)
    _assert_vision_wheel_publishable(wheelhouse_dir)


def _assert_core_wheel_has_bundled_frontend(wheelhouse_dir: Path) -> None:
    core_wheels = sorted(wheelhouse_dir.glob("toposync_core-*.whl"))
    if len(core_wheels) != 1:
        raise RuntimeError(f"Expected exactly one core wheel in wheelhouse, found {len(core_wheels)}")
    core_wheel = core_wheels[0]
    with zipfile.ZipFile(core_wheel) as archive:
        names = archive.namelist()
    required_files = {
        "toposync/_frontend/dist/index.html",
    }
    missing = sorted(required_files.difference(names))
    if missing:
        raise RuntimeError(f"Core wheel is missing bundled frontend assets: {', '.join(missing)}")


def _assert_vision_wheel_publishable(wheelhouse_dir: Path) -> None:
    vision_wheels = sorted(wheelhouse_dir.glob("toposync_ext_vision-*.whl"))
    if len(vision_wheels) != 1:
        raise RuntimeError(f"Expected exactly one vision wheel in wheelhouse, found {len(vision_wheels)}")
    vision_wheel = vision_wheels[0]
    max_bytes = 100 * 1024 * 1024
    size_bytes = vision_wheel.stat().st_size
    if size_bytes >= max_bytes:
        raise RuntimeError(
            f"Vision wheel is too large for default PyPI limits: {vision_wheel.name} ({size_bytes} bytes)"
        )
    with zipfile.ZipFile(vision_wheel) as archive:
        names = archive.namelist()
    if any(name.startswith("toposync_ext_vision/models/") for name in names):
        raise RuntimeError(f"Vision wheel unexpectedly packaged model artifacts: {vision_wheel.name}")


def _install_distribution(*, wheelhouse_dir: Path, venv_dir: Path, constraints_path: Path) -> None:
    _print_step("Creating clean virtual environment")
    _run(["uv", "venv", "--seed", venv_dir])
    python = _venv_python(venv_dir)
    _run([python, "-m", "pip", "install", "--upgrade", "pip"])

    constraints = [
        f"{CORE_PROJECT['name']}=={CORE_PROJECT['version']}",
        f"{APP_PROJECT['name']}=={APP_PROJECT['version']}",
    ]
    for extension_name in DEFAULT_EXTENSION_NAMES:
        project = _load_project(ROOT / "extensions" / extension_name / "pyproject.toml")
        constraints.append(f"{project['name']}=={project['version']}")
    constraints_path.write_text("\n".join(constraints) + "\n", encoding="utf-8")

    _print_step("Installing product bundle from built wheels")
    _run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--find-links",
            wheelhouse_dir,
            "--constraint",
            constraints_path,
            "--prefer-binary",
            f"toposync=={APP_VERSION}",
        ]
    )


def _assert_extensions(payload: object) -> list[str]:
    if not isinstance(payload, list):
        raise RuntimeError(f"/api/extensions returned an unexpected payload: {payload!r}")

    by_id = {}
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError(f"/api/extensions returned a non-object extension entry: {item!r}")
        extension_id = str(item.get("id") or "").strip()
        if not extension_id:
            raise RuntimeError(f"/api/extensions returned an extension without id: {item!r}")
        by_id[extension_id] = item

    expected_ids = set(EXPECTED_EXTENSION_IDS)
    actual_ids = set(by_id)
    if actual_ids != expected_ids:
        raise RuntimeError(
            "Installed bundle exposed an unexpected extension set.\n"
            f"Expected: {sorted(expected_ids)}\n"
            f"Actual:   {sorted(actual_ids)}"
        )

    remote_urls: list[str] = []
    for extension_id, remote_path in EXPECTED_FRONTEND_REMOTES.items():
        frontend = by_id[extension_id].get("frontend")
        if not isinstance(frontend, dict):
            raise RuntimeError(f"Extension {extension_id!r} is missing frontend metadata in /api/extensions")
        actual_remote_path = str(frontend.get("remote_entry_url") or "")
        if actual_remote_path != remote_path:
            raise RuntimeError(
                f"Extension {extension_id!r} exposed remote_entry_url {actual_remote_path!r}, expected {remote_path!r}"
            )
        remote_urls.append(remote_path)

    vision_frontend = by_id["com.toposync.vision"].get("frontend")
    if vision_frontend not in (None, {}):
        raise RuntimeError("Vision should not expose a frontend remote in the default bundle")

    return sorted(remote_urls)


def _assert_host_frontend(base_url: str) -> None:
    payload = _read_text(f"{base_url}/")
    lowered = payload.lower()
    if "<!doctype html" not in lowered and "<html" not in lowered:
        raise RuntimeError("Installed server did not serve the bundled frontend at '/'.")


def _run_playwright(*, base_url: str, remote_urls: list[str]) -> None:
    _print_step("Running distribution browser smoke test")
    env = os.environ.copy()
    env["TOPOSYNC_DISTRIBUTION_BASE_URL"] = base_url
    env["TOPOSYNC_DISTRIBUTION_EXPECTED_EXTENSIONS"] = json.dumps(sorted(EXPECTED_EXTENSION_IDS))
    env["TOPOSYNC_DISTRIBUTION_REMOTE_URLS"] = json.dumps(remote_urls)
    _run(["npx", "playwright", "test", "--config", "playwright.distribution.config.js"], env=env)


def main() -> int:
    if shutil.which("uv") is None:
        raise RuntimeError("uv is required for the distribution smoke test")
    if shutil.which("npm") is None:
        raise RuntimeError("npm is required for the distribution smoke test")
    if shutil.which("npx") is None:
        raise RuntimeError("npx is required for the distribution smoke test")

    with tempfile.TemporaryDirectory(prefix="toposync-dist-test-") as tmp:
        temp_root = Path(tmp)
        wheelhouse_dir = temp_root / "wheelhouse"
        venv_dir = temp_root / "venv"
        runtime_dir = temp_root / "runtime"
        data_dir = temp_root / "data"
        log_path = temp_root / "toposync-serve.log"
        constraints_path = temp_root / "constraints.txt"
        wheelhouse_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        _build_frontends()
        _build_wheelhouse(wheelhouse_dir)
        _install_distribution(wheelhouse_dir=wheelhouse_dir, venv_dir=venv_dir, constraints_path=constraints_path)

        port = _find_free_port()
        base_url = f"http://{HOST}:{port}"
        server_env = os.environ.copy()
        server_env["TOPOSYNC_AUTH_MODE"] = "bypass"
        server_env.pop("TOPOSYNC_FRONTEND_DIR", None)
        server_env.pop("TOPOSYNC_NO_FRONTEND", None)
        server_env.pop("PYTHONPATH", None)

        _print_step("Starting installed Toposync server")
        server_log = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                str(_venv_toposync(venv_dir)),
                "serve",
                "--host",
                HOST,
                "--port",
                str(port),
                "--data-dir",
                str(data_dir),
                "--log-level",
                "warning",
            ],
            cwd=runtime_dir,
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(base_url=base_url, process=process, log_path=log_path)

            _print_step("Checking bundled frontend from installed server")
            _assert_host_frontend(base_url)

            _print_step("Checking /api/extensions from installed server")
            payload = _read_json(f"{base_url}/api/extensions")
            remote_urls = _assert_extensions(payload)

            _run_playwright(base_url=base_url, remote_urls=remote_urls)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
            server_log.close()

    print("\n[ok] Distribution smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
