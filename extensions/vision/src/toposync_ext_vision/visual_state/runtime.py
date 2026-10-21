from __future__ import annotations

import asyncio
import io
import math
import re
import threading
import time
import uuid
from typing import Callable

import numpy as np
from PIL import Image

from toposync.runtime.pipelines.execution import BaseOperatorRuntime
from toposync.runtime.pipelines.preview import (
    PipelinePreviewError, freeze_reference_packet, reference_capture_evidence,
    reference_input_pipeline,
)
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ..processing.runtime_backends import build_embedding_backend
from ..registry import ModelRegistry, build_default_model_registry
from .matching import ReferenceEmbedding, compare_references
from .inspection import decision_visuals
from .preparation import VisualRegionUnavailable, prepare_visual_frame, visual_preparation_revision
from .schemas import MultimodalEvaluation, Observation, ReferenceEvidence, SuggestionProvenance, VisualStateConfig
from .state import VisualStateReducer


class VisualStateRuntime(BaseOperatorRuntime):
    """Independent local and multimodal workers, each with at most one latest frame."""

    def __init__(self, config, dependencies, *, backend_factory=build_embedding_backend,
                 preview_input_pipeline=None,
                 clock: Callable[[], float] = time.monotonic, unix_clock: Callable[[], float] = time.time):
        self.config = VisualStateConfig.model_validate(config)
        self.dependencies = dependencies
        self._backend_factory = backend_factory
        self._clock, self._unix_clock = clock, unix_clock
        self._backend = None
        self._references = {}
        self._ai_references = {}
        self._ai_classes = []
        self._ai_revision = None
        self._ai_evaluation = None
        self._suggestion_store = None
        self._suggestion_task = None
        self._suggestion_retry_at = 0.0
        self._suggestion_collect_at = 0.0
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
        self._release_publication = None

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
        self._ai_references.clear()

    def _initialize(self, store):
        if self.config.strategy != "direct":
            registry = self.dependencies.vision_model_registry
            if not isinstance(registry, ModelRegistry):
                registry = build_default_model_registry()
            manifest = registry.get_manifest(self.config.model_id)
            if manifest is None or manifest.task != "embedding":
                raise ValueError("visual sensor requires an explicit image embedding model")
            self._backend = self._backend_factory(manifest)
            if self._backend.revision != self.config.model_revision:
                raise ValueError("visual sensor model revision changed; recalibrate its references")
        reference_bytes = 0
        for category in self.config.classes:
            count = min(len(category.reference_ids), self.config.ai_references_per_class)
            # Spread over capture order; this is not a claim of visual diversity.
            selected = [category.reference_ids[round(index * (len(category.reference_ids) - 1) / max(1, count - 1))]
                        for index in range(count)]
            if category.enabled and self.config.strategy != "examples":
                self._ai_classes.append({"class_id": category.class_id, "label": category.label,
                                         "description": category.description, "reference_ids": selected})
            for reference_id in category.reference_ids:
                if self._closed.is_set():
                    raise RuntimeError("visual sensor stopped")
                reference, content = store.read_configured_reference(self.config, category.class_id, reference_id)
                if not category.enabled:
                    continue
                if self.config.strategy != "examples" and reference_id in selected:
                    reference_bytes += len(content)
                    if reference_bytes > 64 * 1024 * 1024:
                        raise ValueError("visual sensor AI references exceed the memory limit")
                    self._ai_references[reference_id] = content
                if self._backend is None:
                    continue
                with Image.open(io.BytesIO(content)) as image:
                    frame = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
                self._references[reference_id] = ReferenceEmbedding(
                    category.class_id, self._backend.revision, reference.evidence.preparation_revision,
                    self._backend.embed(frame),
                )

    async def prepare(self, context):
        self.config.validate_activation()
        if self.dependencies.services is None:
            raise ValueError("visual sensor requires local configuration and private references")
        if (self.config.strategy != "examples"
                and "ai.infer.classify_visual_state" not in self.dependencies.services.list_ids()):
            raise ValueError("visual sensor multimodal service is unavailable")
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
        if visual_preparation_revision(self.config, self._prefix) != self.config.preparation_revision:
            raise ValueError("visual sensor preparation changed; capture compatible references")
        if (self.config.canonical_region is not None
                and "cameras.visual_region.prepare" not in self.dependencies.services.list_ids()):
            raise ValueError("visual sensor canonical region service is unavailable")
        if self.config.region_id is not None and self.config.canonical_region is None and not any(
            node["operator"] == "camera.image_crop" for node in self._prefix.graph["nodes"]
        ):
            raise ValueError("visual sensor region requires a prepared image crop")
        store = await self.dependencies.services.call("vision.visual_references")
        if self._preview_input_pipeline is None and self.config.suggestions.mode == "review":
            self._suggestion_store = store
        try:
            await context.run_blocking(self._native, self._initialize, store, mode="thread_pool",
                                       concurrency_key="vision.visual_state.resources", max_concurrency=1)
        except BaseException:
            await self.shutdown()
            raise
        self._reducer = VisualStateReducer(self.config, runtime_epoch=uuid.uuid4().hex, clock=self._clock)
        if self._preview_input_pipeline is None and "vision.visual_states" in self.dependencies.services.list_ids():
            try:
                publication = await self.dependencies.services.call("vision.visual_states")

                def snapshot():
                    if self._closed.is_set() or context.is_cancelled():
                        return None
                    return self._reducer.tick()

                self._release_publication = publication.register(pipeline_name, node_id, self.config, snapshot)
            except BaseException:
                await self.shutdown()
                raise

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

    async def _analyze(self, context, packet):
        frame, evidence = await prepare_visual_frame(self.config, self._prefix, packet, self.dependencies.services,
                                                     clock=self._clock, unix_clock=self._unix_clock)
        vector = None
        if self.config.strategy != "direct":
            vector = await context.run_blocking(self._native, self._backend.embed, frame, mode="thread_pool",
                concurrency_key=f"vision.visual_state:{self.config.sensor_id}", max_concurrency=1)
        return vector, evidence, frame

    def _observation(self, evidence, observed, *, started, method="local", **decision):
        return Observation(
            sensor_id=self.config.sensor_id, runtime_epoch=self._reducer.runtime_epoch,
            source_identity=evidence["source_identity"], preparation_revision=evidence["preparation_revision"],
            reference_revision=self.config.reference_revision, model_revision=self.config.model_revision,
            sample=evidence["sample"], observed_at=evidence["observed_at"], observed_monotonic=observed,
            freshness_basis=evidence["freshness_basis"], region_quality="valid", method=method,
            duration_ms=min(3_600_000, max(0, round((self._clock() - started) * 1000))), **decision,
        )

    def _fresh(self, observed, token):
        return token == self._token and 0 <= (self._clock() - observed) * 1000 < self.config.temporal.maximum_frame_age_ms

    async def _infer_multimodal(self, frame, observed):
        remaining = min(self.config.ai_timeout_ms / 1000,
                        observed + self.config.temporal.maximum_frame_age_ms / 1000 - self._clock())
        if remaining <= 0:
            return {"reason": "visual_state_deadline"}
        # The router owns monotonic deadlines and native cleanup; convert the testable observation clock once.
        return await self.dependencies.services.call("ai.infer.classify_visual_state",
            image=frame, classes=self._ai_classes, reference_images=dict(self._ai_references),
            sensor_id=self.config.sensor_id, profile_id=self.config.ai_profile_id,
            scene_description=self.config.scene_description, deadline_monotonic=time.monotonic() + remaining,
            max_attempts=self.config.ai_max_attempts, allow_remote_images=self.config.ai_allow_remote_images,
            sensor_limits=self.config.ai_limits.model_dump())

    def _multimodal_result(self, result, evidence, observed, started):
        value = result.model_dump() if hasattr(result, "model_dump") else result
        if not isinstance(value, dict):
            raise ValueError("invalid_visual_state_response")
        answer = value.get("observation")
        if answer is None:
            reason = value.get("reason")
            if not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,159}", reason) is None:
                reason = "visual_state_provider_failed"
            return self._observation(evidence, observed, started=started, method="multimodal", reason=reason), None
        revision = value.get("configuration_revision")
        if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{64}", revision) is None:
            raise ValueError("invalid_visual_state_response")
        if not isinstance(answer, dict) or set(answer) != {
            "observable", "class_id", "reason", "declared_confidence", "reference_ids",
        }:
            raise ValueError("invalid_visual_state_response")
        category, identifiers, confidence = answer["class_id"], answer["reference_ids"], answer["declared_confidence"]
        if (type(answer["observable"]) is not bool
                or (category is not None and (not isinstance(category, str) or category not in {
                    item["class_id"] for item in self._ai_classes}))
                or (not answer["observable"] and category is not None)
                or not isinstance(answer["reason"], str) or not answer["reason"].strip() or len(answer["reason"]) > 320
                or not isinstance(identifiers, list) or len(identifiers) > 64
                or any(not isinstance(identifier, str) or identifier not in self._ai_references for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
                or (confidence is not None and (type(confidence) not in {float, int}
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1))):
            raise ValueError("invalid_visual_state_response")
        observation = self._observation(evidence, observed, started=started, method="multimodal",
            class_id=category, acceptance="enter" if category is not None else "abstain",
            reference_ids=identifiers, declared_confidence=confidence,
            reason="multimodal_unobservable" if not answer["observable"] else
                   "multimodal_match" if category is not None else "multimodal_ambiguous")
        if not answer["observable"]:
            observation = observation.model_copy(update={"region_quality": "unobservable"})
        return observation, revision

    def _queue_suggestion(self, context, frame, evidence, metadata, observation, revision, runtime_epoch):
        if (self._suggestion_store is None or self._closed.is_set() or observation.class_id is None
                or observation.region_quality != "valid" or revision is None
                or self._clock() < self._suggestion_retry_at
                or (self._suggestion_task is not None and not self._suggestion_task.done())):
            return
        try:
            provenance = SuggestionProvenance(runtime_epoch=runtime_epoch, proposed_class_id=observation.class_id,
                configuration_digest=self._suggestion_store.configuration_digest(self.config),
                reference_revision=self.config.reference_revision, model_revision=self.config.model_revision,
                ai_configuration_revision=revision, profile_id=metadata.get("profile_id"),
                provider_id=metadata.get("provider_id"), model=metadata.get("model"),
                reason=metadata["observation"]["reason"], declared_confidence=observation.declared_confidence,
                input_reference_ids=tuple(self._ai_references))
            prepared_evidence = ReferenceEvidence.model_validate(evidence)
        except (ValueError, TypeError, KeyError):
            context.metrics.record_error(RuntimeError("visual_state_suggestion_provenance_invalid"))
            self._suggestion_retry_at = self._clock() + 60
            return
        # At most one retained frame and one native write per sensor; no suggestion queue.
        self._suggestion_retry_at = self._clock() + 1
        collect = self._clock() >= self._suggestion_collect_at
        if collect:
            self._suggestion_collect_at = self._clock() + 60

        def save():
            if self._closed.is_set():
                return
            if collect:
                self._suggestion_store.collect(limit=20)
            if self._closed.is_set():
                return
            output = io.BytesIO()
            Image.fromarray(frame[:, :, ::-1]).save(output, format="PNG")
            if not self._closed.is_set():
                self._suggestion_store.offer_suggestion(self.config, observation.class_id, output.getvalue(),
                                                       prepared_evidence, provenance)

        async def persist():
            try:
                await context.scheduler.run_sync(save, mode="thread_pool",
                    concurrency_key="vision.visual_state.suggestions", max_concurrency=1)
            except Exception:
                context.metrics.record_error(RuntimeError("visual_state_suggestion_storage_failed"))
                self._suggestion_retry_at = self._clock() + 60

        self._suggestion_task = asyncio.create_task(persist())

    async def _publish(self, context, processing_status):
        state = self._reducer.tick().model_copy(update={"processing_status": processing_status, "ai_evaluation": self._ai_evaluation})
        marker = (state.runtime_epoch, state.state_version, processing_status, self._ai_evaluation)
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
        ai_worker = None
        ai_pending = ai_active = None
        pending = active = None
        next_evaluation = 0.0
        analysis_started = 0.0
        failed = False
        try:
            while not context.is_cancelled() and not self._closed.is_set():
                await self._publish(context, "evaluating" if worker or ai_worker else
                                    "waiting" if pending or ai_pending else "failed" if failed else "idle")
                ready, _ = await asyncio.wait({task for task in (reader, worker, ai_worker) if task is not None}, timeout=0.05)
                if context.is_cancelled() or self._closed.is_set():
                    break
                if reader in ready:
                    packet = reader.result()
                    if packet is not None:
                        token = self._token
                        admitted = self._admit(packet)
                        if token != self._token:
                            pending = None
                            ai_pending = None
                        if admitted is not None:
                            pending = admitted
                    reader = asyncio.create_task(context.read())
                if worker is not None and worker in ready:
                    try:
                        vector, evidence, prepared_frame = worker.result()
                        frozen, _, observed, token = active
                        if self._fresh(observed, token):
                            needs_ai = self.config.strategy == "direct"
                            if not needs_ai:
                                decision = compare_references(
                                    self.config, vector, self._references, model_revision=self._backend.revision,
                                    preparation_revision=evidence["preparation_revision"],
                                    current_class_id=self._reducer.current_class_id,
                                )
                                observation = self._observation(evidence, observed, started=analysis_started,
                                    class_id=decision.class_id, acceptance=decision.acceptance, similarity=decision.similarity,
                                    margin=decision.margin, reference_ids=decision.reference_ids, reason=decision.reason)
                                needs_ai = self.config.strategy == "fallback" and decision.reason in {
                                    "ambiguous_classes", "low_similarity",
                                }
                                if not needs_ai:
                                    ai_pending = None
                                    self._reducer.observe(observation)
                            if needs_ai:
                                deferred = self._observation(evidence, observed, started=analysis_started,
                                    method="multimodal" if self.config.strategy == "direct" else "local",
                                    reason="multimodal_pending")
                                self._reducer.await_multimodal(deferred)
                                prepared_frame.flags.writeable = False
                                ai_pending = prepared_frame, evidence, observed, token, analysis_started
                    except asyncio.CancelledError:
                        raise
                    except VisualRegionUnavailable:
                        if active[3] == self._token:
                            self._invalidate("region_unavailable")
                            pending = None
                            ai_pending = None
                    except PipelinePreviewError:
                        if active[3] == self._token:
                            self._invalidate("input_unobservable")
                            pending = None
                            ai_pending = None
                    except Exception:
                        context.metrics.record_error(RuntimeError("visual_state_local_analysis_failed"))
                        if active[3] == self._token:
                            self._invalidate("local_analysis_failed")
                            pending = None
                            ai_pending = None
                            failed = True
                    worker = active = None
                if ai_worker is not None and ai_worker in ready:
                    prepared_frame, evidence, observed, token, started = ai_active
                    superseded = False
                    try:
                        result = ai_worker.result()
                        metadata = result.model_dump() if hasattr(result, "model_dump") else result
                        metadata = metadata if isinstance(metadata, dict) else {}
                        previous = self._reducer.snapshot().last_analysis
                        superseded = previous is not None and (
                            previous.sample.capture_instance != evidence["sample"]["capture_instance"]
                            or (previous.sample.generation, previous.sample.sequence) >= (
                                evidence["sample"]["generation"], evidence["sample"]["sequence"]))
                        observation, revision = self._multimodal_result(result, evidence, observed, started)
                        self._queue_suggestion(context, prepared_frame, evidence, metadata, observation, revision,
                                               self._ai_evaluation.runtime_epoch)
                        if self._fresh(observed, token) and not superseded:
                            if revision is not None:
                                if self._ai_revision is not None and revision != self._ai_revision:
                                    self._reducer = VisualStateReducer(self.config, runtime_epoch=uuid.uuid4().hex, clock=self._clock)
                                    observation = observation.model_copy(update={"runtime_epoch": self._reducer.runtime_epoch})
                                self._ai_revision = revision
                            self._reducer.observe(observation)
                            if observation.region_quality != "valid":
                                self._token += 1
                                pending = ai_pending = None
                            status = "complete" if metadata.get("observation") is not None else "limited" if (
                                re.fullmatch(r"(?:(?:sensor|group)_)?(?:minute|hour|day|month)_limit", observation.reason)
                                or metadata.get("retry_at") is not None) else "failed"
                            reason = observation.reason
                        else:
                            status, reason = "discarded", "multimodal_observation_expired" if not self._fresh(observed, token) else "multimodal_superseded"
                        retry_at = metadata.get("retry_at")
                        self._ai_evaluation = self._ai_evaluation.model_copy(update={
                            "status": status, "reason": reason,
                            "duration_ms": min(3_600_000, max(0, round((self._clock() - started) * 1000))),
                            "retry_at": retry_at if type(retry_at) in {int, float} and math.isfinite(retry_at) and retry_at >= 0 else None,
                            **{key: metadata[key][:256] for key in ("profile_id", "provider_id", "model") if isinstance(metadata.get(key), str)},
                        })
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        context.metrics.record_error(RuntimeError("visual_state_multimodal_analysis_failed"))
                        self._ai_evaluation = self._ai_evaluation.model_copy(update={"status": "failed",
                            "reason": "visual_state_multimodal_analysis_failed",
                            "duration_ms": min(3_600_000, max(0, round((self._clock() - started) * 1000)))})
                        if self._fresh(observed, token) and not superseded:
                            self._reducer.observe(self._observation(evidence, observed, started=started,
                                method="multimodal", reason="visual_state_multimodal_analysis_failed"))
                            failed = True
                    ai_worker = ai_active = None
                if ai_worker is None and ai_pending is not None:
                    ai_active, ai_pending = ai_pending, None
                    prepared_frame, evidence, observed, token, started = ai_active
                    if self._fresh(observed, token):
                        self._reducer.await_multimodal(self._observation(evidence, observed, started=started,
                            method="multimodal", reason="multimodal_pending"))
                        self._ai_evaluation = MultimodalEvaluation(runtime_epoch=self._reducer.runtime_epoch,
                            sample=evidence["sample"], observed_at=evidence["observed_at"], status="running",
                            reason="multimodal_pending", profile_id=self.config.ai_profile_id)
                        ai_worker = asyncio.create_task(self._infer_multimodal(prepared_frame, observed))
                    else:
                        ai_active = None
                if worker is None and pending is not None and self._clock() >= next_evaluation:
                    active, pending = pending, None
                    frozen, evidence, observed, token = active
                    if (self._clock() - observed) * 1000 <= self.config.temporal.maximum_frame_age_ms and token == self._token:
                        analysis_started = self._clock()
                        worker = asyncio.create_task(self._analyze(context, frozen))
                        next_evaluation = self._clock() + self.config.evaluation_interval_ms / 1000
                        failed = False
                    else:
                        active = None
        finally:
            self._closed.set()
            if self._release_publication is not None:
                self._release_publication()
            reader.cancel()
            if worker is not None:
                worker.cancel()
            if ai_worker is not None:
                ai_worker.cancel()
            cleanup = asyncio.gather(*(task for task in (reader, worker, ai_worker, self._suggestion_task) if task is not None), return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            await self.shutdown()
            if cancelled:
                raise asyncio.CancelledError

    async def shutdown(self):
        self._closed.set()
        if self._release_publication is not None:
            self._release_publication()
        # The native call owns its resources until it returns, even if its awaiting task is canceled.
        if self._native_lock.acquire(blocking=False):
            try:
                self._close_backend()
            finally:
                self._native_lock.release()

    @property
    def resources_released(self):
        return (self._closed.is_set() and not self._native_lock.locked()
                and (self._suggestion_task is None or self._suggestion_task.done()))
