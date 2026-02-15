from __future__ import annotations

import asyncio
from pathlib import Path

from toposync.runtime.notifications import NotificationsRuntime


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

