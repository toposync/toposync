from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .access import ReadFilter
from .events import EventBroadcaster
from .occurrences import Contribution, Enrichment, OccurrenceStore, OccurrenceChange, OccurrenceReference
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
        self.occurrences = OccurrenceStore(self.store)
        self.broadcaster = EventBroadcaster()
        self.invalidate_occurrence_runtime: Callable[[str], Awaitable[None]] | None = None
        self.invalidate_occurrence_derivatives: Callable[[str], Awaitable[None]] | None = None

    async def _invalidate_occurrence(self, change: OccurrenceChange) -> None:
        self.broadcaster.discard(change.invalidated_notifications)
        for identifier in change.invalidated_notifications:
            self.broadcaster.publish({"op": "invalidate", "notification_id": identifier,
                "occurrence_id": change.document["id"]})
        results = await asyncio.gather(*(callback(change.document["id"]) for callback in (
            self.invalidate_occurrence_runtime, self.invalidate_occurrence_derivatives) if callback is not None), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _publish_occurrence(self, identifier: str, *, now: float | None = None) -> None:
        published = await asyncio.to_thread(self.occurrences.publish, identifier, now=now)
        if published is not None:
            record, created = published
            self.broadcaster.publish({"op": "insert" if created else "update", "notification": _to_public(record)},
                include_global=record.priority_bucket != "silent")

    async def record_occurrence(self, contribution: Contribution, *, retention_seconds: float,
                                request_notice: bool = False, now: float | None = None,
                                return_change: bool = False) -> dict | OccurrenceChange:
        change = await asyncio.to_thread(self.occurrences.record, contribution,
            retention_seconds=retention_seconds, request_notice=request_notice, now=now)
        if change.changed:
            await self._publish_occurrence(change.document["id"], now=now)
        if change.requested_delivery is not None:
            # This sink records a local simulation receipt; it has no external transport.
            await asyncio.to_thread(self.occurrences.finish_delivery, change.requested_delivery,
                "confirmed", reason="simulated_sink", now=now)
        return change if return_change else change.document

    async def enrichment_allowed(self, reference: OccurrenceReference, *, evidence: dict | None = None) -> bool:
        return await asyncio.to_thread(self.occurrences.enrichment_allowed, reference, evidence=evidence)

    async def enrich_occurrence(self, enrichment: Enrichment, *, now: float | None = None) -> dict:
        change = await asyncio.to_thread(self.occurrences.enrich, enrichment, now=now)
        if change.changed:
            await self._publish_occurrence(change.document["id"], now=now)
        return change.document

    async def erase_occurrence(self, identifier: str, *, expected_revision: int) -> dict:
        change = await asyncio.to_thread(self.occurrences.erase, identifier, expected_revision=expected_revision)
        await self._invalidate_occurrence(change)
        return change.document

    async def revoke_occurrence_enrichment(self, identifier: str, *, expected_revision: int) -> dict:
        change = await asyncio.to_thread(self.occurrences.revoke_enrichment, identifier, expected_revision=expected_revision)
        await self._invalidate_occurrence(change)
        await self._publish_occurrence(identifier)
        return change.document

    async def expire_occurrences(self, *, now: float | None = None) -> int:
        changes = await asyncio.to_thread(self.occurrences.expire, now=now)
        results = await asyncio.gather(*(self._invalidate_occurrence(change) for change in changes), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return len(changes)

    async def list(
        self,
        *,
        before: int | None = None,
        limit: int = 50,
        priorities: list[str] | tuple[str, ...] | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        data_filter: dict[str, str] | None = None,
        notification_ids: list[str] | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        include_silent: bool = False,
        read_filter: ReadFilter | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        return await asyncio.to_thread(
            self.list_sync,
            before=before,
            limit=limit,
            priorities=priorities,
            types=types,
            query=query,
            data_filter=data_filter,
            notification_ids=notification_ids,
            created_after=created_after,
            created_before=created_before,
            include_silent=include_silent,
            read_filter=read_filter,
        )

    def list_sync(
        self,
        *,
        before: int | None = None,
        limit: int = 50,
        priorities: list[str] | tuple[str, ...] | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        query: str | None = None,
        data_filter: dict[str, str] | None = None,
        notification_ids: list[str] | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        include_silent: bool = False,
        read_filter: ReadFilter | None = None,
        cancel_check: _CancelCheck | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        records, next_cursor = self.store.list(
            before=before,
            limit=limit,
            priorities=priorities,
            types=types,
            query=query,
            data_filter=data_filter,
            notification_ids=notification_ids,
            created_after=created_after,
            created_before=created_before,
            include_silent=include_silent,
            read_filter=read_filter,
            cancel_check=cancel_check,
        )
        if cancel_check is not None:
            cancel_check()
        return ([_to_public(r) for r in records], next_cursor)

    async def count_by_priority(self) -> dict[str, int]:
        return await asyncio.to_thread(self.store.count_by_priority)

    async def count_summary(self, *, read_filter: ReadFilter | None = None, viewer_id: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self.count_summary_sync, read_filter=read_filter, viewer_id=viewer_id)

    def count_summary_sync(self, *, read_filter: ReadFilter | None = None, viewer_id: str = "", cancel_check: _CancelCheck | None = None) -> dict[str, Any]:
        total_by_priority = self.store.count_by_priority(cancel_check=cancel_check, read_filter=read_filter)
        last_viewed_seq = self.store.last_viewed_seq(cancel_check=cancel_check, viewer_id=viewer_id)
        unread_by_priority = self.store.count_by_priority(
            after_seq=last_viewed_seq,
            read_filter=read_filter,
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

    async def mark_all_viewed(self, *, read_filter: ReadFilter | None = None, viewer_id: str = "") -> dict[str, Any]:
        await asyncio.to_thread(self.store.mark_all_viewed, viewer_id=viewer_id)
        return await self.count_summary(read_filter=read_filter, viewer_id=viewer_id)

    async def get(self, notification_id: str, *, read_filter: ReadFilter | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, notification_id, read_filter=read_filter)

    async def event_visible(self, event: dict, *, read_filter: ReadFilter) -> bool:
        if event.get("op") == "invalidate":
            identifier = event.get("occurrence_id")
            return isinstance(identifier, str) and await asyncio.to_thread(
                self.occurrences.get, identifier, read_filter=read_filter) is not None
        if event.get("op") == "resync":
            identifier = event.get("notification_id")
            # Overflow carries no source data; the stream checks its own permission.
            if identifier is None:
                return True
            return isinstance(identifier, str) and await self.get(identifier, read_filter=read_filter) is not None
        queued = event.get("notification")
        if not isinstance(queued, dict) or not read_filter(queued.get("payload") or {}):
            return False
        current = await self.get(queued.get("id", ""), read_filter=read_filter)
        if current is None:
            return False
        # A queued snapshot must not resurrect content edited or erased since publication.
        for key in ("updatedAt", "title", "description", "imageUrl", "priority"):
            if current.get(key) != queued.get(key):
                return False
        return {key: value for key, value in current["payload"].items() if key != "ephemeral_image_status"} == {
            key: value for key, value in queued.get("payload", {}).items() if key != "ephemeral_image_status"}

    def get_sync(
        self,
        notification_id: str,
        *,
        read_filter: ReadFilter | None = None,
        cancel_check: _CancelCheck | None = None,
    ) -> dict[str, Any] | None:
        rec = self.store.get(notification_id, cancel_check=cancel_check, read_filter=read_filter)
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
        batch_size: int = 500,
    ) -> int:
        reason = str(reason or "").strip() or "runtime_restart"
        closed = 0
        while True:
            records = await asyncio.to_thread(
                self.store.list_open_pipeline_notifications, limit=max(1, int(batch_size))
            )
            if not records:
                break
            for rec in records:
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
