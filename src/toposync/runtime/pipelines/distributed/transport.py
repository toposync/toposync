from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Protocol


logger = logging.getLogger("toposync.pipelines.transport")


class ProcessingTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessingServerRef:
    id: str
    kind: Literal["inprocess", "http"] = "inprocess"
    url: str = ""


class ProcessingRuntimeLike(Protocol):
    def apply_config(self, payload: dict[str, Any]) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def replay_after(self, last_event_id: int) -> list[dict[str, Any]]: ...

    def ack(self, last_event_id: int) -> None: ...

    @property
    def broadcaster(self): ...  # noqa: ANN001


class ProcessingTransport(Protocol):
    async def push_config(self, payload: dict[str, Any]) -> None: ...

    async def stream_events(self, *, last_event_id: int = 0) -> AsyncIterator[dict[str, Any]]: ...

    async def ack(self, last_event_id: int) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def import_vision_manifest(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class InProcessProcessingTransport:
    def __init__(self, runtime: ProcessingRuntimeLike) -> None:
        self._runtime = runtime
        self._queue: asyncio.Queue[dict[str, Any]] | None = None

    async def push_config(self, payload: dict[str, Any]) -> None:
        self._runtime.apply_config(payload)

    async def stream_events(self, *, last_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        for item in self._runtime.replay_after(int(last_event_id)):
            yield dict(item)
        q = self._runtime.broadcaster.subscribe()
        self._queue = q
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            try:
                self._runtime.broadcaster.unsubscribe(q)
            except Exception:
                pass

    async def ack(self, last_event_id: int) -> None:
        self._runtime.ack(int(last_event_id))

    async def status(self) -> dict[str, Any]:
        return self._runtime.status()

    async def import_vision_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ProcessingTransportError("Vision manifest import is not supported for in-process transport")

    async def close(self) -> None:
        if self._queue is not None:
            try:
                self._runtime.broadcaster.unsubscribe(self._queue)
            except Exception:
                pass
            self._queue = None


class HttpProcessingTransport:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 30.0,
        username: str = "",
        password: str = "",
    ) -> None:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            raise ProcessingTransportError("Missing processing server base_url")
        self._base = base
        self._timeout_s = float(timeout_s)
        self._username = str(username or "").strip()
        self._password = str(password or "").strip()
        self._client = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import httpx  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise ProcessingTransportError("HttpProcessingTransport requires httpx") from exc
        auth = None
        if self._username or self._password:
            auth = httpx.BasicAuth(self._username, self._password)
        self._client = httpx.AsyncClient(timeout=None, auth=auth)
        return self._client

    async def push_config(self, payload: dict[str, Any]) -> None:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/config"
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing config push failed: {res.status_code} {res.text}")

    async def stream_events(self, *, last_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/events/stream"
        headers: dict[str, str] = {}
        if int(last_event_id) > 0:
            headers["Last-Event-ID"] = str(int(last_event_id))

        async with client.stream("GET", url, headers=headers) as res:
            if res.status_code >= 300:
                raise ProcessingTransportError(f"Processing event stream failed: {res.status_code} {res.text}")
            async for line in res.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                if isinstance(event, dict):
                    yield event

    async def ack(self, last_event_id: int) -> None:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/events/ack"
        res = await client.post(url, json={"last_event_id": int(last_event_id)}, timeout=self._timeout_s)
        if res.status_code >= 300:
            logger.debug("processing ack failed status=%s body=%s", res.status_code, res.text)

    async def status(self) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/status"
        res = await client.get(url, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing status failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def import_vision_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/manifests/import"
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(
                f"Processing vision manifest import failed: {res.status_code} {res.text}"
            )
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = None
