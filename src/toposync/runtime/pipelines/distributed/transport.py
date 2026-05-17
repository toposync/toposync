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
    async def apply_config(self, payload: dict[str, Any]) -> None: ...

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

    async def inspect_vision_custom_onnx(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]: ...

    async def preview_vision_custom_onnx(
        self,
        *,
        payload: dict[str, Any],
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]: ...

    async def import_vision_custom_onnx(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def probe_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def inspect_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def export_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def import_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def upload_vision_model_artifact(
        self,
        *,
        model_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class InProcessProcessingTransport:
    def __init__(self, runtime: ProcessingRuntimeLike) -> None:
        self._runtime = runtime
        self._queue: asyncio.Queue[dict[str, Any]] | None = None

    async def push_config(self, payload: dict[str, Any]) -> None:
        await self._runtime.apply_config(payload)

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

    async def inspect_vision_custom_onnx(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        _ = (filename, content_type, content)
        raise ProcessingTransportError("Custom ONNX inspection is not supported for in-process transport")

    async def preview_vision_custom_onnx(
        self,
        *,
        payload: dict[str, Any],
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        _ = (payload, filename, content_type, content)
        raise ProcessingTransportError("Custom ONNX preview is not supported for in-process transport")

    async def import_vision_custom_onnx(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise ProcessingTransportError("Custom ONNX import is not supported for in-process transport")

    async def probe_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise ProcessingTransportError("Hugging Face probe is not supported for in-process transport")

    async def inspect_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise ProcessingTransportError("Hugging Face inspect is not supported for in-process transport")

    async def export_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise ProcessingTransportError("Hugging Face export is not supported for in-process transport")

    async def import_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise ProcessingTransportError("Hugging Face import is not supported for in-process transport")

    async def upload_vision_model_artifact(
        self,
        *,
        model_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        raise ProcessingTransportError("Vision model artifact upload is not supported for in-process transport")

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

    async def inspect_vision_custom_onnx(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/custom-onnx/inspect"
        files = {"file": (filename or "custom-model.onnx", content, content_type or "application/octet-stream")}
        res = await client.post(url, files=files, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing custom ONNX inspect failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def preview_vision_custom_onnx(
        self,
        *,
        payload: dict[str, Any],
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/custom-onnx/preview"
        files = {"image": (filename or "preview-image.png", content, content_type or "application/octet-stream")}
        data = {"config_json": json.dumps(payload)}
        res = await client.post(url, data=data, files=files, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing custom ONNX preview failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def import_vision_custom_onnx(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/custom-onnx/import"
        res = await client.post(url, json=payload, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing custom ONNX import failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def probe_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/huggingface/probe"
        res = await client.post(url, json=payload, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing Hugging Face probe failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def inspect_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/huggingface/inspect"
        res = await client.post(url, json=payload, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing Hugging Face inspect failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def export_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/huggingface/export"
        res = await client.post(url, json=payload, timeout=max(self._timeout_s, 1200.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing Hugging Face export failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def import_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/huggingface/import"
        res = await client.post(url, json=payload, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing Hugging Face import failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def install_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/models/{model_id}/install"
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing vision model install failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def cancel_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/models/{model_id}/cancel"
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing vision model cancel failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def retry_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/models/{model_id}/retry"
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing vision model retry failed: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def upload_vision_model_artifact(
        self,
        *,
        model_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/vision/models/{model_id}/artifact"
        files = {"file": (filename or f"{model_id}.onnx", content, content_type or "application/octet-stream")}
        res = await client.post(url, files=files, timeout=max(self._timeout_s, 120.0))
        if res.status_code >= 300:
            raise ProcessingTransportError(f"Processing vision model artifact upload failed: {res.status_code} {res.text}")
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
