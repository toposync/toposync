from __future__ import annotations

import base64
import struct
import time
import zlib
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import PipelineRuntimeDependencies, SinkRuntime, SourceOperatorRuntime, TransformOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Artifact, Lifecycle, Packet


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _ensure_original_artifact(packet: Packet) -> Packet:
    artifacts = dict(packet.artifacts)
    payload = packet.payload
    changed = False

    payload_frame = packet.payload.get("frame")
    if payload_frame is not None:
        payload2 = dict(packet.payload)
        payload2.pop("frame", None)
        payload = payload2
        changed = True

        if "frame_original" not in artifacts:
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload"},
            )
            changed = True
        if "frame" not in artifacts:
            artifacts["frame"] = Artifact(
                name="frame",
                data=payload_frame,
                mime_type="image/raw",
                metadata={"source": "frame_contract.migrated_payload", "derived_from": "frame_original"},
            )
            changed = True

    if "frame_original" not in artifacts:
        stream_frame = artifacts.get("frame")
        if stream_frame is not None and (stream_frame.data is not None or stream_frame.reference):
            artifacts["frame_original"] = Artifact(
                name="frame_original",
                data=stream_frame.data,
                reference=stream_frame.reference,
                mime_type=stream_frame.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame"},
            )
            changed = True

    if "frame" not in artifacts:
        original = artifacts.get("frame_original")
        if original is not None and (original.data is not None or original.reference):
            artifacts["frame"] = Artifact(
                name="frame",
                data=original.data,
                reference=original.reference,
                mime_type=original.mime_type,
                metadata={"source": "frame_contract.aliased_from_frame_original", "derived_from": "frame_original"},
            )
            changed = True

    if not changed:
        return packet
    return Packet(
        packet_id=packet.packet_id,
        parent_packet_id=packet.parent_packet_id,
        stream_id=packet.stream_id,
        lifecycle=packet.lifecycle,
        created_at=packet.created_at,
        created_monotonic_ns=packet.created_monotonic_ns,
        payload=dict(payload),
        artifacts=artifacts,
        metadata=dict(packet.metadata),
    )


def _encode_artifact_inline(artifact: Artifact) -> dict[str, Any] | None:
    if artifact.data is None:
        if artifact.reference:
            return {
                "reference": str(artifact.reference),
                "mime_type": artifact.mime_type,
                "metadata": _as_dict(artifact.metadata),
            }
        return None

    blob: bytes | None = None
    mime = artifact.mime_type or "application/octet-stream"
    if isinstance(artifact.data, (bytes, bytearray, memoryview)):
        blob = bytes(artifact.data)
    elif hasattr(artifact.data, "shape") and hasattr(artifact.data, "dtype"):
        try:
            blob = _encode_png(artifact.data)
            mime = "image/png"
        except Exception:
            return None
    else:
        return None

    if not blob:
        return None
    return {
        "inline_b64": base64.b64encode(blob).decode("ascii"),
        "mime_type": mime,
        "metadata": _as_dict(artifact.metadata),
    }


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return struct.pack("!I", len(data)) + chunk + struct.pack("!I", crc)


def _encode_png(image: Any) -> bytes:
    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PNG encoding requires numpy") from exc

    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8, copy=False)
    if arr.ndim == 2:
        height, width = int(arr.shape[0]), int(arr.shape[1])
        color_type = 0
    elif arr.ndim == 3 and int(arr.shape[2]) in {3, 4}:
        height, width = int(arr.shape[0]), int(arr.shape[1])
        channels = int(arr.shape[2])
        color_type = 2 if channels == 3 else 6
    else:
        raise ValueError("Unsupported image shape for PNG encoding")

    if height < 1 or width < 1:
        raise ValueError("Invalid image dimensions")

    arr = np.ascontiguousarray(arr)
    raw = bytearray()
    if arr.ndim == 2:
        for y in range(height):
            raw.append(0)
            raw.extend(arr[y].tobytes())
    else:
        for y in range(height):
            raw.append(0)
            raw.extend(arr[y].reshape(-1).tobytes())

    compressed = zlib.compress(bytes(raw), level=6)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return header + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


def _serialize_packet(packet: Packet) -> dict[str, Any]:
    packet = _ensure_original_artifact(packet)
    artifacts: dict[str, Any] = {}
    for name, art in packet.artifacts.items():
        encoded = _encode_artifact_inline(art)
        if encoded is None:
            continue
        artifacts[name] = encoded

    payload = dict(packet.payload)
    payload.pop("frame", None)
    return {
        "packet_id": packet.packet_id,
        "parent_packet_id": packet.parent_packet_id,
        "stream_id": packet.stream_id,
        "lifecycle": packet.lifecycle.value,
        "created_at": float(packet.created_at),
        "payload": payload,
        "metadata": dict(packet.metadata),
        "artifacts": artifacts,
    }


def _deserialize_packet(data: dict[str, Any]) -> Packet:
    lifecycle = Lifecycle.UPDATE
    raw_lifecycle = _as_str(data.get("lifecycle")).strip().lower()
    if raw_lifecycle == "open":
        lifecycle = Lifecycle.OPEN
    elif raw_lifecycle == "close":
        lifecycle = Lifecycle.CLOSE

    artifacts: dict[str, Artifact] = {}
    raw_artifacts = _as_dict(data.get("artifacts"))
    for name, value in raw_artifacts.items():
        rec = _as_dict(value)
        reference = _as_str(rec.get("reference")).strip() or None
        mime = _as_str(rec.get("mime_type")).strip() or None
        meta = _as_dict(rec.get("metadata"))
        blob_b64 = _as_str(rec.get("inline_b64")).strip()
        blob = None
        if blob_b64:
            try:
                blob = base64.b64decode(blob_b64)
            except Exception:
                blob = None
        artifacts[str(name)] = Artifact(
            name=str(name),
            data=blob,
            reference=reference,
            mime_type=mime,
            metadata=meta,
        )

    packet_id = _as_str(data.get("packet_id")).strip() or None
    parent_packet_id = _as_str(data.get("parent_packet_id")).strip() or None
    stream_id = _as_str(data.get("stream_id")).strip() or "stream:unknown"
    payload = _as_dict(data.get("payload"))
    metadata = _as_dict(data.get("metadata"))
    created_at = _as_float(data.get("created_at"), time.time())
    return Packet(
        packet_id=packet_id or Packet.create(stream_id=stream_id).packet_id,
        parent_packet_id=parent_packet_id or None,
        stream_id=stream_id,
        lifecycle=lifecycle,
        created_at=float(created_at),
        created_monotonic_ns=time.monotonic_ns(),
        payload=dict(payload),
        artifacts=artifacts,
        metadata=dict(metadata),
    )


class RemoteSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_timeout_s: float = Field(default=0.2, ge=0.01, le=5.0)


class TargetFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_node_id: str
    target_port: str = "in"

    @field_validator("target_node_id", "target_port")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class ProjectToOriginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline_name: str
    target_node_id: str
    target_port: str = "in"

    @field_validator("pipeline_name", "target_node_id", "target_port")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

class RemoteSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = RemoteSourceConfig.model_validate(config)
        self._dependencies = dependencies

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        inbox = getattr(self._dependencies, "origin_inbox", None)
        if inbox is None:
            raise RuntimeError("dist.remote_source requires PipelineRuntimeDependencies.origin_inbox")
        timeout_s = float(self._config.poll_timeout_s)
        result = await inbox.get(timeout_s=timeout_s, cancel_event=context.cancel_event)
        if not result.accepted or result.item is None:
            return None
        event = result.item
        packet = _deserialize_packet(_as_dict(event.get("packet")))
        target = {
            "node_id": _as_str(event.get("target_node_id")).strip(),
            "port": _as_str(event.get("target_port")).strip() or "in",
        }
        metadata = dict(packet.metadata)
        metadata["dist_target"] = target
        return Packet(
            packet_id=packet.packet_id,
            parent_packet_id=packet.parent_packet_id,
            stream_id=packet.stream_id,
            lifecycle=packet.lifecycle,
            created_at=packet.created_at,
            created_monotonic_ns=packet.created_monotonic_ns,
            payload=dict(packet.payload),
            artifacts=dict(packet.artifacts),
            metadata=metadata,
        )


class TargetFilterRuntime(TransformOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = TargetFilterConfig.model_validate(config)

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        target = packet.metadata.get("dist_target")
        rec = _as_dict(target)
        node_id = _as_str(rec.get("node_id")).strip()
        port = _as_str(rec.get("port")).strip() or "in"
        if node_id != self._config.target_node_id:
            return []
        if port != self._config.target_port:
            return []
        return [packet]


class ProjectToOriginRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], dependencies: PipelineRuntimeDependencies) -> None:
        self._config = ProjectToOriginConfig.model_validate(config)
        self._dependencies = dependencies

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = getattr(
            self._dependencies,
            "processing_emit_projected_event",
            None,
        )
        if not callable(emit):
            raise RuntimeError(
                "dist.project_to_origin requires PipelineRuntimeDependencies.processing_emit_projected_event",
            )
        event = {
            "pipeline_name": self._config.pipeline_name,
            "target_node_id": self._config.target_node_id,
            "target_port": self._config.target_port,
            "packet": _serialize_packet(packet),
        }
        await emit(event)
        return []


def register_distributed_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="dist.remote_source",
        description="Source operator that reads projected packets from the processing transport inbox.",
        config_model=RemoteSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        capabilities=["distributed", "origin_only", "source"],
        defaults=RemoteSourceConfig().model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: RemoteSourceRuntime(config, deps),
    )
    registry.register_operator(
        operator_id="dist.target_filter",
        description="Routes projected packets to the intended origin node using metadata.dist_target.",
        config_model=TargetFilterConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["distributed", "origin_only"],
        defaults=TargetFilterConfig(target_node_id="node").model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, _deps: TargetFilterRuntime(config),
    )
    registry.register_operator(
        operator_id="dist.project_to_origin",
        description="Projects packets from processing runtime to origin runtime (transport boundary).",
        config_model=ProjectToOriginConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        capabilities=["distributed", "processing_only", "sink"],
        defaults=ProjectToOriginConfig(pipeline_name="pipeline", target_node_id="node").model_dump(),
        share_strategy="never",
        owner="core",
        runtime_factory=lambda config, deps: ProjectToOriginRuntime(config, deps),
    )
