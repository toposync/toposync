from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tomllib
import zipfile

import uvicorn

from toposync.runtime.extension_management import (
    install_manual_extension,
    validate_extension_install_spec,
)
from toposync.runtime.config_store import ConfigStore, UserDataPaths


DEFAULT_PROCESSING_PORT = 49321


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    if raw.lower() in {"none", "off", "false", "disabled"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def _uvicorn_graceful_shutdown_timeout(args: argparse.Namespace) -> int | None:
    value = getattr(args, "graceful_shutdown_timeout", None)
    return value if isinstance(value, int) and value > 0 else None


def _add_shutdown_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--graceful-shutdown-timeout",
        type=int,
        default=_env_optional_int("TOPOSYNC_GRACEFUL_SHUTDOWN_TIMEOUT"),
        help=(
            "Seconds to wait for open HTTP connections during server shutdown. "
            "Use 0 to keep Uvicorn's default indefinite wait."
        ),
    )


def _print_check(ok: bool, message: str) -> None:
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {message}")


def _load_pyproject(path: Path) -> dict:
    return tomllib.loads(path.joinpath("pyproject.toml").read_text(encoding="utf-8"))


def _doctor_local_extension(path: Path) -> int:
    errors = 0
    try:
        data = _load_pyproject(path)
    except Exception as exc:
        _print_check(False, f"pyproject.toml could not be read: {exc}")
        return 1

    project = data.get("project") if isinstance(data, dict) else {}
    package_name = str(project.get("name") if isinstance(project, dict) else "").strip()
    _print_check(bool(package_name), f"project package name: {package_name or 'missing'}")
    if not package_name.startswith("toposync-ext-"):
        errors += 1
        _print_check(False, "Python package name must start with toposync-ext-")

    entry_points = project.get("entry-points", {}) if isinstance(project, dict) else {}
    extension_eps = entry_points.get("toposync.extensions", {}) if isinstance(entry_points, dict) else {}
    has_entry_point = isinstance(extension_eps, dict) and bool(extension_eps)
    _print_check(has_entry_point, "toposync.extensions entry point declared")
    if not has_entry_point:
        return errors + 1

    first_value = str(next(iter(extension_eps.values())) or "")
    module_name = first_value.split(":", 1)[0].strip()
    package_root = module_name.split(".", 1)[0].strip()
    package_dir = path / "src" / package_root
    _print_check(package_dir.is_dir(), f"import package directory exists: src/{package_root}")
    if not package_dir.is_dir():
        errors += 1

    manifest_path = package_dir / "extension.json"
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _print_check(True, f"extension.json id: {manifest.get('id') or 'missing'}")
        except Exception as exc:
            errors += 1
            _print_check(False, f"extension.json is invalid JSON: {exc}")
    else:
        errors += 1
        _print_check(False, "extension.json exists in the Python package")

    frontend = manifest.get("frontend") if isinstance(manifest, dict) else None
    remote_entry_for_wheel: str | None = None
    if isinstance(frontend, dict):
        remote_entry = str(frontend.get("remote_entry") or "remoteEntry.js").strip()
        remote_entry_for_wheel = remote_entry
        remote_path = package_dir / "static" / remote_entry
        if remote_path.is_file():
            _print_check(True, f"frontend remote exists: static/{remote_entry}")
        else:
            errors += 1
            _print_check(False, f"frontend remote missing: static/{remote_entry}")
    else:
        _print_check(True, "no frontend remote declared")

    errors += _doctor_local_wheel(path, package_root, remote_entry_for_wheel)
    return errors


def _doctor_local_wheel(path: Path, package_root: str, remote_entry: str | None) -> int:
    dist_dir = path / "dist"
    wheels = sorted(
        dist_dir.glob("*.whl"),
        key=lambda item: item.stat().st_mtime,
    )
    if not wheels:
        _print_check(True, "wheel content check skipped: no wheel found in dist/")
        return 0

    wheel = wheels[-1]
    errors = 0
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
    except Exception as exc:
        _print_check(False, f"wheel could not be inspected: {exc}")
        return 1

    manifest_name = f"{package_root}/extension.json"
    if manifest_name in names:
        _print_check(True, f"wheel includes {manifest_name}")
    else:
        errors += 1
        _print_check(False, f"wheel is missing {manifest_name}")

    if remote_entry:
        remote_name = f"{package_root}/static/{remote_entry}"
        if remote_name in names:
            _print_check(True, f"wheel includes {remote_name}")
        else:
            errors += 1
            _print_check(False, f"wheel is missing {remote_name}")
        has_chunk = any(
            name.startswith(f"{package_root}/static/")
            and name.endswith(".js")
            and name != remote_name
            for name in names
        )
        if has_chunk:
            _print_check(True, "wheel includes generated frontend chunks")
        else:
            errors += 1
            _print_check(False, "wheel is missing generated frontend chunks")

    return errors


def _run_extension_doctor(spec: str) -> int:
    try:
        validated = validate_extension_install_spec(spec)
    except ValueError as exc:
        _print_check(False, str(exc))
        return 1

    _print_check(True, f"install source: {validated.source_kind}")
    _print_check(True, f"pip spec: {validated.pip_spec}")
    _print_check(True, f"package: {validated.package}")

    if validated.source_kind == "local":
        return _doctor_local_extension(Path(validated.pip_spec))
    return 0


async def _install_extension_and_update_config(spec: str, *, editable: bool) -> object:
    config_store = ConfigStore(paths=UserDataPaths.resolve())
    await config_store.load()
    return await install_manual_extension(
        config_store,
        spec,
        editable=True if editable else None,
    )


def _run_extension_install(spec: str, *, editable: bool) -> int:
    try:
        result = asyncio.run(_install_extension_and_update_config(spec, editable=editable))
    except ValueError as exc:
        print(str(exc))
        return 1

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return 0 if result.ok else int(result.return_code or 1)


def _run_extension_dev(args: argparse.Namespace) -> None:
    path = Path(args.path).expanduser().resolve()
    os.environ["TOPOSYNC_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    doctor_code = _run_extension_doctor(str(path))
    if doctor_code != 0:
        raise SystemExit(doctor_code)

    install_code = _run_extension_install(str(path), editable=True)
    if install_code != 0:
        raise SystemExit(install_code)

    ui_package = path / "ui" / "package.json"
    if ui_package.is_file():
        print("Frontend bundle command: npm --prefix ui run build")
        print("Run it again after UI changes, then restart Toposync if needed.")

    uvicorn.run(
        "toposync.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=_uvicorn_graceful_shutdown_timeout(args),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="toposync")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the Toposync backend server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=_env_int("TOPOSYNC_BACKEND_PORT", 8000))
    serve.add_argument("--log-level", default="info")
    _add_shutdown_argument(serve)
    serve.add_argument(
        "--data-dir",
        default=None,
        help="Override TOPOSYNC_DATA_DIR (where config.json and user files live).",
    )
    serve.add_argument(
        "--frontend-dir",
        default=None,
        help="Serve the built frontend from this directory (expects index.html).",
    )
    serve.add_argument(
        "--no-frontend",
        action="store_true",
        help="Disable serving the frontend host (even if TOPOSYNC_FRONTEND_DIR or a bundled UI is present).",
    )

    processing = sub.add_parser("processing-serve", help="Run the Toposync processing server (distributed pipelines).")
    processing.add_argument("--host", default="127.0.0.1")
    processing.add_argument("--port", type=int, default=_env_int("TOPOSYNC_PROCESSING_PORT", DEFAULT_PROCESSING_PORT))
    processing.add_argument("--log-level", default="info")
    _add_shutdown_argument(processing)
    processing.add_argument(
        "--data-dir",
        default=None,
        help="Override TOPOSYNC_DATA_DIR (where config.json is read from).",
    )

    extension = sub.add_parser("extension", help="Create, validate, and install Toposync extensions.")
    extension_sub = extension.add_subparsers(dest="extension_command", required=True)

    doctor = extension_sub.add_parser("doctor", help="Validate an extension package, GitHub URL, or local path.")
    doctor.add_argument("spec")

    install = extension_sub.add_parser("install", help="Install an extension package, GitHub URL, or local path.")
    install.add_argument("spec")
    install.add_argument("--editable", action="store_true", help="Install a local extension in editable mode.")

    dev = extension_sub.add_parser("dev", help="Install a local extension in editable mode and run Toposync.")
    dev.add_argument("path")
    dev.add_argument("--data-dir", default=".toposync-data-extension-dev")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=_env_int("TOPOSYNC_BACKEND_PORT", 8000))
    dev.add_argument("--log-level", default="info")
    _add_shutdown_argument(dev)

    args = parser.parse_args(argv)

    if args.command == "serve":
        if args.data_dir:
            os.environ["TOPOSYNC_DATA_DIR"] = args.data_dir
        if args.frontend_dir:
            os.environ["TOPOSYNC_FRONTEND_DIR"] = args.frontend_dir
        if args.no_frontend:
            os.environ["TOPOSYNC_NO_FRONTEND"] = "1"
        uvicorn.run(
            "toposync.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            timeout_graceful_shutdown=_uvicorn_graceful_shutdown_timeout(args),
        )
    if args.command == "processing-serve":
        if args.data_dir:
            os.environ["TOPOSYNC_DATA_DIR"] = args.data_dir
        os.environ.setdefault("TOPOSYNC_ROLE", "processing")
        uvicorn.run(
            "toposync.processing_server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            timeout_graceful_shutdown=_uvicorn_graceful_shutdown_timeout(args),
        )
    if args.command == "extension":
        if args.extension_command == "doctor":
            raise SystemExit(_run_extension_doctor(args.spec))
        if args.extension_command == "install":
            raise SystemExit(_run_extension_install(args.spec, editable=args.editable))
        if args.extension_command == "dev":
            _run_extension_dev(args)
