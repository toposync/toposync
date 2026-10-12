from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_home_assistant.pipelines import HomeAssistantNotifyRuntime


@pytest.mark.parametrize("tag", ["", "event:1", "a" * 64, "é" * 32, "a" * 65, "é" * 33, "🚗" * 17])
def test_notification_tags_respect_utf8_byte_limit(tag: str) -> None:
    runtime = HomeAssistantNotifyRuntime(
        {"tag_template": "{{subject.id}}"}, PipelineRuntimeDependencies()
    )
    packet = Packet.create(stream_id="event:1", payload={"subject": {"id": tag}})
    result = runtime._tag(packet)
    if not tag:
        assert result is None
    elif len(tag.encode("utf-8")) <= 64:
        assert result == tag
    else:
        assert result and len(result.encode("utf-8")) <= 64


def test_long_notification_tags_preserve_identity_and_clear_across_restarts() -> None:
    calls = []

    async def call_service(**kwargs):
        calls.append(kwargs["data"])

    services = ServiceRegistry()
    services.register("home_assistant.call_service", call_service)
    config = {
        "server_id": "ha-main",
        "notify_service": "notify.mobile_app_iphone",
        "notify_when": "open_update",
        "close_behavior": "clear",
        "tag_template": "toposync-{{subject.id}}",
    }

    async def run():
        for subject_id in ["shared-prefix-" * 50 + "first", "shared-prefix-" * 50 + "second"]:
            for lifecycle in [Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE]:
                runtime = HomeAssistantNotifyRuntime(config, PipelineRuntimeDependencies(services=services))
                await runtime.process_packet(
                    Packet.create(
                        stream_id=subject_id, lifecycle=lifecycle,
                        payload={"subject": {"id": subject_id}},
                    ), SimpleNamespace(),
                )

    asyncio.run(run())
    assert len(calls) == 6
    tags = [call["data"]["tag"] for call in calls]
    assert tags[0] == tags[1] == tags[2]
    assert tags[3] == tags[4] == tags[5]
    assert tags[0] != tags[3]
    assert all(len(tag.encode("utf-8")) <= 64 for tag in tags)
    assert calls[2]["message"] == calls[5]["message"] == "clear_notification"


def test_home_assistant_notify_runtime_treats_first_update_as_event_start_without_tag() -> None:
    calls: list[dict[str, object]] = []

    async def _fake_call_service(
        *,
        server_id: str,
        domain: str,
        service_name: str,
        data: dict[str, object],
    ) -> None:
        calls.append(
            {
                "server_id": server_id,
                "domain": domain,
                "service": service_name,
                "data": data,
            }
        )

    services = ServiceRegistry()
    services.register("home_assistant.call_service", _fake_call_service)

    runtime = HomeAssistantNotifyRuntime(
        {
            "server_id": "ha-main",
            "notify_service": "notify.mobile_app_pixel_9",
            "notify_when": "open",
            "title": "",
            "message": "",
        },
        PipelineRuntimeDependencies(services=services),
    )

    context = SimpleNamespace(node_id="ha_notify")

    async def _run() -> None:
        update_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "subject": {"type": "event", "id": "event:1", "category": "person"},
            },
        )
        second_update_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "subject": {"type": "event", "id": "event:1", "category": "person"},
            },
        )
        await runtime.process_packet(update_packet, context)
        await runtime.process_packet(second_update_packet, context)

    asyncio.run(_run())

    assert len(calls) == 1
    first = calls[0]
    assert first["server_id"] == "ha-main"
    assert first["domain"] == "notify"
    assert first["service"] == "mobile_app_pixel_9"
    assert first["data"] == {
        "title": "Person detected",
        "message": "Front gate • Driveway",
    }


def test_home_assistant_notify_runtime_uses_explicit_tag_to_clear_on_close() -> None:
    calls: list[dict[str, object]] = []

    async def _fake_call_service(
        *,
        server_id: str,
        domain: str,
        service_name: str,
        data: dict[str, object],
    ) -> None:
        calls.append(
            {
                "server_id": server_id,
                "domain": domain,
                "service": service_name,
                "data": data,
            }
        )

    services = ServiceRegistry()
    services.register("home_assistant.call_service", _fake_call_service)

    runtime = HomeAssistantNotifyRuntime(
        {
            "server_id": "ha-main",
            "notify_service": "notify.mobile_app_pixel_9",
            "notify_when": "open",
            "close_behavior": "clear",
            "title": "",
            "message": "",
            "tag_template": "camera:{{camera_id}}:event:{{payload.subject.id}}",
        },
        PipelineRuntimeDependencies(services=services),
    )

    context = SimpleNamespace(node_id="ha_notify")

    async def _run() -> None:
        open_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.OPEN,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "event_id": "event:1",
                "subject": {"type": "event", "id": "event:1", "category": "person"},
            },
        )
        close_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.CLOSE,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "event_id": "event:1",
                "subject": {"type": "event", "id": "event:1", "category": "person"},
            },
        )
        await runtime.process_packet(open_packet, context)
        await runtime.process_packet(close_packet, context)

    asyncio.run(_run())

    assert len(calls) == 2
    assert calls[0]["data"] == {
        "title": "Person detected",
        "message": "Front gate • Driveway",
        "data": {"tag": "camera:front:event:event:1"},
    }
    assert calls[1]["data"] == {
        "message": "clear_notification",
        "data": {"tag": "camera:front:event:event:1"},
    }
