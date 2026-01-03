from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventOutcome
from toposync.runtime.event_bus import Handler as EventHandler
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry


class HelloLampExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_hello_lamp")

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:
        bus.on("device.action_requested", self._on_device_action(services), priority=100)

        @app.get("/api/hello-lamp")
        async def hello_lamp() -> dict[str, str]:
            return {"hello": "lamp"}

    def _on_device_action(self, services: ServiceRegistry) -> EventHandler:
        async def handler(payload: Any, _ctx: dict[str, Any]) -> EventOutcome | None:
            if not isinstance(payload, dict):
                return None
            if payload.get("device_id") != "lamp1":
                return None
            if payload.get("action") != "toggle":
                return None

            state = await services.call("devices.toggle", device_id="lamp1")
            return EventOutcome(
                prevent_default=True,
                stop_propagation=True,
                result={"device_id": "lamp1", "state": state, "handled_by": "com.toposync.hello_lamp"},
            )

        return handler
