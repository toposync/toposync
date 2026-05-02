from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, TransformOperatorRuntime
from toposync.runtime.pipelines.images import resolve_image_artifact_for_data, set_image_key
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from toposync_ext_ai.providers import ConditionEvaluationResult, RegionDetection, RegionDetectionResult

from .image_utils import crop_bbox01, expand_bbox01, image_size, normalize_bbox01
from .schemas import AiConditionFilterConfig, AiSmartCropConfig


@dataclass(slots=True)
class _SmartCropState:
    bbox01: tuple[float, float, float, float] | None = None
    confidence: float = 0.0
    label: str = ""
    selected_detection_index: int | None = None
    detections: list[RegionDetection] = field(default_factory=list)
    last_eval_at: float = 0.0
    last_result: RegionDetectionResult | None = None
    ptz_pose: tuple[float, float, float] | None = None
    ptz_pending_pose: tuple[float, float, float] | None = None
    ptz_idle_since: float = 0.0
    last_ptz_check_at: float = 0.0


@dataclass(slots=True)
class _ConditionState:
    last_eval_at: float = 0.0
    last_result: ConditionEvaluationResult | None = None
    stream_allowed: bool | None = None


@dataclass(frozen=True, slots=True)
class _DetectionSelection:
    bbox01: tuple[float, float, float, float]
    confidence: float
    label: str
    selected_detection_index: int | None
    detections: list[RegionDetection]


class AiSmartCropRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = AiSmartCropConfig.model_validate(config)
        self._deps = dependencies
        self._states: dict[str, _SmartCropState] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        stream_key = packet.stream_id
        if packet.lifecycle == Lifecycle.CLOSE:
            self._states.pop(stream_key, None)
            return [packet]

        cfg = self._config
        description = cfg.target_description.strip()
        if not description:
            return [self._annotate(packet, status="skipped", reason="missing_target_description")]

        image_key, artifact_name, image = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=cfg.input_with_fallback,
            fallback_to_stream_frame=cfg.fallback_to_stream_frame,
        )
        if image is None:
            return [self._annotate(packet, status="skipped", reason="missing_image")]

        state = self._states.setdefault(stream_key, _SmartCropState())
        now = time.monotonic()
        refresh_reason = await self._refresh_reason(packet=packet, state=state, now=now)
        should_infer = state.bbox01 is None or bool(refresh_reason)

        result: RegionDetectionResult | None = None
        if should_infer:
            result = await self._locate_region(image=image, description=description)
            state.last_eval_at = now
            state.last_result = result
            selection = self._select_detection(result=result, description=description)
            if selection is not None:
                state.bbox01 = selection.bbox01
                state.confidence = float(selection.confidence)
                state.label = selection.label or description
                state.selected_detection_index = selection.selected_detection_index
                state.detections = list(selection.detections)
            elif cfg.missing_policy != "reuse_last":
                state.bbox01 = None
                state.confidence = 0.0
                state.label = ""
                state.selected_detection_index = None
                state.detections = []

        bbox01 = state.bbox01
        if bbox01 is None:
            annotated = self._annotate(
                packet,
                status="not_found",
                reason=(result.reason if result is not None else "no_cached_bbox"),
                result=result,
                input_artifact_name=artifact_name,
                input_image_key=image_key,
            )
            if cfg.missing_policy == "drop":
                return []
            return [annotated]

        padded = expand_bbox01(bbox01, padding_ratio=cfg.padding_ratio)
        crop = crop_bbox01(image, bbox01=padded, min_crop_size_px=cfg.min_crop_size_px)
        if crop is None:
            annotated = self._annotate(
                packet,
                status="skipped",
                reason="crop_failed",
                result=result or state.last_result,
                bbox01=bbox01,
                padded_bbox01=padded,
                input_artifact_name=artifact_name,
                input_image_key=image_key,
            )
            return [] if cfg.missing_policy == "drop" else [annotated]

        out = self._with_crop_artifact(
            packet=packet,
            crop=crop,
            bbox01=bbox01,
            padded_bbox01=padded,
            input_artifact_name=artifact_name,
            input_image_key=image_key,
            result=result or state.last_result,
            confidence=state.confidence,
            label=state.label or description,
            selected_detection_index=state.selected_detection_index,
            detections=state.detections,
        )
        return [out]

    def _select_detection(self, *, result: RegionDetectionResult, description: str) -> _DetectionSelection | None:
        candidates: list[tuple[int, RegionDetection, tuple[float, float, float, float]]] = []
        for index, detection in enumerate(result.detections):
            bbox = normalize_bbox01(detection.bbox01)
            if bbox is None or detection.confidence < self._config.confidence_threshold:
                continue
            candidates.append((index, detection, bbox))

        if not candidates and result.found and result.confidence >= self._config.confidence_threshold:
            bbox = normalize_bbox01(result.bbox01)
            if bbox is not None:
                detection = RegionDetection(
                    bbox01=list(bbox),
                    confidence=float(result.confidence),
                    label=result.label or description,
                    reason=result.reason,
                )
                candidates.append((0, detection, bbox))

        if not candidates:
            return None

        strategy = self._config.detection_strategy
        if strategy == "union":
            x1 = min(item[2][0] for item in candidates)
            y1 = min(item[2][1] for item in candidates)
            x2 = max(item[2][2] for item in candidates)
            y2 = max(item[2][3] for item in candidates)
            confidence = min(float(item[1].confidence) for item in candidates)
            label = result.label or description
            return _DetectionSelection(
                bbox01=(x1, y1, x2, y2),
                confidence=confidence,
                label=label,
                selected_detection_index=None,
                detections=[item[1] for item in candidates],
            )

        if strategy == "first":
            selected = candidates[0]
        else:
            selected = max(candidates, key=lambda item: (item[1].confidence, -item[0]))
        return _DetectionSelection(
            bbox01=selected[2],
            confidence=float(selected[1].confidence),
            label=selected[1].label or result.label or description,
            selected_detection_index=selected[0],
            detections=[item[1] for item in candidates],
        )

    async def _locate_region(self, *, image: Any, description: str) -> RegionDetectionResult:
        services = self._deps.services
        if services is None:
            return RegionDetectionResult(found=False, reason="ai_service_unavailable")
        value = await services.call(
            "ai.infer.locate_region",
            image=image,
            description=description,
            profile_id=self._config.profile_id,
            fallback_profile_ids=self._config.fallback_profile_ids,
            min_confidence=float(self._config.confidence_threshold),
            fallback_on_low_confidence=bool(self._config.fallback_on_low_confidence),
        )
        if isinstance(value, RegionDetectionResult):
            return value
        if isinstance(value, dict):
            return RegionDetectionResult.model_validate(value)
        return RegionDetectionResult(found=False, reason="invalid_ai_service_response")

    async def _refresh_reason(self, *, packet: Packet, state: _SmartCropState, now: float) -> str:
        if state.bbox01 is None:
            return "initial"
        interval = float(self._config.refresh_interval_seconds)
        if interval == 0 or (interval > 0 and now - state.last_eval_at >= interval):
            return "interval"
        ptz_reason = await self._ptz_refresh_reason(packet=packet, state=state, now=now)
        return ptz_reason

    async def _ptz_refresh_reason(self, *, packet: Packet, state: _SmartCropState, now: float) -> str:
        if not self._config.refresh_on_ptz_idle:
            return ""
        if now - state.last_ptz_check_at < 1.0:
            return ""
        state.last_ptz_check_at = now
        camera_id = str(packet.payload.get("camera_id") or packet.metadata.get("camera_id") or "").strip()
        services = self._deps.services
        if not camera_id or services is None:
            return ""
        try:
            status = await services.call("cameras.ptz.get_status", camera_id=camera_id)
        except Exception:
            return ""
        if not isinstance(status, dict):
            return ""
        move_status = str(status.get("move_status") or "").strip().lower()
        if move_status and move_status not in {"idle", "stopped", "unknown"}:
            state.ptz_pending_pose = None
            state.ptz_idle_since = 0.0
            return ""
        try:
            pose = (
                round(float(status.get("pan") or 0.0), 4),
                round(float(status.get("tilt") or 0.0), 4),
                round(float(status.get("zoom") or 0.0), 4),
            )
        except Exception:
            return ""
        if state.ptz_pose is None:
            state.ptz_pose = pose
            return ""
        if pose == state.ptz_pose:
            state.ptz_pending_pose = None
            state.ptz_idle_since = 0.0
            return ""
        if state.ptz_pending_pose != pose:
            state.ptz_pending_pose = pose
            state.ptz_idle_since = now
            return ""
        if now - state.ptz_idle_since < float(self._config.ptz_idle_debounce_seconds):
            return ""
        state.ptz_pose = pose
        state.ptz_pending_pose = None
        state.ptz_idle_since = 0.0
        return "ptz_idle"

    def _with_crop_artifact(
        self,
        *,
        packet: Packet,
        crop: Any,
        bbox01: tuple[float, float, float, float],
        padded_bbox01: tuple[float, float, float, float],
        input_artifact_name: str | None,
        input_image_key: str | None,
        result: RegionDetectionResult | None,
        confidence: float,
        label: str,
        selected_detection_index: int | None,
        detections: list[RegionDetection],
    ) -> Packet:
        cfg = self._config
        output_name = cfg.output_artifact_name or "ai_crop"
        detection_payloads = self._detections_payload(detections)
        metadata: dict[str, Any] = {
            "source": "ai.smart_crop",
            "source_artifact_name": input_artifact_name,
            "source_image_key": input_image_key,
            "bbox01": list(padded_bbox01),
            "bbox01_detected": list(bbox01),
            "padding_ratio": float(cfg.padding_ratio),
            "confidence": float(confidence),
            "label": label,
            "detection_strategy": cfg.detection_strategy,
            "selected_detection_index": selected_detection_index,
            "detections": detection_payloads,
        }
        size = image_size(crop)
        out = packet.with_artifact(Artifact(name=output_name, data=crop, mime_type="image/raw", metadata=metadata))
        if cfg.output_image_key:
            out = set_image_key(out, key=cfg.output_image_key, artifact_name=output_name)

        payload = self._build_found_payload(
            out.payload,
            bbox01=bbox01,
            padded_bbox01=padded_bbox01,
            confidence=confidence,
            label=label,
            result=result,
            output_artifact_name=output_name,
            input_artifact_name=input_artifact_name,
            input_image_key=input_image_key,
            selected_detection_index=selected_detection_index,
            detections=detections,
        )

        if cfg.set_stream_frame and output_name != "frame":
            out = out.with_artifact(
                Artifact(
                    name="frame",
                    data=crop,
                    mime_type="image/raw",
                    metadata={"source": "ai.smart_crop", "derived_from": output_name},
                )
            )
            payload.setdefault("images", {})
            if isinstance(payload["images"], dict):
                payload["images"]["treated"] = "frame"
            if size is not None:
                payload["frame_width"], payload["frame_height"] = size
        elif output_name == "frame" and size is not None:
            payload["frame_width"], payload["frame_height"] = size

        return replace(out, payload=payload)

    def _build_found_payload(
        self,
        payload: dict[str, Any],
        *,
        bbox01: tuple[float, float, float, float],
        padded_bbox01: tuple[float, float, float, float],
        confidence: float,
        label: str,
        result: RegionDetectionResult | None,
        output_artifact_name: str,
        input_artifact_name: str | None,
        input_image_key: str | None,
        selected_detection_index: int | None,
        detections: list[RegionDetection],
    ) -> dict[str, Any]:
        next_payload = dict(payload)
        next_payload["object_bbox01"] = list(bbox01)
        next_payload["object_confidence"] = float(confidence)
        next_payload["object_category_label"] = label
        detected = {
            "category": label,
            "label": label,
            "confidence": float(confidence),
            "bbox01": list(bbox01),
            "source": "ai.smart_crop",
        }
        detection_payloads = self._detections_payload(detections) or [detected]
        next_payload["detected_object"] = detected
        next_payload["detected_objects"] = detection_payloads
        next_payload["frame_crop"] = {
            "bbox01": list(padded_bbox01),
            "bbox01_detected": list(bbox01),
            "detection_strategy": self._config.detection_strategy,
            "selected_detection_index": selected_detection_index,
            "set_stream_frame": bool(self._config.set_stream_frame),
            "output_artifact_name": output_artifact_name,
        }
        return self._annotate_payload(
            next_payload,
            task="smart_crop",
            data={
                "status": "found",
                "target_description": self._config.target_description,
                "confidence": float(confidence),
                "bbox01": list(bbox01),
                "bbox01_padded": list(padded_bbox01),
                "label": label,
                "detection_strategy": self._config.detection_strategy,
                "selected_detection_index": selected_detection_index,
                "selected_detection": detected,
                "detections": detection_payloads,
                "profile_id": result.profile_id if result is not None else "",
                "provider_id": result.provider_id if result is not None else "",
                "model": result.model if result is not None else "",
                "attempts": [item.model_dump() for item in (result.attempts if result is not None else [])],
                "input_artifact_name": input_artifact_name,
                "input_image_key": input_image_key,
                "output_artifact_name": output_artifact_name,
            },
        )

    @staticmethod
    def _detections_payload(detections: list[RegionDetection]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for index, detection in enumerate(detections):
            bbox = normalize_bbox01(detection.bbox01)
            if bbox is None:
                continue
            label = detection.label
            payloads.append(
                {
                    "index": index,
                    "category": label,
                    "label": label,
                    "confidence": float(detection.confidence),
                    "bbox01": list(bbox),
                    "reason": detection.reason,
                    "source": "ai.smart_crop",
                }
            )
        return payloads

    def _annotate(
        self,
        packet: Packet,
        *,
        status: str,
        reason: str,
        result: RegionDetectionResult | None = None,
        bbox01: tuple[float, float, float, float] | None = None,
        padded_bbox01: tuple[float, float, float, float] | None = None,
        input_artifact_name: str | None = None,
        input_image_key: str | None = None,
    ) -> Packet:
        payload = self._annotate_payload(
            packet.payload,
            task="smart_crop",
            data={
                "status": status,
                "reason": reason,
                "target_description": self._config.target_description,
                "confidence": float(result.confidence) if result is not None else 0.0,
                "bbox01": list(bbox01) if bbox01 is not None else None,
                "bbox01_padded": list(padded_bbox01) if padded_bbox01 is not None else None,
                "detection_strategy": self._config.detection_strategy,
                "detections": self._detections_payload(result.detections if result is not None else []),
                "profile_id": result.profile_id if result is not None else "",
                "provider_id": result.provider_id if result is not None else "",
                "model": result.model if result is not None else "",
                "attempts": [item.model_dump() for item in (result.attempts if result is not None else [])],
                "input_artifact_name": input_artifact_name,
                "input_image_key": input_image_key,
            },
        )
        return replace(packet, payload=payload)

    @staticmethod
    def _annotate_payload(payload: dict[str, Any], *, task: str, data: dict[str, Any]) -> dict[str, Any]:
        next_payload = dict(payload)
        ai = next_payload.get("ai")
        ai_payload = dict(ai) if isinstance(ai, dict) else {}
        ai_payload[task] = data
        next_payload["ai"] = ai_payload
        return next_payload


class AiConditionFilterRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = AiConditionFilterConfig.model_validate(config)
        self._deps = dependencies
        self._states: dict[str, _ConditionState] = {}

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        stream_key = packet.stream_id
        state = self._states.setdefault(stream_key, _ConditionState())

        if packet.lifecycle == Lifecycle.CLOSE:
            allowed = bool(state.stream_allowed)
            self._states.pop(stream_key, None)
            return [self._annotate(packet, result=state.last_result, status="closed")] if allowed else []

        description = self._config.condition_description.strip()
        if not description:
            return [self._annotate(packet, result=None, status="skipped", reason="missing_condition_description")]

        image_key, artifact_name, image = resolve_image_artifact_for_data(
            packet,
            input_with_fallback=self._config.input_with_fallback,
            fallback_to_stream_frame=self._config.fallback_to_stream_frame,
        )
        if image is None:
            return self._handle_failure(packet, state=state, reason="missing_image")

        now = time.monotonic()
        result = self._cached_result(state=state, now=now)
        if result is None:
            result = await self._evaluate_condition(image=image, description=description)
            state.last_eval_at = now
            if result.reason not in {
                "ai_service_unavailable",
                "all_ai_profiles_failed",
                "no_ai_profile_available",
                "invalid_ai_service_response",
            }:
                state.last_result = result

        passes = bool(result.matches and result.confidence >= self._config.confidence_threshold)
        if packet.lifecycle == Lifecycle.OPEN:
            state.stream_allowed = passes
        elif state.stream_allowed is None and passes:
            state.stream_allowed = True

        annotated = self._annotate(
            packet,
            result=result,
            status="matched" if passes else "not_matched",
            input_artifact_name=artifact_name,
            input_image_key=image_key,
        )
        return [annotated] if passes else []

    async def _evaluate_condition(self, *, image: Any, description: str) -> ConditionEvaluationResult:
        services = self._deps.services
        if services is None:
            return ConditionEvaluationResult(matches=False, reason="ai_service_unavailable")
        value = await services.call(
            "ai.infer.evaluate_condition",
            image=image,
            description=description,
            profile_id=self._config.profile_id,
            fallback_profile_ids=self._config.fallback_profile_ids,
            min_confidence=float(self._config.confidence_threshold),
            fallback_on_low_confidence=bool(self._config.fallback_on_low_confidence),
        )
        if isinstance(value, ConditionEvaluationResult):
            return value
        if isinstance(value, dict):
            return ConditionEvaluationResult.model_validate(value)
        return ConditionEvaluationResult(matches=False, reason="invalid_ai_service_response")

    def _cached_result(self, *, state: _ConditionState, now: float) -> ConditionEvaluationResult | None:
        result = state.last_result
        if result is None:
            return None
        age = now - state.last_eval_at
        if self._config.evaluation_interval_seconds > 0 and age < self._config.evaluation_interval_seconds:
            return result
        if self._config.reuse_last_decision_seconds > 0 and age <= self._config.reuse_last_decision_seconds:
            return result
        return None

    def _handle_failure(self, packet: Packet, *, state: _ConditionState, reason: str) -> list[Packet]:
        policy = self._config.failure_policy
        if policy == "pass_through":
            return [self._annotate(packet, result=state.last_result, status="skipped", reason=reason)]
        if policy == "reuse_last" and state.last_result is not None:
            passes = bool(
                state.last_result.matches and state.last_result.confidence >= self._config.confidence_threshold
            )
            annotated = self._annotate(packet, result=state.last_result, status="reused", reason=reason)
            return [annotated] if passes else []
        return []

    def _annotate(
        self,
        packet: Packet,
        *,
        result: ConditionEvaluationResult | None,
        status: str,
        reason: str = "",
        input_artifact_name: str | None = None,
        input_image_key: str | None = None,
    ) -> Packet:
        payload = dict(packet.payload)
        ai = payload.get("ai")
        ai_payload = dict(ai) if isinstance(ai, dict) else {}
        ai_payload["condition_filter"] = {
            "status": status,
            "reason": reason or (result.reason if result is not None else ""),
            "condition_description": self._config.condition_description,
            "matches": bool(result.matches) if result is not None else False,
            "confidence": float(result.confidence) if result is not None else 0.0,
            "profile_id": result.profile_id if result is not None else "",
            "provider_id": result.provider_id if result is not None else "",
            "model": result.model if result is not None else "",
            "attempts": [item.model_dump() for item in (result.attempts if result is not None else [])],
            "input_artifact_name": input_artifact_name,
            "input_image_key": input_image_key,
        }
        payload["ai"] = ai_payload
        return replace(packet, payload=payload)
