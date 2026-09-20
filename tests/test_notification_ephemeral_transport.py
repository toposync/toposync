"""Selected live image delivery must never become notification history."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from toposync.app import create_app
from toposync.runtime.notifications.events import EventBroadcaster
from toposync.runtime.notifications.runtime import NotificationsRuntime
from toposync.runtime.pipelines.operators_sinks import NotifyConfig


def image(milliseconds=500):
    return {"schemaVersion": 1, "expiresAt": time.time() * 1000 + milliseconds,
            "dataBase64": "cHJpdmF0ZS1waXhlbHM=", "packetId": "frame-1"}


def event(identifier="selected", sequence=1):
    return {"op": "update", "notification": {"id": identifier, "payload": {"sequence": sequence}}}


def test_images_require_explicit_realtime_opt_in():
    assert NotifyConfig().include_ephemeral_image is False
    assert NotifyConfig(include_ephemeral_image=True).realtime is True
    with pytest.raises(ValueError, match="require realtime"):
        NotifyConfig(include_ephemeral_image=True, realtime=False)


def test_selected_images_are_not_sent_to_global_other_or_non_opt_in_subscribers():
    async def scenario():
        broadcaster = EventBroadcaster()
        global_queue = broadcaster.subscribe()
        selected = broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        other = broadcaster.subscribe(notification_id="other", include_ephemeral_image=True)
        metadata = broadcaster.subscribe(notification_id="selected")
        value = event()
        broadcaster.publish(value, ephemeral_image=image())
        assert "ephemeralImage" in selected.get_nowait()["notification"]
        assert "ephemeralImage" not in global_queue.get_nowait()["notification"]
        assert "ephemeralImage" not in metadata.get_nowait()["notification"]
        assert "ephemeralImage" not in value["notification"]
        assert other.empty()
        for queue in [global_queue, selected, other, metadata]:
            broadcaster.unsubscribe(queue)

    asyncio.run(scenario())


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_selected_stream_task_cancellation_releases_subscription(tmp_path, spec_version):
    async def scenario():
        app = create_app()
        app.state.notifications = NotificationsRuntime(data_dir=tmp_path)
        app.state.auth = SimpleNamespace(mode="bypass", requires_setup=lambda: False, authorize=lambda **_kwargs: None)
        request = SimpleNamespace(app=app, state=SimpleNamespace(), is_disconnected=lambda: asyncio.sleep(0, result=False))
        route = next(route for route in app.routes if route.path == "/api/notifications/{notification_id}/stream")
        response = await route.endpoint(request, "selected", include_ephemeral_image=True)
        streaming = asyncio.Event()
        async def send(message):
            if message["type"] == "http.response.body":
                streaming.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send))
        await asyncio.wait_for(streaming.wait(), timeout=1)
        assert app.state.notifications.broadcaster.ephemeral_image_stream_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not app.state.notifications.broadcaster._subscribers

    asyncio.run(scenario())


def test_selected_stream_capacity_race_is_bounded_and_preflight_returns_429(tmp_path):
    async def scenario():
        app = create_app()
        app.state.notifications = NotificationsRuntime(data_dir=tmp_path)
        app.state.auth = SimpleNamespace(mode="bypass", requires_setup=lambda: False, authorize=lambda **_kwargs: None)
        request = SimpleNamespace(app=app, state=SimpleNamespace(), is_disconnected=lambda: asyncio.sleep(0, result=False))
        route = next(route for route in app.routes if route.path == "/api/notifications/{notification_id}/stream")
        responses = [await route.endpoint(request, "selected", include_ephemeral_image=True) for _ in range(17)]
        for response in responses[:16]:
            assert await anext(response.body_iterator) == "retry: 1000\n\n"
        assert "ephemeral_image_stream_limit" in await anext(responses[-1].body_iterator)
        assert app.state.notifications.broadcaster.ephemeral_image_stream_count == 16
        with pytest.raises(HTTPException) as caught:
            await route.endpoint(request, "selected", include_ephemeral_image=True)
        assert caught.value.status_code == 429
        for response in responses:
            await response.body_iterator.aclose()
        assert not app.state.notifications.broadcaster._subscribers

    asyncio.run(scenario())


def test_silent_updates_reach_only_the_selected_detail_and_never_persist_image(tmp_path):
    async def scenario():
        runtime = NotificationsRuntime(data_dir=tmp_path)
        first = await runtime.upsert(type="fixture", title="Silent", dedupe_key="subject",
                                     payload={"priority": "silent"})
        global_queue = runtime.broadcaster.subscribe()
        selected = runtime.broadcaster.subscribe(notification_id=first["id"], include_ephemeral_image=True)
        transient = image()
        returned = await runtime.upsert(type="fixture", title="Silent", dedupe_key="subject",
                                        payload={"priority": "silent"}, ephemeral_image=transient)
        assert "ephemeralImage" in selected.get_nowait()["notification"]
        assert global_queue.empty()
        assert "ephemeralImage" not in returned
        assert "ephemeralImage" not in await runtime.get(first["id"])
        records, _ = runtime.list_sync(include_silent=True, priorities=["silent"])
        assert transient["dataBase64"] not in json.dumps(records)
        rows = runtime.store._conn.execute("SELECT payload_json FROM notification").fetchall()
        assert all(transient["dataBase64"] not in row[0] for row in rows)
        assert not any(path.suffix in {".jpg", ".png", ".webp"} for path in tmp_path.rglob("*"))
        runtime.broadcaster.unsubscribe(global_queue)
        runtime.broadcaster.unsubscribe(selected)

    asyncio.run(scenario())


def test_latest_only_queue_expiration_close_and_unsubscribe_release_bytes():
    async def scenario():
        broadcaster = EventBroadcaster()
        selected = broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        broadcaster.publish(event(), ephemeral_image=image())
        previous = selected.get_nowait()
        broadcaster.publish(event(sequence=2), ephemeral_image=image(15))
        assert "ephemeralImage" not in previous["notification"]
        assert selected.qsize() == 1
        await asyncio.sleep(0.025)
        assert "ephemeralImage" not in selected.get_nowait()["notification"]
        for sequence in range(1000):
            broadcaster.publish(event(sequence=sequence), ephemeral_image=image())
        assert selected.qsize() == 1
        assert selected.get_nowait()["notification"]["payload"]["sequence"] == 999
        broadcaster.publish(event(), ephemeral_image=image())
        current = selected.get_nowait()
        broadcaster.publish({"op": "update", "notification": {"id": "selected", "payload": {"lifecycle": "close"}}})
        assert "ephemeralImage" not in current["notification"]
        assert "ephemeralImage" not in selected.get_nowait()["notification"]
        broadcaster.publish(event(), ephemeral_image=image())
        current = selected.get_nowait()
        broadcaster.unsubscribe(selected)
        assert "ephemeralImage" not in current["notification"]
        assert not broadcaster._subscribers

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, {}, {"expiresAt": True}, {"expiresAt": float("nan")},
                                   {"expiresAt": -1}, {"expiresAt": 1e30}, {"expiresAt": 10**1000}])
def test_invalid_or_stale_expiry_omits_image(value):
    async def scenario():
        broadcaster = EventBroadcaster()
        queue = broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        broadcaster.publish(event(), ephemeral_image=value)
        assert "ephemeralImage" not in queue.get_nowait()["notification"]
        broadcaster.unsubscribe(queue)

    asyncio.run(scenario())


def test_stream_count_and_envelope_size_are_bounded():
    async def scenario():
        broadcaster = EventBroadcaster()
        with pytest.raises(ValueError, match="selected notification"):
            broadcaster.subscribe(include_ephemeral_image=True)
        queues = [broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True) for _ in range(16)]
        with pytest.raises(ValueError, match="Too many"):
            broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        oversized = image()
        oversized["dataBase64"] = "x" * (384 * 1024)
        broadcaster.publish(event(), ephemeral_image=oversized)
        assert all("ephemeralImage" not in queue.get_nowait()["notification"] for queue in queues)
        for queue in queues:
            broadcaster.unsubscribe(queue)
        queue = broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        broadcaster.unsubscribe(queue)

    asyncio.run(scenario())


def test_clock_rollback_during_publish_cannot_extend_image_retention():
    async def scenario():
        broadcaster = EventBroadcaster()
        queue = broadcaster.subscribe(notification_id="selected", include_ephemeral_image=True)
        with patch("toposync.runtime.notifications.events.time.time", side_effect=[1000.0, 900.0]):
            broadcaster.publish(event(), ephemeral_image={"expiresAt": 1000500, "dataBase64": "AA=="})
        expiry = broadcaster._subscribers[queue].expiry
        assert 0 < expiry.when() - asyncio.get_running_loop().time() <= 0.5
        broadcaster.unsubscribe(queue)

    asyncio.run(scenario())


def test_notify_opt_in_encodes_real_pixels_without_storage_and_shutdown_clears_them(tmp_path, monkeypatch):
    import base64
    import io
    from PIL import Image
    from test_notification_image import packet
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.operators_sinks import NotifyRuntime

    monkeypatch.setattr(time, "time", lambda: 1000.1)

    async def scenario():
        notifications = NotificationsRuntime(data_dir=tmp_path)
        notify = NotifyRuntime({"include_ephemeral_image": True, "update_interval_seconds": 0,
                                "include_payload_paths": ["camera_id", "source_stream_id", "capture_evidence"]},
                               PipelineRuntimeDependencies(notifications_upsert=notifications.upsert))
        value = packet()
        value.payload["subject"] = {"id": "person"}
        context = SimpleNamespace(pipeline_name="fixture", node_id="notify")
        await notify.process_packet(value, context)
        items, _ = await notifications.list()
        selected = notifications.broadcaster.subscribe(notification_id=items[0]["id"], include_ephemeral_image=True)
        value.payload["capture_evidence"]["sequence"] += 1
        value.artifacts["main"].metadata["image_geometry"]["capture_evidence"]["sequence"] += 1
        await notify.process_packet(value, context)
        emitted = selected.get_nowait()["notification"]
        descriptor = emitted["ephemeralImage"]
        assert descriptor["packetId"] == value.packet_id
        assert descriptor["parentPacketId"] == value.parent_packet_id
        assert descriptor["captureEvidence"] == value.payload["capture_evidence"]
        with Image.open(io.BytesIO(base64.b64decode(descriptor["dataBase64"]))) as decoded:
            decoded.load()
            assert decoded.size == (64, 48)
            assert decoded.getpixel((20, 20))[0] > 230
        stored = await notifications.get(emitted["id"])
        assert descriptor["dataBase64"] not in json.dumps(stored)
        assert stored["payload"]["ephemeral_image_status"] == {"status": "ready", "reason": "ready"}
        await notify.shutdown()
        assert "ephemeralImage" not in emitted
        closed = selected.get_nowait()["notification"]
        assert closed["payload"]["lifecycle"] == "close"
        assert "ephemeralImage" not in closed
        notifications.broadcaster.unsubscribe(selected)

    asyncio.run(scenario())


def test_malformed_capture_does_not_suppress_metadata_notification():
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
    from toposync.runtime.pipelines.runtime import Packet, Lifecycle

    async def scenario():
        emitted = []
        async def upsert(**kwargs):
            emitted.append(kwargs)
        notify = NotifyRuntime({"include_ephemeral_image": True}, PipelineRuntimeDependencies(notifications_upsert=upsert))
        value = Packet.create(stream_id="fixture", lifecycle=Lifecycle.UPDATE,
                              payload={"subject": {"id": "person"}, "capture_evidence": {"published_at": object()}})
        await notify.process_packet(value, SimpleNamespace(pipeline_name="fixture", node_id="notify"))
        assert len(emitted) == 1
        assert emitted[0]["ephemeral_image"] is None
        assert emitted[0]["payload"]["ephemeral_image_status"]["status"] == "unavailable"

    asyncio.run(scenario())


def test_selected_route_authorizes_stream_and_serializes_live_image_without_cache(tmp_path):
    async def scenario():
        app = create_app()
        runtime = NotificationsRuntime(data_dir=tmp_path)
        app.state.notifications = runtime
        actions = []
        app.state.auth = SimpleNamespace(mode="bypass", requires_setup=lambda: False,
                                         authorize=lambda **kwargs: actions.append(kwargs["action"]))
        request = SimpleNamespace(app=app, state=SimpleNamespace(), is_disconnected=lambda: asyncio.sleep(0, result=False))
        route = next(route for route in app.routes if route.path == "/api/notifications/{notification_id}/stream")
        for _ in range(20):
            abandoned = await route.endpoint(request, "selected", include_ephemeral_image=True)
            await abandoned.body_iterator.aclose()
        assert runtime.broadcaster.ephemeral_image_stream_count == 0
        actions.clear()
        response = await route.endpoint(request, "selected", include_ephemeral_image=True)
        assert actions == ["core:notifications:stream"]
        assert response.headers["cache-control"] == "no-store, no-transform"
        iterator = response.body_iterator
        assert await anext(iterator) == "retry: 1000\n\n"
        assert "event: ready" in await anext(iterator)
        runtime.broadcaster.publish(event(), ephemeral_image=image())
        wire = await anext(iterator)
        assert json.loads(wire.removeprefix("data: "))["notification"]["ephemeralImage"]["packetId"] == "frame-1"
        await iterator.aclose()
        assert not runtime.broadcaster._subscribers
        def denied(**_kwargs):
            raise HTTPException(status_code=403, detail="Denied")
        app.state.auth.authorize = denied
        with pytest.raises(HTTPException) as caught:
            await route.endpoint(request, "selected", include_ephemeral_image=True)
        assert caught.value.status_code == 403
        assert not runtime.broadcaster._subscribers

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_at", ["http.response.start", "http.response.body"])
def test_selected_stream_disconnect_releases_subscription_before_response_returns(tmp_path, failure_at):
    from starlette.requests import ClientDisconnect

    async def scenario():
        app = create_app()
        app.state.notifications = NotificationsRuntime(data_dir=tmp_path)
        app.state.auth = SimpleNamespace(mode="bypass", requires_setup=lambda: False, authorize=lambda **_kwargs: None)
        request = SimpleNamespace(app=app, state=SimpleNamespace(), is_disconnected=lambda: asyncio.sleep(0, result=False))
        route = next(route for route in app.routes if route.path == "/api/notifications/{notification_id}/stream")
        response = await route.endpoint(request, "selected", include_ephemeral_image=True)
        async def send(message):
            if message["type"] == failure_at:
                raise OSError("Client closed")
        async def receive():
            return {"type": "http.disconnect"}
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert app.state.notifications.broadcaster.ephemeral_image_stream_count == 0
        assert not app.state.notifications.broadcaster._subscribers

    asyncio.run(scenario())
