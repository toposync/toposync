from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

from toposync.extensions.manager import ExtensionManager
from toposync.runtime.config_store import AppSettings, ConfigStore, Pipeline, UserDataPaths
from toposync.runtime.event_bus import EventBus
from toposync.runtime.notifications.events import EventBroadcaster
from toposync.runtime.services import ServiceRegistry
from toposync.runtime.processing_diagnostics import collect_processing_server_diagnostics

from ..builtins import register_builtin_operators
from ..compiler import PipelineGraphCompiler
from ..execution import PipelineRuntimeDependencies
from ..execution_scheduler import ExecutionScheduler
from ..observability import OBSERVABILITY_BATCH_EVENT_TYPE, PROJECTED_PACKET_EVENT_TYPE
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


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    return max(int(min_value), min(int(max_value), value))


class ProcessingConfig(BaseModel):
    pipelines: list[Pipeline] = Field(default_factory=list)
    settings: AppSettings | None = None


class ProcessingAck(BaseModel):
    last_event_id: int = Field(default=0, ge=0)


class ProcessingVisionManifestImportRequest(BaseModel):
    manifest_text: str = ""
    artifact_path: str = ""
    replace_existing: bool = False
    imported_by: dict[str, Any] = Field(default_factory=dict)


class ProcessingVisionCustomOnnxRequest(BaseModel):
    artifact_path: str = ""
    uploaded_filename: str = ""
    display_name: str = ""
    task: Literal["classification", "detection", "segmentation"] = "detection"
    adapter_family: str = ""
    tensor_name: str = ""
    width: int = 640
    height: int = 640
    layout: str = "nchw"
    color_order: str = "rgb"
    resize_mode: str = "stretch"
    rescale_factor: float = 1.0
    normalization_mean: list[float] = Field(default_factory=list)
    normalization_std: list[float] = Field(default_factory=list)
    output_name: str = ""
    label_output_name: str = ""
    mask_output_name: str = ""
    box_format: str = "xyxy01"
    mask_format: str = "full_frame_binary"
    class_labels: list[str] = Field(default_factory=list)
    source_url: str = ""
    replace_existing: bool = False
    imported_by: dict[str, Any] = Field(default_factory=dict)


class ProcessingVisionHuggingFaceProbeRequest(BaseModel):
    repo: str = ""
    revision: str = ""


class ProcessingVisionHuggingFaceInspectRequest(BaseModel):
    repo_id: str = ""
    revision: str = ""
    onnx_filename: str = ""
    task: Literal["classification", "detection", "segmentation"] = "detection"


class ProcessingVisionHuggingFaceExportRequest(BaseModel):
    repo_id: str = ""
    revision: str = ""
    task: Literal["classification", "detection", "segmentation"] = "detection"
    recipe_id: str = ""
    acknowledge_upstream_terms: bool = False


class ProcessingVisionHuggingFaceImportRequest(BaseModel):
    artifact_path: str = ""
    repo_id: str = ""
    resolved_revision: str = ""
    onnx_filename: str = ""
    uploaded_filename: str = ""
    display_name: str = ""
    task: Literal["classification", "detection", "segmentation"] = "detection"
    adapter_family: str = ""
    artifact_source_kind: str = "hub_onnx"
    tensor_name: str = ""
    width: int = 640
    height: int = 640
    layout: str = "nchw"
    color_order: str = "rgb"
    resize_mode: str = "stretch"
    rescale_factor: float = 1.0
    normalization_mean: list[float] = Field(default_factory=list)
    normalization_std: list[float] = Field(default_factory=list)
    output_name: str = ""
    label_output_name: str = ""
    mask_output_name: str = ""
    box_format: str = "xyxy01"
    mask_format: str = "full_frame_binary"
    class_labels: list[str] = Field(default_factory=list)
    recipe_id: str = ""
    replace_existing: bool = False
    imported_by: dict[str, Any] = Field(default_factory=dict)


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
        self._observability_buffer: deque[dict[str, Any]] = deque()
        self._observability_flush_handle: asyncio.TimerHandle | None = None
        self._observability_flush_interval_s = _env_float(
            "TOPOSYNC_PROCESSING_OBSERVABILITY_FLUSH_INTERVAL_MS",
            1000.0,
        ) / 1000.0
        self._observability_batch_size = _env_int(
            "TOPOSYNC_PROCESSING_OBSERVABILITY_BATCH_SIZE",
            200,
            min_value=1,
            max_value=10_000,
        )
        self._observability_max_buffer = _env_int(
            "TOPOSYNC_PROCESSING_OBSERVABILITY_MAX_BUFFER",
            10_000,
            min_value=100,
            max_value=1_000_000,
        )
        self._observability_emitted_batches = 0
        self._observability_emitted_records = 0
        self._observability_dropped_records = 0

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
        self._cancel_observability_flush()
        self._observability_buffer.clear()

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
            "observability": {
                "buffered_records": len(self._observability_buffer),
                "emitted_batches": self._observability_emitted_batches,
                "emitted_records": self._observability_emitted_records,
                "dropped_records": self._observability_dropped_records,
                "batch_size": self._observability_batch_size,
                "flush_interval_s": self._observability_flush_interval_s,
                "max_buffer": self._observability_max_buffer,
            },
        }

    async def apply_config(self, payload: dict[str, Any]) -> None:
        parsed = ProcessingConfig.model_validate(payload)
        if parsed.settings is not None:
            await self._config_store.replace_settings(parsed.settings)
        desired = [p for p in parsed.pipelines if getattr(p, "enabled", True) is not False]
        self._reconcile(desired)

    def _reconcile(self, desired: list[Pipeline]) -> None:
        active = self._active
        if active is not None:
            existing_sig = json.dumps(
                [p.model_dump(mode="json") for p in active.pipelines], sort_keys=True
            )
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

        artifact_max_bytes_per_packet = _env_int(
            "TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET", 128 * 1024 * 1024
        )
        artifact_max_total_bytes_per_pipeline = _env_int(
            "TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE", 512 * 1024 * 1024
        )
        artifact_max_total_bytes_global = _env_int(
            "TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL", 1024 * 1024 * 1024
        )
        artifact_global_counter = (
            ArtifactMemoryCounter(limit_bytes=artifact_max_total_bytes_global)
            if artifact_max_total_bytes_global > 0
            else None
        )
        deps = PipelineRuntimeDependencies(
            config_store=self._config_store,
            services=self._services,
            logger=logger,
            files_dir=self._config_store.paths.files_dir,
            pipeline_snapshot_store=self._snapshot_store,
            processing_emit_projected_event=self._emit_projected_event,
            pipeline_telemetry_store=self._pipeline_telemetry_store,
            pipeline_observability_sink=self._enqueue_observability_record,
            execution_scheduler=ExecutionScheduler(),
            artifact_max_bytes_per_packet=artifact_max_bytes_per_packet,
            artifact_max_total_bytes_per_pipeline=artifact_max_total_bytes_per_pipeline,
            artifact_global_counter=artifact_global_counter,
        )
        bundle = PipelineBundleRuntime(
            report=report,
            registry=self._registry,
            dependencies=deps,
            bundle_name="processing_bundle",
        )
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

        enriched = dict(event)
        enriched["event_type"] = PROJECTED_PACKET_EVENT_TYPE
        self._publish_processing_event(
            enriched,
            recent={
                "pipeline_name": pipeline_name,
                "target_node_id": target_node_id,
                "target_port": str(event.get("target_port") or "in"),
            },
        )

    def _publish_processing_event(
        self,
        event: dict[str, Any],
        *,
        recent: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(event, dict):
            return
        self._event_seq += 1
        enriched = dict(event)
        event_type = str(enriched.get("event_type") or PROJECTED_PACKET_EVENT_TYPE).strip()
        enriched["event_type"] = event_type
        enriched["event_id"] = self._event_seq
        summary = dict(recent or {})
        summary["event_id"] = self._event_seq
        summary["event_type"] = event_type
        self._recent_events.append(summary)
        self._replay_events.append(enriched)
        self.broadcaster.publish(enriched)

    def _enqueue_observability_record(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            return
        while len(self._observability_buffer) >= self._observability_max_buffer:
            self._observability_buffer.popleft()
            self._observability_dropped_records += 1
        self._observability_buffer.append(dict(record))
        if len(self._observability_buffer) >= self._observability_batch_size:
            self._cancel_observability_flush()
            self._flush_observability()
            return
        self._schedule_observability_flush()

    def _schedule_observability_flush(self) -> None:
        if self._observability_flush_handle is not None:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        self._observability_flush_handle = loop.call_later(
            max(0.001, float(self._observability_flush_interval_s)),
            self._flush_observability,
        )

    def _cancel_observability_flush(self) -> None:
        handle = self._observability_flush_handle
        self._observability_flush_handle = None
        if handle is not None:
            handle.cancel()

    def _flush_observability(self) -> None:
        self._observability_flush_handle = None
        if not self._observability_buffer:
            return
        records: list[dict[str, Any]] = []
        while self._observability_buffer and len(records) < self._observability_batch_size:
            records.append(self._observability_buffer.popleft())
        if not records:
            return
        self._observability_emitted_batches += 1
        self._observability_emitted_records += len(records)
        self._publish_processing_event(
            {
                "event_type": OBSERVABILITY_BATCH_EVENT_TYPE,
                "schema_version": 1,
                "records": records,
            },
            recent={"record_count": len(records)},
        )
        if self._observability_buffer:
            self._schedule_observability_flush()

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
            if hmac.compare_digest(provided_user, expected_user) and hmac.compare_digest(
                provided_pass, expected_pass
            ):
                return await call_next(request)

        return Response(
            status_code=401, headers={"WWW-Authenticate": "Basic"}, content="Unauthorized"
        )

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
        await runtime.apply_config(body.model_dump(mode="json"))
        return {"ok": True}

    @app.get("/api/processing/status")
    async def get_processing_status() -> dict[str, Any]:
        status = runtime.status()
        timeout_s = _env_float("TOPOSYNC_PROCESSING_STATUS_DIAGNOSTICS_TIMEOUT", 3.5)
        try:
            status.update(
                await asyncio.wait_for(
                    collect_processing_server_diagnostics(
                        data_dir=str(config_store.paths.data_dir)
                    ),
                    timeout=timeout_s,
                )
            )
        except asyncio.TimeoutError:
            status["diagnostics"] = {
                "ok": False,
                "error": "processing diagnostics timed out",
                "timeout_s": timeout_s,
            }
        except Exception:
            status["diagnostics"] = {"ok": False, "error": "processing diagnostics failed"}
        return status

    @app.post("/api/processing/vision/manifests/import")
    async def import_processing_vision_manifest(
        body: ProcessingVisionManifestImportRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry import ModelRegistryError, import_custom_manifest
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            return import_custom_manifest(
                manifest_text=body.manifest_text,
                artifact_path_override=body.artifact_path,
                data_dir=config_store.paths.data_dir,
                replace_existing=bool(body.replace_existing),
                imported_by=dict(body.imported_by or {}),
                imported_via="api_processing_server_import",
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/processing/vision/custom-onnx/inspect")
    async def inspect_processing_custom_onnx(
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.custom_onnx import stage_custom_onnx_upload
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                stage_custom_onnx_upload,
                stream=file.file,
                filename=file.filename or "custom-model.onnx",
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await file.close()
        return dict(result or {})

    @app.post("/api/processing/vision/custom-onnx/preview")
    async def preview_processing_custom_onnx(
        config_json: str = Form("{}"),
        image: UploadFile = File(...),
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.custom_onnx import preview_custom_onnx_model
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            body = ProcessingVisionCustomOnnxRequest.model_validate_json(config_json or "{}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Invalid custom ONNX preview config: {exc}"
            ) from exc

        try:
            image_bytes = await image.read()
            result = await asyncio.to_thread(
                preview_custom_onnx_model,
                image_bytes=image_bytes,
                artifact_path=body.artifact_path,
                display_name=body.display_name,
                task=body.task,
                adapter_family=body.adapter_family,
                uploaded_filename=body.uploaded_filename,
                tensor_name=body.tensor_name,
                width=body.width,
                height=body.height,
                layout=body.layout,
                color_order=body.color_order,
                resize_mode=body.resize_mode,
                rescale_factor=body.rescale_factor,
                normalization_mean=body.normalization_mean,
                normalization_std=body.normalization_std,
                output_name=body.output_name,
                label_output_name=body.label_output_name,
                mask_output_name=body.mask_output_name,
                box_format=body.box_format,
                mask_format=body.mask_format,
                class_labels=body.class_labels,
                source_url=body.source_url,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await image.close()
        return dict(result or {})

    @app.post("/api/processing/vision/custom-onnx/import")
    async def import_processing_custom_onnx(
        body: ProcessingVisionCustomOnnxRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.custom_onnx import import_custom_onnx_model
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                import_custom_onnx_model,
                artifact_path=body.artifact_path,
                display_name=body.display_name,
                task=body.task,
                adapter_family=body.adapter_family,
                uploaded_filename=body.uploaded_filename,
                tensor_name=body.tensor_name,
                width=body.width,
                height=body.height,
                layout=body.layout,
                color_order=body.color_order,
                resize_mode=body.resize_mode,
                rescale_factor=body.rescale_factor,
                normalization_mean=body.normalization_mean,
                normalization_std=body.normalization_std,
                output_name=body.output_name,
                label_output_name=body.label_output_name,
                mask_output_name=body.mask_output_name,
                box_format=body.box_format,
                mask_format=body.mask_format,
                class_labels=body.class_labels,
                source_url=body.source_url,
                replace_existing=body.replace_existing,
                imported_by=dict(body.imported_by or {}),
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/huggingface/probe")
    async def probe_processing_huggingface(
        body: ProcessingVisionHuggingFaceProbeRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.huggingface import probe_huggingface_repo
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                probe_huggingface_repo,
                repo=body.repo,
                revision=body.revision,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/huggingface/inspect")
    async def inspect_processing_huggingface(
        body: ProcessingVisionHuggingFaceInspectRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.huggingface import inspect_huggingface_onnx
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                inspect_huggingface_onnx,
                repo=body.repo_id,
                revision=body.revision,
                onnx_filename=body.onnx_filename,
                task=body.task,
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/huggingface/export")
    async def export_processing_huggingface(
        body: ProcessingVisionHuggingFaceExportRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.huggingface import export_huggingface_model
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                export_huggingface_model,
                repo=body.repo_id,
                revision=body.revision,
                task=body.task,
                recipe_id=body.recipe_id,
                acknowledge_upstream_terms=bool(body.acknowledge_upstream_terms),
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/huggingface/import")
    async def import_processing_huggingface(
        body: ProcessingVisionHuggingFaceImportRequest,
    ) -> dict[str, Any]:
        try:
            from toposync_ext_vision.registry.huggingface import import_huggingface_onnx_model
            from toposync_ext_vision.registry.manifests import ModelRegistryError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

        try:
            result = await asyncio.to_thread(
                import_huggingface_onnx_model,
                artifact_path=body.artifact_path,
                repo_id=body.repo_id,
                resolved_revision=body.resolved_revision,
                onnx_filename=body.onnx_filename,
                display_name=body.display_name,
                task=body.task,
                adapter_family=body.adapter_family,
                artifact_source_kind=body.artifact_source_kind,
                uploaded_filename=body.uploaded_filename,
                tensor_name=body.tensor_name,
                width=body.width,
                height=body.height,
                layout=body.layout,
                color_order=body.color_order,
                resize_mode=body.resize_mode,
                rescale_factor=body.rescale_factor,
                normalization_mean=body.normalization_mean,
                normalization_std=body.normalization_std,
                output_name=body.output_name,
                label_output_name=body.label_output_name,
                mask_output_name=body.mask_output_name,
                box_format=body.box_format,
                mask_format=body.mask_format,
                class_labels=body.class_labels,
                recipe_id=body.recipe_id,
                replace_existing=body.replace_existing,
                imported_by=dict(body.imported_by or {}),
                data_dir=config_store.paths.data_dir,
            )
        except (ModelRegistryError, FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

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
            raise HTTPException(
                status_code=500, detail=f"Vision install service unavailable: {exc}"
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/models/{model_id}/cancel")
    async def cancel_processing_vision_model(
        model_id: str,
        body: ProcessingVisionModelInstallRequest,
    ) -> dict[str, Any]:
        try:
            result = await services.call(
                "vision.model_install.cancel",
                model_id=model_id,
                requested_by=dict(body.requested_by or {}),
                data_dir=config_store.paths.data_dir,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=500, detail=f"Vision install service unavailable: {exc}"
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(result or {})

    @app.post("/api/processing/vision/models/{model_id}/retry")
    async def retry_processing_vision_model(
        model_id: str,
        body: ProcessingVisionModelInstallRequest,
    ) -> dict[str, Any]:
        try:
            result = await services.call(
                "vision.model_install.retry",
                model_id=model_id,
                requested_by=dict(body.requested_by or {}),
                data_dir=config_store.paths.data_dir,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=500, detail=f"Vision install service unavailable: {exc}"
            ) from exc
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
            raise HTTPException(
                status_code=500, detail=f"Vision extension unavailable: {exc}"
            ) from exc

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
