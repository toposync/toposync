from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from typing import Any

from .events import EventBroadcaster
from .store import NotificationRecord, NotificationStore


def _iso(ts: float) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), tz=dt.UTC).isoformat()
    except Exception:
        return dt.datetime.now(tz=dt.UTC).isoformat()


def _to_public(rec: NotificationRecord) -> dict[str, Any]:
    image_url = f"/files/{rec.image_path}" if rec.image_path else None
    return {
        "id": rec.id,
        "type": rec.type,
        "title": rec.title,
        "description": rec.description,
        "imageUrl": image_url,
        "createdAt": _iso(rec.created_at),
        "updatedAt": _iso(rec.updated_at),
        "payload": rec.payload,
    }


class NotificationsRuntime:
    def __init__(self, *, data_dir: Path) -> None:
        self.store = NotificationStore(data_dir / "notifications" / "notifications.sqlite3")
        self.broadcaster = EventBroadcaster()

    async def list(self, *, before: int | None = None, limit: int = 50) -> tuple[list[dict[str, Any]], int | None]:
        records, next_cursor = await asyncio.to_thread(self.store.list, before=before, limit=limit)
        return ([_to_public(r) for r in records], next_cursor)

    async def get(self, notification_id: str) -> dict[str, Any] | None:
        rec = await asyncio.to_thread(self.store.get, notification_id)
        return _to_public(rec) if rec is not None else None

    async def upsert(
        self,
        *,
        type: str,
        title: str,
        description: str = "",
        image_path: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        rec, created = await asyncio.to_thread(
            self.store.upsert,
            type=type,
            title=title,
            description=description,
            image_path=image_path,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        public = _to_public(rec)
        self.broadcaster.publish({"op": "insert" if created else "update", "notification": public})
        return public

    async def close_open_pipeline_notifications(
        self,
        *,
        reason: str = "runtime_restart",
        limit: int = 5000,
    ) -> int:
        reason = str(reason or "").strip() or "runtime_restart"
        records = await asyncio.to_thread(self.store.list_open_pipeline_notifications, limit=int(limit))
        if not records:
            return 0

        now_ts = float(time.time())
        closed = 0
        for rec in records:
            if not rec.dedupe_key:
                continue
            payload = dict(rec.payload or {})
            payload["lifecycle"] = "close"
            payload["status"] = "closed"
            payload["reason"] = reason

            event = payload.get("event")
            if not isinstance(event, dict):
                event = {}
            started_ts = event.get("started_ts")
            try:
                started_ts_f = float(started_ts)
            except Exception:
                started_ts_f = 0.0
            if not started_ts_f:
                started_ts_f = float(rec.created_at or now_ts)
            event["started_ts"] = float(started_ts_f)
            event["ts"] = float(now_ts)
            event["duration_seconds"] = max(0.0, float(now_ts) - float(started_ts_f))
            payload["event"] = event

            await self.upsert(
                type=rec.type,
                title=rec.title,
                description=rec.description,
                image_path=rec.image_path,
                payload=payload,
                dedupe_key=rec.dedupe_key,
            )
            closed += 1
        return closed
