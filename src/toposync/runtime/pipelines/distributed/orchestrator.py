from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from toposync.runtime.config_store import ConfigStore, Pipeline, ProcessingServer
from toposync.runtime.notifications import NotificationsRuntime

from ..compiler import GraphCompileError, PipelineGraphCompiler
from ..execution import PipelineRuntime, PipelineRuntimeDependencies
from ..operator_registry import OperatorRegistry
from ..runtime import BoundedChannel, DropPolicy
from .plan import build_distributed_graphs
from .transport import HttpProcessingTransport, ProcessingTransport


logger = logging.getLogger("toposync.pipelines.orchestrator")


@dataclass(slots=True)
class _PipelineHandle:
    pipeline: Pipeline
    runtime: PipelineRuntime
    started_at: float
    mode: str


@dataclass(slots=True)
class _ServerHandle:
    server: ProcessingServer
    transport: ProcessingTransport
    pump_task: asyncio.Task[None]
    last_event_id: int = 0
    started_at: float = field(default_factory=time.time)


class PipelinesOrchestrator:
    def __init__(
        self,
        *,
        config_store: ConfigStore,
        operator_registry: OperatorRegistry,
        compiler: PipelineGraphCompiler,
        notifications: NotificationsRuntime,
        files_dir,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._config_store = config_store
        self._registry = operator_registry
        self._compiler = compiler
        self._notifications = notifications
        self._files_dir = files_dir
        self._poll_interval_s = float(poll_interval_s)

        self._stop = asyncio.Event()
        self._reload = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._pipelines: dict[str, _PipelineHandle] = {}
        self._inboxes: dict[str, BoundedChannel[dict[str, Any]]] = {}
        self._servers: dict[str, _ServerHandle] = {}
        self._last_sig: str = ""
        self._last_error: str | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="toposync.pipelines.orchestrator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._stop_all()

    def trigger_reload(self) -> None:
        self._reload.set()

    def status(self) -> dict[str, Any]:
        pipelines = [
            {
                "name": name,
                "mode": handle.mode,
                "processing_server_id": getattr(handle.pipeline, "processing_server_id", "local"),
                "started_at": handle.started_at,
                "snapshot": handle.runtime.snapshot(),
            }
            for name, handle in sorted(self._pipelines.items(), key=lambda item: item[0])
        ]
        servers = [
            {
                "id": sid,
                "kind": handle.server.kind,
                "url": handle.server.url,
                "started_at": handle.started_at,
                "last_event_id": handle.last_event_id,
            }
            for sid, handle in sorted(self._servers.items(), key=lambda item: item[0])
        ]
        return {
            "running": self._task is not None and not self._stop.is_set(),
            "pipelines": pipelines,
            "servers": servers,
            "last_error": self._last_error,
            "last_signature": self._last_sig,
        }

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._reconcile()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("pipelines reconcile failed: %s", exc)
            try:
                self._reload.clear()
                await asyncio.wait_for(
                    asyncio.wait(
                        [asyncio.create_task(self._stop.wait()), asyncio.create_task(self._reload.wait())],
                        return_when=asyncio.FIRST_COMPLETED,
                    ),
                    timeout=self._poll_interval_s,
                )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    async def _reconcile(self) -> None:
        enabled = bool(await self._config_store.get_pipelines_feature_flag())
        pipelines = await self._config_store.list_pipelines()
        servers = await self._config_store.list_processing_servers()

        desired = [p for p in pipelines if p.type == "final" and getattr(p, "enabled", True) is not False]
        desired_sig = json.dumps(
            {
                "enabled": enabled,
                "pipelines": [p.model_dump(mode="json") for p in desired],
                "servers": [s.model_dump(mode="json") for s in servers],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        if desired_sig == self._last_sig:
            return
        self._last_sig = desired_sig

        await self._stop_all()
        if not enabled:
            return

        servers_by_id = {s.id: s for s in servers}
        local: list[Pipeline] = []
        remote_groups: dict[str, list[Pipeline]] = {}
        for p in desired:
            sid = str(getattr(p, "processing_server_id", "") or "").strip() or "local"
            if sid == "local":
                local.append(p)
            else:
                remote_groups.setdefault(sid, []).append(p)

        for p in local:
            await self._start_local_pipeline(p)

        for sid, group in remote_groups.items():
            server = servers_by_id.get(sid)
            if server is None:
                logger.warning("unknown processing server id=%s (skipping pipelines=%s)", sid, [p.name for p in group])
                continue
            for p in group:
                await self._start_origin_pipeline_for_remote(p)
            await self._start_remote_server(server, group)

    async def _stop_all(self) -> None:
        for handle in list(self._pipelines.values()):
            try:
                await handle.runtime.stop()
            except Exception:
                pass
        self._pipelines.clear()

        for q in list(self._inboxes.values()):
            try:
                await q.put({"packet": {}}, timeout_s=0.0, cancel_event=None)
            except Exception:
                pass
        self._inboxes.clear()

        for handle in list(self._servers.values()):
            handle.pump_task.cancel()
            try:
                await handle.pump_task
            except asyncio.CancelledError:
                pass
            try:
                await handle.transport.close()
            except Exception:
                pass
        self._servers.clear()

    async def _start_local_pipeline(self, pipeline: Pipeline) -> None:
        try:
            compiled = self._compiler.compile_pipeline(pipeline)
        except GraphCompileError as exc:
            logger.warning("pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = PipelineRuntimeDependencies(
            config_store=self._config_store,
            files_dir=self._files_dir,
            notifications_upsert=self._notifications.upsert,
            logger=logger,
        )
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="local")

    async def _start_origin_pipeline_for_remote(self, pipeline: Pipeline) -> None:
        graphs = build_distributed_graphs(pipeline, self._registry)
        if graphs.origin_graph is None:
            logger.warning("pipeline has no origin graph name=%s (skipping)", pipeline.name)
            return

        inbox = BoundedChannel[dict[str, Any]](
            name=f"origin_inbox[{pipeline.name}]",
            maxsize=64,
            drop_policy=DropPolicy.DROP_OLDEST,
        )
        self._inboxes[pipeline.name] = inbox
        origin_pipeline = Pipeline(name=pipeline.name, type="final", graph=graphs.origin_graph)
        try:
            compiled = self._compiler.compile_pipeline(origin_pipeline)
        except GraphCompileError as exc:
            logger.warning("origin pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = PipelineRuntimeDependencies(
            config_store=self._config_store,
            files_dir=self._files_dir,
            notifications_upsert=self._notifications.upsert,
            origin_inbox=inbox,
            logger=logger,
        )
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="origin")

    async def _start_remote_server(self, server: ProcessingServer, pipelines: list[Pipeline]) -> None:
        if server.kind != "http":
            logger.warning("processing server kind=%s not supported yet (id=%s)", server.kind, server.id)
            return
        transport = HttpProcessingTransport(base_url=server.url)

        async def pump() -> None:
            last_event_id = 0
            try:
                async for event in transport.stream_events(last_event_id=0):
                    try:
                        eid = int(event.get("event_id") or 0)
                    except Exception:
                        eid = 0
                    if eid:
                        last_event_id = max(last_event_id, eid)
                    name = str(event.get("pipeline_name") or "").strip()
                    inbox = self._inboxes.get(name)
                    if inbox is None:
                        continue
                    await inbox.put(event, timeout_s=0.05, cancel_event=None)
                    if eid:
                        await transport.ack(eid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("processing pump failed server=%s: %s", server.id, exc)
            finally:
                handle = self._servers.get(server.id)
                if handle is not None:
                    handle.last_event_id = last_event_id

        pump_task = asyncio.create_task(pump(), name=f"processing_pump[{server.id}]")
        self._servers[server.id] = _ServerHandle(server=server, transport=transport, pump_task=pump_task)

        payload = {"pipelines": [p.model_dump(mode="json") for p in pipelines]}
        await transport.push_config(payload)
