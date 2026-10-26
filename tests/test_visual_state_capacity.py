"""Opt-in wall-clock runtime measurement with supplied local weights and synthetic pixels."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import time

import pytest

from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
from toposync_ext_vision.registry.manifests import ModelManifest

from test_visual_state_runtime import frame, harness
from test_visual_state_telemetry import record


class WallClock:
    def __call__(self):
        return time.monotonic()

    def unix(self):
        return time.time()


def override_inference_threads(monkeypatch, threads):
    """Change only this test process's session construction, never product settings."""
    if threads not in (1, 2, 4):
        raise ValueError("capacity inference threads must be 1, 2 or 4")
    import onnxruntime as ort
    original = ort.InferenceSession

    def session(model, *, sess_options, **options):
        sess_options.intra_op_num_threads = threads
        return original(model, sess_options=sess_options, **options)

    monkeypatch.setattr(ort, "InferenceSession", session)


@pytest.mark.parametrize("threads", [1, 4])
def test_inference_thread_override_runs_native_backend(tmp_path, monkeypatch, threads):
    import numpy as np
    from test_visual_state_runtime import color_model
    from toposync_ext_vision.processing.runtime_backends import build_embedding_backend

    monkeypatch.setenv("TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS", "CPUExecutionProvider")
    override_inference_threads(monkeypatch, threads)
    backend = build_embedding_backend(color_model(tmp_path))
    try:
        settings = backend._session.get_session_options()
        assert settings.intra_op_num_threads == threads and settings.inter_op_num_threads == 1
        assert backend.providers == ["CPUExecutionProvider"]
        vector = backend.embed(np.full((10, 12, 3), 255, dtype=np.uint8))
        assert vector.shape == (3,) and float(np.linalg.norm(vector)) == pytest.approx(1)
    finally:
        backend.close()


def percentiles(values):
    ordered = sorted(values)
    return {str(percentile): ordered[math.ceil(len(ordered) * percentile / 100) - 1] if ordered else None
            for percentile in (50, 95, 99)}


def summarize(phases, publications):
    results = []
    for phase in phases:
        observation = next((item for item in publications
            if phase["first_receipt"] is not None
            and phase["first_receipt"] <= item["at"] < phase["end"]
            and item["class_id"] == phase["class_id"]
            and item["sequence"] >= phase["first_sequence"]
            and item["confirmed_at"] is not None
            and item["confirmed_at"] != phase["confirmation_at_start"]), None)
        results.append({**phase, "latency_ms": (observation["at"] - phase["first_receipt"]) * 1000
                        if observation else None})
    transitions = [item for item in results if item["phase"] > 0]
    latencies = [item["latency_ms"] for item in transitions if item["latency_ms"] is not None]
    missed = len(transitions) - len(latencies)
    return {"phases": results, "expected_transitions": len(transitions), "missed_transitions": missed,
            "matched_transition_percentiles_ms": percentiles(latencies),
            "all_transition_percentiles_ms": percentiles(latencies) if not missed else None,
            "within_1000_ms": sum(value <= 1000 for value in latencies),
            "within_2000_ms": sum(value <= 2000 for value in latencies)}


def test_summary_keeps_misses_and_rejects_old_confirmations():
    phases = [{"phase": index, "class_id": "red", "first_receipt": float(index), "end": index + 1.,
               "first_sequence": index + 1, "confirmation_at_start": 10.} for index in range(3)]
    publications = [{"at": 1.2, "class_id": "red", "sequence": 2, "confirmed_at": 10.},
                    {"at": 2.3, "class_id": "red", "sequence": 2, "confirmed_at": 11.},
                    {"at": 2.4, "class_id": "red", "sequence": 3, "confirmed_at": 12.}]
    result = summarize(phases, publications)
    assert result["expected_transitions"] == 2
    assert result["missed_transitions"] == 1
    assert result["phases"][1]["latency_ms"] is None
    assert result["phases"][2]["latency_ms"] == pytest.approx(400)
    assert result["all_transition_percentiles_ms"] is None
    assert result["within_1000_ms"] == result["within_2000_ms"] == 1
    assert percentiles([]) == {"50": None, "95": None, "99": None}


@pytest.mark.parametrize("sensor_count", [1, 4, 8])
def test_local_model_runtime_capacity(tmp_path, sensor_count, monkeypatch):
    model_path = os.environ.get("VISUAL_STATE_CAPACITY_MODEL")
    output_root = os.environ.get("VISUAL_STATE_CAPACITY_OUTPUT")
    if not model_path or not output_root:
        pytest.skip("explicit local model and external report directory required")
    resource = pytest.importorskip("resource", reason="capacity resource measurements require a Unix host")
    root = Path(output_root).resolve(strict=True)
    repository = Path(__file__).resolve().parents[1]
    if root.is_relative_to(repository) or tmp_path.resolve().is_relative_to(repository):
        pytest.fail("capacity reports and temporary data must be outside the repository")
    output = root / f"capacity-{sensor_count}.json"
    if output.exists():
        pytest.fail("preserve the existing capacity report; use a new run directory")
    model = Path(model_path).resolve(strict=True)
    manifest = ModelManifest.model_validate_json(model.read_text()).bind_source_path(model)
    policy_path = os.environ.get("VISUAL_STATE_CAPACITY_COMPARISON")
    comparison = json.loads(Path(policy_path).read_text()) if policy_path else None
    threads = os.environ.get("VISUAL_STATE_CAPACITY_INTRA_THREADS")
    if threads is not None:
        override_inference_threads(monkeypatch, int(threads))

    async def run():
        scheduler = ExecutionScheduler(thread_pool_max_workers=2)
        sensors, tasks = [], []
        clock = WallClock()
        interval, dwell, phase_count = .1, 2.25, 13
        before = resource.getrusage(resource.RUSAGE_SELF)
        started = time.monotonic()
        report = {"sensor_count": sensor_count, "synthetic_pixels": True, "quality_approved": False,
                  "platform": platform.platform(), "cpu_count": os.cpu_count(),
                  "model_manifest_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                  "model_sha256": manifest.sha256, "license": manifest.license.model_dump(),
                  "versions": {name: metadata.version(name) for name in ("numpy", "onnxruntime")},
                  "scheduler_threads": 2, "input_interval_ms": interval * 1000,
                  "state_duration_ms": dwell * 1000, "phase_count": phase_count,
                  "limitations": ["Synthetic same-image references; no semantic quality claim",
                      "Receipt-to-runtime publication only; no camera, transport or Home Assistant",
                      "Twelve transitions per sensor are descriptive, not a tail confidence bound",
                      "Sequential preparation; model file may already be cached by the operating system",
                      "Resource usage includes measurement and fixtures; machine may have concurrent work"]}
        try:
            for index in range(sensor_count):
                directory = tmp_path / str(index)
                directory.mkdir()
                prepare_started = time.monotonic()
                runtime, context, _, store, _ = await harness(directory, sensor_id=f"sensor_{index}",
                    scheduler=scheduler, clock=clock, manifest=manifest, comparison=comparison)
                sensor = {"runtime": runtime, "context": context, "store": store,
                          "metrics": record(context), "publications": [], "sent": 0, "maximum_depth": 0}
                sensors.append(sensor)
                await runtime.prepare(context)
                sensor["prepare_ms"] = (time.monotonic() - prepare_started) * 1000
                sensor["providers"] = runtime._backend.providers
                settings = runtime._backend._session.get_session_options()
                sensor["inference_threads"] = {"intra": settings.intra_op_num_threads,
                                               "inter": settings.inter_op_num_threads}
                sensor["configuration"] = runtime.config.model_dump(mode="json")
                original = context.emit

                async def emit(packet, *, sensor=sensor, original=original, **options):
                    state = packet.payload["visual_state"]
                    sensor["publications"].append({"at": time.monotonic(), "class_id": state["class_id"],
                        "sequence": (state.get("sample") or {}).get("sequence", -1),
                        "confirmed_at": state["confirmed_at"]})
                    return await original(packet, **options)

                context.emit = emit
                tasks.append(asyncio.create_task(runtime.run(context)))
            start = time.monotonic()
            for sensor in sensors:
                sensor["phases"] = [{"phase": index, "class_id": "red" if index % 2 == 0 else "blue",
                    "scheduled": start + index * dwell, "end": start + (index + 1) * dwell,
                    "first_receipt": None, "first_sequence": None, "confirmation_at_start": None}
                    for index in range(phase_count)]
            slot, feed_delay_ms = 0, []
            while True:
                await asyncio.sleep(max(0, start + slot * interval - time.monotonic()))
                now = time.monotonic()
                if now >= start + dwell * phase_count:
                    break
                latest_slot = max(slot, math.floor((now - start) / interval))
                slot = latest_slot
                feed_delay_ms.append((now - start - slot * interval) * 1000)
                phase_index = math.floor((now - start) / dwell)
                for sensor in sensors:
                    phase = sensor["phases"][phase_index]
                    packet = frame(clock, slot + 1, phase["class_id"])
                    if phase["first_receipt"] is None:
                        phase.update(first_receipt=packet.payload["capture_evidence"]["received_monotonic"],
                            first_sequence=slot + 1,
                            confirmation_at_start=sensor["runtime"]._reducer.snapshot().confirmed_at)
                    await sensor["context"].inputs["in"].put(packet)
                    sensor["sent"] += 1
                    sensor["maximum_depth"] = max(sensor["maximum_depth"], sensor["context"].inputs["in"].depth)
                slot += 1
            planned_frames = math.ceil(dwell * phase_count / interval)
            report.update(planned_frames_per_sensor=planned_frames,
                feed_skipped_slots=planned_frames - sensors[0]["sent"],
                feed_delay_percentiles_ms=percentiles(feed_delay_ms))
        finally:
            for sensor in sensors:
                sensor["context"].cancel_event.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            pool = scheduler._thread_pool
            await scheduler.shutdown()
            if pool is not None:
                await asyncio.to_thread(pool.shutdown, wait=True)
            for sensor in sensors:
                await sensor["runtime"].shutdown()
                sensor["store"].close()
            assert all(sensor["runtime"].resources_released for sensor in sensors)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        report["sensors"] = [{"index": index, "prepare_ms": sensor["prepare_ms"],
            "providers": sensor["providers"], "configuration": sensor["configuration"],
            "inference_threads": sensor["inference_threads"],
            "sent_frames": sensor["sent"], "maximum_input_depth": sensor["maximum_depth"],
            "input_channel": asdict(sensor["context"].inputs["in"].metrics_snapshot()),
            "publications": sensor["publications"],
            **summarize(sensor["phases"], sensor["publications"]),
            "telemetry": dict(sensor["metrics"])} for index, sensor in enumerate(sensors)]
        after = resource.getrusage(resource.RUSAGE_SELF)
        report.update(wall_seconds=time.monotonic() - started,
            process_cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
            process_peak_rss_bytes=after.ru_maxrss * (1 if platform.system() == "Darwin" else 1024),
            resources_released=True)
        assert all(sensor["maximum_depth"] <= 1 for sensor in sensors)
        with output.open("x") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")

    asyncio.run(run())
