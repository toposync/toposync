from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "header",
    "password",
    "secret",
    "token",
    "url",
)


@dataclass(frozen=True, slots=True)
class PlaybackEventRecord:
    playback_session_id: str
    transmission_id: str
    output_id: str | None
    client_kind: str
    platform: str
    app_state: str | None
    pip_active: bool | None
    type: str
    severity: str
    at_unix: float
    received_at_unix: float
    message: str | None
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "playback_session_id": self.playback_session_id,
            "transmission_id": self.transmission_id,
            "output_id": self.output_id,
            "client_kind": self.client_kind,
            "platform": self.platform,
            "app_state": self.app_state,
            "pip_active": self.pip_active,
            "type": self.type,
            "severity": self.severity,
            "at_unix": self.at_unix,
            "received_at_unix": self.received_at_unix,
            "message": self.message,
            "data": dict(self.data),
        }


class PlaybackEventStore:
    def __init__(self, *, retention_seconds: float = 900.0, max_events: int = 500) -> None:
        self.retention_seconds = max(1.0, float(retention_seconds))
        self.max_events = max(1, int(max_events))
        self._events: deque[PlaybackEventRecord] = deque()
        self._lock = asyncio.Lock()

    async def record_batch(
        self,
        *,
        playback_session_id: str,
        transmission_id: str,
        output_id: str | None,
        client_kind: str,
        platform: str,
        app_state: str | None,
        pip_active: bool | None,
        events: list[dict[str, Any]],
        now_unix: float | None = None,
    ) -> int:
        now = float(now_unix if now_unix is not None else time.time())
        accepted = 0
        async with self._lock:
            for event in events:
                event_type = str(event.get("type") or "").strip()[:80]
                if not event_type:
                    continue
                severity = str(event.get("severity") or "info").strip().lower()
                if severity not in {"debug", "info", "warn", "error"}:
                    severity = "info"
                try:
                    at_unix = float(event.get("at_unix") or now)
                except Exception:
                    at_unix = now
                message = event.get("message")
                data = event.get("data")
                self._events.append(
                    PlaybackEventRecord(
                        playback_session_id=str(playback_session_id).strip(),
                        transmission_id=str(transmission_id).strip(),
                        output_id=str(output_id).strip() or None if output_id is not None else None,
                        client_kind=str(client_kind).strip(),
                        platform=str(platform).strip(),
                        app_state=str(app_state).strip() or None if app_state is not None else None,
                        pip_active=pip_active if isinstance(pip_active, bool) else None,
                        type=event_type,
                        severity=severity,
                        at_unix=at_unix,
                        received_at_unix=now,
                        message=str(message).strip()[:500] if message is not None else None,
                        data=_sanitize_data(data),
                    )
                )
                accepted += 1
            self._prune_locked(now)
            return accepted

    async def list_events(
        self,
        *,
        transmission_id: str | None = None,
        since_unix: float | None = None,
        limit: int | None = None,
    ) -> list[PlaybackEventRecord]:
        tid = str(transmission_id or "").strip()
        since = float(since_unix) if since_unix is not None else None
        max_items = max(1, int(limit)) if limit is not None else self.max_events
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            out: list[PlaybackEventRecord] = []
            for event in reversed(self._events):
                if tid and event.transmission_id != tid:
                    continue
                if since is not None and event.at_unix < since and event.received_at_unix < since:
                    continue
                out.append(event)
                if len(out) >= max_items:
                    break
            out.reverse()
            return out

    async def retained_count(self) -> int:
        async with self._lock:
            self._prune_locked(time.time())
            return len(self._events)

    def _prune_locked(self, now_unix: float) -> None:
        cutoff = float(now_unix) - self.retention_seconds
        while self._events and self._events[0].received_at_unix < cutoff:
            self._events.popleft()
        while len(self._events) > self.max_events:
            self._events.popleft()


def summarize_active_sessions(
    events: list[PlaybackEventRecord],
    *,
    now_unix: float,
    active_window_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    cutoff = float(now_unix) - max(1.0, float(active_window_seconds))
    for event in events:
        summary = sessions.get(event.playback_session_id)
        if summary is None:
            summary = {
                "playback_session_id": event.playback_session_id,
                "transmission_id": event.transmission_id,
                "output_id": event.output_id,
                "client_kind": event.client_kind,
                "platform": event.platform,
                "app_state": event.app_state,
                "pip_active": event.pip_active,
                "first_event_at_unix": event.at_unix,
                "last_event_at_unix": event.at_unix,
                "last_type": event.type,
                "last_severity": event.severity,
            }
            sessions[event.playback_session_id] = summary
        summary["first_event_at_unix"] = min(float(summary["first_event_at_unix"]), event.at_unix)
        if event.at_unix >= float(summary["last_event_at_unix"]):
            summary.update(
                {
                    "output_id": event.output_id,
                    "client_kind": event.client_kind,
                    "platform": event.platform,
                    "app_state": event.app_state,
                    "pip_active": event.pip_active,
                    "last_event_at_unix": event.at_unix,
                    "last_type": event.type,
                    "last_severity": event.severity,
                }
            )
    return [
        summary
        for summary in sorted(
            sessions.values(),
            key=lambda item: float(item["last_event_at_unix"]),
            reverse=True,
        )
        if float(summary["last_event_at_unix"]) >= cutoff
    ]


def _sanitize_data(value: Any, *, depth: int = 0) -> dict[str, Any]:
    sanitized = _sanitize_value(value, depth=depth)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _is_sensitive_key(text_key):
                out[text_key] = "[REDACTED]"
            else:
                out[text_key] = _sanitize_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, str):
        if "://" in value:
            return "[REDACTED_URL]"
        return value[:500]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value
    return str(value)[:500]


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)
