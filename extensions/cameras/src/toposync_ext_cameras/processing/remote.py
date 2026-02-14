from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
import time
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
        self._last_event_id: int = 0
        self._last_acked_event_id: int = 0
        self._last_ack_ts: float = 0.0

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
        ack_url = f"{base}/api/processor/detections/ack"
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
                    headers = {}
                    if self._last_event_id > 0:
                        headers["Last-Event-ID"] = str(self._last_event_id)

                    async with client.stream("GET", stream_url, headers=headers) as res:
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
                            if self._handle_event(event):
                                await self._maybe_ack(client, ack_url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("remote processor stream failed server=%s: %s", self.server.id, exc)
                    await asyncio.sleep(min(10.0, reconnect_delay))
                    reconnect_delay = min(10.0, reconnect_delay * 1.6)

    async def _maybe_ack(self, client: httpx.AsyncClient, ack_url: str) -> None:
        if self._last_event_id <= self._last_acked_event_id:
            return
        now = time.monotonic()
        if (now - self._last_ack_ts) < 0.4 and (self._last_event_id - self._last_acked_event_id) < 20:
            return
        try:
            res = await client.post(ack_url, json={"last_event_id": self._last_event_id}, timeout=5.0)
            res.raise_for_status()
            self._last_acked_event_id = self._last_event_id
            self._last_ack_ts = now
        except Exception as exc:  # noqa: BLE001
            logger.debug("remote processor ack failed server=%s: %s", self.server.id, exc)

    def _handle_event(self, event: dict[str, Any]) -> bool:
        if not isinstance(event, dict):
            return False
        try:
            eid_i = int(event.get("event_id") or 0)
        except Exception:
            eid_i = 0
        enriched = dict(event)
        enriched["source"] = {"kind": "remote", "server_id": self.server.id, "url": self.server.url}
        try:
            self._on_event(enriched)
            if eid_i > 0:
                self._last_event_id = max(self._last_event_id, eid_i)
        except Exception:
            return False
        return True
