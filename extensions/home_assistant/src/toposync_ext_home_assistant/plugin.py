from __future__ import annotations

from fastapi import FastAPI

from toposync.extensions import BaseExtension
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry


class HomeAssistantExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_home_assistant")

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        return None

