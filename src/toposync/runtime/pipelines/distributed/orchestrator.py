from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from toposync.runtime.config_store import (
    EXTENSION_MANAGEMENT_KEY,
    PROCESSING_SERVERS_KEY,
    AppSettings,
    ConfigStore,
    Pipeline,
    ProcessingServer,
)
from toposync.runtime.notifications import NotificationsRuntime

from ..compiler import (
    CompilationReport,
    CompiledPipeline,
    GraphCompileError,
    PipelineGraphCompiler,
    SharedNodeOccurrence,
)
from ..execution import PipelineRuntime, PipelineRuntimeDependencies
from ..operator_registry import OperatorRegistry
from ..runtime import BoundedChannel, DropPolicy
from ..shared_runtime import PipelineBundleRuntime, SharedRuntimeBuildError
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
class _BundleHandle:
    pipelines: list[Pipeline]
    runtime: PipelineBundleRuntime
    started_at: float


@dataclass(slots=True)
class _ServerHandle:
    server: ProcessingServer
    transport: ProcessingTransport
    pump_task: asyncio.Task[None]
    config_payload: dict[str, Any] = field(default_factory=dict)
    last_event_id: int = 0
    started_at: float = field(default_factory=time.time)


def _processing_settings_payload(settings: AppSettings) -> dict[str, Any]:
    core = dict(settings.core)
    core.pop(PROCESSING_SERVERS_KEY, None)
    core.pop(EXTENSION_MANAGEMENT_KEY, None)
    return AppSettings(core=core, extensions=dict(settings.extensions)).model_dump(mode="json")


def _remote_config_payload(
    pipelines: list[Pipeline],
    *,
    settings_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pipelines": [p.model_dump(mode="json") for p in pipelines],
        "settings": settings_payload,
    }


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
        runtime_dependencies: PipelineRuntimeDependencies | None = None,
    ) -> None:
        self._config_store = config_store
        self._registry = operator_registry
        self._compiler = compiler
        self._notifications = notifications
        self._files_dir = files_dir
        self._poll_interval_s = float(poll_interval_s)
        self._runtime_deps_base = runtime_dependencies or PipelineRuntimeDependencies()

        self._stop = asyncio.Event()
        self._reload = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._pipelines: dict[str, _PipelineHandle] = {}
        self._local_bundle: _BundleHandle | None = None
        self._inboxes: dict[str, BoundedChannel[dict[str, Any]]] = {}
        self._servers: dict[str, _ServerHandle] = {}
        self._last_sig: str = ""
        self._last_settings_sig: str = ""
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
        pipelines: list[dict[str, Any]] = []
        local_bundle: dict[str, Any] | None = None

        if self._local_bundle is not None:
            try:
                local_bundle = self._local_bundle.runtime.snapshot()
            except Exception as exc:  # noqa: BLE001
                local_bundle = {"error": str(exc)}
            for pipeline in self._local_bundle.pipelines:
                pipelines.append(
                    {
                        "name": pipeline.name,
                        "mode": "bundle",
                        "processing_server_id": "local",
                        "started_at": self._local_bundle.started_at,
                        "bundle_name": getattr(
                            self._local_bundle.runtime, "bundle_name", "local_bundle"
                        ),
                    },
                )

        pipelines.extend(
            [
                {
                    "name": name,
                    "mode": handle.mode,
                    "processing_server_id": getattr(
                        handle.pipeline, "processing_server_id", "local"
                    ),
                    "started_at": handle.started_at,
                    "snapshot": handle.runtime.snapshot(),
                }
                for name, handle in sorted(self._pipelines.items(), key=lambda item: item[0])
            ],
        )
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
            "local_bundle": local_bundle,
            "pipelines": pipelines,
            "servers": servers,
            "last_error": self._last_error,
            "last_signature": self._last_sig,
            "last_settings_signature": self._last_settings_sig,
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
                        [
                            asyncio.create_task(self._stop.wait()),
                            asyncio.create_task(self._reload.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    ),
                    timeout=self._poll_interval_s,
                )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    async def _reconcile(self) -> None:
        pipelines = await self._config_store.list_pipelines()
        servers = await self._config_store.list_processing_servers()
        settings = await self._config_store.get_settings()
        settings_payload = _processing_settings_payload(settings)
        settings_sig = json.dumps(settings_payload, sort_keys=True, separators=(",", ":"))

        desired = [p for p in pipelines if getattr(p, "enabled", True) is not False]
        desired_sig = json.dumps(
            {
                "pipelines": [p.model_dump(mode="json") for p in desired],
                "servers": [s.model_dump(mode="json") for s in servers],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        servers_by_id = {s.id: s for s in servers}
        local: list[Pipeline] = []
        remote_groups: dict[str, list[Pipeline]] = {}
        for p in desired:
            sid = str(getattr(p, "processing_server_id", "") or "").strip() or "local"
            if sid == "local":
                local.append(p)
            else:
                remote_groups.setdefault(sid, []).append(p)

        if desired_sig == self._last_sig:
            if settings_sig != self._last_settings_sig:
                synced = await self._sync_remote_settings(
                    remote_groups=remote_groups,
                    settings_payload=settings_payload,
                )
                if synced:
                    self._last_settings_sig = settings_sig
            else:
                await self._ensure_remote_configs(
                    remote_groups=remote_groups,
                    settings_payload=settings_payload,
                )
            return

        self._last_sig = desired_sig
        self._last_settings_sig = settings_sig

        await self._stop_all()

        if len(local) > 1:
            started = await self._start_local_bundle(local)
            if not started:
                for p in local:
                    await self._start_local_pipeline(p)
        elif local:
            await self._start_local_pipeline(local[0])

        for sid, group in remote_groups.items():
            server = servers_by_id.get(sid)
            if server is None:
                logger.warning(
                    "unknown processing server id=%s (skipping pipelines=%s)",
                    sid,
                    [p.name for p in group],
                )
                continue
            for p in group:
                await self._start_origin_pipeline_for_remote(p)
            await self._start_remote_server(server, group, settings_payload=settings_payload)

    async def _sync_remote_settings(
        self,
        *,
        remote_groups: dict[str, list[Pipeline]],
        settings_payload: dict[str, Any],
    ) -> bool:
        ok = True
        for sid, group in remote_groups.items():
            handle = self._servers.get(sid)
            if handle is None:
                ok = False
                logger.warning("processing server %s is not connected; settings sync deferred", sid)
                continue
            payload = _remote_config_payload(group, settings_payload=settings_payload)
            handle.config_payload = payload
            try:
                await handle.transport.push_config(payload)
            except Exception as exc:  # noqa: BLE001
                ok = False
                logger.warning("processing settings sync failed server=%s: %s", sid, exc)
        return ok

    async def _ensure_remote_configs(
        self,
        *,
        remote_groups: dict[str, list[Pipeline]],
        settings_payload: dict[str, Any],
    ) -> None:
        for sid, group in remote_groups.items():
            handle = self._servers.get(sid)
            if handle is None:
                continue
            expected_names = {p.name for p in group}
            try:
                status = await handle.transport.status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("processing status check failed server=%s: %s", sid, exc)
                continue
            remote_names_raw = status.get("pipelines")
            remote_names = {
                str(item or "").strip()
                for item in (remote_names_raw if isinstance(remote_names_raw, list) else [])
                if str(item or "").strip()
            }
            if bool(status.get("active")) and expected_names.issubset(remote_names):
                continue
            payload = _remote_config_payload(group, settings_payload=settings_payload)
            handle.config_payload = payload
            try:
                await handle.transport.push_config(payload)
                logger.info("re-synced processing config server=%s", sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("processing config re-sync failed server=%s: %s", sid, exc)

    async def _stop_all(self) -> None:
        if self._local_bundle is not None:
            try:
                await self._local_bundle.runtime.stop()
            except Exception:
                pass
            self._local_bundle = None

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

    def _build_runtime_dependencies(
        self,
        *,
        origin_inbox: BoundedChannel[dict[str, Any]] | None,
    ) -> PipelineRuntimeDependencies:
        base = self._runtime_deps_base
        return PipelineRuntimeDependencies(
            config_store=self._config_store,
            services=base.services,
            files_dir=self._files_dir,
            pipeline_snapshot_store=getattr(base, "pipeline_snapshot_store", None),
            notifications_upsert=self._notifications.upsert,
            origin_inbox=origin_inbox,
            classifier_backend_factory=base.classifier_backend_factory,
            detector_backend_factory=base.detector_backend_factory,
            segmenter_backend_factory=base.segmenter_backend_factory,
            pose_backend_factory=base.pose_backend_factory,
            tracker_backend_factory=base.tracker_backend_factory,
            vision_model_registry=base.vision_model_registry,
            processing_emit_projected_event=base.processing_emit_projected_event,
            pipeline_stats_store=base.pipeline_stats_store,
            pipeline_telemetry_store=base.pipeline_telemetry_store,
            pipeline_storage_manager=base.pipeline_storage_manager,
            pipeline_graph_limits_by_pipeline=base.pipeline_graph_limits_by_pipeline,
            execution_scheduler=base.execution_scheduler,
            artifact_max_bytes_per_packet=base.artifact_max_bytes_per_packet,
            artifact_max_total_bytes_per_pipeline=base.artifact_max_total_bytes_per_pipeline,
            artifact_global_counter=base.artifact_global_counter,
            logger=logger,
        )

    async def _start_local_bundle(self, pipelines: list[Pipeline]) -> bool:
        compiled: list[CompiledPipeline] = []
        valid_pipelines: list[Pipeline] = []
        for pipeline in pipelines:
            try:
                compiled_pipeline = self._compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                logger.warning("pipeline compile failed name=%s: %s", pipeline.name, exc)
                continue
            compiled.append(compiled_pipeline)
            valid_pipelines.append(pipeline)

        if not compiled:
            return False

        grouped: dict[str, list[SharedNodeOccurrence]] = {}
        for compiled_pipeline in compiled:
            for node in compiled_pipeline.nodes:
                if not node.shareable:
                    continue
                grouped.setdefault(node.signature, []).append(
                    SharedNodeOccurrence(
                        pipeline_name=compiled_pipeline.name,
                        node_id=node.node_id,
                        signature=node.signature,
                    ),
                )
        shared = {
            signature: tuple(occurrences)
            for signature, occurrences in grouped.items()
            if len(occurrences) > 1
        }
        report = CompilationReport(
            pipelines=tuple(compiled),
            shared_signatures=shared,
        )

        deps = self._build_runtime_dependencies(origin_inbox=None)
        try:
            runtime = PipelineBundleRuntime(
                report=report,
                registry=self._registry,
                dependencies=deps,
                bundle_name="local_bundle",
            )
        except SharedRuntimeBuildError as exc:
            logger.warning("local bundle build failed: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("local bundle init failed: %s", exc)
            return False

        try:
            await runtime.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("local bundle start failed: %s", exc)
            return False

        self._local_bundle = _BundleHandle(
            pipelines=valid_pipelines, runtime=runtime, started_at=time.time()
        )
        return True

    async def _start_local_pipeline(self, pipeline: Pipeline) -> None:
        try:
            compiled = self._compiler.compile_pipeline(pipeline)
        except GraphCompileError as exc:
            logger.warning("pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = self._build_runtime_dependencies(origin_inbox=None)
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(
            pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="local"
        )

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
        origin_pipeline = Pipeline(name=pipeline.name, graph=graphs.origin_graph)
        try:
            compiled = self._compiler.compile_pipeline(origin_pipeline)
        except GraphCompileError as exc:
            logger.warning("origin pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = self._build_runtime_dependencies(origin_inbox=inbox)
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(
            pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="origin"
        )

    async def _start_remote_server(
        self,
        server: ProcessingServer,
        pipelines: list[Pipeline],
        *,
        settings_payload: dict[str, Any],
    ) -> None:
        if server.kind != "http":
            logger.warning(
                "processing server kind=%s not supported yet (id=%s)", server.kind, server.id
            )
            return
        transport = HttpProcessingTransport(
            base_url=server.url,
            username=getattr(server, "username", ""),
            password=getattr(server, "password", ""),
        )
        payload = _remote_config_payload(pipelines, settings_payload=settings_payload)

        async def pump() -> None:
            last_event_id = 0
            backoff_s = 0.5
            try:
                while True:
                    try:
                        handle = self._servers.get(server.id)
                        config_payload = (
                            dict(handle.config_payload)
                            if handle is not None and handle.config_payload
                            else dict(payload)
                        )
                        await transport.push_config(config_payload)
                        async for event in transport.stream_events(last_event_id=last_event_id):
                            backoff_s = 0.5
                            try:
                                eid = int(event.get("event_id") or 0)
                            except Exception:
                                eid = 0
                            name = str(event.get("pipeline_name") or "").strip()
                            inbox = self._inboxes.get(name)
                            if inbox is None:
                                if eid:
                                    last_event_id = max(last_event_id, eid)
                                    await transport.ack(last_event_id)
                                continue
                            put_result = await inbox.put(event, timeout_s=0.05, cancel_event=None)
                            if eid and put_result.accepted:
                                last_event_id = max(last_event_id, eid)
                                await transport.ack(last_event_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("processing pump failed server=%s: %s", server.id, exc)
                        await asyncio.sleep(backoff_s)
                        backoff_s = min(10.0, backoff_s * 1.5)
                        continue
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(10.0, backoff_s * 1.5)
            except asyncio.CancelledError:
                raise
            finally:
                handle = self._servers.get(server.id)
                if handle is not None:
                    handle.last_event_id = last_event_id

        pump_task = asyncio.create_task(pump(), name=f"processing_pump[{server.id}]")
        self._servers[server.id] = _ServerHandle(
            server=server,
            transport=transport,
            pump_task=pump_task,
            config_payload=payload,
        )

        try:
            await transport.push_config(payload)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("processing config push failed server=%s; will retry: %s", server.id, exc)
