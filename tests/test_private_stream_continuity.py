from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from toposync.runtime.config_store import ConfigStore, Pipeline, ProcessingServer, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.distributed.processing_server import ProcessingServerRuntime
from toposync.runtime.pipelines.distributed.stream_contract import (
    PRIVATE_EVENT_MAX_BYTES,
    ProcessingContinuityError,
)
from toposync.runtime.pipelines.distributed.transport import (
    HttpProcessingTransport,
    _bounded_private_lines,
)
from toposync.runtime.pipelines.runtime import BoundedChannel, DropPolicy
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.identity.pipelines import register_identity_operators


def dependencies(tmp_path):
    config = ConfigStore(
        paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        )
    )
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_identity_operators(registry)
    return dict(
        config_store=config, operator_registry=registry, compiler=PipelineGraphCompiler(registry)
    )


def processing(tmp_path):
    runtime = ProcessingServerRuntime(**dependencies(tmp_path), services=ServiceRegistry())
    runtime._private_mode = True
    return runtime


def publish(runtime, phase="update", content="test"):
    runtime._publish_processing_event(
        {
            "event_type": "packet.projected",
            "packet": {"lifecycle": phase, "payload": {"content": content}},
        }
    )


def test_private_replay_byte_bound_and_slow_subscriber_do_not_retain_bodies(tmp_path):
    runtime = processing(tmp_path)
    runtime._private_replay_max_bytes = 600
    subscriber = runtime.broadcaster.subscribe()
    for index in range(30):
        publish(runtime, "open" if index == 0 else "update", "x" * 120)
        assert runtime._replay_bytes <= 600
        assert runtime._replay_bytes == sum(
            len(item) for item in runtime._private_payloads.values()
        )
    while not subscriber.empty():
        assert set(subscriber.get_nowait()) == {"event_id"}
    with pytest.raises(ProcessingContinuityError, match="replay_gap"):
        runtime.next_private_event(0, runtime._stream_instance_id)
    assert len(runtime._private_payloads) == len(runtime._replay_events) < 30


def test_oversized_private_event_is_explicit_failure_without_retention(tmp_path):
    runtime = processing(tmp_path)
    publish(runtime, "open", "x" * PRIVATE_EVENT_MAX_BYTES)
    assert runtime._replay_bytes == 0 and not runtime._private_payloads
    with pytest.raises(ProcessingContinuityError, match="event_size_limit_exceeded"):
        runtime.next_private_event(0, runtime._stream_instance_id)
    publish(runtime, "close")
    assert not runtime._private_payloads


def test_ack_accounts_bytes_checks_generation_and_never_hides_restart(tmp_path):
    async def scenario():
        runtime = processing(tmp_path)
        instance = runtime._stream_instance_id
        for phase in ("open", "update", "close"):
            publish(runtime, phase)
        first_size = len(runtime._private_payloads[1])
        before = runtime._replay_bytes
        runtime.ack(1, stream_instance_id=instance)
        assert runtime._replay_bytes == before - first_size
        assert json.loads(runtime.next_private_event(1, instance))["event_id"] == 2
        with pytest.raises(ProcessingContinuityError, match="cursor_unavailable"):
            runtime.next_private_event(0, instance)
        with pytest.raises(ProcessingContinuityError, match="ack_ahead"):
            runtime.ack(4, stream_instance_id=instance)
        with pytest.raises(ProcessingContinuityError, match="restarted"):
            runtime.ack(3, stream_instance_id="another-server")
        assert runtime.last_acked_event_id == 1
        runtime.ack(3, stream_instance_id=instance)
        assert runtime._replay_bytes == 0 and not runtime._private_payloads
        await runtime.stop()
        publish(runtime, "open")
        with pytest.raises(ProcessingContinuityError, match="restarted"):
            runtime.ack(100, stream_instance_id=instance)
        assert runtime.last_acked_event_id == 0 and len(runtime._replay_events) == 1
        runtime._private_mode = True
        with pytest.raises(ProcessingContinuityError, match="restarted"):
            runtime.next_private_event(0, instance)
        await runtime.stop()

    asyncio.run(scenario())


def test_legacy_replay_keeps_full_event_contract(tmp_path):
    runtime = processing(tmp_path)
    runtime._private_mode = False
    subscriber = runtime.broadcaster.subscribe()
    publish(runtime, "open")
    assert subscriber.get_nowait()["packet"]["lifecycle"] == "open"
    assert runtime.replay_after(0)[0]["event_id"] == 1
    runtime.ack(1)
    assert runtime.replay_after(0) == []


def test_small_complete_event_does_not_wait_for_stream_eof():
    class OpenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"event_id":1}\n\n'
            await asyncio.Event().wait()

    async def scenario():
        response = httpx.Response(200, stream=OpenStream())
        reader = _bounded_private_lines(response)
        try:
            assert await asyncio.wait_for(anext(reader), timeout=0.5) == 'data: {"event_id":1}'
        finally:
            await reader.aclose()
            await response.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload", [b"x" * (PRIVATE_EVENT_MAX_BYTES + 33), b'data: {"event_id":1}']
)
def test_private_reader_rejects_oversized_or_truncated_lines(payload):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(0, len(payload), 65536):
                yield payload[index : index + 65536]

    async def scenario():
        response = httpx.Response(200, stream=Stream())
        with pytest.raises(ProcessingContinuityError):
            async for _ in _bounded_private_lines(response):
                pass
        await response.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body,instance",
    [
        ('data: {"event_id":2}\n\n', "expected"),
        ('data: {"event_type":"continuity_error","reason":"replay_gap"}\n\n', "expected"),
        ('data: {"event_id":1}\n\n', "new-generation"),
    ],
)
def test_private_transport_rejects_gap_or_server_restart(body, instance):
    async def scenario():
        def respond(request):
            return httpx.Response(
                200, headers={"X-Toposync-Stream-Instance": instance}, content=body
            )

        transport = HttpProcessingTransport(
            base_url="http://127.0.0.1", username="test", password="test"
        )
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        transport._private_stream = True
        transport._stream_instance_id = "expected"
        try:
            with pytest.raises(ProcessingContinuityError):
                async for _ in transport.stream_events():
                    pass
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_restart_detected_before_resending_processing_configuration():
    async def scenario():
        requests = []

        def respond(request):
            requests.append(request.method)
            return httpx.Response(
                200,
                json={
                    "stream_instance_id": "new",
                    "transport_capabilities": ["private_artifacts_v1", "private_stream_v1"],
                },
            )

        transport = HttpProcessingTransport(
            base_url="http://127.0.0.1", username="test", password="test"
        )
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        transport._stream_instance_id = "old"
        try:
            with pytest.raises(ProcessingContinuityError, match="restarted"):
                await transport.push_config(
                    {"required_capabilities": ["private_artifacts_v1"], "pipelines": []}
                )
            assert requests == ["GET"]
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_private_pump_waits_for_inbox_acceptance_and_suspends_after_gap(tmp_path, monkeypatch):
    async def scenario():
        acks = []
        attempting = asyncio.Event()

        class Transport:
            def __init__(self, **kwargs):
                pass

            async def push_config(self, payload):
                pass

            async def stream_events(self, **kwargs):
                for event_id, phase in enumerate(("open", "update", "close"), 1):
                    attempting.set()
                    yield {
                        "event_id": event_id,
                        "event_type": "packet.projected",
                        "pipeline_name": "probe",
                        "packet": {"lifecycle": phase},
                    }
                raise ProcessingContinuityError("injected_gap")

            async def ack(self, event_id):
                acks.append(event_id)

            async def close(self):
                pass

            async def status(self):
                raise AssertionError("Suspended server must not be automatically reconfigured")

        monkeypatch.setattr(
            "toposync.runtime.pipelines.distributed.orchestrator.HttpProcessingTransport", Transport
        )
        shared = dependencies(tmp_path)
        orchestrator = PipelinesOrchestrator(
            **shared,
            notifications=NotificationsRuntime(data_dir=tmp_path),
            files_dir=tmp_path / "files",
        )
        inbox = BoundedChannel(name="test", maxsize=8, drop_policy=DropPolicy.BLOCK)
        orchestrator._inboxes["probe"] = inbox
        for index in range(8):
            await inbox.put({"prefill": index})
        graph = build_pipeline_graph_v2(
            graph_uid="probe",
            nodes=[
                {
                    "id": "extract",
                    "operator": "vision.identity_evidence",
                    "config": {"enabled": True},
                },
                {
                    "id": "resolve",
                    "operator": "vision.recognize_identity",
                    "config": {"enabled": True},
                },
            ],
            edges=[
                {
                    "from": {"node": "extract", "port": "out"},
                    "to": {"node": "resolve", "port": "in"},
                }
            ],
        )
        pipeline = Pipeline(name="probe", graph=graph)
        server = ProcessingServer(
            id="remote", name="Remote", kind="http", url="http://127.0.0.1:12345"
        )
        try:
            await orchestrator._start_remote_server(server, [pipeline], settings_payload={})
            handle = orchestrator._servers["remote"]
            await asyncio.wait_for(attempting.wait(), 1)
            await asyncio.sleep(0.03)
            assert acks == [] and not handle.pump_task.done()
            for index in range(8):
                value = await inbox.get(timeout_s=1)
                assert value.item == {"prefill": index}
            phases = []
            for _ in range(3):
                value = await inbox.get(timeout_s=1)
                phases.append(value.item["packet"]["lifecycle"])
            await asyncio.wait_for(handle.pump_task, 1)
            assert phases == ["open", "update", "close"]
            assert acks == [1, 2, 3]
            assert handle.continuity_error == "injected_gap"
            assert handle.last_event_id == 3
            assert "delivery suspended" in orchestrator.status()["last_error"]
            await orchestrator._ensure_remote_configs(
                remote_groups={"remote": [pipeline]}, settings_payload={}
            )
        finally:
            await orchestrator.stop()

    asyncio.run(scenario())


def test_private_stream_http_negotiation_connection_limit_and_cleanup(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from toposync.runtime.pipelines.distributed import processing_server as module

    captured = []
    original = module.ProcessingServerRuntime

    def capture(**kwargs):
        runtime = original(**kwargs)
        captured.append(runtime)
        return runtime

    monkeypatch.setattr(module, "ProcessingServerRuntime", capture)
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_ROLE", "processing")
    monkeypatch.setenv("TOPOSYNC_PROCESSING_USERNAME", "test")
    monkeypatch.setenv("TOPOSYNC_PROCESSING_PASSWORD", "disposable")
    monkeypatch.setenv("TOPOSYNC_EXTENSION_AUTO_INSTALL_ON_STARTUP", "0")
    with TestClient(module.create_processing_app()) as client:
        runtime = captured[0]
        runtime._private_mode = True
        auth = ("test", "disposable")
        url = "/api/processing/events/stream"
        assert client.get(url).status_code == 401
        assert client.get(url, auth=auth).status_code == 409
        headers = {"X-Toposync-Private-Stream": "private_stream_v1"}
        runtime._private_connections = 4
        assert client.get(url, auth=auth, headers=headers).status_code == 429
        runtime._private_connections = 0
        # Abort before the response body starts: no generator finally block runs.
        from starlette.requests import ClientDisconnect, Request

        endpoint = next(
            route.endpoint for route in client.app.routes if getattr(route, "path", None) == url
        )

        async def abort_headers():
            scope = {
                "type": "http",
                "method": "GET",
                "path": url,
                "headers": [(b"x-toposync-private-stream", b"private_stream_v1")],
                "asgi": {"spec_version": "2.4"},
            }

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                assert message["type"] == "http.response.start"
                raise OSError("connection closed before response.start")

            for _ in range(4):
                response = await endpoint(Request(scope))
                with pytest.raises(ClientDisconnect):
                    await response(scope, receive, send)
                assert runtime._private_connections == 0
                assert not runtime.broadcaster._subscribers
            assert (await endpoint(Request(scope))).status_code == 200

        asyncio.run(abort_headers())
        runtime._private_error = "replay_gap"
        response = client.get(url, auth=auth, headers=headers)
        assert response.status_code == 200
        assert response.headers["X-Toposync-Stream-Instance"] == runtime._stream_instance_id
        assert '"reason": "replay_gap"' in response.text
        assert runtime._private_connections == 0
        assert not runtime.broadcaster._subscribers
        assert (
            client.post(
                "/api/processing/events/ack",
                auth=auth,
                json={"last_event_id": 0, "stream_instance_id": "stale"},
            ).status_code
            == 409
        )
