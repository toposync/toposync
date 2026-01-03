from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="toposync")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the TopoSync backend server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")

    args = parser.parse_args(argv)

    if args.command == "serve":
        uvicorn.run(
            "toposync.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
