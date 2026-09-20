"""Replay finite legacy events through the compiler, scheduler and notification store.

Run this same file in separate processes with PYTHONPATH pointing at each checkout's
src directory. Compare the report's ``observable`` member exactly. This measures
compatibility for a finite fixture; it does not evaluate accuracy or camera load.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from pydantic import BaseModel
import toposync.runtime.pipelines.runtime as packet_module
from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry


class EmptyConfig(BaseModel):
    pass


def packet_value(packet):
    value = asdict(packet)
    # Only the monotonic scheduling origin varies. Capture time and packet IDs
    # are fixed inputs and must compare exactly, including parent lineage.
    value.pop("created_monotonic_ns")
    for artifact in value["artifacts"].values():
        # The base Artifact predates the private flag: all its artifacts were
        # public. Materialize that default, retaining true as a real difference.
        artifact.setdefault("private", False)
        artifact["data"] = hashlib.sha256(artifact["data"]).hexdigest()
    return value


async def replay(root: Path, output: Path, disabled: bool):
    module_path = Path(packet_module.__file__).resolve()
    assert module_path.is_relative_to(root / "src"), module_path
    output.mkdir(parents=True, exist_ok=False)
    notifications = NotificationStore(output / "notifications.sqlite3")
    collected, streamed, records, inputs, forbidden = [], [], [], [], []
    complete = asyncio.Event()

    def forbid(*args, **kwargs):
        forbidden.append("identity work invoked")
        raise AssertionError(forbidden[-1])

    class NoIdentityServices(ServiceRegistry):
        async def call(self, service_id, **kwargs):
            if service_id.startswith(("vision.identity", "models.")):
                forbid()
            return await super().call(service_id, **kwargs)

    class Source(SourceOperatorRuntime):
        def __init__(self):
            self.index = 0

        async def produce(self, context):
            if self.index == 6:
                await context.sleep(0.01)
                return None
            index = self.index
            self.index += 1
            visit, phase = divmod(index, 3)
            packet = Packet(
                packet_id=f"packet-{index}",
                stream_id="camera:legacy",
                lifecycle=(Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[phase],
                created_at=1700000000.0 + index,
                created_monotonic_ns=time.monotonic_ns(),
                parent_packet_id=f"frame-{index}",
                payload={
                    "camera_id": "legacy",
                    "frame_ts": 1700000000.0 + index,
                    "correlation_id": f"visit-{visit}",
                    "subject": {
                        "id": f"event-{visit}",
                        "type": "event",
                        "category": ("person", "dog")[visit],
                        "bbox01": [0.1, 0.1, 0.8, 0.8],
                    },
                    "tracklet_id": f"track-{visit}",
                    "world_position": {"x": float(index), "z": 2.0},
                },
                artifacts={
                    "main": Artifact(
                        name="main",
                        data=f"finite-frame-{index}".encode(),
                        mime_type="application/octet-stream",
                        reference=f"/finite/{index}",
                        metadata={"frame_index": index},
                    )
                },
                metadata={"fixture": "legacy-replay-v1"},
            )
            inputs.append(packet_value(packet))
            return packet

    class Collect(TransformOperatorRuntime):
        def __init__(self, target):
            self.target = target

        async def process_packet(self, packet, context):
            self.target.append(packet_value(packet))
            if len(records) == len(streamed) == 6:
                complete.set()
            return [packet]

    async def upsert(**values):
        record, created = await asyncio.to_thread(notifications.upsert, **values, now=1700000100.0)
        records.append((record, created))
        if len(records) == len(streamed) == 6:
            complete.set()

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    chain = ["source"]
    nodes = [{"id": "source", "operator": "test.legacy_source", "config": {}}]
    if disabled:
        from toposync_ext_vision.identity.pipelines import (
            IdentityEvidenceRuntime,
            RecognizeIdentityRuntime,
            register_identity_operators,
        )

        IdentityEvidenceRuntime._extractor = forbid
        RecognizeIdentityRuntime._resolve = forbid
        register_identity_operators(registry)
        for node, operator in (
            ("context", "vision.identity_context"),
            ("extract", "vision.identity_evidence"),
            ("resolve", "vision.recognize_identity"),
        ):
            chain.append(node)
            nodes.append({"id": node, "operator": operator, "config": {"enabled": False}})
    chain.extend(["collect", "notify"])
    nodes.extend(
        [
            {"id": "collect", "operator": "test.legacy_collect", "config": {}},
            {"id": "notify", "operator": "core.notify", "config": {"update_interval_seconds": 0}},
            {"id": "stream", "operator": "test.legacy_stream", "config": {}},
        ]
    )
    for name, runtime, source in (
        ("source", lambda config, dependencies: Source(), True),
        ("collect", lambda config, dependencies: Collect(collected), False),
        ("stream", lambda config, dependencies: Collect(streamed), False),
    ):
        registry.register_operator(
            operator_id=f"test.legacy_{name}",
            config_model=EmptyConfig,
            inputs=[] if source else [{"name": "in", "required": True}],
            outputs=[{"name": "out"}],
            defaults={},
            share_strategy="never",
            runtime_factory=runtime,
        )
    pairs = list(zip(chain, chain[1:])) + [("source", "stream")]
    graph = build_pipeline_graph_v2(
        graph_uid="legacy_replay",
        nodes=nodes,
        edges=[
            {
                "id": f"edge-{index}",
                "from": {"node": start, "port": "out"},
                "to": {"node": end, "port": "in"},
                "queue": {"max_items": 16, "drop_policy": "block"},
            }
            for index, (start, end) in enumerate(pairs)
        ],
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="legacy_replay", graph=graph)
    )
    runtime = PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=PipelineRuntimeDependencies(
            services=NoIdentityServices(), notifications_upsert=upsert
        ),
    )
    try:
        await runtime.start()
        await asyncio.wait_for(complete.wait(), timeout=10)
    finally:
        await runtime.stop()
        await runtime.dependencies.execution_scheduler.shutdown()
    assert not forbidden, forbidden
    assert collected == streamed == inputs and len(inputs) == 6
    ids = {}
    normalized = []
    for record, created in records:
        value = asdict(record)
        # SQLite generates random notification IDs. Preserve equality/inequality
        # relations and dedupe keys; only rename IDs in first-observed order.
        value["id"] = ids.setdefault(record.id, f"notification-{len(ids)}")
        normalized.append({"record": value, "created": created})
    assert len(ids) == 2 and sum(created for _, created in records) == 2
    assert [row["record"]["payload"]["lifecycle"] for row in normalized] == [
        "open",
        "update",
        "close",
    ] * 2
    report = {
        "core_path": str(module_path),
        "disabled": disabled,
        "forbidden_calls": forbidden,
        "graph": graph,
        "observable": {
            "packets": collected,
            "stream_branch": streamed,
            "notifications": normalized,
        },
    }
    (output / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "packets": len(collected),
                "stream_packets": len(streamed),
                "notifications": len(ids),
                "core_path": str(module_path),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args()
    asyncio.run(replay(args.root.resolve(), args.output.resolve(), args.disabled))
