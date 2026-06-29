from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies, SinkRuntime, SourceOperatorRuntime
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME, normalize_artifact_name
from toposync.runtime.pipelines.operator_registry import OperatorDiagnostic, OperatorRegistry
from toposync.runtime.pipelines.packet_contract import resolve_media_ts
from toposync.runtime.pipelines.runtime import Lifecycle, Packet

from ..api.models import EXTENSION_ID
from ..streaming.runtime_state import TransmissionRuntimeState


@dataclass(frozen=True, slots=True)
class StreamingRuntimeBindings:
    runtime_state: TransmissionRuntimeState


_GLOBAL_RUNTIME_BINDINGS: StreamingRuntimeBindings | None = None


def set_streaming_runtime_bindings(bindings: StreamingRuntimeBindings | None) -> None:
    global _GLOBAL_RUNTIME_BINDINGS
    _GLOBAL_RUNTIME_BINDINGS = bindings


def get_streaming_runtime_bindings() -> StreamingRuntimeBindings | None:
    return _GLOBAL_RUNTIME_BINDINGS


class PublishVideoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str = ""
    input_artifact_name: str = ""
    resize_mode: Literal["contain", "none"] = "contain"
    writer_priority: int = 0
    bypass_mode: Literal["auto", "force_on", "force_off"] = "auto"
    publication_enabled: bool = True
    publication_camera_id: str = ""
    publication_camera_source_id: str = ""
    publication_live_view_id: str = ""
    publication_live_view_label: str = ""
    publication_variant_id: str = ""
    publication_variant_label: str = ""
    publication_role: Literal["main", "sub", "zoom", "custom"] = "custom"
    publication_label: str = ""
    publication_show_in_dashboard: bool = True
    publication_show_in_home_assistant: bool = False
    publication_quality_profile_id: str = ""

    @field_validator(
        "transmission_id",
        "publication_camera_id",
        "publication_camera_source_id",
        "publication_live_view_id",
        "publication_live_view_label",
        "publication_variant_id",
        "publication_variant_label",
        "publication_label",
        "publication_quality_profile_id",
        mode="after",
    )
    @classmethod
    def _trim_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("input_artifact_name", mode="after")
    @classmethod
    def _trim_input_artifact_name(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _validate_publication_target(self) -> "PublishVideoConfig":
        return self


class DemandGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transmission_id: str = ""
    demand_scope: Literal["transmission", "output"] = "transmission"
    output_id: str = ""
    quality_profile_id: str = ""
    poll_interval_ms: int = Field(default=500, ge=100, le=10_000)
    fail_open: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_scope(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        scope = str(data.get("demand_scope") or "").strip().lower()
        if scope == "auto":
            scope = "transmission"
        if scope not in {"transmission", "output"}:
            has_specific_output = bool(
                str(data.get("output_id") or "").strip()
                or str(data.get("quality_profile_id") or "").strip()
            )
            scope = "output" if has_specific_output else "transmission"
        data["demand_scope"] = scope
        return data

    @field_validator("transmission_id", "output_id", "quality_profile_id", mode="after")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class PublishVideoRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = PublishVideoConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        bindings = get_streaming_runtime_bindings()
        if bindings is None:
            return []
        if not self._config.transmission_id:
            return []

        writer_id = _build_writer_id(context)
        lifecycle_state = packet.lifecycle

        if lifecycle_state == Lifecycle.CLOSE:
            await bindings.runtime_state.close_writer(
                transmission_id=self._config.transmission_id,
                writer_id=writer_id,
            )
            return []

        frame = _extract_frame(packet, artifact_name=self._config.input_artifact_name)
        frame_ts = _resolve_frame_ts(packet)

        await bindings.runtime_state.update_writer_frame(
            transmission_id=self._config.transmission_id,
            writer_id=writer_id,
            lifecycle_state=lifecycle_state,
            writer_priority=self._config.writer_priority,
            frame=frame,
            frame_ts=frame_ts,
        )
        return []


class DemandGateRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = DemandGateConfig.model_validate(config)
        self._dependencies = dependencies
        self._last_open: bool | None = None
        self._last_payload: dict[str, Any] = {}

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        is_open, payload = await self._resolve_gate_state()
        if self._last_open is not None and self._last_open == is_open:
            self._last_payload = payload
            return None

        self._last_open = is_open
        self._last_payload = payload
        lifecycle = Lifecycle.OPEN if is_open else Lifecycle.CLOSE
        output_id, quality_profile_id = self._selected_demand_target()
        stream_id = _demand_gate_stream_id(
            self._config.transmission_id,
            output_id=output_id,
            quality_profile_id=quality_profile_id,
        )
        return Packet.create(
            stream_id=stream_id,
            lifecycle=lifecycle,
            payload={
                "gate_open": bool(is_open),
                "transmission_id": self._config.transmission_id,
                "demand_scope": self._config.demand_scope,
                "output_id": output_id,
                "quality_profile_id": quality_profile_id,
                **payload,
            },
        )

    async def idle_sleep(self, context) -> None:  # noqa: ANN001
        await context.sleep(max(0.1, float(self._config.poll_interval_ms) / 1000.0))

    async def _resolve_gate_state(self) -> tuple[bool, dict[str, Any]]:
        services = self._dependencies.services
        if services is None:
            return bool(self._config.fail_open), {
                "demand_active": bool(self._config.fail_open),
                "reason": "services_unavailable",
            }
        try:
            output_id, quality_profile_id = self._selected_demand_target()
            raw = await services.call(
                "streaming.demand.snapshot",
                transmission_id=self._config.transmission_id,
                output_id=output_id,
                quality_profile_id=quality_profile_id,
            )
        except Exception as exc:  # noqa: BLE001
            return bool(self._config.fail_open), {
                "demand_active": bool(self._config.fail_open),
                "reason": "demand_service_error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }

        snapshot = raw if isinstance(raw, dict) else {}
        active = bool(snapshot.get("demand_active") or snapshot.get("active") or snapshot.get("demand_signal"))
        return active, {
            "demand_active": active,
            "reason": str(snapshot.get("reason") or ("active_demand" if active else "no_active_demand")),
            "viewer_count_total": int(snapshot.get("viewer_count_total") or 0),
            "primed": bool(snapshot.get("primed")),
            "hint_active": bool(snapshot.get("hint_active")),
            "matched_outputs": int(snapshot.get("matched_outputs") or 0),
        }

    def _selected_demand_target(self) -> tuple[str, str]:
        if self._config.demand_scope != "output":
            return "", ""
        return self._config.output_id, self._config.quality_profile_id


def register_streaming_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("stream.demand_gate") is None:
        registry.register_operator(
            operator_id="stream.demand_gate",
            description="Demand-driven gate that opens camera capture only while a stream has viewers or heartbeat leases.",
            config_model=DemandGateConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            capabilities=["streaming", "gate_control", "realtime"],
            defaults=DemandGateConfig(transmission_id="stream_default").model_dump(mode="json"),
            share_strategy="by_signature",
            owner=EXTENSION_ID,
            runtime_factory=lambda config, deps: DemandGateRuntime(config, deps),
        )

    if registry.get("stream.publish_video") is not None:
        return

    registry.register_operator(
        operator_id="stream.publish_video",
        description="Publishes pipeline video frames as a generated live transmission variant.",
        config_model=PublishVideoConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["streaming", "sink", "realtime"],
        defaults=PublishVideoConfig(publication_enabled=True).model_dump(mode="json"),
        share_strategy="never",
        requires_artifacts=[MAIN_ARTIFACT_NAME],
        requires_media_fields=["ts", "width", "height"],
        input_modalities=["video"],
        owner=EXTENSION_ID,
        diagnostics_factory=_publish_video_diagnostics,
        runtime_factory=lambda config, _deps: PublishVideoRuntime(config),
    )


def _publish_video_diagnostics(_config: dict[str, Any], context: dict[str, Any]) -> list[OperatorDiagnostic]:
    upstream_nodes = context.get("upstream_nodes")
    if not isinstance(upstream_nodes, list):
        return []

    diagnostics: list[OperatorDiagnostic] = []
    seen_codes: set[str] = set()

    def add(code: str, message: str, suggestion: str, details: dict[str, Any] | None = None) -> None:
        if code in seen_codes:
            return
        seen_codes.add(code)
        diagnostics.append(
            OperatorDiagnostic(
                severity="warning",
                code=code,
                message=message,
                suggestion=suggestion,
                details=details or {},
            )
        )

    for item in upstream_nodes:
        node = item if isinstance(item, dict) else {}
        operator_id = str(node.get("operator_id") or "").strip()
        cfg = node.get("normalized_config")
        cfg = cfg if isinstance(cfg, dict) else {}
        node_id = str(node.get("node_id") or "").strip()

        if operator_id == "camera.motion_gate" and not bool(cfg.get("emit_when_idle", False)):
            add(
                "stream_publish_video_event_gated_motion",
                f"stream.publish_video is downstream of motion gate '{node_id}' with emit_when_idle=false.",
                "Use a continuous branch into stream.publish_video and keep motion gate on a separate analytics/event branch.",
                {"source_node_id": node_id},
            )
            continue

        emit_mode = str(cfg.get("emit_mode") or "").strip().lower()
        if operator_id == "vision.detect" and emit_mode in {"events", "event", "filter", "filter_frames"}:
            add(
                "stream_publish_video_event_gated_detection",
                f"stream.publish_video is downstream of detection '{node_id}' in emit_mode={emit_mode}.",
                "Use emit_mode='annotate' for visual streaming, or split detection onto a separate analytics/event branch.",
                {"source_node_id": node_id, "emit_mode": emit_mode},
            )
            continue

        if operator_id == "vision.track":
            add(
                "stream_publish_video_event_gated_tracking",
                f"stream.publish_video is downstream of tracking '{node_id}', which emits object event packets.",
                "Use a continuous visual branch for normal streaming, or keep this branch only when event-gated streaming is intentional.",
                {"source_node_id": node_id},
            )
            continue

        if operator_id == "vision.group_events":
            add(
                "stream_publish_video_event_gated_group_events",
                f"stream.publish_video is downstream of grouped events '{node_id}', which emits group lifecycle packets.",
                "Use a continuous visual branch for normal streaming, or keep this branch only when event-gated streaming is intentional.",
                {"source_node_id": node_id},
            )

    return diagnostics


def _build_writer_id(context) -> str:  # noqa: ANN001
    pipeline_name = str(getattr(context, "pipeline_name", "pipeline") or "pipeline").strip()
    node_id = str(getattr(context, "node_id", "stream.publish_video") or "stream.publish_video").strip()
    return f"{pipeline_name}:{node_id}"


def _demand_gate_stream_id(transmission_id: str, *, output_id: str = "", quality_profile_id: str = "") -> str:
    transmission = str(transmission_id or "").strip() or "stream"
    output = str(output_id or "").strip()
    profile = str(quality_profile_id or "").strip()
    suffix = ":".join(item for item in (output, profile) if item)
    return f"demand:{transmission}:{suffix}" if suffix else f"demand:{transmission}"


def _resolve_frame_ts(packet: Packet) -> float:
    return float(resolve_media_ts(packet))


def _extract_frame(packet: Packet, *, artifact_name: str | None) -> numpy.ndarray | None:
    name = normalize_artifact_name(artifact_name)
    artifact = packet.artifacts.get(name)
    if artifact is not None:
        return _normalize_artifact_frame(artifact.data)
    return None


def _normalize_artifact_frame(value: Any) -> numpy.ndarray | None:
    if value is None:
        return None

    frame = numpy.asarray(value)
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim != 3:
        return None

    channels = int(frame.shape[2])
    if channels == 4:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    elif channels == 1:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif channels < 3:
        return None
    elif channels > 3:
        frame = frame[:, :, :3]

    if frame.dtype != numpy.uint8:
        frame = numpy.clip(frame, 0, 255).astype(numpy.uint8)

    return numpy.ascontiguousarray(frame)
