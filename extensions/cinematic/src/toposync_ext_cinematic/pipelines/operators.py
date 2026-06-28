from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.pipelines.execution import SourceOperatorRuntime
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.operator_registry import (
    OperatorRegistry,
    artifact_name_hint,
    metadata_path_hint,
    payload_path_hint,
)
from toposync.runtime.pipelines.runtime import Lifecycle

from ..constants import EXTENSION_ID, OPERATOR_ID_DIRECTOR_SOURCE


Priority = Literal["low", "medium", "high"]
CameraMode = Literal["all", "include", "exclude"]
SourceRole = Literal["main", "sub", "zoom", "auto"]
WarmupMode = Literal["off", "next_idle", "event_high", "adaptive"]


class CinematicDirectorSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras_mode: CameraMode = "all"
    camera_ids: list[str] = Field(default_factory=list)
    priority_filter: list[Priority] = Field(default_factory=list)
    include_pipelines: list[str] = Field(default_factory=list)
    exclude_pipelines: list[str] = Field(default_factory=list)
    pipeline_camera_map: dict[str, str] = Field(default_factory=dict)
    manual_camera_priorities: dict[str, int] = Field(default_factory=dict)
    manual_event_type_priorities: dict[str, int] = Field(default_factory=dict)
    preferred_source_role: SourceRole = "auto"
    idle_dwell_seconds: float = Field(default=8.0, ge=2.0, le=120.0)
    event_min_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    cut_cooldown_seconds: float = Field(default=1.5, ge=0.0, le=60.0)
    close_hold_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    current_camera_sticky_seconds: float = Field(default=4.0, ge=0.0, le=60.0)
    max_event_hold_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    max_cuts_per_minute: int = Field(default=12, ge=1, le=120)
    fps: float = Field(default=8.0, ge=1.0, le=60.0)
    width: int = Field(default=1280, ge=160, le=7680)
    height: int = Field(default=720, ge=90, le=4320)
    warmup_mode: WarmupMode = "off"
    max_warm_cameras: int = Field(default=0, ge=0, le=8)
    handoff_timeout_seconds: float = Field(default=3.0, ge=0.1, le=30.0)
    stale_frame_max_age_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    ignore_own_pipeline_events: bool = True

    @field_validator("camera_ids", "include_pipelines", "exclude_pipelines", mode="before")
    @classmethod
    def _normalize_text_list(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        out: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out

    @field_validator("priority_filter", mode="before")
    @classmethod
    def _dedupe_priorities(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        out: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            text = str(item or "").strip().lower()
            if text not in {"low", "medium", "high"} or text in seen:
                continue
            out.append(text)
            seen.add(text)
        return out

    @field_validator("manual_camera_priorities", "manual_event_type_priorities", mode="before")
    @classmethod
    def _normalize_priority_map(cls, values: Any) -> dict[str, int]:
        if values is None:
            return {}
        if not isinstance(values, dict):
            return values
        out: dict[str, int] = {}
        for key, value in (values or {}).items():
            text = str(key or "").strip()
            if not text:
                continue
            out[text] = int(value)
        return out

    @field_validator("pipeline_camera_map", mode="before")
    @classmethod
    def _normalize_text_map(cls, values: Any) -> dict[str, str]:
        if values is None:
            return {}
        if not isinstance(values, dict):
            return values
        out: dict[str, str] = {}
        for key, value in (values or {}).items():
            pipeline_name = str(key or "").strip()
            camera_id = str(value or "").strip()
            if not pipeline_name or not camera_id:
                continue
            out[pipeline_name] = camera_id
        return out


class CinematicDirectorSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, object]) -> None:
        self._config = CinematicDirectorSourceConfig.model_validate(config)
        self._gate_open = True
        self._gate_known = False

    async def produce(self, context) -> object | None:  # noqa: ANN001
        await self._consume_gate_packets(context)
        return None

    async def idle_sleep(self, context) -> None:  # noqa: ANN001
        await context.sleep(max(0.1, 1.0 / float(self._config.fps)))

    async def _consume_gate_packets(self, context) -> None:  # noqa: ANN001
        gate_channel = context.inputs.get("gate")
        if gate_channel is None:
            self._gate_open = True
            self._gate_known = True
            return

        if not self._gate_known:
            self._gate_open = False

        while True:
            result = await gate_channel.get(timeout_s=0.0, cancel_event=context.cancel_event)
            if not result.accepted:
                break
            packet = result.item
            if packet is None:
                continue
            value = packet.payload.get("gate_open")
            if isinstance(value, bool):
                self._gate_open = value
                self._gate_known = True
            elif packet.lifecycle == Lifecycle.OPEN:
                self._gate_open = True
                self._gate_known = True
            elif packet.lifecycle == Lifecycle.CLOSE:
                self._gate_open = False
                self._gate_known = True


def _expression_hints() -> list[object]:
    return [
        payload_path_hint("payload.cinematic", value_type="object", description="Cinematic director metadata."),
        payload_path_hint("payload.cinematic.mode", value_type="string", description="Current director mode."),
        payload_path_hint("payload.cinematic.cut_reason", value_type="string", description="Reason for the current cut."),
        payload_path_hint("payload.cinematic.active_camera_id", value_type="string", description="Camera currently selected by the director."),
        payload_path_hint("payload.cinematic.active_event", value_type="object", description="Event currently driving the shot."),
        payload_path_hint("payload.cinematic.framing", value_type="object", description="Future-safe framing intent for zoom and overlays."),
        payload_path_hint("payload.camera_id", value_type="string", description="Active camera identifier."),
        payload_path_hint("payload.camera_name", value_type="string", description="Active camera display name."),
        payload_path_hint("payload.camera_source_id", value_type="string", description="Active camera source identifier."),
        payload_path_hint("payload.media", value_type="object", description="Video media descriptor."),
        metadata_path_hint("metadata.cinematic_mode", value_type="string", description="Current director mode copied into metadata."),
        metadata_path_hint("metadata.cinematic_cut_reason", value_type="string", description="Current cut reason copied into metadata."),
        artifact_name_hint(MAIN_ARTIFACT_NAME, description="Primary full-frame video artifact."),
    ]


def register_cinematic_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get(OPERATOR_ID_DIRECTOR_SOURCE) is not None:
        return

    registry.register_operator(
        operator_id=OPERATOR_ID_DIRECTOR_SOURCE,
        description="Event-directed cinematic camera source that chooses one active camera for a single video transmission.",
        config_model=CinematicDirectorSourceConfig,
        inputs=[{"name": "gate", "required": False}],
        outputs=[{"name": "out"}],
        capabilities=["source", "video", "realtime", "cinematic", "gate_control"],
        defaults=CinematicDirectorSourceConfig().model_dump(mode="json"),
        produces_payload_keys=["cinematic", "camera_id", "camera_name", "camera_source_id"],
        produces_artifacts=[MAIN_ARTIFACT_NAME],
        produces_source_fields=[
            "device_id",
            "source_id",
            "source_name",
            "view_id",
            "role",
            "kind",
            "modality",
            "name",
            "transport",
            "clock_domain",
        ],
        produces_media_fields=["modality", "ts", "width", "height", "frame_rate"],
        output_modalities=["video"],
        expression_hints=_expression_hints(),
        share_strategy="never",
        owner=EXTENSION_ID,
        runtime_factory=lambda config, _deps: CinematicDirectorSourceRuntime(config),
    )
