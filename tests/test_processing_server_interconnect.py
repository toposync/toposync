from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
import pytest
import uvicorn

from toposync.processing_server import create_app as create_processing_app
from toposync.runtime.config_store import ConfigStore, Pipeline, ProcessingServer, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import (
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SinkRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport


class _LiveProcessingServer:
    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._error: BaseException | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = int(sock.getsockname()[1])
        self._socket = sock

        config = uvicorn.Config(
            self._app,
            lifespan="on",
            loop="asyncio",
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._run,
            name="test-toposync-processing-server",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._error is not None:
                raise RuntimeError("Processing server failed to start") from self._error
            if self._server.started:
                return
            if self._thread is not None and not self._thread.is_alive():
                break
            time.sleep(0.02)

        self.stop()
        if self._error is not None:
            raise RuntimeError("Processing server failed to start") from self._error
        raise RuntimeError("Processing server did not start in time")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive() and self._server is not None:
                self._server.force_exit = True
                self._thread.join(timeout=2.0)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _run(self) -> None:
        assert self._server is not None
        assert self._socket is not None
        try:
            asyncio.run(self._server.serve(sockets=[self._socket]))
        except BaseException as exc:  # noqa: BLE001
            self._error = exc


class _FiniteSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "processing:probe"
    packets: int = Field(default=4, ge=1, le=20)
    interval_ms: int = Field(default=10, ge=0, le=1000)


class _ProcessingWorkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    matrix_size: int = Field(default=24, ge=2, le=96)


class _OriginCollectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_packets: int = Field(default=4, ge=1, le=100)


class _FiniteSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any], side: str) -> None:
        parsed = _FiniteSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._packets = int(parsed.packets)
        self._interval_s = float(parsed.interval_ms) / 1000.0
        self._sequence = 0
        self._next_tick = time.monotonic()
        self._counters = counters
        self._side = side

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._sequence >= self._packets:
            return None
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())

        sequence = self._sequence
        self._sequence += 1
        self._counters["source_packets"] = int(self._counters.get("source_packets", 0)) + 1
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload={"sequence": sequence, "source_side": self._side},
            metadata={"source_side": self._side},
        )


class _ProcessingWorkRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], counters: dict[str, Any], side: str) -> None:
        parsed = _ProcessingWorkConfig.model_validate(config)
        self._matrix_size = int(parsed.matrix_size)
        self._counters = counters
        self._side = side

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        result = await context.run_blocking(_run_best_available_workload, self._matrix_size)

        self._counters["processing_work_calls"] = (
            int(self._counters.get("processing_work_calls", 0)) + 1
        )
        engine_key = f"{result.get('engine')}:{result.get('device')}"
        engines = self._counters.setdefault("engines", {})
        engines[engine_key] = int(engines.get(engine_key, 0)) + 1

        payload = dict(packet.payload)
        payload["processing_probe"] = {
            **result,
            "side": self._side,
            "pid": os.getpid(),
            "pipeline_name": str(getattr(context, "pipeline_name", "") or ""),
            "node_id": str(getattr(context, "node_id", "") or ""),
            "sequence": int(packet.payload.get("sequence", -1)),
        }
        metadata = dict(packet.metadata)
        metadata["processing_probe_side"] = self._side
        return [replace(packet, payload=payload, metadata=metadata)]


class _OriginCollectRuntime(SinkRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        counters: dict[str, Any],
        done_event: asyncio.Event | None,
        side: str,
    ) -> None:
        parsed = _OriginCollectConfig.model_validate(config)
        self._expected_packets = int(parsed.expected_packets)
        self._counters = counters
        self._done_event = done_event
        self._side = side

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        probe = packet.payload.get("processing_probe")
        collected = self._counters.setdefault("collected", [])
        collected.append(
            {
                "sequence": int(packet.payload.get("sequence", -1)),
                "stream_id": packet.stream_id,
                "origin_side": self._side,
                "probe": dict(probe) if isinstance(probe, dict) else {},
            }
        )
        self._counters["origin_packets"] = int(self._counters.get("origin_packets", 0)) + 1
        if self._done_event is not None and len(collected) >= self._expected_packets:
            self._done_event.set()
        return []


def _run_best_available_workload(matrix_size: int) -> dict[str, Any]:
    size = int(matrix_size)
    torch_error = ""
    try:
        import torch  # type: ignore

        cuda_available = bool(torch.cuda.is_available())
        device = "cuda" if cuda_available else "cpu"
        tensor = torch.arange(size * size, dtype=torch.float32, device=device).reshape(size, size)
        product = torch.mm(tensor, tensor.transpose(0, 1))
        checksum = float(product.mean().detach().cpu().item())
        if device == "cuda":
            torch.cuda.synchronize()
        return {
            "engine": "torch",
            "device": device,
            "cuda_available": cuda_available,
            "checksum": round(checksum, 4),
        }
    except Exception as exc:  # noqa: BLE001
        torch_error = f"{type(exc).__name__}: {exc}"[:160]

    numpy_error = ""
    try:
        import numpy as np  # type: ignore

        values = np.arange(size * size, dtype=np.float32).reshape(size, size)
        product = values @ values.T
        return {
            "engine": "numpy",
            "device": "cpu",
            "cuda_available": False,
            "checksum": round(float(product.mean()), 4),
            "torch_error": torch_error,
        }
    except Exception as exc:  # noqa: BLE001
        numpy_error = f"{type(exc).__name__}: {exc}"[:160]

    values = [float(i) for i in range(size * size)]
    checksum = 0.0
    for row in range(size):
        row_start = row * size
        checksum += sum(values[row_start + col] * float(col + 1) for col in range(size))
    return {
        "engine": "python",
        "device": "cpu",
        "cuda_available": False,
        "checksum": round(checksum, 4),
        "torch_error": torch_error,
        "numpy_error": numpy_error,
    }


def _register_probe_operators(
    registry: OperatorRegistry,
    *,
    counters: dict[str, Any],
    side: str,
    done_event: asyncio.Event | None = None,
) -> None:
    registry.register_operator(
        operator_id="test.processing_finite_source",
        config_model=_FiniteSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["source", "test"],
        defaults=_FiniteSourceConfig().model_dump(),
        share_strategy="never",
        owner="test",
        runtime_factory=lambda config, _deps: _FiniteSourceRuntime(config, counters, side),
    )
    registry.register_operator(
        operator_id="test.processing_workload",
        config_model=_ProcessingWorkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["heavy_compute", "test"],
        defaults=_ProcessingWorkConfig().model_dump(),
        execution_mode="thread_pool",
        max_concurrency=1,
        share_strategy="never",
        owner="test",
        runtime_factory=lambda config, _deps: _ProcessingWorkRuntime(config, counters, side),
    )
    registry.register_operator(
        operator_id="test.origin_collect",
        config_model=_OriginCollectConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["origin_only", "sink", "test"],
        defaults=_OriginCollectConfig().model_dump(),
        share_strategy="never",
        owner="test",
        runtime_factory=lambda config, _deps: _OriginCollectRuntime(
            config,
            counters,
            done_event,
            side,
        ),
    )


def _processing_probe_graph(*, expected_packets: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "source",
                "operator": "test.processing_finite_source",
                "config": {
                    "stream_id": "processing:probe",
                    "packets": expected_packets,
                    "interval_ms": 10,
                },
            },
            {
                "id": "workload",
                "operator": "test.processing_workload",
                "config": {"matrix_size": 24},
            },
            {
                "id": "collect",
                "operator": "test.origin_collect",
                "config": {"expected_packets": expected_packets},
            },
        ],
        "edges": [
            {
                "from": {"node": "source", "port": "out"},
                "to": {"node": "workload", "port": "in"},
                "maxsize": 16,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "workload", "port": "out"},
                "to": {"node": "collect", "port": "in"},
                "maxsize": 16,
                "drop_policy": "drop_oldest",
            },
        ],
    }


async def _wait_for_processing_ack(
    *,
    base_url: str,
    expected_events: int,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    transport = HttpProcessingTransport(base_url=base_url, timeout_s=2.0)
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, Any] = {}
    try:
        while time.monotonic() < deadline:
            last_status = await transport.status()
            if int(last_status.get("last_acked_event_id") or 0) >= expected_events:
                return last_status
            await asyncio.sleep(0.05)
        return last_status
    finally:
        await transport.close()


def test_processing_server_http_interconnect_executes_remote_workload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_packets = 4
    processing_counters: dict[str, Any] = {}

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "processing-data"))
    monkeypatch.delenv("TOPOSYNC_PROCESSING_USERNAME", raising=False)
    monkeypatch.delenv("TOPOSYNC_PROCESSING_PASSWORD", raising=False)

    processing_app = create_processing_app()
    live_server = _LiveProcessingServer(processing_app)
    live_server.start()
    try:
        processing_registry: OperatorRegistry = processing_app.state.pipeline_operator_registry
        _register_probe_operators(
            processing_registry,
            counters=processing_counters,
            side="processing-server",
        )

        async def scenario() -> None:
            origin_counters: dict[str, Any] = {}
            done_event = asyncio.Event()

            origin_registry = OperatorRegistry()
            register_builtin_operators(origin_registry)
            _register_probe_operators(
                origin_registry,
                counters=origin_counters,
                side="origin-main",
                done_event=done_event,
            )

            paths = UserDataPaths(
                data_dir=tmp_path / "origin-data",
                config_path=tmp_path / "origin-data" / "config.json",
                files_dir=tmp_path / "origin-data" / "files",
            )
            config_store = ConfigStore(paths=paths)
            await config_store.upsert_processing_server(
                ProcessingServer(
                    id="edge_gpu",
                    name="Edge GPU",
                    kind="http",
                    url=live_server.base_url,
                )
            )
            await config_store.create_pipeline(
                Pipeline(
                    name="processing_server_probe",
                    processing_server_id="edge_gpu",
                    graph=_processing_probe_graph(expected_packets=expected_packets),
                )
            )

            notifications = NotificationsRuntime(data_dir=tmp_path / "origin-notifications")
            orchestrator = PipelinesOrchestrator(
                config_store=config_store,
                operator_registry=origin_registry,
                compiler=PipelineGraphCompiler(origin_registry),
                notifications=notifications,
                files_dir=paths.files_dir,
                poll_interval_s=999.0,
                runtime_dependencies=PipelineRuntimeDependencies(),
            )

            try:
                await orchestrator._reconcile()
                await asyncio.wait_for(done_event.wait(), timeout=8.0)

                status = await _wait_for_processing_ack(
                    base_url=live_server.base_url,
                    expected_events=expected_packets,
                )

                assert status.get("active") is True
                assert "processing_server_probe" in status.get("pipelines", [])
                assert int(status.get("last_event_id") or 0) >= expected_packets
                assert int(status.get("last_acked_event_id") or 0) >= expected_packets

                assert int(processing_counters.get("source_packets", 0)) == expected_packets
                assert int(processing_counters.get("processing_work_calls", 0)) == expected_packets
                assert int(origin_counters.get("source_packets", 0)) == 0
                assert int(origin_counters.get("processing_work_calls", 0)) == 0

                collected = origin_counters.get("collected")
                assert isinstance(collected, list)
                assert len(collected) >= expected_packets
                for item in collected[:expected_packets]:
                    probe = item.get("probe")
                    assert isinstance(probe, dict)
                    assert probe.get("side") == "processing-server"
                    assert probe.get("device") in {"cpu", "cuda"}
                    assert probe.get("engine") in {"torch", "numpy", "python"}
                    assert float(probe.get("checksum") or 0.0) > 0.0

                assert processing_counters.get("engines")
            finally:
                await orchestrator.stop()

        asyncio.run(scenario())
    finally:
        live_server.stop()
