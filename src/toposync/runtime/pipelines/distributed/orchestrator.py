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
    Composition,
    ConfigStore,
    Pipeline,
    PipelineValidationError,
    ProcessingServer,
)
from toposync.runtime.notifications import NotificationsRuntime

from ..compiler import (
    CompilationReport,
    GraphCompileError,
    PipelineGraphCompiler,
)
from ..execution_scheduler import ExecutionScheduler
from ..execution import PipelineRuntime, PipelineRuntimeDependencies
from ..inspection import pipeline_inspection_revision
from ..operator_registry import OperatorRegistry
from ..observability import (
    OBSERVABILITY_BATCH_EVENT_TYPE,
    PROJECTED_PACKET_EVENT_TYPE,
    apply_observability_batch,
)
from ..runtime import BoundedChannel, DropPolicy
from ..shared_runtime import PipelineBundleRuntime, SharedRuntimeBuildError
from .plan import build_distributed_graphs, required_transport_capabilities
from .transport import HttpProcessingTransport, PROCESSING_READY_EVENT_TYPE
from .stream_contract import PRIVATE_INBOX_MAX_ITEMS, ProcessingContinuityError


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
    transport: HttpProcessingTransport
    pump_task: asyncio.Task[None]
    config_payload: dict[str, Any] = field(default_factory=dict)
    last_event_id: int = 0
    event_epoch: str = ""
    continuity_error: str | None = None
    started_at: float = field(default_factory=time.time)
    observability_applied_records: int = 0
    observability_skipped_records: int = 0


def _processing_settings_payload(settings: AppSettings) -> dict[str, Any]:
    core = dict(settings.core)
    core.pop(PROCESSING_SERVERS_KEY, None)
    core.pop(EXTENSION_MANAGEMENT_KEY, None)
    core.pop("automatic", None)
    return AppSettings(core=core, extensions=dict(settings.extensions)).model_dump(mode="json")


def _remote_config_payload(
    pipelines: list[Pipeline],
    *,
    settings_payload: dict[str, Any],
    registry: OperatorRegistry | None = None,
) -> dict[str, Any]:
    return {
        "required_capabilities": sorted(required_transport_capabilities(pipelines, registry))
        if registry
        else [],
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
        automatic_controller=None,
    ) -> None:
        self._config_store = config_store
        self._automatic = automatic_controller
        self._registry = operator_registry
        self._compiler = compiler
        self._notifications = notifications
        self._files_dir = files_dir
        self._poll_interval_s = float(poll_interval_s)
        self._runtime_deps_base = runtime_dependencies or PipelineRuntimeDependencies()
        if self._runtime_deps_base.execution_scheduler is None:
            self._runtime_deps_base.execution_scheduler = ExecutionScheduler()

        self._stop = asyncio.Event()
        self._reload = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._pipelines: dict[str, _PipelineHandle] = {}
        self._local_bundle: _BundleHandle | None = None
        self._inboxes: dict[str, BoundedChannel[dict[str, Any]]] = {}
        self._servers: dict[str, _ServerHandle] = {}
        self._last_sig: str = ""
        self._failed_local_plan: tuple[str, str] | None = None
        self._last_settings_sig: str = ""
        self._last_remote_sig: str = ""
        self._last_error: str | None = None
        self._activation_errors: dict[str, str] = {}
        self._activation_retry_requested = False
        self._last_activation_compositions: list[Composition] = []
        self._reconcile_lock = asyncio.Lock()
        if self._runtime_deps_base.demand_registry is not None:
            self._runtime_deps_base.demand_registry.changed = self.trigger_reload

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
        demands = self._runtime_deps_base.demand_registry
        if demands is not None:
            for identifier in list(demands.flow_leases.values()):
                demands.release(identifier, "manual_flow_host_stopped")
            demands.flow_leases.clear()
        await self._runtime_deps_base.execution_scheduler.shutdown()

    def trigger_reload(self) -> None:
        self._failed_local_plan = None
        self._activation_retry_requested = bool(self._activation_errors)
        self._reload.set()

    def status(self) -> dict[str, Any]:
        pipelines: list[dict[str, Any]] = []
        local_bundle: dict[str, Any] | None = None
        graph_info = self.graph_runtime_info()

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
                        "reason": "consumer_demand" if pipeline.name.startswith("demand_") else "pipeline_enabled",
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
                "event_epoch": handle.event_epoch,
                "continuity_error": handle.continuity_error,
                "observability_applied_records": handle.observability_applied_records,
                "observability_skipped_records": handle.observability_skipped_records,
            }
            for sid, handle in sorted(self._servers.items(), key=lambda item: item[0])
        ]
        return {
            "running": self._task is not None and not self._stop.is_set(),
            "local_bundle": local_bundle,
            "pipelines": pipelines,
            "servers": servers,
            "graph_info": {"graphs": graph_info},
            "last_error": self._last_error,
            "activation_errors": dict(self._activation_errors),
        }

    def graph_runtime_info(self) -> list[dict[str, Any]]:
        graphs: list[dict[str, Any]] = []
        if self._local_bundle is not None:
            try:
                info = self._local_bundle.runtime.graph_runtime_info()
                info["mode"] = "bundle"
                info["processing_server_id"] = "local"
                info["started_at"] = self._local_bundle.started_at
                graphs.append(info)
            except Exception as exc:  # noqa: BLE001
                graphs.append({"graph_id": "local_bundle", "error": str(exc)})
        for name, handle in sorted(self._pipelines.items(), key=lambda item: item[0]):
            try:
                info = handle.runtime.graph_runtime_info(graph_id=name)
                info["mode"] = handle.mode
                info["processing_server_id"] = getattr(
                    handle.pipeline, "processing_server_id", "local"
                )
                info["started_at"] = handle.started_at
                graphs.append(info)
            except Exception as exc:  # noqa: BLE001
                graphs.append({"graph_id": name, "error": str(exc)})
        return graphs

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
                    self._reload.wait(),
                    timeout=self._poll_interval_s,
                )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    async def _reconcile(self) -> None:
        async with self._reconcile_lock:
            await self._reconcile_plan()

    async def _reconcile_plan(self) -> None:
        config = await self._config_store.get_config()
        if self._automatic is not None:
            self._automatic.prepare_config(config)
        pipelines = config.pipelines
        servers = await self._config_store.list_processing_servers()
        settings = config.settings
        settings_payload = _processing_settings_payload(settings)
        settings_sig = json.dumps(settings_payload, sort_keys=True, separators=(",", ":"))

        desired = [p for p in pipelines if getattr(p, "enabled", True) is not False]
        demands = self._runtime_deps_base.demand_registry
        if demands is not None:
            await self._prune_revoked_local_demands()
            demands.bind(pipelines)
            if self._automatic is not None:
                await self._automatic.synchronize(config)
            desired = demands.synchronize_flows(pipelines)
            await self._prune_revoked_local_demands()
            desired = [pipeline for pipeline in desired if not demands.pipeline_blocked(pipeline)]
            desired.extend(demands.pipelines())
        desired_sig = json.dumps(
            {
                "pipelines": [p.model_dump(mode="json") for p in desired],
                "servers": [s.model_dump(mode="json") for s in servers],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        if self._failed_local_plan == (desired_sig, settings_sig):
            return

        preflight_required = (
            desired_sig != self._last_sig
            or self._activation_retry_requested
            or (bool(self._activation_errors) and (
                settings_sig != self._last_settings_sig
                or config.compositions != self._last_activation_compositions
            ))
        )
        if preflight_required:
            self._activation_retry_requested = False
            activation_errors: dict[str, str] = {}
            for pipeline in desired:
                try:
                    await self._config_store.validate_pipeline_activation(pipeline, config=config)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc) if isinstance(exc, PipelineValidationError) else "Activation checks unavailable"
                    activation_errors[pipeline.name] = message
                    logger.warning("pipeline activation blocked name=%s: %s", pipeline.name, message)
            self._activation_errors = activation_errors
            if demands is not None:
                for pipeline in pipelines:
                    if pipeline.name in activation_errors and pipeline.graph.get("meta", {}).get("result_consumer"):
                        demands.reject_flow(pipeline, activation_errors[pipeline.name])
            if await self._config_store.get_config() != config:
                # A user may disable or edit a pipeline while remote preflight waits.
                # Reconcile the new snapshot before starting any of its predecessors.
                self._activation_retry_requested = True
                return
            self._last_activation_compositions = [item.model_copy(deep=True) for item in config.compositions]
        # Keep rejected configurations out of both origins and remote config sync.
        # Failed preflight is retried only after a relevant edit or explicit reload.
        desired = [pipeline for pipeline in desired if pipeline.name not in self._activation_errors]
        if demands is not None:
            desired = [pipeline for pipeline in desired if all(
                demands.allowed(node["config"]["demand_id"])
                for node in pipeline.graph.get("nodes", []) if node.get("config", {}).get("demand_id")
            )]

        servers_by_id = {s.id: s for s in servers}
        local: list[Pipeline] = []
        remote_groups: dict[str, list[Pipeline]] = {}
        for p in desired:
            sid = str(getattr(p, "processing_server_id", "") or "").strip() or "local"
            if sid == "local":
                local.append(p)
            else:
                remote_groups.setdefault(sid, []).append(p)

        if desired_sig == self._last_sig and not preflight_required:
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

        if local:
            if not await self._start_local_bundle(local, catalog=[*pipelines, *[p for p in desired if p.name not in {item.name for item in pipelines}]]):
                self._failed_local_plan = (desired_sig, settings_sig)
                return
        elif self._local_bundle is not None:
            await self._local_bundle.runtime.stop()
            self._local_bundle = None

        self._last_sig = desired_sig
        self._failed_local_plan = None
        self._last_settings_sig = settings_sig

        remote_sig = json.dumps({
            "groups": {sid: [pipeline.model_dump(mode="json") for pipeline in group] for sid, group in remote_groups.items()},
            "servers": [server.model_dump(mode="json") for server in servers],
        }, sort_keys=True)
        if remote_sig == self._last_remote_sig:
            return
        await self._stop_remote()
        self._last_remote_sig = remote_sig

        for sid, group in remote_groups.items():
            server = servers_by_id.get(sid)
            if server is None:
                logger.warning(
                    "unknown processing server id=%s (skipping pipelines=%s)",
                    sid,
                    [p.name for p in group],
                )
                continue
            private_stream = "private_artifacts_v1" in required_transport_capabilities(group, self._registry)
            for p in group:
                await self._start_origin_pipeline_for_remote(p, private_stream=private_stream)
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
            if handle is None or handle.continuity_error:
                ok = False
                logger.warning("processing server %s is not connected; settings sync deferred", sid)
                continue
            payload = _remote_config_payload(
                group, settings_payload=settings_payload, registry=self._registry
            )
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
            if handle is None or handle.continuity_error:
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
            payload = _remote_config_payload(
                group, settings_payload=settings_payload, registry=self._registry
            )
            handle.config_payload = payload
            try:
                await handle.transport.push_config(payload)
                logger.info("re-synced processing config server=%s", sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("processing config re-sync failed server=%s: %s", sid, exc)

    async def _stop_all(self) -> None:
        await self._stop_remote()
        if self._local_bundle is not None:
            try:
                await self._local_bundle.runtime.stop()
            finally:
                self._local_bundle = None

    async def _stop_remote(self) -> None:
        # Stop producers before consumers: a reconnect must not recreate an
        # origin runtime while reconciliation is tearing that runtime down.
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

    def _build_runtime_dependencies(
        self,
        *,
        origin_inbox: BoundedChannel[dict[str, Any]] | None,
        pipelines: list[Pipeline],
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
            pipeline_observability_sink=base.pipeline_observability_sink,
            inspection_store=base.inspection_store,
            inspection_node_occurrences=base.inspection_node_occurrences,
            demand_registry=base.demand_registry,
            inspection_revisions={pipeline.name: pipeline_inspection_revision(pipeline) for pipeline in pipelines},
            pipeline_storage_manager=base.pipeline_storage_manager,
            pipeline_graph_limits_by_pipeline=base.pipeline_graph_limits_by_pipeline,
            execution_scheduler=base.execution_scheduler,
            artifact_max_bytes_per_packet=base.artifact_max_bytes_per_packet,
            artifact_max_total_bytes_per_pipeline=base.artifact_max_total_bytes_per_pipeline,
            artifact_global_counter=base.artifact_global_counter,
            logger=logger,
        )

    async def _prune_revoked_local_demands(self) -> None:
        handle = self._local_bundle
        demands = self._runtime_deps_base.demand_registry
        if handle is None or demands is None:
            return
        configured = {pipeline.name: pipeline for pipeline in handle.pipelines}
        survivors = tuple(pipeline for pipeline in handle.runtime.report.pipelines
            if all(demands.allowed(node.normalized_config["demand_id"])
                   for node in pipeline.nodes if node.operator_id in {"core.demand_sink", "core.demand_consumer"})
            and not demands.pipeline_blocked(configured[pipeline.name]))
        if len(survivors) == len(handle.runtime.report.pipelines):
            return
        if not survivors:
            await handle.runtime.stop()
            self._local_bundle = None
            return
        report = CompilationReport(survivors, {})
        try:
            await handle.runtime.reconcile(report)
        finally:
            if handle.runtime.report is report:
                names = {pipeline.name for pipeline in survivors}
                handle.pipelines = [pipeline for pipeline in handle.pipelines if pipeline.name in names]

    async def _start_local_bundle(self, pipelines: list[Pipeline], *, catalog: list[Pipeline]) -> bool:
        try:
            report = self._compiler.compile_many(pipelines, catalog=catalog)
        except GraphCompileError as exc:
            self._last_error = "Local plan rejected"
            logger.warning("local plan rejected (%s)", type(exc).__name__)
            return False

        deps = self._build_runtime_dependencies(origin_inbox=None, pipelines=[*catalog, *pipelines])
        if self._local_bundle is not None:
            try:
                await self._local_bundle.runtime.reconcile(report, deps)
            except Exception as exc:
                self._last_error = "Local plan preparation failed"
                logger.warning("local plan preparation failed (%s)", type(exc).__name__)
                return False
            finally:
                if self._local_bundle.runtime.report is report:
                    self._local_bundle.pipelines = list(pipelines)
            if self._last_error and self._last_error.startswith("Local plan"):
                self._last_error = None
            return True
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
            pipelines=list(pipelines), runtime=runtime, started_at=time.time()
        )
        return True

    async def _start_local_pipeline(self, pipeline: Pipeline) -> None:
        try:
            compiled = self._compiler.compile_pipeline(pipeline)
        except GraphCompileError as exc:
            logger.warning("pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = self._build_runtime_dependencies(origin_inbox=None, pipelines=[pipeline])
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(
            pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="local"
        )

    async def _start_origin_pipeline_for_remote(self, pipeline: Pipeline, *, private_stream: bool | None = None) -> None:
        graphs = build_distributed_graphs(pipeline, self._registry)
        if graphs.origin_graph is None:
            logger.warning("pipeline has no origin graph name=%s (skipping)", pipeline.name)
            return

        private = private_stream if private_stream is not None else "private_artifacts_v1" in required_transport_capabilities([pipeline], self._registry)
        inbox = BoundedChannel[dict[str, Any]](
            name=f"origin_inbox[{pipeline.name}]",
            maxsize=PRIVATE_INBOX_MAX_ITEMS if private else 64,
            drop_policy=DropPolicy.BLOCK if private else DropPolicy.DROP_OLDEST,
        )
        self._inboxes[pipeline.name] = inbox
        origin_pipeline = Pipeline(name=pipeline.name, graph=graphs.origin_graph)
        try:
            compiled = self._compiler.compile_pipeline(origin_pipeline)
        except GraphCompileError as exc:
            logger.warning("origin pipeline compile failed name=%s: %s", pipeline.name, exc)
            return

        deps = self._build_runtime_dependencies(origin_inbox=inbox, pipelines=[pipeline])
        runtime = PipelineRuntime(compiled=compiled, registry=self._registry, dependencies=deps)
        await runtime.start()
        self._pipelines[pipeline.name] = _PipelineHandle(
            pipeline=pipeline, runtime=runtime, started_at=time.time(), mode="origin"
        )

    async def _suspend_remote_origins(self, server_id: str) -> None:
        for name, handle in list(self._pipelines.items()):
            if handle.mode != "origin" or handle.pipeline.processing_server_id != server_id:
                continue
            # Keep the handle registered until shutdown finishes, so cancellation
            # during reconciliation still leaves it available to _stop_all.
            await handle.runtime.stop()
            if self._pipelines.get(name) is handle:
                self._pipelines.pop(name, None)
                self._inboxes.pop(name, None)

    async def _resume_remote_origins(self, config_payload: dict[str, Any]) -> None:
        for item in config_payload.get("pipelines", []):
            if self._stop.is_set():
                return
            pipeline = Pipeline.model_validate(item)
            if pipeline.enabled and pipeline.name not in self._pipelines:
                await self._start_origin_pipeline_for_remote(pipeline)

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
        payload = _remote_config_payload(
            pipelines, settings_payload=settings_payload, registry=self._registry
        )

        async def pump() -> None:
            last_event_id = 0
            event_epoch = ""
            backoff_s = 0.5
            private = "private_artifacts_v1" in payload.get("required_capabilities", [])

            async def acknowledge() -> None:
                handle = self._servers.get(server.id)
                if handle is not None:
                    handle.last_event_id = last_event_id
                    handle.event_epoch = event_epoch
                try:
                    if event_epoch:
                        await transport.ack(last_event_id, event_epoch=event_epoch)
                    else:
                        await transport.ack(last_event_id)
                except ProcessingContinuityError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # An ACK failure does not prove the data stream failed. Its
                    # accepted inbox packets must remain available to consumers.
                    logger.warning("processing acknowledgment failed server=%s: %s", server.id, exc)

            try:
                while True:
                    try:
                        handle = self._servers.get(server.id)
                        config_payload = (
                            dict(handle.config_payload)
                            if handle is not None and handle.config_payload
                            else dict(payload)
                        )
                        private = "private_artifacts_v1" in config_payload.get("required_capabilities", [])
                        await transport.push_config(config_payload)
                        if not private:
                            await self._resume_remote_origins(config_payload)
                        async for event in transport.stream_events(
                            last_event_id=last_event_id, event_epoch=event_epoch,
                        ):
                            backoff_s = 0.5
                            try:
                                eid = int(event.get("event_id") or 0)
                            except Exception:
                                eid = 0
                            event_type = str(event.get("event_type") or "").strip()
                            incoming_epoch = str(event.get("event_epoch") or "").strip()
                            if not private and event_type == PROCESSING_READY_EVENT_TYPE:
                                if incoming_epoch and incoming_epoch != event_epoch:
                                    if event_epoch:
                                        await self._suspend_remote_origins(server.id)
                                        await self._resume_remote_origins(config_payload)
                                    event_epoch = incoming_epoch
                                    last_event_id = 0
                                continue
                            if not private and incoming_epoch != event_epoch:
                                continue
                            if not private and event_epoch and eid > 0 and eid <= last_event_id:
                                await acknowledge()
                                continue
                            if event_type == OBSERVABILITY_BATCH_EVENT_TYPE:
                                self._apply_processing_observability_event(server.id, event)
                                if eid:
                                    last_event_id = max(last_event_id, eid)
                                    await acknowledge()
                                continue
                            if event_type and event_type != PROJECTED_PACKET_EVENT_TYPE:
                                if private:
                                    raise ProcessingContinuityError("unsupported_private_event_type")
                                if eid:
                                    last_event_id = max(last_event_id, eid)
                                    await acknowledge()
                                continue
                            name = str(event.get("pipeline_name") or "").strip()
                            inbox = self._inboxes.get(name)
                            if inbox is None:
                                if private:
                                    raise ProcessingContinuityError("private_origin_inbox_missing")
                                if eid:
                                    last_event_id = max(last_event_id, eid)
                                    await acknowledge()
                                continue
                            put_result = await inbox.put(
                                event, timeout_s=None if private else 0.05, cancel_event=self._stop,
                            )
                            if private and not put_result.accepted:
                                raise ProcessingContinuityError("private_origin_inbox_rejected")
                            if eid and put_result.accepted:
                                last_event_id = max(last_event_id, eid)
                                await acknowledge()
                    except asyncio.CancelledError:
                        raise
                    except ProcessingContinuityError as exc:
                        handle = self._servers.get(server.id)
                        if handle is not None:
                            handle.continuity_error = str(exc)
                        self._last_error = f"Processing server {server.id}: {exc}; delivery suspended"
                        logger.error("processing continuity lost server=%s reason=%s", server.id, exc)
                        return
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("processing pump failed server=%s: %s", server.id, exc)
                        if not private:
                            await self._suspend_remote_origins(server.id)
                        await asyncio.sleep(backoff_s)
                        backoff_s = min(10.0, backoff_s * 1.5)
                        continue
                    if not private:
                        await self._suspend_remote_origins(server.id)
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(10.0, backoff_s * 1.5)
            except asyncio.CancelledError:
                raise
            finally:
                handle = self._servers.get(server.id)
                if handle is not None:
                    handle.last_event_id = last_event_id
                    handle.event_epoch = event_epoch

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
            logger.warning(
                "processing config push failed server=%s; will retry: %s", server.id, exc
            )

    def _apply_processing_observability_event(
        self,
        server_id: str,
        event: dict[str, Any],
    ) -> None:
        handle = self._servers.get(server_id)
        try:
            result = apply_observability_batch(
                event,
                stats_store=self._runtime_deps_base.pipeline_stats_store,
                telemetry_store=self._runtime_deps_base.pipeline_telemetry_store,
                apply_image_markers=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("processing observability apply failed server=%s: %s", server_id, exc)
            if handle is not None:
                handle.observability_skipped_records += len(event.get("records") or [])
            return
        if handle is not None:
            handle.observability_applied_records += int(result.get("applied") or 0)
            handle.observability_skipped_records += int(result.get("skipped") or 0)
