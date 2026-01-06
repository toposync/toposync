from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RemoteProcessorServer:
    id: str
    url: str


class RemoteProcessorClient:
    def __init__(
        self,
        *,
        server: RemoteProcessorServer,
        on_event: callable,
        stop_event: asyncio.Event,
    ) -> None:
        self.server = server
        self._on_event = on_event
        self._stop_event = stop_event

        self._desired_config_json: str | None = None
        self._watch_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch(), name=f"cameras.remote[{self.server.id}]")

    async def stop(self) -> None:
        if self._watch_task is None:
            return
        self._watch_task.cancel()
        try:
            await self._watch_task
        except asyncio.CancelledError:
            pass
        self._watch_task = None

    def update_config(self, payload: dict[str, Any]) -> None:
        self._desired_config_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    async def _watch(self) -> None:
        base = self.server.url.rstrip("/")
        config_url = f"{base}/api/processor/config"
        stream_url = f"{base}/api/processor/detections/stream"

        last_sent: str | None = None
        reconnect_delay = 1.5

        async with httpx.AsyncClient(timeout=None) as client:
            while not self._stop_event.is_set():
                desired = self._desired_config_json
                if desired and desired != last_sent:
                    try:
                        res = await client.post(config_url, content=desired, headers={"content-type": "application/json"})
                        res.raise_for_status()
                        last_sent = desired
                        reconnect_delay = 1.5
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("remote processor config push failed server=%s: %s", self.server.id, exc)
                        await asyncio.sleep(min(10.0, reconnect_delay))
                        reconnect_delay = min(10.0, reconnect_delay * 1.6)
                        continue

                try:
                    async with client.stream("GET", stream_url) as res:
                        res.raise_for_status()
                        reconnect_delay = 1.5
                        async for line in res.aiter_lines():
                            if self._stop_event.is_set():
                                break
                            if not line:
                                continue
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if not raw:
                                continue
                            try:
                                event = json.loads(raw)
                            except Exception:
                                continue
                            self._handle_event(event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("remote processor stream failed server=%s: %s", self.server.id, exc)
                    await asyncio.sleep(min(10.0, reconnect_delay))
                    reconnect_delay = min(10.0, reconnect_delay * 1.6)

    def _handle_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        enriched = dict(event)
        enriched["source"] = {"kind": "remote", "server_id": self.server.id, "url": self.server.url}
        try:
            self._on_event(enriched)
        except Exception:
            return
