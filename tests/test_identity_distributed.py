from __future__ import annotations

import asyncio

from fastapi import FastAPI
import httpx
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import OperatorRegistry, register_builtin_operators
from toposync.runtime.pipelines.distributed.plan import (
    build_distributed_graphs,
    required_transport_capabilities,
)
from toposync.runtime.pipelines.distributed.transport import (
    HttpProcessingTransport,
    ProcessingTransportError,
)
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators
from toposync_ext_vision.processing.tasks.group_events import VisionGroupEventsRuntime


def test_recognition_grouping_stays_at_origin_without_changing_existing_placement():
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_vision_pipeline_operators(registry)
    nodes = [
        {"id": "extract", "operator": "vision.identity_evidence", "config": {"enabled": True}},
        {"id": "recognize", "operator": "vision.recognize_identity", "config": {"enabled": True}},
        {"id": "group", "operator": "vision.group_events", "config": {}},
        {"id": "notify", "operator": "core.notify", "config": {}},
    ]
    edges = [
        {"from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"}}
        for source, target in [
            ("extract", "recognize"),
            ("recognize", "group"),
            ("group", "notify"),
        ]
    ]
    pipeline = Pipeline(
        name="test",
        enabled=True,
        graph=build_pipeline_graph_v2(graph_uid="identity", nodes=nodes, edges=edges),
    )
    assert required_transport_capabilities([pipeline], registry) == {"private_artifacts_v1"}
    graphs = build_distributed_graphs(pipeline, registry)
    assert "group" in {node["id"] for node in graphs.origin_graph["nodes"]}
    legacy = pipeline.model_copy(
        update={
            "graph": build_pipeline_graph_v2(graph_uid="legacy", nodes=nodes[2:], edges=edges[2:])
        }
    )
    legacy_graphs = build_distributed_graphs(legacy, registry)
    assert "group" in {node["id"] for node in legacy_graphs.processing_graph["nodes"]}
    assert required_transport_capabilities([legacy], registry) == set()


def test_group_does_not_inherit_individual_name_or_private_decision():
    async def scenario():
        runtime = VisionGroupEventsRuntime({"mode": "session", "update_interval_seconds": 0})
        value = Packet.create(
            stream_id="event:one",
            lifecycle=Lifecycle.OPEN,
            payload={
                "source_stream_id": "camera:test",
                "camera_id": "camera",
                "event_id": "one",
                "frame_ts": 1,
                "subject": {
                    "type": "event",
                    "id": "one",
                    "category": "cat",
                    "bbox01": [0.1, 0.1, 0.5, 0.5],
                },
                "identity_id": "private-id",
                "recognition": {"status": "recognized", "occurrence_id": "private-occurrence"},
            },
            artifacts={
                "identity_decision": Artifact(
                    name="identity_decision", data=b"private-id", private=True
                )
            },
        )
        grouped = (await runtime.process_packet(value, None))[0]
        assert grouped.payload["subject"]["type"] == "group_event"
        assert grouped.payload["identity_id"] is None
        assert "recognition" not in grouped.payload
        assert "identity_decision" not in grouped.artifacts

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url,username,password",
    [
        ("http://192.0.2.1:8000", "test", "test"),
        ("http://127.0.0.1:8000", "", ""),
        ("https://example.invalid", "test", ""),
    ],
)
def test_private_transport_requires_secure_authentication_before_network(url, username, password):
    async def scenario():
        transport = HttpProcessingTransport(base_url=url, username=username, password=password)
        with pytest.raises(ProcessingTransportError, match="authenticated HTTPS"):
            await transport.push_config(
                {"required_capabilities": ["private_artifacts_v1"], "pipelines": []}
            )
        assert transport._client is None

    asyncio.run(scenario())


def test_private_protocol_rejects_older_peer_without_pushing_graph():
    async def scenario():
        app = FastAPI()
        received = []

        @app.get("/api/processing/status")
        async def status():
            return {"ok": True}

        @app.post("/api/processing/config")
        async def config():
            received.append(True)
            return {"ok": True}

        transport = HttpProcessingTransport(
            base_url="http://127.0.0.1", username="test", password="test"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            transport._client = client
            with pytest.raises(ProcessingTransportError, match="does not support"):
                await transport.push_config(
                    {"required_capabilities": ["private_artifacts_v1"], "pipelines": []}
                )
            assert not received

    asyncio.run(scenario())


def test_private_evidence_crosses_authenticated_process_boundary_and_replays(tmp_path):
    """Real HTTP/SSE and separate PIDs; synthetic evidence does not evaluate accuracy."""
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import time

    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.operators_distributed import _deserialize_packet
    from toposync.runtime.services import ServiceRegistry
    from toposync_ext_vision.identity.pipelines import RecognizeIdentityRuntime, occurrence_key
    from toposync_ext_vision.identity.store import IdentityStore

    class Context:
        async def run_blocking(self, function, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "TOPOSYNC_DATA_DIR": str(tmp_path / "processing"),
        "TOPOSYNC_PROCESSING_USERNAME": "isolated-test",
        "TOPOSYNC_PROCESSING_PASSWORD": "disposable-transport-contract",
        "TOPOSYNC_PROCESSING_STATUS_DIAGNOSTICS_TIMEOUT": "0.1",
    }
    output = tmp_path / "processing.log"
    with output.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(root / "tests/fixtures/identity_processing_probe.py"), str(port)],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            with httpx.Client(trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    assert process.poll() is None, output.read_text()[-5000:]
                    try:
                        response = client.get(base_url + "/api/processing/status")
                        if response.status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("isolated processing process did not become ready")
                assert client.get(base_url + "/api/processing/events/stream").status_code == 401

            async def scenario():
                transport = HttpProcessingTransport(
                    base_url=base_url,
                    username="isolated-test",
                    password="disposable-transport-contract",
                )
                store = IdentityStore(tmp_path / "origin", scope="isolated-test")
                try:
                    graph = build_pipeline_graph_v2(
                        graph_uid="private-process-probe",
                        nodes=[
                            {
                                "id": "source",
                                "operator": "test.private_probe",
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
                                "from": {"node": "source", "port": "out"},
                                "to": {"node": "resolve", "port": "in"},
                                "queue": {"max_items": 16, "drop_policy": "block"},
                            }
                        ],
                    )
                    payload = {
                        "pipelines": [
                            Pipeline(name="isolated_probe", graph=graph).model_dump(mode="json")
                        ]
                    }
                    # A new processing peer rejects a caller that skips capability negotiation.
                    with pytest.raises(ProcessingTransportError, match="409"):
                        await transport.push_config(payload)
                    payload["required_capabilities"] = ["private_artifacts_v1"]
                    await transport.push_config(payload)
                    events = []
                    stream = transport.stream_events()
                    try:
                        async with asyncio.timeout(15):
                            async for event in stream:
                                if "packet" in event:
                                    events.append(event)
                                    if len(events) == 3:
                                        break
                    except TimeoutError as exc:
                        status = await transport.status()
                        raise AssertionError(
                            f"projected events={len(events)} active={status.get('active')} last_event_id={status.get('last_event_id')} log={output.read_text()[-2000:]}"
                        ) from exc
                    finally:
                        await stream.aclose()
                    packets = [_deserialize_packet(event["packet"]) for event in events]
                    assert [value.lifecycle for value in packets] == [
                        Lifecycle.OPEN,
                        Lifecycle.UPDATE,
                        Lifecycle.CLOSE,
                    ]
                    assert all(
                        value.payload["processing_pid"] == process.pid != os.getpid()
                        for value in packets
                    )
                    assert packets[0].artifacts["identity_evidence"].private
                    services = ServiceRegistry()
                    services.register("vision.identity.store", lambda: store)
                    resolver = RecognizeIdentityRuntime(
                        {"enabled": True}, PipelineRuntimeDependencies(services=services)
                    )
                    for value in packets:
                        result = (await resolver.process_packet(value, Context()))[0]
                        assert result.payload["world_position"] == {"x": 1.5, "z": 2.5}
                        assert result.payload["subject"]["id"] == "tracked-pet"
                    assert len(store.observations()) == 2
                    assert store.occurrence_details(occurrence_key(packets[0]))["closed"]
                    replay = transport.stream_events(last_event_id=events[0]["event_id"])
                    try:
                        async with asyncio.timeout(5):
                            replayed = []
                            async for event in replay:
                                if "packet" in event:
                                    replayed.append(event)
                                    await resolver.process_packet(
                                        _deserialize_packet(event["packet"]), Context()
                                    )
                                    if len(replayed) == 2:
                                        break
                    finally:
                        await replay.aclose()
                    assert [event["event_id"] for event in replayed] == [
                        event["event_id"] for event in events[1:]
                    ]
                    assert len(store.observations()) == 2
                    await transport.ack(events[-1]["event_id"])
                    await transport.push_config({"pipelines": []})
                finally:
                    store.close()
                    await transport.close()

            asyncio.run(scenario())
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
