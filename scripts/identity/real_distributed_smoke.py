"""Validate real extraction over authenticated HTTP with a separate processing server.

Uses one licensed public photo per species, repeated within one occurrence.
This verifies integration, replay and persistence, not biometric accuracy.
No downloads, cameras, calibrated profile, or existing gallery mutations.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import numpy as np
from PIL import Image, ImageOps

from real_pipeline_smoke import EmptyConfig, SOURCES
from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    Packet,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
)
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport
from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync.runtime.pipelines.operators_distributed import _deserialize_packet
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.identity.pipelines import RecognizeIdentityRuntime, occurrence_key
from toposync_ext_vision.identity.store import IdentityStore


class PhotoSource(SourceOperatorRuntime):
    def __init__(self, photos: Path):
        self.sequence = 0
        self.frames = {}
        for species, filename, digest in SOURCES:
            path = photos / filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"Unexpected test photo: {filename}")
            with Image.open(path) as original:
                prepared = ImageOps.exif_transpose(original).convert("RGB")
                prepared.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                self.frames[species] = np.asarray(prepared)

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
                "camera_id": "offline-distributed-smoke",
                "frame_ts": time.time(),
                "correlation_id": f"distributed-smoke-{species}",
                "subject": {
                    "id": f"individual-{species}",
                    "type": "event",
                    "category": species,
                    "bbox01": [0, 0, 1, 1],
                },
                "tracklet_id": f"track-{species}",
                "world_position": {"x": 1.0, "z": 2.0},
                "processing_pid": os.getpid(),
            },
            artifacts={
                "main": Artifact(
                    name="main",
                    data=self.frames[species],
                    mime_type="image/raw",
                    metadata={"color_order": "rgb"},
                )
            },
        )


def serve(photos: Path, port: int):
    import uvicorn

    from toposync.processing_server import create_app

    application = create_app()
    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            app.state.pipeline_operator_registry.register_operator(
                operator_id="test.remote_photo_source",
                config_model=EmptyConfig,
                inputs=[],
                outputs=[{"name": "out"}],
                capabilities=["source"],
                defaults={},
                share_strategy="never",
                owner="test",
                runtime_factory=lambda _config, _dependencies: PhotoSource(photos),
            )
            yield

    application.router.lifespan_context = lifespan
    uvicorn.run(application, host="127.0.0.1", port=port, log_level="warning", access_log=False)


async def run(base_url: str, processing_pid: int, output: Path):
    transport = HttpProcessingTransport(
        base_url=base_url,
        username="isolated-test",
        password="disposable-real-model-smoke",
    )
    gallery = IdentityStore(output / "gallery", scope="licensed-public-remote-smoke")
    notifications = NotificationStore(output / "notifications.sqlite3")
    scheduler = ExecutionScheduler()
    records = []
    resolved = []
    events = []

    class Context:
        pipeline_name = "real_distributed_identity_smoke"
        node_id = "identity"

        async def run_blocking(self, function, *args, **kwargs):
            return await scheduler.run_sync(function, *args, mode="thread_pool", **kwargs)

    async def upsert(**values):
        records.append(notifications.upsert(**values))

    services = ServiceRegistry()
    services.register("vision.identity.store", lambda: gallery)
    dependencies = PipelineRuntimeDependencies(services=services, notifications_upsert=upsert)
    resolver = RecognizeIdentityRuntime({"enabled": True}, dependencies)
    sink = NotifyRuntime({"update_interval_seconds": 0, "dedupe_by_occurrence": True}, dependencies)
    graph = build_pipeline_graph_v2(
        graph_uid="real_distributed_identity_smoke",
        nodes=[
            {"id": "source", "operator": "test.remote_photo_source", "config": {}},
            {
                "id": "extract",
                "operator": "vision.identity_evidence",
                "config": {
                    "enabled": True,
                    "sample_interval_seconds": 0.25,
                },
            },
            {"id": "resolve", "operator": "vision.recognize_identity", "config": {"enabled": True}},
            {
                "id": "notify",
                "operator": "core.notify",
                "config": {"update_interval_seconds": 0, "dedupe_by_occurrence": True},
            },
        ],
        edges=[
            {
                "from": {"node": source, "port": "out"},
                "to": {"node": target, "port": "in"},
                "queue": {"max_items": 16, "drop_policy": "block"},
            }
            for source, target in (
                ("source", "extract"),
                ("extract", "resolve"),
                ("resolve", "notify"),
            )
        ],
    )
    started = time.perf_counter()
    try:
        await transport.push_config(
            {
                "required_capabilities": ["private_artifacts_v1"],
                "pipelines": [
                    Pipeline(name="real_distributed_identity_smoke", graph=graph).model_dump(
                        mode="json"
                    )
                ],
            }
        )
        stream = transport.stream_events()
        try:
            async with asyncio.timeout(45):
                async for event in stream:
                    if "packet" not in event:
                        continue
                    events.append(event)
                    packet = _deserialize_packet(event["packet"])
                    result = (await resolver.process_packet(packet, Context()))[0]
                    resolved.append(result)
                    await sink.process_packet(result, Context())
                    if len(events) == 9:
                        break
        finally:
            await stream.aclose()
            diagnostics = [
                {
                    "species": packet.payload["subject"]["category"],
                    "lifecycle": packet.lifecycle.value,
                    "recognition": packet.payload.get("recognition"),
                    "frame_ts": packet.payload["frame_ts"],
                    "processing_pid": packet.payload["processing_pid"],
                    "serialized_event_bytes": len(json.dumps(event).encode()),
                    "frame_bytes": packet.artifacts["main"].data.nbytes,
                }
                for event, packet in zip(events, resolved)
            ]
            (output / "packet-diagnostics.json").write_text(
                json.dumps(diagnostics, indent=2) + "\n"
            )
        assert len(events) == 9
        assert processing_pid != os.getpid()
        assert all(packet.payload["processing_pid"] == processing_pid for packet in resolved)
        assert len({record.id for record, _created in records}) == 3
        assert sum(created for _record, created in records) == 3
        results = []
        for species, filename, digest in SOURCES:
            packets = [
                packet for packet in resolved if packet.payload["subject"]["category"] == species
            ]
            assert [packet.lifecycle for packet in packets] == [
                Lifecycle.OPEN,
                Lifecycle.UPDATE,
                Lifecycle.CLOSE,
            ]
            for packet in packets:
                assert packet.payload["subject"]["id"] == f"individual-{species}"
                assert packet.payload["tracklet_id"] == f"track-{species}"
                assert packet.payload["world_position"] == {"x": 1.0, "z": 2.0}
                assert packet.payload["recognition"]["reason"] == "calibration_required"
                assert "identity_evidence" not in packet.artifacts
                assert packet.artifacts["identity_decision"].private
            detail = gallery.occurrence_details(occurrence_key(packets[0]))
            assert detail["closed"] and len(detail["observations"]) == 2
            assert all(
                item["eligible"] and not item["reference"] and gallery.image(item["id"])
                for item in detail["observations"]
            )
            results.append(
                {"species": species, "photo": filename, "sha256": digest, "observations": 2}
            )
        assert len(gallery.observations()) == 6
        notification_ids = {record.id for record, _created in records}
        replay = transport.stream_events(last_event_id=events[0]["event_id"])
        replayed_ids = []
        try:
            async with asyncio.timeout(15):
                async for event in replay:
                    if "packet" not in event:
                        continue
                    packet = _deserialize_packet(event["packet"])
                    result = (await resolver.process_packet(packet, Context()))[0]
                    await sink.process_packet(result, Context())
                    replayed_ids.append(event["event_id"])
                    if len(replayed_ids) == 8:
                        break
        finally:
            await replay.aclose()
        assert replayed_ids == [event["event_id"] for event in events[1:]]
        assert len(gallery.observations()) == 6
        assert {record.id for record, _created in records} == notification_ids
        assert sum(created for _record, created in records) == 3
        for record, _created in records:
            serialized = json.dumps(record.payload)
            assert all(
                token not in serialized
                for token in (
                    '"vector"',
                    '"embedding_space"',
                    '"identity_decision"',
                    '"identity_evidence"',
                )
            )
        assert all(
            record.payload["status"] == "closed" for record in notifications.list(limit=10)[0]
        )
        await transport.ack(events[-1]["event_id"])
        gallery.close()
        reopened = IdentityStore(output / "gallery", scope="licensed-public-remote-smoke")
        try:
            assert len(reopened.observations()) == 6
            assert all(
                reopened.occurrence_details(occurrence_key(packet))["closed"] for packet in resolved
            )
        finally:
            reopened.close()
        return {
            "purpose": __doc__,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "origin_pid": os.getpid(),
            "processing_pid": processing_pid,
            "frame_preprocessing": "EXIF orientation, RGB, thumbnail maximum 1024x1024, Lanczos",
            "notifications": 3,
            "observations_after_restart": 6,
            "replayed_events": len(replayed_ids),
            "results": results,
        }
    finally:
        try:
            await transport.push_config({"pipelines": []})
        finally:
            await transport.close()
            await scheduler.shutdown()
            gallery.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos-dir", type=Path, required=True)
    parser.add_argument("--model-data-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--serve", type=int)
    args = parser.parse_args()
    if args.serve is not None:
        serve(args.photos_dir, args.serve)
        return
    if args.output_dir is None or args.model_data_dir is None:
        parser.error("--output-dir and --model-data-dir are required for the client")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    processing_data = output / "processing"
    processing_data.mkdir()
    (processing_data / "vision-models").symlink_to(
        args.model_data_dir.resolve() / "vision-models", target_is_directory=True
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "TOPOSYNC_DATA_DIR": str(processing_data),
        "TOPOSYNC_PROCESSING_USERNAME": "isolated-test",
        "TOPOSYNC_PROCESSING_PASSWORD": "disposable-real-model-smoke",
        "TOPOSYNC_PROCESSING_STATUS_DIAGNOSTICS_TIMEOUT": "0.1",
        "TOPOSYNC_EXTENSION_AUTO_INSTALL_ON_STARTUP": "0",
    }
    with (output / "processing.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                str(port),
                "--photos-dir",
                str(args.photos_dir.resolve()),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Processing process exited; inspect processing.log")
                    try:
                        if client.get(base_url + "/api/processing/status").status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise TimeoutError("Processing server did not become ready")
                assert client.get(base_url + "/api/processing/events/stream").status_code == 401
            result = asyncio.run(run(base_url, process.pid, output))
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    main()
