from __future__ import annotations

import asyncio
import datetime as dt
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
