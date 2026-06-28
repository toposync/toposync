from __future__ import annotations

import asyncio
from typing import Any

import pytest

from toposync_ext_cinematic.director import NotificationEventFeed
from toposync_ext_cinematic.pipelines import CinematicDirectorSourceConfig


class _Services:
    def __init__(self, notifications: list[dict[str, Any]], *, next_cursor: int | None = 42) -> None:
        self.notifications = notifications
        self.next_cursor = next_cursor
        self.calls: list[dict[str, Any]] = []

    async def call(self, service_id: str, **kwargs: Any) -> Any:
        self.calls.append({"service_id": service_id, **kwargs})
        assert service_id == "notifications.list"
        return {"notifications": self.notifications, "next_cursor": self.next_cursor}


def _config(**values: object) -> CinematicDirectorSourceConfig:
    return CinematicDirectorSourceConfig.model_validate(values)


def _notification(
    notification_id: str | None,
    *,
    payload: dict[str, Any],
    priority: str = "medium",
    updated_at: str = "1970-01-01T00:00:20+00:00",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "priority": priority,
        "createdAt": "1970-01-01T00:00:10+00:00",
        "updatedAt": updated_at,
        "payload": payload,
    }
    if notification_id is not None:
        item["id"] = notification_id
    return item


def test_notification_event_feed_uses_notification_service_and_normalizes_event() -> None:
    asyncio.run(_run_uses_notification_service_and_normalizes_event())


async def _run_uses_notification_service_and_normalizes_event() -> None:
    services = _Services(
        [
            _notification(
                "n1",
                priority="high",
                payload={
                    "pipeline_name": "person-front",
                    "camera_id": "front",
                    "source_id": "main",
                    "stream_id": "stream-1",
                    "lifecycle": "open",
                    "priority": "high",
                    "subject": {"id": "person-1", "type": "person", "area_label": "Door"},
                    "event_id": "event-1",
                    "event": {"started_ts": 15.0, "ts": 20.0},
                    "data": {"confidence": 0.91},
                },
            )
        ],
        next_cursor=7,
    )
    feed = NotificationEventFeed(
        services,
        _config(priority_filter=["high"]),
        notification_types=["pipelines.event"],
    )

    batch = await feed.poll(cursor=99, limit=999)

    assert services.calls == [
        {
            "service_id": "notifications.list",
            "before": 99,
            "limit": 250,
            "priorities": ["high"],
            "types": ["pipelines.event"],
        }
    ]
    assert batch.next_cursor == 7
    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.key == "notification:n1"
    assert event.source_kind == "notification"
    assert event.priority == "high"
    assert event.lifecycle == "open"
    assert event.pipeline_name == "person-front"
    assert event.notification_id == "n1"
    assert event.event_id == "event-1"
    assert event.camera_id == "front"
    assert event.source_id == "main"
    assert event.area_label == "Door"
    assert event.confidence == pytest.approx(0.91)
    assert event.opened_at == pytest.approx(15.0)
    assert event.updated_at == pytest.approx(20.0)
    assert event.closed_at is None


def test_notification_event_feed_resolves_camera_id_by_configured_precedence() -> None:
    asyncio.run(_run_resolves_camera_id_by_configured_precedence())


async def _run_resolves_camera_id_by_configured_precedence() -> None:
    services = _Services(
        [
            _notification(
                "payload",
                payload={
                    "pipeline_name": "p1",
                    "camera_id": "payload-camera",
                    "source": {"device_id": "source-camera"},
                },
            ),
            _notification(
                "source",
                payload={"pipeline_name": "p2", "source": {"device_id": "source-camera"}},
            ),
            _notification(
                "data",
                payload={"pipeline_name": "p3", "data": {"camera_id": "data-camera"}},
            ),
            _notification(
                "subject",
                payload={"pipeline_name": "p4", "subject": {"camera_id": "subject-camera"}},
            ),
            _notification(
                "subject-source",
                payload={
                    "pipeline_name": "p5",
                    "subject": {"source": {"device_id": "subject-source-camera"}},
                },
            ),
            _notification("mapped", payload={"pipeline_name": "mapped-pipeline"}),
        ]
    )
    feed = NotificationEventFeed(
        services,
        _config(pipeline_camera_map={"mapped-pipeline": "mapped-camera"}),
    )

    batch = await feed.poll()

    by_id = {event.notification_id: event.camera_id for event in batch.events}
    assert by_id == {
        "payload": "payload-camera",
        "source": "source-camera",
        "data": "data-camera",
        "subject": "subject-camera",
        "subject-source": "subject-source-camera",
        "mapped": "mapped-camera",
    }


def test_notification_event_feed_coalesces_by_key_precedence_and_keeps_newest() -> None:
    asyncio.run(_run_coalesces_by_key_precedence_and_keeps_newest())


async def _run_coalesces_by_key_precedence_and_keeps_newest() -> None:
    services = _Services(
        [
            _notification(
                "with-id-a",
                updated_at="1970-01-01T00:00:11+00:00",
                payload={"camera_id": "front", "subject_id": "same-subject"},
            ),
            _notification(
                "with-id-b",
                updated_at="1970-01-01T00:00:12+00:00",
                payload={"camera_id": "garage", "subject_id": "same-subject"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:13+00:00",
                payload={"camera_id": "old", "subject_id": "subject-only"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:14+00:00",
                payload={"camera_id": "new", "subject_id": "subject-only"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:15+00:00",
                payload={"camera_id": "event-old", "event_id": "event-only"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:16+00:00",
                payload={"camera_id": "event-new", "event_id": "event-only"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:17+00:00",
                payload={"pipeline_name": "p", "camera_id": "fallback", "stream_id": "stream"},
            ),
            _notification(
                None,
                updated_at="1970-01-01T00:00:18+00:00",
                payload={"pipeline_name": "p", "camera_id": "fallback", "stream_id": "stream"},
            ),
        ]
    )
    feed = NotificationEventFeed(services, _config())

    batch = await feed.poll()

    by_key = {event.key: event for event in batch.events}
    assert set(by_key) == {
        "notification:with-id-a",
        "notification:with-id-b",
        "subject:subject-only",
        "event:event-only",
        "pipeline_camera_stream:p|fallback|stream",
    }
    assert by_key["notification:with-id-a"].camera_id == "front"
    assert by_key["notification:with-id-b"].camera_id == "garage"
    assert by_key["subject:subject-only"].camera_id == "new"
    assert by_key["event:event-only"].camera_id == "event-new"
    assert by_key["pipeline_camera_stream:p|fallback|stream"].updated_at == pytest.approx(18.0)


def test_notification_event_feed_normalizes_closed_and_unknown_values() -> None:
    asyncio.run(_run_normalizes_closed_and_unknown_values())


async def _run_normalizes_closed_and_unknown_values() -> None:
    services = _Services(
        [
            _notification(
                "closed",
                priority="unexpected",
                payload={
                    "camera_id": "front",
                    "status": "closed",
                    "priority": "unexpected",
                    "event": {"started_ts": 10.0, "ts": 22.0},
                },
            )
        ]
    )
    feed = NotificationEventFeed(services, _config())

    batch = await feed.poll()

    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.priority == "medium"
    assert event.lifecycle == "close"
    assert event.closed_at == pytest.approx(22.0)
