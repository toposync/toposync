"""Binary, fair interleaving; capture clocks and per-stream CLOSE remain independent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

from pydantic import BaseModel, ConfigDict

from .execution import BaseOperatorRuntime
from .operator_registry import OperatorRegistry
from .runtime import Packet


class MergeStreamsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _qualified_packet(packet: Packet, port: str) -> Packet:
    previous = packet.metadata.get("merge_origin")
    if isinstance(previous, dict) and all(
        isinstance(value, (str, int, type(None))) for value in previous.values()
    ):
        namespace = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
        original_id = packet.metadata.get("input_packet_id")
        if (
            packet.stream_id == f"merged:{namespace}"
            and packet.packet_id
            == hashlib.sha256(f"{namespace}:{original_id}".encode()).hexdigest()
        ):
            return packet
    source = packet.payload.get("source")
    source = source if isinstance(source, dict) else {}
    capture = packet.payload.get("capture_evidence")
    capture = capture if isinstance(capture, dict) else {}
    origin = {
        "device_id": source.get("device_id"),
        "source_id": source.get("source_id"),
        "capture_instance": capture.get("capture_instance"),
        "generation": capture.get("generation"),
        "stream_id": packet.stream_id,
    }
    origin = {
        key: value if isinstance(value, (str, int, type(None))) else None
        for key, value in origin.items()
    }
    if not any(origin[key] is not None for key in ("device_id", "source_id", "capture_instance")):
        origin["input_port"] = port
    namespace = hashlib.sha256(json.dumps(origin, sort_keys=True).encode()).hexdigest()
    return replace(
        packet,
        stream_id=f"merged:{namespace}",
        packet_id=hashlib.sha256(f"{namespace}:{packet.packet_id}".encode()).hexdigest(),
        metadata={**packet.metadata, "merge_origin": origin, "input_packet_id": packet.packet_id},
    )


class MergeStreamsRuntime(BaseOperatorRuntime):
    async def run(self, context) -> None:
        pending = {asyncio.create_task(context.read(port=port)): port for port in ("left", "right")}
        try:
            while not context.cancel_event.is_set():
                ready, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in ready:
                    port = pending.pop(task)
                    packet = task.result()
                    if packet is not None:
                        await context.emit(
                            _qualified_packet(
                                packet, f"{context.pipeline_name}/{context.node_id}/{port}"
                            )
                        )
                    if not context.cancel_event.is_set():
                        pending[asyncio.create_task(context.read(port=port))] = port
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def register_merge_operators(registry: OperatorRegistry) -> None:
    registry.register_operator(
        operator_id="core.merge_streams",
        description="Interleave two independent streams without synchronizing their clocks.",
        config_model=MergeStreamsConfig,
        inputs=[{"name": "left", "required": True}, {"name": "right", "required": True}],
        outputs=[{"name": "out"}],
        capabilities=["core", "merge", "lifecycle", "split_stream"],
        state_kind="stateless",
        owner="core",
        runtime_factory=lambda _config, _dependencies: MergeStreamsRuntime(),
    )
