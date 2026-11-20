"""Equivalent offline event load through the real scheduler and SQLite notifications.

Compares source -> notification against the same source with identity operators.
It uses licensed photos only for workload, not recognition accuracy. With
--vision-input the real detector and tracker consume those static photos.
--published includes real public projection, one subscriber and JSON consumption,
but not HTTP delivery or browser rendering.
No physical camera is exercised; this is not the final installation profile.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import time

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from toposync.runtime.config_store import Pipeline
from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.notifications import NotificationsRuntime
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
from scripts.identity.real_pipeline_smoke import SOURCES


class EmptyConfig(BaseModel):
    pass


class SourceConfig(BaseModel):
    source_index: int = Field(default=0, ge=0, le=1)


def worker_resources():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "pid": os.getpid(),
        "cpu_seconds_including_startup": usage.ru_utime + usage.ru_stime,
        "peak_rss_native_units": usage.ru_maxrss,
    }


def _timed_upsert(notifications, values):
    started = time.perf_counter()
    record, created = notifications.upsert(**values)
    return record, created, started, time.perf_counter()


async def persist_notification(notifications, values):
    # Mesmo encaminhamento usado por NotificationsRuntime.upsert. Os tempos de
    # SQLite são medidos na thread, sem confundir espera do executor com disco.
    return await asyncio.to_thread(_timed_upsert, notifications, values)


async def trial(
    photos: Path,
    output: Path,
    *,
    enabled: bool,
    visits: int,
    interval: float,
    parallel: bool = False,
    context_only: bool = False,
    vision_input: bool = False,
    published: bool = False,
    sources: int = 1,
) -> dict:
    if sources not in (1, 2):
        raise ValueError("benchmark supports one or two sources")
    frames = {}
    for species, filename, digest in SOURCES:
        path = photos / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            frames[species] = np.asarray(image)
            if vision_input:
                frames[species] = frames[species][:, :, ::-1].copy()
    gallery = IdentityStore(output / "gallery", scope="offline-load-fixture")
    publication = NotificationsRuntime(data_dir=output) if published else None
    subscriber = publication.broadcaster.subscribe() if publication else None
    notifications = publication.store if publication else NotificationStore(output / "notifications.sqlite3")
    sent, latencies, identities, lifecycles = {}, [], set(), []
    planned, emission_lateness = {}, []
    created_count = 0
    completed, warmed, recognition_completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recognition_lifecycles = []
    recognition_reasons = Counter()
    measured_occurrences = set()
    recognition_latencies = []
    expected_evidence = Counter()

    warm_visits = 3 * sources
    warm_packets = 3 * warm_visits
    measured_visits = visits * sources
    total_packets = warm_packets + measured_visits * 3

    def check_warmup():
        if len(lifecycles) >= warm_packets and (not parallel or len(recognition_lifecycles) >= warm_packets):
            warmed.set()

    class Source(SourceOperatorRuntime):
        def __init__(self, source_index):
            self.source_index = source_index
            self.sequence = 0
            self.deadline = None

        async def produce(self, context):
            if self.sequence >= 3 * (visits + 3):
                await context.sleep(0.01)
                return None
            if self.sequence == 9:
                await warmed.wait()
                self.deadline = None
            now = time.perf_counter()
            self.deadline = now if self.deadline is None else self.deadline + interval
            await context.sleep(max(0, self.deadline - now))
            sequence = self.sequence
            self.sequence += 1
            local_visit = sequence // 3
            visit = local_visit * sources + self.source_index
            species = SOURCES[(local_visit + self.source_index) % len(SOURCES)][0]
            camera = "offline-load" if sources == 1 else f"offline-load-{self.source_index}"
            phase = (Lifecycle.OPEN, Lifecycle.UPDATE, Lifecycle.CLOSE)[sequence % 3]
            packet_key = (f"load-{visit}", phase.value)
            planned[packet_key] = self.deadline
            sent[packet_key] = time.perf_counter()
            if visit >= warm_visits:
                if phase == Lifecycle.OPEN:
                    expected_evidence[camera, species] += 1
                emission_lateness.append((sent[packet_key] - self.deadline) * 1000)
            payload = {
                "camera_id": camera,
                "frame_ts": time.time(),
                "benchmark_visit": visit,
            }
            if not vision_input:
                payload.update({
                    "source_stream_id": camera,
                    "correlation_id": f"load-{visit}",
                    "subject": {
                        "id": f"load-{visit}", "type": "event", "category": species,
                        "bbox01": [0, 0, 1, 1],
                    },
                    "world_position": {"x": 1.0, "z": 2.0},
                })
            return Packet.create(
                stream_id=f"load-{visit}" if vision_input else ("licensed-photo-load" if sources == 1 else camera),
                lifecycle=phase,
                payload=payload,
                artifacts={
                    "frame": Artifact(
                        name="frame", data=frames[species],
                        metadata={"color_order": "bgr" if vision_input else "rgb"}
                    )
                },
            )

    async def upsert(**values):
        nonlocal created_count
        service_started = time.perf_counter()
        if publication:
            public = await publication.upsert(**values)
            event = subscriber.get_nowait()
            assert event["notification"] == public
            assert json.loads(json.dumps(event))["notification"]["id"] == public["id"]
            identifier, created = public["id"], event["op"] == "insert"
            storage_started = stored_at = None
        else:
            record, created, storage_started, stored_at = await persist_notification(
                notifications, values
            )
            identifier = record.id
        returned_at = time.perf_counter()
        payload = values["payload"]
        key = (f"load-{payload['data']['benchmark_visit']}", payload["lifecycle"])
        visit = int(key[0].split("-")[1])
        identities.add(identifier)
        created_count += int(created)
        lifecycles.append(key)
        if visit >= warm_visits:
            if enabled and not parallel:
                summary = payload["data"].get("recognition", {})
                if summary.get("occurrence_id"):
                    measured_occurrences.add(summary["occurrence_id"])
            latencies.append(
                {
                    "source_index": visit % sources,
                    "visit": visit,
                    "emitted_s": sent[key] - started,
                    "delivered_s": returned_at - started,
                    "phase": key[1],
                    "species": payload["subject"]["category"],
                    "source_to_upsert_ms": (service_started - sent[key]) * 1000,
                    "notification_executor_wait_ms": (storage_started - service_started) * 1000 if storage_started is not None else None,
                    "notification_storage_ms": (stored_at - storage_started) * 1000 if stored_at is not None else None,
                    "notification_resume_ms": (returned_at - stored_at) * 1000 if stored_at is not None else None,
                    "milliseconds": (returned_at - sent[key]) * 1000,
                    "planned_milliseconds": (returned_at - planned[key]) * 1000,
                }
            )
        check_warmup()
        if len(lifecycles) == total_packets:
            completed.set()

    class RecognitionCollector(TransformOperatorRuntime):
        async def process_packet(self, value, context):
            visit = value.payload["benchmark_visit"]
            key = (f"load-{visit}", value.lifecycle.value)
            recognition_lifecycles.append(key)
            if visit >= warm_visits:
                summary = value.payload["recognition"]
                recognition_reasons[summary["reason"]] += 1
                recognition_latencies.append((time.perf_counter() - sent[key]) * 1000)
                if summary.get("occurrence_id"):
                    measured_occurrences.add(summary["occurrence_id"])
            check_warmup()
            if len(recognition_lifecycles) == total_packets:
                recognition_completed.set()
            return []

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_identity_operators(registry)
    if vision_input:
        from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators
        register_vision_pipeline_operators(registry)
    registry.register_operator(
        operator_id="test.load_source",
        config_model=SourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults={},
        share_strategy="never",
        runtime_factory=lambda config, dependencies: Source(config.get("source_index", 0)),
    )
    nodes = [{"id": "source", "operator": "test.load_source", "config": {}}]
    if vision_input:
        nodes.extend([
            {"id": "detect", "operator": "vision.detect", "config": {
                "model_id": "rfdetr_det_medium", "emit_mode": "annotate",
                "categories": ["person", "cat", "dog"], "input_artifact_name": "frame",
            }},
            {"id": "track", "operator": "vision.track", "config": {
                "default_interval_seconds": 0,
            }},
        ])
    if context_only:
        nodes.append(
            {"id": "context", "operator": "vision.identity_context", "config": {"enabled": True}}
        )
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
        {"id": "notify", "operator": "core.notify", "config": {
            "update_interval_seconds": 0, "include_payload_paths": ["benchmark_visit"],
        }}
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
            *([("source", "detect"), ("detect", "track")] if vision_input else []),
            ("track" if vision_input else "source", "context"),
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
    if sources == 2:
        # Independent detector/tracker state, shared scheduler, storage and notification subscriber.
        second_nodes = [
            {**node, "id": "second_" + node["id"],
             "config": {"source_index": 1} if node["id"] == "source" else node["config"]}
            for node in nodes
        ]
        second_edges = [
            {**edge, "id": "second_" + edge["id"],
             "from": {**edge["from"], "node": "second_" + edge["from"]["node"]},
             "to": {**edge["to"], "node": "second_" + edge["to"]["node"]}}
            for edge in edges
        ]
        nodes.extend(second_nodes)
        edges.extend(second_edges)
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
    measured_visits_with_evidence = {}
    try:
        await runtime.start()
        await asyncio.wait_for(warmed.wait(), timeout=30)
        await asyncio.wait_for(completed.wait(), timeout=max(30, visits * 3 * interval * 3))
        notifications_finished = time.perf_counter()
        if parallel:
            await asyncio.wait_for(recognition_completed.wait(), timeout=30)
        drain_seconds = time.perf_counter() - notifications_finished
        snapshot = runtime.snapshot()
        detector_providers = {
            node_id: runtime._runtime_by_node[node_id]._backend.providers
            for node_id in ("detect", "second_detect") if node_id in runtime._runtime_by_node
        }
        if enabled:
            closed_count = gallery._connection.execute(
                "SELECT count(*) FROM occurrence WHERE closed=1"
            ).fetchone()[0]
            assert closed_count == measured_visits + warm_visits
            assert len(measured_occurrences) == measured_visits
            placeholders = ",".join("?" for _ in measured_occurrences)
            measured_visits_with_evidence = {
                (camera, species): count for camera, species, count in gallery._connection.execute(
                    f"SELECT o.camera_id, p.species, count(DISTINCT p.occurrence_id) "
                    f"FROM observation p JOIN occurrence o ON o.id = p.occurrence_id "
                    f"WHERE p.eligible=1 AND p.occurrence_id IN ({placeholders}) GROUP BY o.camera_id, p.species",
                    tuple(measured_occurrences),
                )
            }
            assert measured_visits_with_evidence == expected_evidence, (
                "some measured visits did not produce eligible evidence"
            )
            extraction_worker = await runtime.dependencies.execution_scheduler.run_sync(
                worker_resources,
                mode="process_pool",
                process_pool_key="vision.identity_evidence",
                concurrency_key="vision.identity_evidence",
                max_concurrency=1,
            )

        evidence_counts = {
            row[0]: row[1]
            for row in gallery._connection.execute(
                "SELECT species,count(*) FROM observation WHERE eligible=1 GROUP BY species"
            )
        }
        assert len(lifecycles) == total_packets and len(latencies) == measured_visits * 3
        assert created_count == measured_visits + warm_visits and len(identities) == measured_visits + warm_visits
        for delivered in ([lifecycles, recognition_lifecycles] if parallel else [lifecycles]):
            assert all(
                [phase for name, phase in delivered if name == f"load-{index}"]
                == ["open", "update", "close"]
                for index in range(measured_visits + warm_visits)
            )
        if publication:
            assert subscriber.empty()
    except BaseException as error:
        # Preservar contadores antes da parada para distinguir perda, pressão e
        # falha técnica. Nenhuma imagem, vetor ou payload entra no diagnóstico.
        def incomplete_visits(delivered):
            # Only list visits actually offered; a warmup failure never started the measurement.
            phases = {}
            for name, phase in delivered:
                phases.setdefault(name, []).append(phase)
            return {
                name: phases.get(name, [])
                for name in sorted({name for name, _ in sent})
                if phases.get(name) != ["open", "update", "close"]
            }

        (output / "failure.json").write_text(
            json.dumps(
                {
                    "instrument_version": 5,
                    "error_type": type(error).__name__,
                    "expected_packets": total_packets,
                    "measured_evidence_expected": {f"{camera}/{species}": count for (camera, species), count in expected_evidence.items()},
                    "measured_evidence_observed": {f"{camera}/{species}": count for (camera, species), count in measured_visits_with_evidence.items()},
                    "warmup_complete": warmed.is_set(),
                    "source_packets_emitted": len(sent),
                    "notification_incomplete_visits": incomplete_visits(lifecycles),
                    "recognition_incomplete_visits": incomplete_visits(recognition_lifecycles) if parallel else None,
                    "notification_callbacks": len(lifecycles),
                    "notifications_created": created_count,
                    "distinct_notifications": len(identities),
                    "recognition_callbacks": len(recognition_lifecycles),
                    "notification_phases": {
                        phase: sum(item[1] == phase for item in lifecycles)
                        for phase in ("open", "update", "close")
                    },
                    "runtime": runtime.snapshot(),
                    "release_gate_approved": False,
                },
                indent=2,
            )
            + "\n"
        )
        raise
    finally:
        await runtime.stop()
        await runtime.dependencies.execution_scheduler.shutdown()
        gallery.close()
        if publication:
            publication.broadcaster.unsubscribe(subscriber)
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    values = [row["milliseconds"] for row in latencies]
    actual_rates = []
    for index in range(sources):
        source_rows = [row for row in latencies if row["source_index"] == index]
        rates = {}
        for label, field in (("emitted_hz", "emitted_s"), ("delivered_hz", "delivered_s")):
            times = [row[field] for row in source_rows]
            rates[label] = (len(times) - 1) / (max(times) - min(times))
        actual_rates.append(rates)
    return {
        "instrument_version": 5,
        "notification_execution": "NotificationsRuntime.upsert with subscribed JSON consumer" if published else "asyncio.to_thread, matching NotificationsRuntime.upsert",
        "notification_limit": "Includes public projection, broadcaster and JSON consumption; excludes HTTP/browser delivery" if published else "Store persistence only; excludes public projection and broadcaster",
        "identity_enabled": enabled,
        "input_path": "RF-DETR Medium -> real tracker" if vision_input else "preassigned subjects",
        "topology": "context_only_diagnostic"
        if context_only
        else "parallel_experiment"
        if parallel
        else "sequential",
        "parallel_experiment_limit": "Existing fanout shares backpressure and memory; bounded workload only"
        if parallel
        else None,
        "queue_metrics": snapshot["channels"],
        "usable_inference_observations_by_species": evidence_counts,
        "measured_visits_with_eligible_evidence": [
            {"camera": camera, "species": species, "visits": count}
            for (camera, species), count in sorted(measured_visits_with_evidence.items())
        ],
        "measured_recognition_reasons": dict(recognition_reasons),
        "source_to_resolver_p95_ms": float(np.percentile(recognition_latencies, 95)) if parallel else None,
        "sources": sources,
        "detector_providers": detector_providers,
        "measured_visits": measured_visits,
        "measured_visits_per_source": visits,
        "reference_identities_at_start": 0,
        "measured_packets": len(latencies),
        "offered_packets_per_second": sources / interval,
        "interval_seconds": interval,
        "warmup_visits_excluded": warm_visits,
        "emission_lateness_p95_ms": float(np.percentile(emission_lateness, 95)),
        "planned_to_notification_p95_ms": float(
            np.percentile([row["planned_milliseconds"] for row in latencies], 95)
        ),
        "recognition_drain_seconds": drain_seconds,
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "max_ms": max(values),
        "p99_ms": float(np.percentile(values, 99)),
        "actual_rates_per_source": actual_rates,
        "p95_ms_per_source": [
            float(np.percentile([row["milliseconds"] for row in latencies if row["source_index"] == index], 95))
            for index in range(sources)
        ],
        "timing_by_species": {
            species: {
                "frame_bytes": int(frames[species].nbytes),
                "source_to_notification_p95_ms": float(
                    np.percentile(
                        [row["milliseconds"] for row in latencies if row["species"] == species], 95
                    )
                ),
                "source_to_upsert_p95_ms": float(
                    np.percentile(
                        [
                            row["source_to_upsert_ms"]
                            for row in latencies
                            if row["species"] == species
                        ],
                        95,
                    )
                ),
                "notification_storage_p95_ms": None if published else float(
                    np.percentile(
                        [
                            row["notification_storage_ms"]
                            for row in latencies
                            if row["species"] == species
                        ],
                        95,
                    )
                ),
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
    parser.add_argument("--published", action="store_true", help="Measure real notification publication and subscribed JSON consumption")
    parser.add_argument("--vision-input", action="store_true", help="Detect and track public frames instead of preassigning subjects")
    parser.add_argument(
        "--context-only",
        action="store_true",
        help="Diagnose link/operator overhead without inference",
    )
    parser.add_argument(
        "--parallel", action="store_true", help="Experimental fanout; requires --enabled"
    )
    parser.add_argument("--sources", type=int, choices=(1, 2), default=1)
    parser.add_argument("--visits", type=int, default=30, help="Measured visits per source")
    parser.add_argument("--interval", type=float, default=0.04)
    args = parser.parse_args()
    if args.context_only and (args.enabled or args.parallel):
        parser.error("--context-only cannot run inference")
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
            context_only=args.context_only,
            vision_input=args.vision_input,
            published=args.published,
            visits=args.visits,
            sources=args.sources,
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
