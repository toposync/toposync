from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

from .state import EventCandidate, EventLifecycle, EventPriority


Cursor = int | str | None
_VALID_PRIORITIES: set[EventPriority] = {"low", "medium", "high"}
_VALID_LIFECYCLES: set[EventLifecycle] = {"open", "update", "close"}


@dataclass(frozen=True, slots=True)
class EventFeedBatch:
    events: list[EventCandidate]
    next_cursor: Cursor = None


class EventFeed(Protocol):
    async def poll(self, cursor: Cursor = None, *, limit: int = 100) -> EventFeedBatch:
        ...


class NotificationEventFeed:
    def __init__(
        self,
        services: Any,
        config: Any | None = None,
        *,
        notification_types: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._services = services
        self._pipeline_camera_map = _normalize_text_map(getattr(config, "pipeline_camera_map", {}))
        self._priority_filter = _normalize_text_list(getattr(config, "priority_filter", []))
        self._notification_types = _normalize_text_list(notification_types)

    async def poll(self, cursor: Cursor = None, *, limit: int = 100) -> EventFeedBatch:
        if self._services is None or not hasattr(self._services, "call"):
            raise RuntimeError("NotificationEventFeed requires a ServiceRegistry-compatible object")

        safe_limit = max(1, min(250, int(limit)))
        raw = await self._services.call(
            "notifications.list",
            before=_cursor_as_int(cursor),
            limit=safe_limit,
            priorities=self._priority_filter or None,
            types=self._notification_types or None,
        )
        notifications, next_cursor = _normalize_list_response(raw)
        events = [
            _event_from_notification(item, pipeline_camera_map=self._pipeline_camera_map)
            for item in notifications
            if isinstance(item, dict)
        ]
        return EventFeedBatch(events=coalesce_event_candidates(events), next_cursor=next_cursor)


def coalesce_event_candidates(events: list[EventCandidate]) -> list[EventCandidate]:
    by_key: dict[str, EventCandidate] = {}
    for event in events:
        key = str(event.key or "").strip()
        if not key:
            continue
        current = by_key.get(key)
        if current is None or _event_updated_at(event) > _event_updated_at(current):
            by_key[key] = event
    return sorted(
        by_key.values(),
        key=lambda event: (_event_updated_at(event), str(event.key)),
        reverse=True,
    )


def _event_from_notification(
    notification: dict[str, Any],
    *,
    pipeline_camera_map: dict[str, str],
) -> EventCandidate:
    payload = _as_dict(notification.get("payload"))
    data = _as_dict(payload.get("data"))
    subject = _as_dict(payload.get("subject"))
    event_payload = _as_dict(payload.get("event"))
    pipeline_name = _first_text(payload.get("pipeline_name"), notification.get("pipeline_name"))
    camera_id = _resolve_camera_id(payload, data, subject, pipeline_name, pipeline_camera_map)
    source_id = _resolve_source_id(payload, data, subject)
    notification_id = _first_text(notification.get("id"), payload.get("notification_id"))
    event_id = _resolve_event_id(payload, data, event_payload)
    subject_id = _resolve_subject_id(payload, subject)
    stream_id = _first_text(payload.get("stream_id"), data.get("stream_id"))
    key = _event_key(
        notification_id=notification_id,
        subject_id=subject_id,
        event_id=event_id,
        pipeline_name=pipeline_name,
        camera_id=camera_id,
        stream_id=stream_id,
    )
    opened_at = _first_float(
        event_payload.get("started_ts"),
        payload.get("opened_at"),
        notification.get("createdAt"),
        notification.get("created_at"),
    )
    updated_at = _first_float(
        event_payload.get("ts"),
        payload.get("updated_at"),
        notification.get("updatedAt"),
        notification.get("updated_at"),
    )
    if updated_at <= 0.0:
        updated_at = opened_at
    lifecycle = _normalize_lifecycle(payload)
    return EventCandidate(
        key=key,
        source_kind="notification",
        priority=_normalize_priority(_first_text(payload.get("priority"), notification.get("priority"))),
        lifecycle=lifecycle,
        pipeline_name=pipeline_name,
        notification_id=notification_id,
        event_id=event_id,
        subject=subject,
        camera_id=camera_id,
        source_id=source_id,
        area_label=_first_text(payload.get("area_label"), data.get("area_label"), subject.get("area_label")),
        confidence=_optional_float(
            payload.get("confidence"),
            data.get("confidence"),
            subject.get("confidence"),
        ),
        opened_at=opened_at,
        updated_at=updated_at,
        closed_at=updated_at if lifecycle == "close" else None,
    )


def _normalize_list_response(raw: Any) -> tuple[list[dict[str, Any]], Cursor]:
    if isinstance(raw, dict):
        items = raw.get("notifications")
        if not isinstance(items, list):
            items = []
        return [item for item in items if isinstance(item, dict)], raw.get("next_cursor")
    if isinstance(raw, tuple) and len(raw) == 2:
        items, next_cursor = raw
        if not isinstance(items, list):
            items = []
        return [item for item in items if isinstance(item, dict)], next_cursor
    return [], None


def _event_key(
    *,
    notification_id: str,
    subject_id: str,
    event_id: str,
    pipeline_name: str,
    camera_id: str,
    stream_id: str,
) -> str:
    if notification_id:
        return f"notification:{notification_id}"
    if subject_id:
        return f"subject:{subject_id}"
    if event_id:
        return f"event:{event_id}"
    fallback = "|".join(
        [
            pipeline_name or "-",
            camera_id or "-",
            stream_id or "-",
        ]
    )
    return f"pipeline_camera_stream:{fallback}"


def _resolve_camera_id(
    payload: dict[str, Any],
    data: dict[str, Any],
    subject: dict[str, Any],
    pipeline_name: str,
    pipeline_camera_map: dict[str, str],
) -> str:
    source = _as_dict(payload.get("source"))
    subject_source = _as_dict(subject.get("source"))
    return _first_text(
        payload.get("camera_id"),
        source.get("device_id"),
        data.get("camera_id"),
        subject.get("camera_id"),
        subject_source.get("device_id"),
        subject.get("device_id"),
        pipeline_camera_map.get(pipeline_name),
    )


def _resolve_source_id(
    payload: dict[str, Any],
    data: dict[str, Any],
    subject: dict[str, Any],
) -> str:
    source = _as_dict(payload.get("source"))
    subject_source = _as_dict(subject.get("source"))
    return _first_text(
        payload.get("source_id"),
        source.get("source_id"),
        source.get("id"),
        data.get("source_id"),
        subject.get("source_id"),
        subject_source.get("source_id"),
        subject_source.get("id"),
    )


def _resolve_subject_id(payload: dict[str, Any], subject: dict[str, Any]) -> str:
    return _first_text(payload.get("subject_id"), subject.get("id"), subject.get("subject_id"))


def _resolve_event_id(
    payload: dict[str, Any],
    data: dict[str, Any],
    event_payload: dict[str, Any],
) -> str:
    return _first_text(payload.get("event_id"), data.get("event_id"), event_payload.get("id"))


def _normalize_priority(value: Any) -> EventPriority:
    text = str(value or "").strip().lower()
    if text in _VALID_PRIORITIES:
        return text  # type: ignore[return-value]
    return "medium"


def _normalize_lifecycle(payload: dict[str, Any]) -> EventLifecycle:
    lifecycle = str(payload.get("lifecycle") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    if lifecycle in _VALID_LIFECYCLES:
        return lifecycle  # type: ignore[return-value]
    if status in {"closed", "close", "ended", "resolved"}:
        return "close"
    if status in {"open", "active"}:
        return "open"
    return "update"


def _event_updated_at(event: EventCandidate) -> float:
    return float(event.updated_at or event.opened_at or 0.0)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_text_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _normalize_text_map(values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in values.items():
        text_key = str(key or "").strip()
        text_value = str(value or "").strip()
        if not text_key or not text_value:
            continue
        out[text_key] = text_value
    return out


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_float(*values: Any) -> float:
    for value in values:
        parsed = _parse_float(value)
        if parsed > 0.0:
            return parsed
    return 0.0


def _optional_float(*values: Any) -> float | None:
    value = _first_float(*values)
    return value if value > 0.0 else None


def _parse_float(value: Any) -> float:
    if isinstance(value, str):
        parsed_time = _parse_iso_timestamp(value)
        if parsed_time > 0.0:
            return parsed_time
    try:
        return float(value)
    except Exception:
        return 0.0


def _parse_iso_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _cursor_as_int(cursor: Cursor) -> int | None:
    if cursor is None:
        return None
    try:
        value = int(cursor)
    except Exception:
        return None
    return value if value > 0 else None
