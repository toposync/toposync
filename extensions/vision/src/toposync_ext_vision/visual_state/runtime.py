from __future__ import annotations

import asyncio
import io
import threading
import time
import uuid
from typing import Callable

import numpy as np
from PIL import Image

from toposync.runtime.pipelines.execution import BaseOperatorRuntime
from toposync.runtime.pipelines.preview import (
    PipelinePreviewError, freeze_reference_packet, reference_capture_evidence,
    reference_input_pipeline, reference_preparation_revision,
)
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ..processing.runtime_backends import build_embedding_backend
from ..registry import ModelRegistry, build_default_model_registry
from .matching import ReferenceEmbedding, compare_references
from .inspection import decision_visuals
from .schemas import Observation, VisualStateConfig
from .state import VisualStateReducer


class VisualStateRuntime(BaseOperatorRuntime):
    """One local inference plus one latest frame; decision expiry never waits for either."""

    def __init__(self, config, dependencies, *, backend_factory=build_embedding_backend,
                 preview_input_pipeline=None,
                 clock: Callable[[], float] = time.monotonic, unix_clock: Callable[[], float] = time.time):
        self.config = VisualStateConfig.model_validate(config)
        self.dependencies = dependencies
        self._backend_factory = backend_factory
        self._clock, self._unix_clock = clock, unix_clock
        self._backend = None
        self._references = {}
        self._native_lock = threading.Lock()
        self._closed = threading.Event()
        self._prefix = None
        self._preview_input_pipeline = preview_input_pipeline.model_copy(deep=True) if preview_input_pipeline is not None else None
        self._reducer = None
        self._instance = None
        self._retired_instances = set()
        self._last_sample = None
        self._last_observed = -1.0
        self._token = 0
        self._last_publication = None
        self._opened = False

    def _native(self, function, *arguments):
        with self._native_lock:
            if self._closed.is_set():
                raise RuntimeError("visual sensor stopped")
            try:
                return function(*arguments)
            finally:
                if self._closed.is_set():
                    self._close_backend()

    def _close_backend(self):
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self._references.clear()

    def _initialize(self, store):
        registry = self.dependencies.vision_model_registry
        if not isinstance(registry, ModelRegistry):
            registry = build_default_model_registry()
        manifest = registry.get_manifest(self.config.model_id)
        if manifest is None or manifest.task != "embedding":
            raise ValueError("visual sensor requires an explicit image embedding model")
        self._backend = self._backend_factory(manifest)
        if self._backend.revision != self.config.model_revision:
            raise ValueError("visual sensor model revision changed; recalibrate its references")
        for category in self.config.classes:
            for reference_id in category.reference_ids:
                if self._closed.is_set():
                    raise RuntimeError("visual sensor stopped")
                reference, content = store.read_configured_reference(self.config, category.class_id, reference_id)
                if not category.enabled:
                    continue
                with Image.open(io.BytesIO(content)) as image:
                    frame = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
                self._references[reference_id] = ReferenceEmbedding(
                    category.class_id, self._backend.revision, reference.evidence.preparation_revision,
                    self._backend.embed(frame),
                )

    async def prepare(self, context):
        self.config.validate_activation()
        if self.config.strategy != "examples":
            raise ValueError("multimodal visual sensor execution is not available yet")
        if self.dependencies.services is None:
            raise ValueError("visual sensor requires local configuration and private references")
        if self._preview_input_pipeline is not None:
            self._prefix = self._preview_input_pipeline
        else:
            if self.dependencies.config_store is None:
                raise ValueError("visual sensor requires local configuration and private references")
            occurrences = context.stats_node_occurrences or ((context.pipeline_name, context.node_id),)
            if len(occurrences) != 1:
                raise ValueError("visual sensors cannot share runtime state across pipeline nodes")
            pipeline_name, node_id = occurrences[0]
            pipeline = await self.dependencies.config_store.get_pipeline(pipeline_name)
            if pipeline is None or pipeline.processing_server_id != "local":
                raise ValueError("visual sensor requires a saved local pipeline")
            nodes = [node for node in pipeline.graph["nodes"] if node["id"] == node_id]
            if (len(nodes) != 1 or nodes[0]["operator"] != "vision.visual_state"
                    or VisualStateConfig.model_validate(nodes[0]["config"]) != self.config):
                raise ValueError("visual sensor configuration changed before execution")
            self._prefix = reference_input_pipeline(pipeline, node_id)
        if reference_preparation_revision(self._prefix) != self.config.preparation_revision:
            raise ValueError("visual sensor preparation changed; capture compatible references")
        if self.config.region_id is not None and not any(
            node["operator"] == "camera.image_crop" for node in self._prefix.graph["nodes"]
        ):
            raise ValueError("visual sensor region requires a prepared image crop")
        store = await self.dependencies.services.call("vision.visual_references")
        try:
            await context.run_blocking(self._native, self._initialize, store, mode="thread_pool",
                                       concurrency_key="vision.visual_state.resources", max_concurrency=1)
        except BaseException:
            await self.shutdown()
            raise
        self._reducer = VisualStateReducer(self.config, runtime_epoch=uuid.uuid4().hex, clock=self._clock)

    def _invalidate(self, reason):
        self._token += 1
        return self._reducer.invalidate(reason)

    def _admit(self, packet):
        if packet.lifecycle == Lifecycle.CLOSE:
            return None
        raw = packet.payload.get("capture_evidence")
        if isinstance(raw, dict) and self._last_sample is not None and raw.get("capture_instance") == self._instance:
            generation, sequence = raw.get("generation"), raw.get("sequence")
            if type(generation) is int and type(sequence) is int and (generation, sequence) <= self._last_sample:
                return None
        try:
            artifact = packet.artifacts.get(self.config.input_artifact_name)
            image = artifact.data if artifact is not None else None
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                    or image.shape[2] != 3 or min(image.shape[:2]) < 1 or image.shape[0] * image.shape[1] > 4_000_000):
                raise ValueError("input must be a bounded BGR image")
            now = self._clock()
            evidence = reference_capture_evidence(
                packet, self._prefix, maximum_age_ms=self.config.temporal.maximum_frame_age_ms,
                input_artifact_name=self.config.input_artifact_name,
                now_monotonic=now, now_unix=self._unix_clock(),
            )
        except (PipelinePreviewError, ValueError, TypeError, KeyError):
            self._invalidate("input_unobservable")
            return None
        if evidence["source_identity"] != self.config.source_identity:
            self._invalidate("source_mismatch")
            return None
        sample = evidence["sample"]
        instance = sample["capture_instance"]
        raw = packet.payload["capture_evidence"]
        observed = raw["captured_monotonic"] if raw.get("physical_timestamp_verified") else raw["received_monotonic"]
        if observed < self._last_observed or instance in self._retired_instances:
            return None
        if instance != self._instance:
            if len(self._retired_instances) >= 64:
                self._invalidate("capture_identity_churn")
                return None
            if self._instance is not None:
                self._retired_instances.add(self._instance)
                self._token += 1
                self._reducer = VisualStateReducer(self.config, runtime_epoch=uuid.uuid4().hex, clock=self._clock)
            self._instance = instance
            self._last_sample = None
        if self._last_sample is not None and sample["generation"] != self._last_sample[0]:
            self._token += 1
            self._reducer = VisualStateReducer(self.config, runtime_epoch=uuid.uuid4().hex, clock=self._clock)
        self._last_sample = (sample["generation"], sample["sequence"])
        self._last_observed = observed
        frozen = freeze_reference_packet(packet, input_artifact_name=self.config.input_artifact_name)
        frozen.artifacts[self.config.input_artifact_name].data.flags.writeable = False
        return frozen, evidence, observed, self._token

    async def _publish(self, context, processing_status):
        state = self._reducer.tick().model_copy(update={"processing_status": processing_status})
        marker = (state.runtime_epoch, state.state_version, processing_status)
        if marker == self._last_publication or context.is_cancelled() or self._closed.is_set():
            return
        self._last_publication = marker
        output = Packet.create(
            stream_id=f"visual_state:{self.config.sensor_id}",
            lifecycle=Lifecycle.UPDATE if self._opened else Lifecycle.OPEN,
            payload={"visual_state": state.model_dump(mode="json")},
        )
        self._opened = True
        await context.emit(output, timeout_s=0)
        if context.inspection_active():
            context.inspect("analyzed", output, reason=state.reason, model_id=self.config.model_id,
                            visuals=decision_visuals(state))

    async def run(self, context):
        if self._reducer is None:
            raise RuntimeError("visual sensor was not prepared")
        reader = asyncio.create_task(context.read())
        worker = None
        pending = active = None
        next_evaluation = 0.0
        analysis_started = 0.0
        failed = False
        try:
            while not context.is_cancelled() and not self._closed.is_set():
                await self._publish(context, "evaluating" if worker else "waiting" if pending else "failed" if failed else "idle")
                ready, _ = await asyncio.wait({reader, worker} if worker else {reader}, timeout=0.05)
                if context.is_cancelled() or self._closed.is_set():
                    break
                if reader in ready:
                    packet = reader.result()
                    if packet is not None:
                        token = self._token
                        admitted = self._admit(packet)
                        if token != self._token:
                            pending = None
                        if admitted is not None:
                            pending = admitted
                    reader = asyncio.create_task(context.read())
                if worker is not None and worker in ready:
                    try:
                        vector = worker.result()
                        frozen, evidence, observed, token = active
                        if token == self._token:
                            decision = compare_references(
                                self.config, vector, self._references, model_revision=self._backend.revision,
                                preparation_revision=evidence["preparation_revision"],
                                current_class_id=self._reducer.current_class_id,
                            )
                            self._reducer.observe(Observation(
                                sensor_id=self.config.sensor_id, runtime_epoch=self._reducer.runtime_epoch,
                                source_identity=evidence["source_identity"], preparation_revision=evidence["preparation_revision"],
                                reference_revision=self.config.reference_revision, model_revision=self.config.model_revision,
                                sample=evidence["sample"], observed_at=evidence["observed_at"], observed_monotonic=observed,
                                freshness_basis=evidence["freshness_basis"], region_quality="valid", method="local",
                                class_id=decision.class_id, acceptance=decision.acceptance, similarity=decision.similarity,
                                margin=decision.margin, reference_ids=decision.reference_ids, reason=decision.reason,
                                duration_ms=min(3_600_000, max(0, round((self._clock() - analysis_started) * 1000))),
                            ))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        context.metrics.record_error(RuntimeError("visual_state_local_analysis_failed"))
                        if active[3] == self._token:
                            self._invalidate("local_analysis_failed")
                            pending = None
                            failed = True
                    worker = active = None
                if worker is None and pending is not None and self._clock() >= next_evaluation:
                    active, pending = pending, None
                    frozen, evidence, observed, token = active
                    if (self._clock() - observed) * 1000 <= self.config.temporal.maximum_frame_age_ms and token == self._token:
                        frame = frozen.artifacts[self.config.input_artifact_name].data
                        analysis_started = self._clock()
                        worker = asyncio.create_task(context.run_blocking(
                            self._native, self._backend.embed, frame, mode="thread_pool",
                            concurrency_key=f"vision.visual_state:{self.config.sensor_id}", max_concurrency=1,
                        ))
                        next_evaluation = self._clock() + self.config.evaluation_interval_ms / 1000
                        failed = False
                    else:
                        active = None
        finally:
            reader.cancel()
            if worker is not None:
                worker.cancel()
            await asyncio.gather(reader, *([worker] if worker else []), return_exceptions=True)
            await self.shutdown()

    async def shutdown(self):
        self._closed.set()
        # The native call owns its resources until it returns, even if its awaiting task is canceled.
        if self._native_lock.acquire(blocking=False):
            try:
                self._close_backend()
            finally:
                self._native_lock.release()

    @property
    def resources_released(self):
        return self._closed.is_set() and not self._native_lock.locked()
