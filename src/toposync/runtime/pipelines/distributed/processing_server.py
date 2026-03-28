from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

from toposync.extensions.manager import ExtensionManager
from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.event_bus import EventBus
from toposync.runtime.notifications.events import EventBroadcaster
from toposync.runtime.services import ServiceRegistry
from toposync.runtime.processing_diagnostics import collect_processing_server_diagnostics

from ..builtins import register_builtin_operators
from ..compiler import PipelineGraphCompiler
from ..execution import PipelineRuntimeDependencies
from ..execution_scheduler import ExecutionScheduler
from ..operator_registry import OperatorRegistry
from ..shared_runtime import PipelineBundleRuntime
from ..runtime import ArtifactMemoryCounter
from ..step_snapshots import PipelineStepSnapshotStore
from ..telemetry import (
    PipelineTelemetryStore,
    create_default_pipeline_telemetry_disk_checkpoint,
    create_default_pipeline_telemetry_store,
)
from .plan import build_distributed_graphs


logger = logging.getLogger("toposync.processing")


class ProcessingConfig(BaseModel):
    pipelines: list[Pipeline] = Field(default_factory=list)


class ProcessingAck(BaseModel):
    last_event_id: int = Field(default=0, ge=0)


class ProcessingVisionManifestImportRequest(BaseModel):
    manifest_text: str = ""
    artifact_path: str = ""
    replace_existing: bool = False


class ProcessingVisionModelInstallRequest(BaseModel):
    force: bool = False
    mode: str = ""
    acknowledge_upstream_terms: bool = False
    requested_by: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class _ActiveBundle:
    runtime: PipelineBundleRuntime
    pipelines: list[Pipeline]


class ProcessingServerRuntime:
    def __init__(
        self,
        *,
        config_store: ConfigStore,
        services: ServiceRegistry,
        operator_registry: OperatorRegistry,
        compiler: PipelineGraphCompiler,
        pipeline_telemetry_store: PipelineTelemetryStore | None = None,
        max_recent_events: int = 2500,
        max_replay_events: int = 500,
    ) -> None:
        self._config_store = config_store
        self._services = services
        self._registry = operator_registry
        self._compiler = compiler
        self._pipeline_telemetry_store = pipeline_telemetry_store
        self._snapshot_store = PipelineStepSnapshotStore(files_dir=config_store.paths.files_dir)
        self.broadcaster = EventBroadcaster(max_queue_size=500)
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=max(200, int(max_recent_events)))
        self._replay_events: deque[dict[str, Any]] = deque(maxlen=max(50, int(max_replay_events)))
        self._event_seq = 0
        self._last_acked_event_id = 0
        self._active: _ActiveBundle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        active = self._active
        self._active = None
        if active is not None:
            try:
                await active.runtime.stop()
            except Exception:
                pass
        self._recent_events.clear()
        self._replay_events.clear()
        self._event_seq = 0
        self._last_acked_event_id = 0

    def status(self) -> dict[str, Any]:
        active = self._active
        return {
            "active": bool(active),
            "pipelines": [p.name for p in (active.pipelines if active else [])],
            "runtime": active.runtime.snapshot() if active else None,
            "last_event_id": self._event_seq,
            "last_acked_event_id": self._last_acked_event_id,
            "recent_events": len(self._recent_events),
            "replay_events": len(self._replay_events),
        }

    def apply_config(self, payload: dict[str, Any]) -> None:
        parsed = ProcessingConfig.model_validate(payload)
        desired = [p for p in parsed.pipelines if p.type == "final"]
        self._reconcile(desired)

    def _reconcile(self, desired: list[Pipeline]) -> None:
        active = self._active
        if active is not None:
            existing_sig = json.dumps([p.model_dump(mode="json") for p in active.pipelines], sort_keys=True)
        else:
            existing_sig = ""
        desired_sig = json.dumps([p.model_dump(mode="json") for p in desired], sort_keys=True)
        if desired_sig == existing_sig:
            return

        loop = self._loop
        if loop is None:
            return
        loop.create_task(self._apply(desired), name="toposync.processing.apply_config")

    async def _apply(self, desired: list[Pipeline]) -> None:
        await self.stop()
        if not desired:
            return

        processing_pipelines: list[Pipeline] = []
        for pipeline in desired:
            graphs = build_distributed_graphs(pipeline, self._registry)
            if graphs.processing_graph is None:
                continue
            processing_pipelines.append(
                Pipeline(
                    name=f"{pipeline.name}__processing",
                    type="final",
                    graph=graphs.processing_graph,
                ),
            )

        if not processing_pipelines:
            return

        report = self._compiler.compile_many(processing_pipelines)
        def _env_int(name: str, default: int) -> int:
            raw = str(os.getenv(name) or "").strip()
            if not raw:
                return int(default)
            try:
                return int(raw)
            except Exception:
                return int(default)

        artifact_max_bytes_per_packet = _env_int("TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET", 128 * 1024 * 1024)
        artifact_max_total_bytes_per_pipeline = _env_int("TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE", 512 * 1024 * 1024)
        artifact_max_total_bytes_global = _env_int("TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL", 1024 * 1024 * 1024)
        artifact_global_counter = (
            ArtifactMemoryCounter(limit_bytes=artifact_max_total_bytes_global) if artifact_max_total_bytes_global > 0 else None
        )
        deps = PipelineRuntimeDependencies(
            config_store=self._config_store,
            services=self._services,
            logger=logger,
            files_dir=self._config_store.paths.files_dir,
            pipeline_snapshot_store=self._snapshot_store,
            processing_emit_projected_event=self._emit_projected_event,
            pipeline_telemetry_store=self._pipeline_telemetry_store,
            execution_scheduler=ExecutionScheduler(),
            artifact_max_bytes_per_packet=artifact_max_bytes_per_packet,
            artifact_max_total_bytes_per_pipeline=artifact_max_total_bytes_per_pipeline,
            artifact_global_counter=artifact_global_counter,
        )
        bundle = PipelineBundleRuntime(report=report, registry=self._registry, dependencies=deps, bundle_name="processing_bundle")
        await bundle.start()
        self._active = _ActiveBundle(runtime=bundle, pipelines=list(desired))

    async def _emit_projected_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        pipeline_name = str(event.get("pipeline_name") or "").strip()
        if not pipeline_name:
            return
        target_node_id = str(event.get("target_node_id") or "").strip()
        if not target_node_id:
            return

        self._event_seq += 1
        enriched = dict(event)
        enriched["event_id"] = self._event_seq
        self._recent_events.append(
            {
                "event_id": self._event_seq,
                "pipeline_name": pipeline_name,
                "target_node_id": target_node_id,
                "target_port": str(event.get("target_port") or "in"),
            },
        )
        self._replay_events.append(enriched)
        self.broadcaster.publish(enriched)

    def replay_after(self, last_event_id: int) -> list[dict[str, Any]]:
        after = max(0, int(last_event_id))
        if after <= 0:
            return list(self._replay_events)
        out: list[dict[str, Any]] = []
        for rec in self._replay_events:
            try:
                rid = int(rec.get("event_id") or 0)
            except Exception:
                rid = 0
            if rid > after:
                out.append(rec)
        return out

    def ack(self, last_event_id: int) -> None:
        acked = max(0, int(last_event_id))
        if acked <= self._last_acked_event_id:
            return
        self._last_acked_event_id = acked
        while self._replay_events:
            try:
                rid = int(self._replay_events[0].get("event_id") or 0)
            except Exception:
                rid = 0
            if rid <= acked:
                self._replay_events.popleft()
                continue
            break

    @property
    def last_acked_event_id(self) -> int:
        return self._last_acked_event_id

    @property
    def telemetry_store(self) -> PipelineTelemetryStore | None:
        return self._pipeline_telemetry_store


def create_processing_app() -> FastAPI:
    app = FastAPI(title="Toposync Processing Server", version="0.1.0")
    bus = EventBus()
    services = ServiceRegistry()
    operator_registry = OperatorRegistry()
    register_builtin_operators(operator_registry)
    pipeline_compiler = PipelineGraphCompiler(operator_registry)
    config_store = ConfigStore(paths=UserDataPaths.resolve())

    runtime = ProcessingServerRuntime(
        config_store=config_store,
        services=services,
        operator_registry=operator_registry,
        compiler=pipeline_compiler,
        pipeline_telemetry_store=create_default_pipeline_telemetry_store(),
    )
    telemetry_checkpoint = None

    def _processing_basic_auth() -> tuple[str, str] | None:
        username = str(os.getenv("TOPOSYNC_PROCESSING_USERNAME") or "").strip()
        password = str(os.getenv("TOPOSYNC_PROCESSING_PASSWORD") or "").strip()
        if not username and not password:
            return None
        return (username, password)

    @app.middleware("http")
    async def _basic_auth(request: Request, call_next):  # noqa: ANN001
        expected = _processing_basic_auth()
        if expected is None:
            return await call_next(request)

        expected_user, expected_pass = expected
        header = str(request.headers.get("authorization") or "")
        if header.lower().startswith("basic "):
            token = header.split(" ", 1)[1].strip()
            decoded = ""
            try:
                decoded = base64.b64decode(token).decode("utf-8")
            except Exception:
                decoded = ""
            provided_user, _, provided_pass = decoded.partition(":")
            if hmac.compare_digest(provided_user, expected_user) and hmac.compare_digest(provided_pass, expected_pass):
                return await call_next(request)

        return Response(status_code=401, headers={"WWW-Authenticate": "Basic"}, content="Unauthorized")

    @app.on_event("startup")
    async def _startup() -> None:
        nonlocal telemetry_checkpoint
        os.environ.setdefault("TOPOSYNC_ROLE", "processing")
        await config_store.load()
        runtime.start()
        telemetry_checkpoint = create_default_pipeline_telemetry_disk_checkpoint(
            runtime.telemetry_store,
            data_dir=config_store.paths.data_dir,
        )
        app.state.pipeline_telemetry_checkpoint = telemetry_checkpoint
        if telemetry_checkpoint is not None:
            try:
                await telemetry_checkpoint.load()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load telemetry checkpoint: %s", exc)
            try:
                telemetry_checkpoint.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to start telemetry checkpoint loop: %s", exc)

        ext_manager = ExtensionManager(group="toposync.extensions")
        app.state.bus = bus
        app.state.services = services
        app.state.config_store = config_store
        app.state.pipeline_operator_registry = operator_registry
        app.state.pipeline_graph_compiler = pipeline_compiler
        await ext_manager.load(app=app, bus=bus, services=services)
        app.state.extensions = ext_manager

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()
        if telemetry_checkpoint is not None:
            try:
                await telemetry_checkpoint.close()
            except Exception:
                pass

    @app.post("/api/processing/config")
    async def set_processing_config(body: ProcessingConfig) -> dict[str, Any]:
        runtime.apply_config(body.model_dump(mode="json"))
        return {"ok": True}

    @app.get("/api/processing/status")
    async def get_processing_status() -> dict[str, Any]:
        status = runtime.status()
        try:
            status.update(await collect_processing_server_diagnostics(data_dir=str(config_store.paths.data_dir)))
        except Exception:
            pass
        return status

    @app.post("/api/processing/vision/manifests/import")
    async def import_processing_vision_manifest(body: ProcessingVisionManifestImportRequest) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry import ModelRegistryError, import_custom_manifest
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Vision extension unavailable: {exc}") from exc

        try:
            return import_custom_manifest(
                manifest_text=body.manifest_text,
                artifact_path_override=body.artifact_path,
                data_dir=config_store.paths.data_dir,
                replace_existing=bool(body.replace_existing),
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/processing/vision/models/{model_id}/install")
    async def install_processing_vision_model(
        model_id: str,
        body: ProcessingVisionModelInstallRequest,
    ) -> dict[str, Any]:
        try:
            result = await services.call(
                "vision.model_install.start",
                model_id=model_id,
                force=bool(body.force),
                mode=str(body.mode or "").strip(),
                acknowledge_upstream_terms=bool(body.acknowledge_upstream_terms),
                requested_by=dict(body.requested_by or {}),
                data_dir=config_store.paths.data_dir,
            )
        except KeyError as exc:
            raise HTTPException(status_code=500, detail=f"Vision install service unavailable: {exc}") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/models/{model_id}/artifact")
    async def upload_processing_vision_model_artifact(
        model_id: str,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.artifact_upload import upload_model_artifact
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Vision extension unavailable: {exc}") from exc

        try:
            result = await asyncio.to_thread(
                upload_model_artifact,
                model_id=model_id,
                stream=file.file,
                filename=file.filename or "",
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await file.close()
        return dict(result or {})

    @app.post("/api/processing/events/ack")
    async def ack_processing_events(body: ProcessingAck) -> dict[str, Any]:
        runtime.ack(body.last_event_id)
        return {"ok": True, "last_acked_event_id": runtime.last_acked_event_id}

    @app.get("/api/processing/events/stream")
    async def stream_processing_events(request: Request) -> StreamingResponse:
        q = runtime.broadcaster.subscribe()
        last_event_id = 0
        try:
            last_event_id = int(request.headers.get("Last-Event-ID") or 0)
        except Exception:
            last_event_id = 0
        replay = runtime.replay_after(last_event_id)

        async def gen():
            try:
                yield "retry: 1000\n\n"
                yield "event: ready\ndata: {}\n\n"
                for item in replay:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                while True:
                    event = await q.get()
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                runtime.broadcaster.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
