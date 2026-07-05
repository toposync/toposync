from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator


logger = logging.getLogger("toposync.pipelines.transport")


class ProcessingTransportError(RuntimeError):
    pass


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
        res = await client.post(f"{self._base}{path}", json=payload, timeout=self._timeout_s if timeout_s is None else timeout_s)
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
        await self._post_json(
            "/api/processing/config",
            payload,
            error_prefix="Processing config push failed",
        )

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
        files = {"file": (filename or "custom-model.onnx", content, content_type or "application/octet-stream")}
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
        files = {"image": (filename or "preview-image.png", content, content_type or "application/octet-stream")}
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

    async def install_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json(
            f"/api/processing/vision/models/{model_id}/install",
            payload,
            error_prefix="Processing vision model install failed",
        )

    async def cancel_vision_model(self, *, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        files = {"file": (filename or f"{model_id}.onnx", content, content_type or "application/octet-stream")}
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
