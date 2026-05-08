#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


def _json_get(base_url: str, path: str, *, timeout_s: float) -> dict[str, Any]:
    url = urllib_parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    req = urllib_request.Request(url, headers={"accept": "application/json"})
    with urllib_request.urlopen(req, timeout=timeout_s) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def _p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.99) - 1))
    return ordered[index]


def _collect_hls_sequence_ages(observability: dict[str, Any], now_unix: float) -> list[float]:
    ages: list[float] = []
    for item in observability.get("items") or []:
        for event in item.get("recent_events") or []:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("hls_media_sequence") is None and data.get("media_sequence") is None:
                continue
            at_unix = _number(event.get("at_unix")) or _number(event.get("received_at_unix"))
            if at_unix is not None:
                ages.append(max(0.0, now_unix - at_unix))
    return ages


def _run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = str(args.base_url)
    interval_s = max(0.5, float(args.interval_seconds))
    duration_s = max(1.0, float(args.duration_seconds))
    timeout_s = max(0.5, float(args.timeout_seconds))
    started = time.time()
    deadline = started + duration_s

    frame_ages: list[float] = []
    hls_sequence_ages: list[float] = []
    publisher_restarts: list[float] = []
    encoder_restarts: list[float] = []
    classifications: Counter[str] = Counter()
    stalls = 0
    samples = 0
    errors: list[str] = []

    while time.time() < deadline:
        now_unix = time.time()
        try:
            health = _json_get(base_url, "/api/streams/runtime/health", timeout_s=timeout_s)
            observability = _json_get(
                base_url,
                "/api/streams/runtime/observability",
                timeout_s=timeout_s,
            )
        except (OSError, urllib_error.URLError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            time.sleep(interval_s)
            continue

        samples += 1
        for transmission in health.get("transmissions") or []:
            if args.transmission_id and transmission.get("transmission_id") != args.transmission_id:
                continue
            age = _number(transmission.get("selected_frame_age_seconds"))
            if age is not None:
                frame_ages.append(age)
                if age >= 3.0 and not transmission.get("event_gated_idle"):
                    stalls += 1
            classification = str(transmission.get("classification") or "unknown")
            classifications[classification] += 1
            for output in transmission.get("outputs") or []:
                restart_count = _number(output.get("publisher_restart_count"))
                if restart_count is not None:
                    publisher_restarts.append(restart_count)
                    encoder_restarts.append(restart_count)

        for item in observability.get("items") or []:
            classification = str(item.get("classification") or "unknown")
            classifications[classification] += 1
        hls_sequence_ages.extend(_collect_hls_sequence_ages(observability, now_unix))
        time.sleep(interval_s)

    finished = time.time()
    return {
        "ok": not errors,
        "mode": args.mode,
        "base_url": base_url,
        "transmission_id": args.transmission_id,
        "duration_seconds": round(finished - started, 3),
        "samples": samples,
        "p99_selected_frame_age_seconds": _p99(frame_ages),
        "p99_hls_media_sequence_age_seconds": _p99(hls_sequence_ages),
        "stalls": stalls,
        "publisher_restart_count_max": max(publisher_restarts) if publisher_restarts else 0,
        "encoder_restart_count_max": max(encoder_restarts) if encoder_restarts else 0,
        "classifications": dict(classifications),
        "errors": errors[-20:],
        "acceptance_reference": {
            "single_stream_2h": "p99 frame age < 3s, zero silent freeze",
            "quad_1h": "no systematic publisher drop",
            "hls": "p99 sequence age < 3 * target duration",
            "encoder": "encoder restart count should remain 0 on stable_apple_tv",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in streaming chaos/soak acceptance harness for a real TopoSync instance.",
        epilog=(
            "Examples:\n"
            "  single stream 2h: scripts/streaming_chaos_acceptance.py "
            "--base-url http://127.0.0.1:8000 --mode single --duration-seconds 7200\n"
            "  quad 1h: scripts/streaming_chaos_acceptance.py "
            "--base-url http://127.0.0.1:8000 --mode quad --duration-seconds 3600"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=["single", "quad"], default="single")
    parser.add_argument("--transmission-id", default=None)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=float, default=2.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = _run(args)
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
