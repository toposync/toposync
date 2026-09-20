from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit
from typing import Any, AsyncIterator


from .stream_contract import (
    PRIVATE_EVENT_MAX_BYTES, PRIVATE_STREAM_CAPABILITY,
    ProcessingTransportError, ProcessingContinuityError,
)

logger = logging.getLogger("toposync.pipelines.transport")
PROCESSING_READY_EVENT_TYPE = "processing_ready"


async def _bounded_private_lines(response):
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            if len(line) > PRIVATE_EVENT_MAX_BYTES + 32:
                raise ProcessingContinuityError("private_event_size_limit_exceeded")
            buffer = bytearray(remainder)
            yield line.decode("utf-8")
        if len(buffer) > PRIVATE_EVENT_MAX_BYTES + 32:
            raise ProcessingContinuityError("private_event_size_limit_exceeded")
    if buffer:
        raise ProcessingContinuityError("incomplete_private_event")


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
        self._private_stream = False
        self._stream_instance_id: str | None = None

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
        self._client = httpx.AsyncClient(timeout=None, auth=auth, trust_env=False)
        return self._client

    @staticmethod
    def _json_response(res: Any, error_prefix: str) -> dict[str, Any]:
        if res.status_code >= 300:
            raise ProcessingTransportError(f"{error_prefix}: {res.status_code} {res.text}")
        body = res.json()
        return body if isinstance(body, dict) else {}

    async def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        error_prefix: str,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        res = await client.post(
            f"{self._base}{path}",
            json=payload,
            timeout=self._timeout_s if timeout_s is None else timeout_s,
        )
        return self._json_response(res, error_prefix)

    async def _post_files(
        self,
        path: str,
        *,
        files: dict[str, Any],
        error_prefix: str,
        data: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        res = await client.post(
            f"{self._base}{path}",
            data=data,
            files=files,
            timeout=self._timeout_s if timeout_s is None else timeout_s,
        )
        return self._json_response(res, error_prefix)

    async def push_config(self, payload: dict[str, Any]) -> None:
        required = set(payload.get("required_capabilities") or [])
        private = "private_artifacts_v1" in required
        if private:
            required.add(PRIVATE_STREAM_CAPABILITY)
            payload = {**payload, "required_capabilities": sorted(required)}
            location = urlsplit(self._base)
            loopback = location.hostname in {"127.0.0.1", "::1", "localhost"}
            if (
                (location.scheme != "https" and not loopback)
                or not self._username
                or not self._password
            ):
                raise ProcessingTransportError(
                    "Private artifacts require authenticated HTTPS (HTTP is allowed only on loopback)"
                )
            status = await self.status()
            if self._stream_instance_id is not None and status.get("stream_instance_id") != self._stream_instance_id:
                raise ProcessingContinuityError("processing_stream_restarted")
            if not required.issubset(set(status.get("transport_capabilities") or [])):
                raise ProcessingTransportError(
                    "Processing server does not support the required private artifact protocol"
                )
        result = await self._post_json(
            "/api/processing/config", payload, error_prefix="Processing config push failed",
        )
        if private:
            instance = result.get("stream_instance_id")
            if not isinstance(instance, str) or not instance:
                raise ProcessingContinuityError("missing_processing_stream_instance")
            if self._stream_instance_id is not None and instance != self._stream_instance_id:
                raise ProcessingContinuityError("processing_stream_restarted")
            self._stream_instance_id = instance
        else:
            self._stream_instance_id = None
        self._private_stream = private

    async def stream_events(self, *, last_event_id: int = 0,
                            event_epoch: str = "") -> AsyncIterator[dict[str, Any]]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/events/stream"
        headers: dict[str, str] = {}
        if self._private_stream:
            headers["X-Toposync-Private-Stream"] = PRIVATE_STREAM_CAPABILITY
        if int(last_event_id) > 0:
            headers["Last-Event-ID"] = str(int(last_event_id))
        if event_epoch:
            headers["Last-Event-Epoch"] = event_epoch

        async with client.stream("GET", url, headers=headers) as res:
            if res.status_code >= 300:
                raise ProcessingTransportError(f"Processing event stream failed: {res.status_code}")
            if self._private_stream and res.headers.get("X-Toposync-Stream-Instance") != self._stream_instance_id:
                raise ProcessingContinuityError("processing_stream_restarted")
            cursor = int(last_event_id)
            lines = _bounded_private_lines(res) if self._private_stream else res.aiter_lines()
            async for line in lines:
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except Exception as exc:
                    if self._private_stream:
                        raise ProcessingContinuityError("invalid_private_event") from exc
                    continue
                if self._private_stream:
                    if not isinstance(event, dict):
                        raise ProcessingContinuityError("invalid_private_event")
                    if event.get("event_type") == "continuity_error":
                        raise ProcessingContinuityError(str(event.get("reason") or "replay_gap"))
                    event_id = event.get("event_id")
                    if type(event_id) is not int or event_id != cursor + 1:
                        raise ProcessingContinuityError("private_event_sequence_gap")
                    cursor = event_id
                if isinstance(event, dict):
                    yield event

    async def ack(self, last_event_id: int, *, event_epoch: str = "") -> None:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/events/ack"
        payload = {"last_event_id": int(last_event_id)}
        if event_epoch:
            payload["event_epoch"] = event_epoch
        if self._private_stream:
            payload["stream_instance_id"] = self._stream_instance_id
        res = await client.post(url, json=payload, timeout=self._timeout_s)
        if self._private_stream and res.status_code == 409:
            raise ProcessingContinuityError("private_ack_rejected")
        if self._private_stream and res.status_code >= 300:
            raise ProcessingTransportError(f"Private processing ack failed: {res.status_code}")
        if res.status_code >= 300:
            logger.debug("processing ack failed status=%s body=%s", res.status_code, res.text)

    async def status(self) -> dict[str, Any]:
        client = await self._ensure_client()
        url = f"{self._base}/api/processing/status"
        res = await client.get(url, timeout=self._timeout_s)
        return self._json_response(res, "Processing status failed")

    async def import_vision_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/manifests/import",
            payload,
            error_prefix="Processing vision manifest import failed",
        )

    async def inspect_vision_custom_onnx(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        files = {
            "file": (
                filename or "custom-model.onnx",
                content,
                content_type or "application/octet-stream",
            )
        }
        return await self._post_files(
            "/api/processing/vision/custom-onnx/inspect",
            files=files,
            error_prefix="Processing custom ONNX inspect failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def preview_vision_custom_onnx(
        self,
        *,
        payload: dict[str, Any],
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        files = {
            "image": (
                filename or "preview-image.png",
                content,
                content_type or "application/octet-stream",
            )
        }
        data = {"config_json": json.dumps(payload)}
        return await self._post_files(
            "/api/processing/vision/custom-onnx/preview",
            data=data,
            files=files,
            error_prefix="Processing custom ONNX preview failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def import_vision_custom_onnx(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/custom-onnx/import",
            payload,
            error_prefix="Processing custom ONNX import failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def probe_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/huggingface/probe",
            payload,
            error_prefix="Processing Hugging Face probe failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def inspect_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/huggingface/inspect",
            payload,
            error_prefix="Processing Hugging Face inspect failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def export_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/huggingface/export",
            payload,
            error_prefix="Processing Hugging Face export failed",
            timeout_s=max(self._timeout_s, 1200.0),
        )

    async def import_vision_huggingface(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            "/api/processing/vision/huggingface/import",
            payload,
            error_prefix="Processing Hugging Face import failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def install_vision_model(
        self, *, model_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/api/processing/vision/models/{model_id}/install",
            payload,
            error_prefix="Processing vision model install failed",
        )

    async def cancel_vision_model(
        self, *, model_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post_json(
            f"/api/processing/vision/models/{model_id}/cancel",
            payload,
            error_prefix="Processing vision model cancel failed",
        )

    async def retry_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            f"/api/processing/vision/models/{model_id}/retry",
            payload,
            error_prefix="Processing vision model retry failed",
        )

    async def upload_vision_model_artifact(
        self,
        *,
        model_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        files = {
            "file": (
                filename or f"{model_id}.onnx",
                content,
                content_type or "application/octet-stream",
            )
        }
        return await self._post_files(
            f"/api/processing/vision/models/{model_id}/artifact",
            files=files,
            error_prefix="Processing vision model artifact upload failed",
            timeout_s=max(self._timeout_s, 120.0),
        )

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = None
