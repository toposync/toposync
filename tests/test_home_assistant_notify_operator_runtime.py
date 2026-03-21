from __future__ import annotations

import asyncio
from types import SimpleNamespace

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_home_assistant.pipelines import HomeAssistantNotifyRuntime


def test_home_assistant_notify_runtime_sends_open_and_clears_on_close() -> None:
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
                "object_category_label": "person",
            },
        )
        update_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.UPDATE,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "object_category_label": "person",
            },
        )
        close_packet = Packet.create(
            stream_id="event:1",
            lifecycle=Lifecycle.CLOSE,
            payload={
                "camera_id": "front",
                "camera_name": "Front gate",
                "area_label": "Driveway",
                "object_category_label": "person",
            },
        )
        await runtime.process_packet(open_packet, context)
        await runtime.process_packet(update_packet, context)
        await runtime.process_packet(close_packet, context)

    asyncio.run(_run())

    assert len(calls) == 2

    first = calls[0]
    assert first["server_id"] == "ha-main"
    assert first["domain"] == "notify"
    assert first["service"] == "mobile_app_pixel_9"
    assert first["data"] == {
        "title": "Person detected",
        "message": "Front gate • Driveway",
        "data": {
            "tag": "ha_notify:ha_notify:camera:front:event:event:1",
        },
    }

    second = calls[1]
    assert second["data"] == {
        "message": "clear_notification",
        "data": {
            "tag": "ha_notify:ha_notify:camera:front:event:event:1",
        },
    }
