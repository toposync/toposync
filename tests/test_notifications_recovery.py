from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import threading

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from toposync.app import (
    CLIENT_CLOSED_REQUEST_STATUS,
    _RequestConcurrencyLimiter,
    _run_cancelable_request_work,
    create_app,
)
import toposync.extensions.manager as ext_manager_mod
from toposync.runtime.notifications import NotificationsRuntime


class _TestCancelled(Exception):
    pass


class _FakeDisconnectRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def test_notification_store_backfills_priority_bucket_from_legacy_payload(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    db_path = data_dir / "notifications" / "notifications.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notification (
          seq          INTEGER PRIMARY KEY AUTOINCREMENT,
          id           TEXT NOT NULL UNIQUE,
          type         TEXT NOT NULL,
          title        TEXT NOT NULL,
          description  TEXT NOT NULL,
          image_path   TEXT,
          payload_json TEXT NOT NULL,
          created_at   REAL NOT NULL,
          updated_at   REAL NOT NULL,
          dedupe_key   TEXT
        );
        CREATE UNIQUE INDEX idx_notification_dedupe_key ON notification(dedupe_key);
        CREATE INDEX idx_notification_seq_desc ON notification(seq DESC);
        CREATE TABLE notification_state (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    rows = [
        ("low-1", "pipelines.event", "Low", {"priority": "low"}, 1_000.0),
        ("missing-1", "pipelines.event", "Missing", {}, 1_001.0),
        ("invalid-1", "pipelines.event", "Invalid", {"priority": "urgent"}, 1_002.0),
        ("high-1", "pipelines.event", "High", {"priority": "high"}, 1_003.0),
    ]
    conn.executemany(
        """
        INSERT INTO notification(id, type, title, description, image_path, payload_json, created_at, updated_at, dedupe_key)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                nid,
                ntype,
                title,
                "",
                None,
                json.dumps(payload, separators=(",", ":")),
                ts,
                ts,
                None,
            )
            for nid, ntype, title, payload, ts in rows
        ],
    )
    conn.commit()
    conn.close()

    notifications = NotificationsRuntime(data_dir=data_dir)
    medium_items, _cursor = notifications.store.list(limit=10, priorities=["medium"])
    assert [item.title for item in medium_items] == ["Invalid", "Missing"]
    assert [item.priority_bucket for item in medium_items] == ["medium", "medium"]
    assert notifications.store.count_by_priority() == {"low": 1, "medium": 2, "high": 1}

    state_row = notifications.store._conn.execute(
        "SELECT value FROM notification_state WHERE key = 'priority_bucket_backfill_v1'"
    ).fetchone()
    assert state_row is not None
    assert state_row["value"] == "1"

    plan_rows = notifications.store._conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT seq FROM notification
        WHERE priority_bucket IN (?)
        ORDER BY seq DESC
        LIMIT ?
        """,
        ("medium", 10),
    ).fetchall()
    assert any("idx_notification_priority_seq" in str(row[3]) for row in plan_rows)


def test_close_open_pipeline_notifications_marks_stale_open_as_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")

        open_rec = await notifications.upsert(
            type="pipelines.event",
            title="Open pipeline notification",
            description="",
            payload={
                "source": "pipelines",
                "status": "open",
                "lifecycle": "open",
                "priority": "medium",
                "event": {"started_ts": 10.0, "ts": 10.0, "duration_seconds": 0.0},
            },
            dedupe_key="pipeline:test:token:abc",
        )
        closed_rec = await notifications.upsert(
            type="pipelines.event",
            title="Already closed",
            description="",
            payload={
                "source": "pipelines",
                "status": "closed",
                "lifecycle": "close",
                "reason": "normal",
                "event": {"started_ts": 1.0, "ts": 2.0, "duration_seconds": 1.0},
            },
            dedupe_key="pipeline:test:token:closed",
        )
        other_rec = await notifications.upsert(
            type="other.event",
            title="Other system",
            description="",
            payload={"source": "other", "status": "open"},
            dedupe_key="other:1",
        )

        assert (await notifications.get(open_rec["id"])) is not None
        assert (await notifications.get(closed_rec["id"])) is not None
        assert (await notifications.get(other_rec["id"])) is not None

        closed = await notifications.close_open_pipeline_notifications(reason="runtime_restart")
        assert closed == 1

        updated = await notifications.get(open_rec["id"])
        assert updated is not None
        payload = updated.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("status") == "closed"
        assert payload.get("lifecycle") == "close"
        assert payload.get("reason") == "runtime_restart"

        original_closed = await notifications.get(closed_rec["id"])
        assert original_closed is not None
        assert original_closed.get("payload", {}).get("reason") == "normal"

        untouched_other = await notifications.get(other_rec["id"])
        assert untouched_other is not None
        assert untouched_other.get("payload", {}).get("source") == "other"

    asyncio.run(scenario())


def test_notifications_upsert_archives_closed_dedupe_before_new_open(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        dedupe_key = "pipeline:test:camera:front:token:track-1"

        first = await notifications.upsert(
            type="pipelines.event",
            title="First",
            payload={
                "source": "pipelines",
                "status": "closed",
                "lifecycle": "close",
                "event": {"started_ts": 1.0, "ts": 2.0, "duration_seconds": 1.0},
            },
            dedupe_key=dedupe_key,
        )
        second = await notifications.upsert(
            type="pipelines.event",
            title="Second",
            payload={
                "source": "pipelines",
                "status": "open",
                "lifecycle": "open",
                "event": {"started_ts": 10.0, "ts": 10.0, "duration_seconds": 0.0},
            },
            dedupe_key=dedupe_key,
        )
        second_closed = await notifications.upsert(
            type="pipelines.event",
            title="Second",
            payload={
                "source": "pipelines",
                "status": "closed",
                "lifecycle": "close",
                "event": {"started_ts": 10.0, "ts": 12.0, "duration_seconds": 2.0},
            },
            dedupe_key=dedupe_key,
        )

        assert first["id"] != second["id"]
        assert second_closed["id"] == second["id"]

        items, _cursor = await notifications.list(limit=20)
        assert len(items) == 2
        by_id = {str(item.get("id")): item for item in items}
        assert by_id[first["id"]]["payload"]["event"]["started_ts"] == 1.0
        assert by_id[second["id"]]["payload"]["event"]["started_ts"] == 10.0
        assert by_id[second["id"]]["payload"]["status"] == "closed"

    asyncio.run(scenario())


def test_notifications_unviewed_count_resets_after_mark_viewed(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")

        await notifications.upsert(
            type="pipelines.event",
            title="High priority",
            payload={"priority": "high"},
        )
        await notifications.upsert(
            type="pipelines.event",
            title="Low priority",
            payload={"priority": "low"},
        )

        counts = await notifications.count_summary()
        assert counts["total"] == 2
        assert counts["unread_total"] == 2
        assert counts["by_priority"] == {"low": 1, "medium": 0, "high": 1}
        assert counts["unread_by_priority"] == {"low": 1, "medium": 0, "high": 1}

        viewed_counts = await notifications.mark_all_viewed()
        assert viewed_counts["total"] == 2
        assert viewed_counts["unread_total"] == 0
        assert viewed_counts["unread_by_priority"] == {"low": 0, "medium": 0, "high": 0}

        await notifications.upsert(
            type="pipelines.event",
            title="Next priority",
            payload={"priority": "medium"},
        )

        next_counts = await notifications.count_summary()
        assert next_counts["total"] == 3
        assert next_counts["unread_total"] == 1
        assert next_counts["by_priority"] == {"low": 1, "medium": 1, "high": 1}
        assert next_counts["unread_by_priority"] == {"low": 0, "medium": 1, "high": 0}

    asyncio.run(scenario())


def test_notifications_upsert_updates_and_preserves_priority_bucket(tmp_path: Path) -> None:
    async def scenario() -> None:
        notifications = NotificationsRuntime(data_dir=tmp_path / "data")
        first = await notifications.upsert(
            type="pipelines.event",
            title="Initial",
            payload={"priority": "low"},
            dedupe_key="pipeline:test:priority",
        )
        changed = await notifications.upsert(
            type="pipelines.event",
            title="Changed",
            payload={"priority": "high"},
            dedupe_key="pipeline:test:priority",
        )
        preserved = await notifications.upsert(
            type="pipelines.event",
            title="Preserved",
            dedupe_key="pipeline:test:priority",
        )

        assert changed["id"] == first["id"]
        assert changed["priority"] == "high"
        assert changed["payload"]["priority"] == "high"
        assert preserved["id"] == first["id"]
        assert preserved["priority"] == "high"
        assert preserved["payload"]["priority"] == "high"

        counts = await notifications.count_summary()
        assert counts["by_priority"] == {"low": 0, "medium": 0, "high": 1}

        high_items, _cursor = await notifications.list(priorities=["high"], limit=10)
        assert [item["title"] for item in high_items] == ["Preserved"]
        medium_items, _cursor = await notifications.list(priorities=["medium"], limit=10)
        assert medium_items == []

    asyncio.run(scenario())


def test_notifications_viewed_endpoint_does_not_reset_on_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])

    with TestClient(create_app()) as client:
        notifications: NotificationsRuntime = client.app.state.notifications
        asyncio.run(
            notifications.upsert(
                type="pipelines.event",
                title="Initial",
                payload={"priority": "high"},
            )
        )

        listed = client.get("/api/notifications")
        assert listed.status_code == 200
        assert listed.json()["notifications"]

        count = client.get("/api/notifications/count")
        assert count.status_code == 200
        assert count.json()["unread_total"] == 1

        viewed = client.post("/api/notifications/viewed")
        assert viewed.status_code == 200
        assert viewed.json()["total"] == 1
        assert viewed.json()["unread_total"] == 0

        asyncio.run(
            notifications.upsert(
                type="pipelines.event",
                title="After view",
                payload={"priority": "low"},
            )
        )

        next_count = client.get("/api/notifications/count")
        assert next_count.status_code == 200
        assert next_count.json()["total"] == 2
        assert next_count.json()["unread_total"] == 1
        assert next_count.json()["unread_by_priority"] == {"low": 1, "medium": 0, "high": 0}


def test_notification_store_cancel_check_interrupts_before_read(tmp_path: Path) -> None:
    notifications = NotificationsRuntime(data_dir=tmp_path / "data")
    notifications.store.upsert(type="pipelines.event", title="Initial", payload={"priority": "medium"})

    def cancel_check() -> None:
        raise _TestCancelled()

    with pytest.raises(_TestCancelled):
        notifications.store.get("missing", cancel_check=cancel_check)

    with pytest.raises(_TestCancelled):
        notifications.store.list(limit=10, cancel_check=cancel_check)

    with pytest.raises(_TestCancelled):
        notifications.store.count_by_priority(cancel_check=cancel_check)


def test_notification_store_cancel_check_interrupts_sqlite_read_and_restores_handler(tmp_path: Path) -> None:
    notifications = NotificationsRuntime(data_dir=tmp_path / "data")
    for i in range(5_000):
        notifications.store.upsert(
            type="pipelines.event",
            title=f"Noise {i}",
            description="scan target",
            payload={"priority": "low"},
            now=1_000 + i,
        )

    calls = 0

    def cancel_after_query_starts() -> None:
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise _TestCancelled()

    with pytest.raises(_TestCancelled):
        notifications.store.list(
            limit=40,
            priorities=["low"],
            query="not-present",
            cancel_check=cancel_after_query_starts,
        )

    assert calls >= 4
    items, cursor = notifications.store.list(limit=5, priorities=["low"])
    assert len(items) == 5
    assert cursor is not None


def test_notification_store_reads_do_not_wait_for_write_lock(tmp_path: Path) -> None:
    notifications = NotificationsRuntime(data_dir=tmp_path / "data")
    notifications.store.upsert(type="pipelines.event", title="Initial", payload={"priority": "medium"})

    done = threading.Event()
    errors: list[BaseException] = []
    result: list[tuple[list[object], int | None]] = []

    def read_in_thread() -> None:
        try:
            result.append(notifications.store.list(limit=1, priorities=["medium"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    notifications.store._lock.acquire()
    try:
        thread = threading.Thread(target=read_in_thread)
        thread.start()
        assert done.wait(1.0)
        thread.join(timeout=1.0)
    finally:
        notifications.store._lock.release()

    assert errors == []
    assert len(result) == 1
    assert len(result[0][0]) == 1


def test_cancelable_request_limiter_cancels_before_work_starts() -> None:
    async def scenario() -> None:
        limiter = asyncio.Semaphore(1)
        await limiter.acquire()
        request = _FakeDisconnectRequest()
        work_ran = False

        def work(_check_cancelled):
            nonlocal work_ran
            work_ran = True
            return {"ok": True}

        task = asyncio.create_task(_run_cancelable_request_work(request, work, limiter=limiter))
        await asyncio.sleep(0.12)
        assert not work_ran
        request.disconnected = True

        with pytest.raises(HTTPException) as exc_info:
            await task
        assert exc_info.value.status_code == CLIENT_CLOSED_REQUEST_STATUS
        assert not work_ran
        limiter.release()

    asyncio.run(scenario())


def test_request_concurrency_limiter_can_prefer_latest_waiter() -> None:
    async def scenario() -> None:
        limiter = _RequestConcurrencyLimiter(1, prefer_latest=True)
        await limiter.acquire()
        order: list[str] = []

        async def waiter(name: str) -> None:
            await limiter.acquire()
            try:
                order.append(name)
            finally:
                limiter.release()

        first = asyncio.create_task(waiter("first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(waiter("second"))
        await asyncio.sleep(0)

        limiter.release()
        await asyncio.gather(first, second)

        assert order == ["second", "first"]

    asyncio.run(scenario())


def test_cancelable_request_limiters_are_independent_by_family() -> None:
    async def scenario() -> None:
        list_limiter = asyncio.Semaphore(1)
        detail_limiter = asyncio.Semaphore(1)
        await list_limiter.acquire()

        def work(_check_cancelled):
            return {"detail": True}

        result = await _run_cancelable_request_work(
            _FakeDisconnectRequest(),
            work,
            limiter=detail_limiter,
        )
        assert result == {"detail": True}
        list_limiter.release()

    asyncio.run(scenario())


def test_notifications_endpoint_filters_priority_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])

    with TestClient(create_app()) as client:
        notifications: NotificationsRuntime = client.app.state.notifications

        for i in range(55):
            notifications.store.upsert(
                type="pipelines.event" if i % 2 else "security.event",
                title=f"Garage medium {i}",
                description="filtered target",
                payload={"priority": "medium", "camera": "garage"},
                now=1_000 + i,
            )

        for i in range(160):
            notifications.store.upsert(
                type="pipelines.event",
                title=f"Recent low {i}",
                description="recent noise",
                payload={"priority": "low"},
                now=2_000 + i,
            )

        unfiltered = client.get("/api/notifications?limit=40")
        assert unfiltered.status_code == 200
        unfiltered_items = unfiltered.json()["notifications"]
        assert len(unfiltered_items) == 40
        assert {item["payload"].get("priority") for item in unfiltered_items} == {"low"}
        assert {item["priority"] for item in unfiltered_items} == {"low"}

        filtered = client.get("/api/notifications?priority=medium&limit=40")
        assert filtered.status_code == 200
        filtered_body = filtered.json()
        filtered_items = filtered_body["notifications"]
        assert len(filtered_items) == 40
        assert {item["payload"].get("priority") for item in filtered_items} == {"medium"}
        assert {item["priority"] for item in filtered_items} == {"medium"}
        assert filtered_body["next_cursor"] is not None

        next_page = client.get(
            f"/api/notifications?priority=medium&before={filtered_body['next_cursor']}&limit=40"
        )
        assert next_page.status_code == 200
        next_items = next_page.json()["notifications"]
        assert len(next_items) == 15
        assert {item["payload"].get("priority") for item in next_items} == {"medium"}
        assert {item["priority"] for item in next_items} == {"medium"}


def test_notifications_endpoint_combines_priority_type_and_query_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])

    with TestClient(create_app()) as client:
        notifications: NotificationsRuntime = client.app.state.notifications
        notifications.store.upsert(
            type="security.event",
            title="Garage person",
            description="target match",
            payload={"priority": "medium"},
            now=1_000,
        )
        notifications.store.upsert(
            type="security.event",
            title="Kitchen person",
            description="target miss",
            payload={"priority": "medium"},
            now=1_001,
        )
        notifications.store.upsert(
            type="pipelines.event",
            title="Garage pipeline",
            description="target type miss",
            payload={"priority": "medium"},
            now=1_002,
        )
        notifications.store.upsert(
            type="security.event",
            title="Garage low",
            description="target priority miss",
            payload={"priority": "low"},
            now=1_003,
        )

        res = client.get("/api/notifications?priority=medium&type=security.event&query=garage")
        assert res.status_code == 200
        items = res.json()["notifications"]
        assert [item["title"] for item in items] == ["Garage person"]
        assert [item["priority"] for item in items] == ["medium"]


def test_notifications_endpoint_medium_priority_includes_missing_or_invalid_payload_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: [])

    with TestClient(create_app()) as client:
        notifications: NotificationsRuntime = client.app.state.notifications
        notifications.store.upsert(
            type="pipelines.event",
            title="Missing priority",
            payload={},
            now=1_000,
        )
        notifications.store.upsert(
            type="pipelines.event",
            title="Invalid priority",
            payload={"priority": "urgent"},
            now=1_001,
        )
        notifications.store.upsert(
            type="pipelines.event",
            title="Explicit medium",
            payload={"priority": "medium"},
            now=1_002,
        )
        notifications.store.upsert(
            type="pipelines.event",
            title="Low priority",
            payload={"priority": "low"},
            now=1_003,
        )

        medium = client.get("/api/notifications?priority=medium")
        assert medium.status_code == 200
        assert [item["title"] for item in medium.json()["notifications"]] == [
            "Explicit medium",
            "Invalid priority",
            "Missing priority",
        ]
        by_title = {item["title"]: item for item in medium.json()["notifications"]}
        assert {item["priority"] for item in by_title.values()} == {"medium"}
        assert by_title["Invalid priority"]["payload"]["priority"] == "urgent"
        assert "priority" not in by_title["Missing priority"]["payload"]

        invalid = client.get("/api/notifications?priority=urgent")
        assert invalid.status_code == 200
        assert invalid.json() == {"notifications": [], "next_cursor": None}
