from __future__ import annotations

import base64
import io
import json
import time
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
    inline_json: Any = None
    encoding = "bytes"
    mime = artifact.mime_type or "application/octet-stream"
    if isinstance(artifact.data, (bytes, bytearray, memoryview)):
        blob = bytes(artifact.data)
    elif hasattr(artifact.data, "shape") and hasattr(artifact.data, "dtype"):
        try:
            blob = _encode_npy(artifact.data)
            encoding = "npy"
            mime = mime or "application/x-toposync-npy"
        except Exception:
            return None
    elif isinstance(artifact.data, (dict, list, str, int, float, bool)):
        inline_json = json.loads(json.dumps(artifact.data))
        encoding = "json"
    else:
        return None

    if encoding != "json" and not blob:
        return None
    out = {
        "mime_type": mime,
        "metadata": _as_dict(artifact.metadata),
        "encoding": encoding,
    }
    if encoding == "json":
        out["inline_json"] = inline_json
    else:
        out["inline_b64"] = base64.b64encode(blob).decode("ascii")
    return out


def _encode_npy(value: Any) -> bytes:
    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("NPY encoding requires numpy") from exc

    buffer = io.BytesIO()
    np.save(buffer, np.asarray(value), allow_pickle=False)
    return buffer.getvalue()


def _decode_inline_artifact_data(rec: dict[str, Any]) -> Any:
    encoding = _as_str(rec.get("encoding")).strip().lower() or "bytes"
    if encoding == "json":
        return rec.get("inline_json")

    blob_b64 = _as_str(rec.get("inline_b64")).strip()
    if not blob_b64:
        return None
    try:
        blob = base64.b64decode(blob_b64)
    except Exception:
        return None

    if encoding == "npy":
        try:
            import numpy as np  # type: ignore
        except Exception:
            return None
        try:
            return np.load(io.BytesIO(blob), allow_pickle=False)
        except Exception:
            return None
    return blob


def _serialize_packet(packet: Packet) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name, art in packet.artifacts.items():
        encoded = _encode_artifact_inline(art)
        if encoded is None:
            continue
        artifacts[name] = encoded

    return {
        "packet_id": packet.packet_id,
        "parent_packet_id": packet.parent_packet_id,
        "stream_id": packet.stream_id,
        "lifecycle": packet.lifecycle.value,
        "created_at": float(packet.created_at),
        "payload": dict(packet.payload),
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
        artifacts[str(name)] = Artifact(
            name=str(name),
            data=_decode_inline_artifact_data(rec),
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
