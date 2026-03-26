from __future__ import annotations

import os
import signal
import subprocess


def _escape_powershell_single_quote(value: str) -> str:
    # PowerShell escapes single quotes in single-quoted strings by doubling them.
    return str(value or "").replace("'", "''")


def find_mediamtx_pids_for_config_path(
    config_path: str,
    *,
    exclude_pids: set[int] | None = None,
) -> list[int]:
    config = str(config_path or "").strip()
    excluded = {int(pid) for pid in (exclude_pids or set()) if int(pid) > 0}
    if not config:
        return []

    if os.name == "nt":
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$cp='{_escape_powershell_single_quote(config)}';"
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $cp + '*')) -and ($_.CommandLine -like '*mediamtx*') } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return []
        out = str(result.stdout or "")
        pids: list[int] = []
        for token in out.split():
            try:
                pid = int(token)
            except Exception:
                continue
            if pid > 0 and pid not in excluded:
                pids.append(pid)
        return sorted(set(pids))

    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    pids: list[int] = []
    for raw_line in str(result.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_raw, command = parts
        if "mediamtx" not in command:
            continue
        if config not in command:
            continue
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        if pid > 0 and pid not in excluded:
            pids.append(pid)
    return sorted(set(pids))


def kill_mediamtx_processes_for_config_path(
    config_path: str,
    *,
    exclude_pids: set[int] | None = None,
) -> list[int]:
    pids = find_mediamtx_pids_for_config_path(config_path, exclude_pids=exclude_pids)
    if not pids:
        return []

    killed: list[int] = []
    if os.name == "nt":
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                killed.append(pid)
            except Exception:
                continue
        return killed

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    return killed
