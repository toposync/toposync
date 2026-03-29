from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.pipelines.telemetry import METRIC_VISION_CONFIDENCE

from ...pipelines.schemas import VisionClassifyImageConfig
from ...registry.manifests import ModelManifest, ModelRegistry, build_default_model_registry
from ..contracts import ClassificationLabelScore, ClassifierBackend, ImageClassificationResult
from ..runtime_backends import build_classifier_backend


class VisionClassifyImageRuntime(TransformOperatorRuntime):
    def __init__(
        self,
        config: dict[str, Any],
        dependencies: PipelineRuntimeDependencies,
        *,
        operator_id: str = "vision.classify_image",
    ) -> None:
        self._parsed = VisionClassifyImageConfig.model_validate(config)
        self._dependencies = dependencies
        self._operator_id = str(operator_id or "").strip() or "vision.classify_image"
        self._backend: ClassifierBackend | None = None
        self._manifest: ModelManifest | None = None

    def _model_registry(self) -> ModelRegistry:
        registry = getattr(self._dependencies, "vision_model_registry", None)
        if isinstance(registry, ModelRegistry):
            return registry
        return build_default_model_registry()

    def _ensure_manifest(self) -> ModelManifest:
        if self._manifest is not None:
            return self._manifest
        self._manifest = self._model_registry().resolve_classifier_manifest(self._parsed.model_id)
        return self._manifest

    def _ensure_backend(self) -> ClassifierBackend:
        if self._backend is not None:
            return self._backend
        backend_factory = getattr(self._dependencies, "classifier_backend_factory", None)
        manifest = self._ensure_manifest()
        backend = build_classifier_backend(manifest) if backend_factory is None else backend_factory(manifest)
        if backend is None or not hasattr(backend, "classify"):
            raise TypeError("classifier_backend_factory must return an object that implements classify()")
        self._backend = backend
        return backend

    def _normalize_result(
        self,
        raw_result: ImageClassificationResult | dict[str, Any] | None,
        *,
        manifest: ModelManifest,
    ) -> ImageClassificationResult:
        if isinstance(raw_result, ImageClassificationResult):
            result = raw_result
        elif isinstance(raw_result, dict):
            result = ImageClassificationResult(**raw_result)
        else:
            result = ImageClassificationResult(labels=[], model_id=manifest.model_id)
        if not str(result.model_id or "").strip():
            result = replace(result, model_id=manifest.model_id)
        return result

    def _serialize_label_score(self, item: ClassificationLabelScore) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": item.label,
            "label_id": item.label_id,
            "score": float(item.score),
        }
        if item.metadata:
            payload["metadata"] = dict(item.metadata)
        return payload

    def _annotate_packet(
        self,
        packet: Packet,
        *,
        manifest: ModelManifest,
        backend: ClassifierBackend,
        result: ImageClassificationResult,
    ) -> Packet:
        labels = result.labels[: max(1, int(self._parsed.top_k))]
        top = labels[0] if labels else None
        top_label_normalized = str(top.label or "").strip().lower() if top is not None and str(top.label or "").strip() else None
        classification = {
            "top_label": top.label if top is not None else None,
            "top_label_normalized": top_label_normalized,
            "top_label_id": top.label_id if top is not None else None,
            "top_score": float(top.score) if top is not None else 0.0,
            "labels": [self._serialize_label_score(item) for item in labels],
            "scores": {item.label: float(item.score) for item in labels if item.label},
        }
        if result.metadata:
            classification["metadata"] = dict(result.metadata)

        payload = dict(packet.payload)
        vision = dict(payload.get("vision")) if isinstance(payload.get("vision"), dict) else {}
        vision.update(
            {
                "task": "classification",
                "model_id": manifest.model_id,
                "runtime": str(getattr(backend, "backend_id", "") or manifest.runtime),
                "classification": classification,
            }
        )
        payload["vision"] = vision
        payload.update(
            {
                "source_stream_id": packet.stream_id,
                "classification_label": classification["top_label"],
                "classification_label_normalized": top_label_normalized,
                "classification_score": classification["top_score"],
            }
        )

        metadata = dict(packet.metadata)
        metadata.update(
            {
                "operator_id": self._operator_id,
                "source_stream_id": packet.stream_id,
                "classification_label": classification["top_label"],
                "classification_label_normalized": top_label_normalized,
                "classification_score": classification["top_score"],
                "vision_task": "classification",
                "vision_model_id": manifest.model_id,
                "vision_runtime": vision["runtime"],
            }
        )
        return replace(packet, payload=payload, metadata=metadata)

    def _record_confidence_telemetry(self, *, context: Any, result: ImageClassificationResult) -> None:
        top = result.top_label
        if top is None:
            return
        observe_numeric = getattr(context, "observe_telemetry_numeric", None)
        if not callable(observe_numeric):
            return
        try:
            observe_numeric(METRIC_VISION_CONFIDENCE, float(top.score), now_s=time.time())
        except Exception:
            return

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        _image_key, _artifact_name, frame = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._parsed.input_with_fallback,
            fallback_to_stream_frame=bool(self._parsed.fallback_to_stream_frame),
        )
        if frame is None:
            return []

        manifest = self._ensure_manifest()
        backend = self._ensure_backend()
        concurrency_key = f"vision.classify_image:{manifest.runtime}:{manifest.model_id}"
        raw_result = await context.run_blocking(
            backend.classify,
            frame,
            concurrency_key=concurrency_key,
        )
        result = self._normalize_result(raw_result, manifest=manifest)
        self._record_confidence_telemetry(context=context, result=result)
        out = self._annotate_packet(packet, manifest=manifest, backend=backend, result=result)
        return [out]
