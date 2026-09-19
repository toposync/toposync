"""Equivalent offline event load through the real scheduler and SQLite notifications.

Compares source -> notification against the same source with identity operators.
It uses licensed photos only for workload, not recognition accuracy. No actual
camera/detector/tracker is exercised; this is not the final installation profile.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
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
from toposync_ext_vision.identity.pipelines import register_identity_operators
from toposync_ext_vision.identity.store import IdentityStore
from real_pipeline_smoke import SOURCES


class EmptyConfig(BaseModel):
    pass


def worker_resources():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "pid": os.getpid(),
        "cpu_seconds_including_startup": usage.ru_utime + usage.ru_stime,
        "peak_rss_native_units": usage.ru_maxrss,
    }


async def trial(
    photos: Path,
    output: Path,
    *,
    enabled: bool,
    visits: int,
    interval: float,
    parallel: bool = False,
) -> dict:
    frames = {}
    for species, filename, digest in SOURCES:
        path = photos / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            frames[species] = np.asarray(image)
    gallery = IdentityStore(output / "gallery", scope="offline-load-fixture")
    notifications = NotificationStore(output / "notifications.sqlite3")
    sent, latencies, identities, lifecycles = {}, [], set(), []
    planned, emission_lateness = {}, []
    created_count = 0
    completed, warmed, recognition_completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recognition_lifecycles = []

    def check_warmup():
        if len(lifecycles) >= 9 and (not parallel or len(recognition_lifecycles) >= 9):
            warmed.set()

    warm_packets = 9
    total_packets = warm_packets + visits * 3

    class Source(SourceOperatorRuntime):
        def __init__(self):
            self.sequence = 0
            self.deadline = None

        async def produce(self, context):
            if self.sequence >= total_packets:
                await context.sleep(0.01)
                return None
            if self.sequence == warm_packets:
                await warmed.wait()
                self.deadline = None
            now = time.perf_counter()
            self.deadline = now if self.deadline is None else self.deadline + interval
            await context.sleep(max(0, self.deadline - now))
            sequence = self.sequence
            self.sequence += 1
            visit = sequence // 3
            species = SOURCES[visit % 3][0]
            phase = (Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[sequence % 3]
            packet_key = (f"load-{visit}", phase.value)
            planned[packet_key] = self.deadline
            sent[packet_key] = time.perf_counter()
            if visit >= 3:
                emission_lateness.append((sent[packet_key] - self.deadline) * 1000)
            return Packet.create(
                stream_id="licensed-photo-load",
                lifecycle=phase,
                payload={
                    "camera_id": "offline-load",
                    "frame_ts": time.time(),
                    "source_stream_id": "offline-load",
                    "correlation_id": f"load-{visit}",
                    "subject": {
                        "id": f"load-{visit}",
                        "type": "event",
                        "category": species,
                        "bbox01": [0, 0, 1, 1],
                    },
                    "world_position": {"x": 1.0, "z": 2.0},
                },
                artifacts={
                    "frame": Artifact(
                        name="frame", data=frames[species], metadata={"color_order": "rgb"}
                    )
                },
            )

    async def upsert(**values):
        nonlocal created_count
        service_started = time.perf_counter()
        record, created = notifications.upsert(**values)
        stored_at = time.perf_counter()
        payload = values["payload"]
        key = (payload["subject"]["id"], payload["lifecycle"])
        visit = int(key[0].split("-")[1])
        identities.add(record.id)
        created_count += int(created)
        lifecycles.append(key)
        if visit >= 3:
            latencies.append(
                {
                    "phase": key[1],
                    "species": payload["subject"]["category"],
                    "source_to_upsert_ms": (service_started - sent[key]) * 1000,
                    "notification_storage_ms": (stored_at - service_started) * 1000,
                    "milliseconds": (time.perf_counter() - sent[key]) * 1000,
                    "planned_milliseconds": (time.perf_counter() - planned[key]) * 1000,
                }
            )
        check_warmup()
        if len(lifecycles) == total_packets:
            completed.set()

    class RecognitionCollector(TransformOperatorRuntime):
        async def process_packet(self, value, context):
            recognition_lifecycles.append((value.payload["subject"]["id"], value.lifecycle.value))
            check_warmup()
            if len(recognition_lifecycles) == total_packets:
                recognition_completed.set()
            return []

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_identity_operators(registry)
    registry.register_operator(
        operator_id="test.load_source",
        config_model=EmptyConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda config, dependencies: Source(),
    )
    nodes = [{"id": "source", "operator": "test.load_source", "config": {}}]
    if enabled:
        nodes.extend(
            [
                {
                    "id": "extract",
                    "operator": "vision.identity_evidence",
                    "config": {
                        "enabled": True,
                        "sample_interval_seconds": 0.25,
                        "input_artifact_name": "frame",
                    },
                },
                {
                    "id": "resolve",
                    "operator": "vision.recognize_identity",
                    "config": {"enabled": True},
                },
            ]
        )
    nodes.append(
        {"id": "notify", "operator": "core.notify", "config": {"update_interval_seconds": 0}}
    )
    edges = [
        {
            "id": f"edge_{index}",
            "from": {"node": left["id"], "port": "out"},
            "to": {"node": right["id"], "port": "in"},
            "queue": {"max_items": 16, "drop_policy": "block"},
        }
        for index, (left, right) in enumerate(zip(nodes, nodes[1:]))
    ]
    if parallel:
        nodes.append(
            {"id": "context", "operator": "vision.identity_context", "config": {"enabled": True}}
        )
        registry.register_operator(
            operator_id="test.recognition_collector",
            config_model=EmptyConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            defaults={},
            share_strategy="never",
            runtime_factory=lambda config, dependencies: RecognitionCollector(),
        )
        nodes.append({"id": "collector", "operator": "test.recognition_collector", "config": {}})
        pairs = [
            ("source", "context"),
            ("context", "notify"),
            ("context", "extract"),
            ("extract", "resolve"),
            ("resolve", "collector"),
        ]
        edges = [
            {
                "id": f"edge_{index}",
                "from": {"node": left, "port": "out"},
                "to": {"node": right, "port": "in"},
                "queue": {"max_items": 16, "drop_policy": "block"},
            }
            for index, (left, right) in enumerate(pairs)
        ]
    graph = build_pipeline_graph_v2(graph_uid="identity_load_fixture", nodes=nodes, edges=edges)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="identity_load_fixture", graph=graph)
    )
    services = ServiceRegistry()
    services.register("vision.identity.store", lambda: gallery)
    runtime = PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=PipelineRuntimeDependencies(services=services, notifications_upsert=upsert),
    )
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    extraction_worker = None
    try:
        await runtime.start()
        await asyncio.wait_for(completed.wait(), timeout=max(30, total_packets * interval * 3))
        notifications_finished = time.perf_counter()
        if parallel:
            await asyncio.wait_for(recognition_completed.wait(), timeout=30)
        drain_seconds = time.perf_counter() - notifications_finished
        snapshot = runtime.snapshot()
        if enabled:
            closed_count = gallery._connection.execute(
                "SELECT count(*) FROM occurrence WHERE closed=1"
            ).fetchone()[0]
            assert closed_count == visits + 3
        if parallel:
            assert recognition_lifecycles == lifecycles
        if enabled:
            extraction_worker = await runtime.dependencies.execution_scheduler.run_sync(
                worker_resources, mode="process_pool", process_pool_key="vision.identity_evidence",
                concurrency_key="vision.identity_evidence", max_concurrency=1,
            )

    finally:
        await runtime.stop()
        await runtime.dependencies.execution_scheduler.shutdown()
        evidence_counts = {
            row[0]: row[1]
            for row in gallery._connection.execute(
                "SELECT species,count(*) FROM observation WHERE eligible=1 GROUP BY species"
            )
        }
        gallery.close()
    if enabled:
        assert set(evidence_counts) == {"person", "cat", "dog"}
        assert sum(evidence_counts.values()) >= visits, (
            "benchmark must actually execute usable inference"
        )
    assert len(lifecycles) == total_packets and len(latencies) == visits * 3
    assert created_count == visits + 3 and len(identities) == visits + 3
    assert all(
        [phase for name, phase in lifecycles if name == f"load-{index}"]
        == ["open", "update", "close"]
        for index in range(visits + 3)
    )
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    values = [row["milliseconds"] for row in latencies]
    return {
        "identity_enabled": enabled,
        "topology": "parallel_experiment" if parallel else "sequential",
        "parallel_experiment_limit": "Existing fanout shares backpressure and memory; bounded workload only"
        if parallel
        else None,
        "queue_metrics": snapshot["channels"],
        "usable_inference_observations_by_species": evidence_counts,
        "measured_visits": visits,
        "measured_packets": len(latencies),
        "interval_seconds": interval,
        "warmup_visits_excluded": 3,
        "emission_lateness_p95_ms": float(np.percentile(emission_lateness, 95)),
        "planned_to_notification_p95_ms": float(
            np.percentile([row["planned_milliseconds"] for row in latencies], 95)
        ),
        "recognition_drain_seconds": drain_seconds,
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "max_ms": max(values),
        "timing_by_species": {
            species: {
                "frame_bytes": int(frames[species].nbytes),
                "source_to_notification_p95_ms": float(np.percentile(
                    [row["milliseconds"] for row in latencies if row["species"] == species], 95)),
                "source_to_upsert_p95_ms": float(np.percentile(
                    [row["source_to_upsert_ms"] for row in latencies if row["species"] == species], 95)),
                "notification_storage_p95_ms": float(np.percentile(
                    [row["notification_storage_ms"] for row in latencies if row["species"] == species], 95)),
            }
            for species in frames
        },
        "per_packet_timings": latencies,
        "by_lifecycle_p95_ms": {
            phase: float(
                np.percentile(
                    [row["milliseconds"] for row in latencies if row["phase"] == phase], 95
                )
            )
            for phase in ["open", "update", "close"]
        },
        "elapsed_seconds": time.perf_counter() - started,
        "cpu_seconds_including_warmup": usage_after.ru_utime
        + usage_after.ru_stime
        - usage_before.ru_utime
        - usage_before.ru_stime,
        "process_peak_rss_native_units": usage_after.ru_maxrss,
        "resource_scope": "Parent process; extraction worker measured separately",
        "extraction_worker": extraction_worker,
        "rss_native_units": "bytes" if platform.system() == "Darwin" else "kibibytes",

        "all_expected_notifications_and_lifecycles": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos-dir", required=True, type=Path)
    parser.add_argument("--model-data-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument(
        "--parallel", action="store_true", help="Experimental fanout; requires --enabled"
    )
    parser.add_argument("--visits", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.04)
    args = parser.parse_args()
    if args.parallel and not args.enabled:
        parser.error("--parallel requires --enabled")
    if not 6 <= args.visits <= 300 or not 0.01 <= args.interval <= 1:
        parser.error("load exceeds bounded experiment")
    args.output_dir.mkdir(mode=0o700, parents=False)
    os.umask(0o077)
    os.environ["TOPOSYNC_DATA_DIR"] = str(args.model_data_dir.resolve())
    result = asyncio.run(
        trial(
            args.photos_dir,
            args.output_dir,
            enabled=args.enabled,
            parallel=args.parallel,
            visits=args.visits,
            interval=args.interval,
        )
    )
    result.update(
        purpose=__doc__,
        python=platform.python_version(),
        platform=platform.platform(),
        processor=platform.processor(),
    )
    (args.output_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
