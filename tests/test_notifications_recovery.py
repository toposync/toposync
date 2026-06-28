from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
from toposync.runtime.notifications import NotificationsRuntime


class _TestCancelled(Exception):
    pass


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
            priorities=["medium"],
            query="scan",
            cancel_check=cancel_after_query_starts,
        )

    assert calls >= 4
    items, cursor = notifications.store.list(limit=5, priorities=["low"])
    assert len(items) == 5
    assert cursor is not None


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

        filtered = client.get("/api/notifications?priority=medium&limit=40")
        assert filtered.status_code == 200
        filtered_body = filtered.json()
        filtered_items = filtered_body["notifications"]
        assert len(filtered_items) == 40
        assert {item["payload"].get("priority") for item in filtered_items} == {"medium"}
        assert filtered_body["next_cursor"] is not None

        next_page = client.get(
            f"/api/notifications?priority=medium&before={filtered_body['next_cursor']}&limit=40"
        )
        assert next_page.status_code == 200
        next_items = next_page.json()["notifications"]
        assert len(next_items) == 15
        assert {item["payload"].get("priority") for item in next_items} == {"medium"}


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

        invalid = client.get("/api/notifications?priority=urgent")
        assert invalid.status_code == 200
        assert invalid.json() == {"notifications": [], "next_cursor": None}
