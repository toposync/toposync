from __future__ import annotations

import argparse
import os

import uvicorn


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="toposync")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the Toposync backend server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=_env_int("TOPOSYNC_BACKEND_PORT", 8000))
    serve.add_argument("--log-level", default="info")
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
        help="Disable serving the built frontend (even if TOPOSYNC_FRONTEND_DIR/frontend/dist is present).",
    )

    processing = sub.add_parser("processing-serve", help="Run the Toposync processing server (distributed pipelines).")
    processing.add_argument("--host", default="127.0.0.1")
    processing.add_argument("--port", type=int, default=_env_int("TOPOSYNC_PROCESSING_PORT", 9001))
    processing.add_argument("--log-level", default="info")
    processing.add_argument(
        "--data-dir",
        default=None,
        help="Override TOPOSYNC_DATA_DIR (where config.json is read from).",
    )

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
        )
