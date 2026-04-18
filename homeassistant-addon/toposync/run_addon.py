from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


OPTIONS_PATH = Path("/data/options.json")


def _load_options() -> dict[str, object]:
    try:
        if not OPTIONS_PATH.is_file():
            return {}
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _setdefault_env(key: str, value: str) -> None:
    if os.getenv(key):
        return
    os.environ[key] = value


def main() -> int:
    options = _load_options()
    log_level = str(options.get("log_level", "") or "").strip()
    if log_level:
        _setdefault_env("TOPOSYNC_LOG_LEVEL", log_level)

    _setdefault_env("TOPOSYNC_DATA_DIR", "/data")
    _setdefault_env("TOPOSYNC_STREAMING_ENGINE_CACHE_DIR", "/data/runtime")
    _setdefault_env("TOPOSYNC_AUTH_MODE", "home_assistant_ingress")
    _setdefault_env("TOPOSYNC_AUTH_INGRESS_ROLE", "owner")
    _setdefault_env("TOPOSYNC_AUTH_INGRESS_TRUSTED_IPS", "127.0.0.1,::1,172.30.32.2,testclient")
    _setdefault_env("TOPOSYNC_AUTH_INGRESS_ENFORCE_TRUSTED", "1")
    _setdefault_env("TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE", "supervisor")

    proc = subprocess.run(
        [
            "toposync",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--data-dir",
            "/data",
        ],
        check=False,
    )
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
