"""Per-call bounded transport; no global SDK client or retry policy is changed."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx


class VisualTransportError(RuntimeError):
    pass


class BoundedVisualTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport=None, *, maximum_bytes=262144):
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0)
        self._maximum_bytes = maximum_bytes

    async def handle_async_request(self, request):
        # Reject compressed replies instead of allocating an unbounded decompression result.
        request.headers["accept-encoding"] = "identity"
        response = await self._transport.handle_async_request(request)
        try:
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                raise VisualTransportError("visual_state_response_encoding")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > self._maximum_bytes):
                raise VisualTransportError("visual_state_response_too_large")
            data = bytearray()
            async for chunk in response.stream:
                if len(data) + len(chunk) > self._maximum_bytes:
                    raise VisualTransportError("visual_state_response_too_large")
                data.extend(chunk)
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(data),
                                  extensions=response.extensions, request=request)
        finally:
            await response.aclose()

    async def aclose(self):
        await self._transport.aclose()


@asynccontextmanager
async def visual_completion_client(*, model, api_key, api_base, timeout):
    route = model.split("/", 1)[0] if "/" in model else "openai"
    if route not in {"openai", "anthropic", "gemini"}:
        raise VisualTransportError("visual_state_transport_unsupported")
    async with httpx.AsyncClient(transport=BoundedVisualTransport(), timeout=timeout, follow_redirects=False) as client:
        if route == "openai":
            from openai import AsyncOpenAI

            sdk = AsyncOpenAI(http_client=client, api_key=api_key or None, base_url=api_base or None,
                              timeout=timeout, max_retries=0)
            try:
                yield sdk
            finally:
                await sdk.close()
        else:
            from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

            class Handler(AsyncHTTPHandler):
                def create_client(self, **_kwargs):
                    return client

            yield Handler(timeout=timeout)
