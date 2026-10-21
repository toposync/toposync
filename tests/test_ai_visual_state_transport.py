from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from toposync_ext_ai import visual_transport
from toposync_ext_ai.providers import build_provider
from toposync_ext_ai.settings import AiProfileConfig, AiProviderConfig
from toposync_ext_ai.visual_state import VisualStateRequest
from toposync_ext_ai.visual_transport import BoundedVisualTransport, VisualTransportError


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture(scope="module")
def sdk_runner():
    # The SDK owns a process-global logging worker bound to one application loop.
    with asyncio.Runner() as runner:
        yield runner


@pytest.mark.parametrize("headers,chunks,reads,error", [
    ({"content-length": "99"}, [b"x"], 0, "visual_state_response_too_large"),
    ({"content-length": "invalid"}, [b"x"], 0, "visual_state_response_too_large"),
    ({"content-encoding": "gzip"}, [b"x"], 0, "visual_state_response_encoding"),
    ({}, [b"12345", b"67890", b"never-read"], 2, "visual_state_response_too_large"),
    ({"content-length": "2"}, [b"12345", b"67890", b"never-read"], 2, "visual_state_response_too_large"),
    ({}, [b"1234", b"5678"], 2, None),
])
def test_response_is_bounded_before_body_buffering_and_closed(headers, chunks, reads, error):
    stream = Stream(chunks)

    async def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers=headers, stream=stream)

    async def scenario():
        async with httpx.AsyncClient(transport=BoundedVisualTransport(httpx.MockTransport(respond), maximum_bytes=8)) as client:
            if error:
                with pytest.raises(VisualTransportError, match=error):
                    await client.get("http://provider.invalid")
            else:
                assert (await client.get("http://provider.invalid")).content == b"12345678"
        assert stream.closed and stream.reads == reads

    asyncio.run(scenario())


def test_cancellation_closes_partially_read_transport():
    entered = asyncio.Event()

    class BlockingStream(Stream):
        async def __aiter__(self):
            yield b"first"
            entered.set()
            await asyncio.Event().wait()

    stream = BlockingStream([])

    async def scenario():
        transport = BoundedVisualTransport(httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(client.get("http://provider.invalid"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,model", [("openai", "gpt-4o-mini"),
    ("anthropic", "claude-3-5-sonnet-20241022"), ("google", "gemini-2.0-flash"),
    ("litellm", "openai/gpt-4o-mini")])
@pytest.mark.parametrize("reply", ["success", "rate-limit", "oversized"])
def test_real_sdk_uses_bounded_per_call_client_without_hidden_retries(monkeypatch, sdk_runner, kind, model, reply):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    calls = []
    streams = []
    result = {"observable": True, "class_id": "closed", "reason": "Opening covered",
              "declared_confidence": .9, "reference_ids": []}

    async def no_network(*_args, **_kwargs):
        raise AssertionError("SDK bypassed the bounded simulated transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)

    async def respond(request):
        calls.append(request)
        assert request.url.host == "provider.invalid"
        if reply == "rate-limit":
            return httpx.Response(429, headers={"retry-after": "10"}, json={"error": {"message": "fixture", "type": "rate_limit_error"}})
        if reply == "oversized":
            stream = Stream([b"x" * 262144, b"x", b"never-read"])
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        text = json.dumps(result)
        if kind == "anthropic":
            body = {"id": "fixture", "type": "message", "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        elif kind == "google":
            body = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP", "index": 0}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        else:
            body = {"id": "fixture", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(visual_transport, "BoundedVisualTransport", lambda: BoundedVisualTransport(httpx.MockTransport(respond)))
    provider = build_provider(AiProviderConfig(id="provider", kind=kind, host="http://provider.invalid", api_key="synthetic"),
                              AiProfileConfig(id="visual", model=model, capabilities=["vision", "structured_json", "multiple_images"]))
    request = VisualStateRequest.model_validate({"classes": [
        {"class_id": "closed", "label": "Closed", "description": "Covered"},
        {"class_id": "open", "label": "Open", "description": "Visible"}]})

    async def scenario():
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
        try:
            if reply == "success":
                observed = await provider.classify_prepared_visual_state(request=request, encoded=[])
                assert observed.model_dump() == result
            else:
                with pytest.raises(Exception):
                    await provider.classify_prepared_visual_state(request=request, encoded=[])
        finally:
            await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=2)
        assert len(calls) == 1
        for stream in streams:
            assert stream.closed and stream.reads == 2

    sdk_runner.run(scenario())


def test_uncertified_sdk_route_is_rejected_before_client_creation():
    async def scenario():
        with pytest.raises(VisualTransportError, match="visual_state_transport_unsupported"):
            async with visual_transport.visual_completion_client(
                model="unverified/model", api_key="synthetic", api_base="http://provider.invalid", timeout=1):
                raise AssertionError("unsupported client was created")
    asyncio.run(scenario())
