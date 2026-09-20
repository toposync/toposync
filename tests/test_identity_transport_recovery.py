"""Real HTTP recovery with a full origin inbox; synthetic, not biometric accuracy."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from pydantic import BaseModel, Field

from toposync.runtime.config_store import Pipeline, ProcessingServer
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.distributed.stream_contract import PRIVATE_INBOX_MAX_ITEMS
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport
from toposync.runtime.pipelines.runtime import BoundedChannel, DropPolicy
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from test_private_stream_continuity import dependencies


class ProbeConfig(BaseModel):
    enabled: bool = True
    visits: int = Field(default=1, ge=1, le=20)


def test_real_http_reconnects_after_full_private_inbox_without_loss(tmp_path):
    root = Path(__file__).resolve().parents[1]
    trace = tmp_path / "stream-trace.jsonl"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "TOPOSYNC_DATA_DIR": str(tmp_path / "processing"),
        "TOPOSYNC_PROCESSING_USERNAME": "isolated-test",
        "TOPOSYNC_PROCESSING_PASSWORD": "disposable-transport-contract",
        "TOPOSYNC_PROCESSING_STATUS_DIAGNOSTICS_TIMEOUT": "0.1",
        "TOPOSYNC_TEST_STREAM_TRACE": str(trace),
    }
    with (tmp_path / "processing.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(root / "tests/fixtures/identity_processing_probe.py"), str(port)],
            cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, "Processing fixture exited before readiness"
                    try:
                        if client.get(base_url + "/api/processing/status").status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "Processing fixture did not become ready"
                    time.sleep(0.05)

            async def scenario():
                shared = dependencies(tmp_path / "origin")
                shared["operator_registry"].register_operator(
                    operator_id="test.private_probe", config_model=ProbeConfig,
                    inputs=[], outputs=[{"name": "out"}], capabilities=["source", "private_data"],
                    defaults=ProbeConfig().model_dump(), share_strategy="never", owner="test",
                )
                orchestrator = PipelinesOrchestrator(
                    **shared, notifications=NotificationsRuntime(data_dir=tmp_path / "origin"),
                    files_dir=tmp_path / "origin/files",
                )
                inbox = BoundedChannel(
                    name="origin_inbox[recovery_probe]", maxsize=PRIVATE_INBOX_MAX_ITEMS,
                    drop_policy=DropPolicy.BLOCK,
                )
                orchestrator._inboxes["recovery_probe"] = inbox
                transport = HttpProcessingTransport(
                    base_url=base_url, username="isolated-test", password="disposable-transport-contract",
                )
                graph = build_pipeline_graph_v2(
                    graph_uid="recovery_probe",
                    nodes=[
                        {"id": "source", "operator": "test.private_probe", "config": {"visits": 5}},
                        {"id": "resolve", "operator": "vision.recognize_identity", "config": {"enabled": True}},
                    ],
                    edges=[{
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "resolve", "port": "in"},
                        "queue": {"max_items": 16, "drop_policy": "block"},
                    }],
                )
                server = ProcessingServer(
                    id="recovery", name="Recovery", kind="http", url=base_url,
                    username="isolated-test", password="disposable-transport-contract",
                )
                try:
                    await orchestrator._start_remote_server(
                        server, [Pipeline(name="recovery_probe", graph=graph)], settings_payload={},
                    )
                    handle = orchestrator._servers[server.id]
                    async with asyncio.timeout(10):
                        while True:
                            blocked = await transport.status()
                            records = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                            if inbox.depth == 8 and inbox.metrics_snapshot().put_attempts >= 9 and blocked["last_acked_event_id"] == 8 and any(row.get("interrupted_after_packets") == 9 for row in records):
                                break
                            assert not handle.pump_task.done(), orchestrator.status()
                            await asyncio.sleep(0.02)
                    # Nono evento chegou ao produtor da fila, mas ainda não foi admitido.
                    blocked = await transport.status()
                    assert inbox.metrics_snapshot().put_accepted == 8
                    assert blocked["last_acked_event_id"] == 8
                    assert blocked["last_event_id"] >= 15
                    assert blocked["private_stream"]["replay_bytes"] > 0
                    assert blocked["private_stream"]["continuity_error"] is None
                    generation = blocked["stream_instance_id"]
                    events = []
                    async with asyncio.timeout(10):
                        for _ in range(15):
                            result = await inbox.get(timeout_s=5)
                            assert result.accepted
                            events.append(result.item)
                    assert [event["packet"]["payload"]["probe_sequence"] for event in events] == list(range(15))
                    assert [event["packet"]["lifecycle"] for event in events] == ["open", "update", "close"] * 5
                    assert len({event["event_id"] for event in events}) == 15
                    assert all(event["packet"]["payload"]["processing_pid"] == process.pid != os.getpid() for event in events)
                    async with asyncio.timeout(5):
                        while True:
                            drained = await transport.status()
                            if drained["last_acked_event_id"] >= events[-1]["event_id"] and drained["private_stream"]["replay_bytes"] == 0:
                                break
                            await asyncio.sleep(0.02)
                    records = [json.loads(line) for line in trace.read_text().splitlines()]
                    attempts = [row for row in records if "attempt" in row]
                    assert len(attempts) == 2
                    assert int(attempts[1]["cursor"]) == events[8]["event_id"]
                    assert drained["stream_instance_id"] == generation
                    assert handle.continuity_error is None and not handle.pump_task.done()
                    assert inbox.depth == 0 and inbox.metrics_snapshot().max_depth_seen == 8
                    await orchestrator.stop()
                    async with asyncio.timeout(5):
                        while (await transport.status())["private_stream"]["connections"]:
                            await asyncio.sleep(0.02)
                    assert handle.pump_task.done()
                    (tmp_path / "result.json").write_text(json.dumps({
                        "packets": 15, "visits": 5, "peak_inbox_depth": 8,
                        "ack_while_full": blocked["last_acked_event_id"],
                        "reconnect_cursor": int(attempts[1]["cursor"]),
                        "stream_attempts": 2, "generation_preserved": True,
                        "all_lifecycles_in_order": True, "replay_bytes_after_drain": 0,
                        "connections_after_stop": 0, "origin_pid": os.getpid(),
                        "processing_pid": process.pid, "biometric_quality_evaluated": False,
                    }, indent=2) + "\n")
                finally:
                    await orchestrator.stop()
                    await transport.close()

            asyncio.run(scenario())
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
