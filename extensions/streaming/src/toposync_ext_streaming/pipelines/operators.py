from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy
from pydantic import BaseModel, ConfigDict, Field, field_validator

from toposync.runtime.pipelines.execution import SinkRuntime
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME, normalize_artifact_name
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
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

    transmission_id: str
    input_artifact_name: str = ""
    resize_mode: Literal["contain", "none"] = "contain"
    writer_priority: int = 0
    bypass_mode: Literal["auto", "force_on", "force_off"] = "auto"

    @field_validator("transmission_id", mode="after")
    @classmethod
    def _validate_transmission_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("transmission_id is required")
        return normalized

    @field_validator("input_artifact_name", mode="after")
    @classmethod
    def _trim_input_artifact_name(cls, value: str) -> str:
        return str(value or "").strip()


class PublishVideoRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = PublishVideoConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001
        bindings = get_streaming_runtime_bindings()
        if bindings is None:
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


def register_streaming_pipeline_operators(registry: OperatorRegistry) -> None:
    if registry.get("stream.publish_video") is not None:
        return

    registry.register_operator(
        operator_id="stream.publish_video",
        description="Publishes pipeline video frames to a configured transmission output.",
        config_model=PublishVideoConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["streaming", "sink", "realtime"],
        defaults=PublishVideoConfig(transmission_id="stream_default").model_dump(mode="json"),
        share_strategy="never",
        requires_media_fields=["ts", "width", "height"],
        input_modalities=["video"],
        owner=EXTENSION_ID,
        runtime_factory=lambda config, _deps: PublishVideoRuntime(config),
    )


def _build_writer_id(context) -> str:  # noqa: ANN001
    pipeline_name = str(getattr(context, "pipeline_name", "pipeline") or "pipeline").strip()
    node_id = str(getattr(context, "node_id", "stream.publish_video") or "stream.publish_video").strip()
    return f"{pipeline_name}:{node_id}"


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
