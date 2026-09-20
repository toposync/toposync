"""Production stream generator and HTTP codec; no socket or model inference."""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from toposync.runtime.pipelines.distributed import processing_server as module
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport


def application(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    owners = []
    constructor = module.ProcessingServerRuntime

    def capture_runtime(**arguments):
        owner = constructor(**arguments)
        owners.append(owner)
        return owner

    monkeypatch.setattr(module, "ProcessingServerRuntime", capture_runtime)
    app = module.create_processing_app()
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", None) == "/api/processing/events/stream")
    return owners[0], endpoint


def request(*, epoch="", cursor=0):
    return Request({"type": "http", "headers": [
        (b"last-event-id", str(cursor).encode()), (b"last-event-epoch", epoch.encode()),
    ]})


async def data(iterator):
    chunk = await asyncio.wait_for(anext(iterator), 1)
    return json.loads(next(line[5:].strip() for line in chunk.splitlines() if line.startswith("data:")))


def test_stream_uses_epoch_header_to_replay_new_counter(tmp_path, monkeypatch):
    owner, endpoint = application(tmp_path, monkeypatch)

    async def scenario():
        owner._publish_processing_event({"sample": "new"})
        response = await endpoint(request(epoch="previous", cursor=100))
        iterator = response.body_iterator
        try:
            assert await anext(iterator) == "retry: 1000\n\n"
            ready = await data(iterator)
            event = await data(iterator)
            assert ready == owner.ready_event()
            assert event["sample"] == "new" and event["event_id"] == 1
            assert event["event_epoch"] == ready["event_epoch"]
        finally:
            await iterator.aclose()
        assert not owner.broadcaster._subscribers

    asyncio.run(scenario())


@pytest.mark.parametrize("reset_before_ready", [False, True])
def test_empty_reconfiguration_announces_epoch_and_discards_old_queued_events(
    tmp_path, monkeypatch, reset_before_ready,
):
    owner, endpoint = application(tmp_path, monkeypatch)

    async def scenario():
        owner._publish_processing_event({"sample": "old-replay"})
        response = await endpoint(request())
        iterator = response.body_iterator
        first_epoch = owner.ready_event()["event_epoch"]
        try:
            assert await anext(iterator) == "retry: 1000\n\n"
            if not reset_before_ready:
                assert (await data(iterator))["event_epoch"] == first_epoch
            owner._publish_processing_event({"sample": "old-queue"})
            await owner._apply([])  # Real empty reconfiguration, no new frames.
            ready = await data(iterator)
            assert ready == owner.ready_event()
            assert ready["event_epoch"] != first_epoch
            assert owner.status()["active"] is False
            owner._publish_processing_event({"sample": "new"})
            event = await data(iterator)
            assert event["sample"] == "new" and event["event_id"] == 1
            assert event["event_epoch"] == ready["event_epoch"]
        finally:
            await iterator.aclose()
        assert not owner.broadcaster._subscribers

    asyncio.run(scenario())


@pytest.mark.parametrize("epoch", ["", "current"])
def test_transport_serializes_epoch_in_resume_header_and_ack_body(epoch):
    requests = []

    def respond(incoming):
        requests.append(incoming)
        if incoming.method == "GET":
            return httpx.Response(200, text='data: {"event_type":"processing_ready"}\n\n')
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        transport = HttpProcessingTransport(base_url="http://test.invalid")
        transport._client = client
        try:
            events = [event async for event in transport.stream_events(last_event_id=100, event_epoch=epoch)]
            await transport.ack(100, event_epoch=epoch)
            assert events == [{"event_type": "processing_ready"}]
            assert requests[0].headers["Last-Event-ID"] == "100"
            assert requests[0].headers.get("Last-Event-Epoch") == (epoch or None)
            assert json.loads(requests[1].content) == {
                "last_event_id": 100, **({"event_epoch": epoch} if epoch else {}),
            }
        finally:
            await transport.close()
        assert client.is_closed

    asyncio.run(scenario())
