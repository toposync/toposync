"""Run licensed local photos through the actual scheduler, identity operators and notifications.

This is an integration smoke check, not an accuracy evaluation: there is only
one photo per species, repeated inside one occurrence. No calibrated profile is
installed, so automatic recognition must abstain. No downloads or cameras.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel

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
from toposync_ext_vision.identity.pipelines import register_identity_operators, occurrence_key
from toposync_ext_vision.identity.store import IdentityStore

SOURCES = (
    ("person", "astronaut.png", "88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5"),
    ("cat", "chelsea.png", "596aa1e7cb875eb79f437e310381d26b338a81c2da23439704a73c4651e8c4bb"),
    (
        "dog",
        "sitting-golden-retriever.jpg",
        "7dc7fba30fa7925b4e9962e29a8deab4b513598df53504ac61dd2b41823a2410",
    ),
)


class EmptyConfig(BaseModel):
    pass


async def run(photos: Path, output: Path) -> dict:
    frames = {}
    for species, filename, digest in SOURCES:
        path = photos / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Unexpected test photo: {filename}")
        with Image.open(path) as original:
            prepared = ImageOps.exif_transpose(original).convert("RGB")
            prepared.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            frames[species] = np.asarray(prepared)
    gallery = IdentityStore(output / "gallery", scope="licensed-public-photo-smoke")
    notifications = NotificationStore(output / "notifications.sqlite3")
    records = []
    received = []
    completed = asyncio.Event()

    class PhotoSource(SourceOperatorRuntime):
        def __init__(self):
            self.sequence = 0

        async def produce(self, context):
            if self.sequence >= 9:
                await context.sleep(0.05)
                return None
            if self.sequence:
                await context.sleep(0.35)
            species = SOURCES[self.sequence // 3][0]
            lifecycle = (Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[self.sequence % 3]
            self.sequence += 1
            return Packet.create(
                stream_id="licensed-public-photo",
                lifecycle=lifecycle,
                payload={
                    "camera_id": "offline-smoke",
                    "frame_ts": time.time(),
                    "correlation_id": f"smoke-{species}",
                    "subject": {
                        "id": f"individual-{species}",
                        "type": "event",
                        "category": species,
                        "bbox01": [0, 0, 1, 1],
                    },
                    "tracklet_id": f"track-{species}",
                    "world_position": {"x": 1.0, "z": 2.0},
                },
                artifacts={
                    "main": Artifact(
                        name="main",
                        data=frames[species],
                        mime_type="image/raw",
                        metadata={"color_order": "rgb"},
                    )
                },
            )

    class Collect(TransformOperatorRuntime):
        async def process_packet(self, packet, context):
            received.append(packet)
            return [packet]

    async def upsert(**values):
        record, created = notifications.upsert(**values)
        records.append((record, created))
        if len(records) == 9:
            completed.set()

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_identity_operators(registry)
    registry.register_operator(
        operator_id="test.photo_source",
        config_model=EmptyConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda config, dependencies: PhotoSource(),
    )
    registry.register_operator(
        operator_id="test.collect",
        config_model=EmptyConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda config, dependencies: Collect(),
    )
    graph = build_pipeline_graph_v2(
        graph_uid="real_identity_smoke",
        nodes=[
            {"id": "source", "operator": "test.photo_source", "config": {}},
            {
                "id": "extract",
                "operator": "vision.identity_evidence",
                "config": {"enabled": True, "sample_interval_seconds": 0.25},
            },
            {"id": "resolve", "operator": "vision.recognize_identity", "config": {"enabled": True}},
            {"id": "collect", "operator": "test.collect", "config": {}},
            {"id": "notify", "operator": "core.notify", "config": {"update_interval_seconds": 0}},
        ],
        edges=[
            {
                "id": f"edge_{index}",
                "from": {"node": source, "port": "out"},
                "to": {"node": target, "port": "in"},
                "queue": {"max_items": 16, "drop_policy": "block"},
            }
            for index, (source, target) in enumerate(
                zip(
                    ("source", "extract", "resolve", "collect"),
                    ("extract", "resolve", "collect", "notify"),
                )
            )
        ],
    )
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="real_identity_smoke", graph=graph)
    )
    services = ServiceRegistry()
    services.register("vision.identity.store", lambda: gallery)
    runtime = PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=PipelineRuntimeDependencies(services=services, notifications_upsert=upsert),
    )
    started = time.perf_counter()
    try:
        await runtime.start()
        await asyncio.wait_for(completed.wait(), timeout=30)
    finally:
        await runtime.stop()
        await runtime.dependencies.execution_scheduler.shutdown()
    diagnostics = [
        {
            "species": packet.payload["subject"]["category"],
            "lifecycle": packet.lifecycle.value,
            "recognition": packet.payload.get("recognition"),
            "frame_ts": packet.payload.get("frame_ts"),
        }
        for packet in received
    ]
    (output / "packet-diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    assert len(received) == 9
    assert len({record.id for record, _ in records}) == 3
    assert sum(created for _, created in records) == 3
    report = []
    for species, filename, digest in SOURCES:
        packets = [
            packet for packet in received if packet.payload["subject"]["category"] == species
        ]
        assert [packet.lifecycle for packet in packets] == [
            Lifecycle.OPEN,
            Lifecycle.UPDATE,
            Lifecycle.CLOSE,
        ]
        for packet in packets:
            assert packet.payload["subject"]["id"] == f"individual-{species}"
            assert packet.payload["world_position"] == {"x": 1.0, "z": 2.0}
            assert packet.payload["tracklet_id"] == f"track-{species}"
            assert packet.payload["recognition"]["reason"] == "calibration_required"
            assert "identity_evidence" not in packet.artifacts
            assert packet.artifacts["identity_decision"].private
        detail = gallery.occurrence_details(occurrence_key(packets[0]))
        assert detail["closed"] and len(detail["observations"]) == 2
        assert all(
            item["eligible"] and not item["reference"] and gallery.image(item["id"])
            for item in detail["observations"]
        )
        report.append(
            {
                "species": species,
                "photo": filename,
                "sha256": digest,
                "observations": 2,
                "lifecycle": ["open", "update", "close"],
                "status": detail["decision"]["status"],
                "reason": detail["decision"]["reason"],
            }
        )
    for record, _ in records:
        serialized = json.dumps(record.payload)
        assert (
            "embedding_space" not in serialized
            and '"vector"' not in serialized
            and "identity_decision" not in serialized
        )
    gallery.close()
    reopened = IdentityStore(output / "gallery", scope="licensed-public-photo-smoke")
    assert len(reopened.observations()) == 6
    reopened.close()
    return {
        "purpose": __doc__,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "notifications": 3,
        "observations_after_restart": 6,
        "results": report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos-dir", required=True, type=Path)
    parser.add_argument("--model-data-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    # Never reuse a database or remove previous evidence on a retry.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    os.environ["TOPOSYNC_DATA_DIR"] = str(args.model_data_dir.resolve())
    report = asyncio.run(run(args.photos_dir, args.output_dir))
    (args.output_dir / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
