from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .events import EventBroadcaster
from .store import NotificationRecord, NotificationStore


_CancelCheck = Callable[[], None]


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
        "priority": rec.priority_bucket,
        "payload": rec.payload,
    }


class NotificationsRuntime:
    def __init__(self, *, data_dir: Path) -> None:
        self.store = NotificationStore(data_dir / "notifications" / "notifications.sqlite3")
        self.broadcaster = EventBroadcaster()

    async def list(
        self,
        *,
        before: int | None = None,
        limit: int = 50,
        priorities: list[str] | tuple[str, ...] | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        include_silent: bool = False,
    ) -> tuple[list[dict[str, Any]], int | None]:
        return await asyncio.to_thread(
            self.list_sync,
            before=before,
            limit=limit,
            priorities=priorities,
            types=types,
            query=query,
            include_silent=include_silent,
        )

    def list_sync(
        self,
        *,
        before: int | None = None,
        limit: int = 50,
        priorities: list[str] | tuple[str, ...] | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        include_silent: bool = False,
        cancel_check: _CancelCheck | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        records, next_cursor = self.store.list(
            before=before,
            limit=limit,
            priorities=priorities,
            types=types,
            query=query,
            include_silent=include_silent,
            cancel_check=cancel_check,
        )
        if cancel_check is not None:
            cancel_check()
        return ([_to_public(r) for r in records], next_cursor)

    async def count_by_priority(self) -> dict[str, int]:
        return await asyncio.to_thread(self.store.count_by_priority)

    async def count_summary(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.count_summary_sync)

    def count_summary_sync(self, *, cancel_check: _CancelCheck | None = None) -> dict[str, Any]:
        total_by_priority = self.store.count_by_priority(cancel_check=cancel_check)
        last_viewed_seq = self.store.last_viewed_seq(cancel_check=cancel_check)
        unread_by_priority = self.store.count_by_priority(
            after_seq=last_viewed_seq,
            cancel_check=cancel_check,
        )
        if cancel_check is not None:
            cancel_check()
        return {
            "total": sum(total_by_priority.values()),
            "by_priority": total_by_priority,
            "unread_total": sum(unread_by_priority.values()),
            "unread_by_priority": unread_by_priority,
        }

    async def mark_all_viewed(self) -> dict[str, Any]:
        await asyncio.to_thread(self.store.mark_all_viewed)
        return await self.count_summary()

    async def get(self, notification_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, notification_id)

    def get_sync(
        self,
        notification_id: str,
        *,
        cancel_check: _CancelCheck | None = None,
    ) -> dict[str, Any] | None:
        rec = self.store.get(notification_id, cancel_check=cancel_check)
        if cancel_check is not None:
            cancel_check()
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
        ephemeral_image: dict[str, Any] | None = None,
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
        self.broadcaster.publish(
            {"op": "insert" if created else "update", "notification": public},
            ephemeral_image=ephemeral_image,
            include_global=rec.priority_bucket != "silent",
        )
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

        closed = 0
        for rec in records:
            if not rec.dedupe_key:
                continue
            payload = dict(rec.payload or {})
            payload["lifecycle"] = "close"
            payload["status"] = "closed"
            payload["reason"] = reason

            # Restart supplies no new source sample. Preserve the recorded
            # interval (including a valid zero origin), never add downtime or
            # infer that an unlabelled legacy source timestamp is Unix time.

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
