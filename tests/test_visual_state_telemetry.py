from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict

import pytest

from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.telemetry import PipelineTelemetryStore
from toposync_ext_vision.visual_state import runtime as runtime_module
from toposync_ext_vision.visual_state.telemetry import DURATIONS, EVENTS, REASONS, VisualStateTelemetry

from test_visual_state_runtime import frame, harness, send, until
from test_visual_state_ai_strategies import ControlledAi, answer, running_sensor, submit
from test_identity_api import client  # noqa: F401
from test_visual_state_config_lifecycle import create_sensor


def record(context):
    values = defaultdict(list)
    original = context.observe_telemetry_numeric

    def observe(metric, value, **kwargs):
        values[metric.removeprefix("visual_state.")].append(value)
        original(metric, value, **kwargs)

    context.observe_telemetry_numeric = observe
    return values


def test_preparation_native_queue_and_inference_have_distinct_measured_spans(tmp_path, monkeypatch):
    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        context.telemetry_store = PipelineTelemetryStore()
        values = record(context)
        await runtime.prepare(context)
        prepare = runtime_module.prepare_visual_frame
        blocking = context.run_blocking
        embed = runtime._backend.embed

        async def preparation(*args, **kwargs):
            clock.now += .005
            return await prepare(*args, **kwargs)

        async def queued(*args, **kwargs):
            clock.now += .003
            return await blocking(*args, **kwargs)

        def inference(image):
            clock.now += .007
            return embed(image)

        monkeypatch.setattr(runtime_module, "prepare_visual_frame", preparation)
        context.run_blocking = queued
        runtime._backend.embed = inference
        try:
            await runtime._analyze(context, frame(clock, 1))
            for name, expected in (("preparation", 5), ("inference_queue", 3), ("inference", 7)):
                assert values[f"duration.{name}_ms"] == pytest.approx([expected])
            assert context.telemetry_store._metric_specs["visual_state.duration.inference_ms"].histogram_max == 10_000
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_acquisition_is_only_measured_with_verified_physical_timestamp(tmp_path):
    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        values = record(context)
        await runtime.prepare(context)
        try:
            runtime._admit(frame(clock, 1))
            assert values["events.acquisition_unknown"] == [1]
            assert not values["duration.acquisition_ms"]
            clock.now += .1
            packet = frame(clock, 2)
            evidence = packet.payload["capture_evidence"]
            evidence.update(physical_timestamp_verified=True, captured_monotonic=clock() - .01,
                            received_monotonic=clock() - .004)
            packet.artifacts["main"].metadata["image_geometry"] = image_geometry(12, 10, evidence)
            assert runtime._admit(packet) is not None
            assert values["duration.acquisition_ms"] == pytest.approx([6])
            assert values["duration.input_age_ms"] == pytest.approx([0, 10])
            for sequence, received in enumerate((None, float("nan"), clock() + 10), start=3):
                clock.now += .1
                packet = frame(clock, sequence)
                evidence = packet.payload["capture_evidence"]
                evidence.update(physical_timestamp_verified=True, captured_monotonic=clock() - .01,
                                received_monotonic=received)
                packet.artifacts["main"].metadata["image_geometry"] = image_geometry(12, 10, evidence)
                assert runtime._admit(packet) is not None
            assert values["duration.acquisition_ms"] == pytest.approx([6])
            assert values["events.acquisition_unknown"] == [1, 1, 1, 1]
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_confirmation_expiry_replays_and_invalid_frames_count_without_tick_spam(tmp_path):
    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        values = record(context)
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        try:
            await send(runtime, context, clock, 1)
            await send(runtime, context, clock, 2)
            await until(lambda: values["events.confirmed"] == [1])
            assert values["duration.confirmation_ms"] == pytest.approx([260])
            await context.inputs["in"].put(frame(clock, 2))
            await until(lambda: values["events.input_replayed"] == [1])
            clock.now += 2.1
            await until(lambda: values["events.expired"] == [1])
            for _ in range(5):
                await runtime._publish(context, "idle")
            assert values["events.expired"] == [1]
            assert values["events.confirmed"] == [1]
            assert values["duration.publication_ms"] and values["duration.evidence_age_ms"]
            invalid = frame(clock, 3)
            invalid.artifacts["main"].metadata.clear()
            await context.inputs["in"].put(invalid)
            await until(lambda: values["events.input_discarded"] == [1])
            await until(lambda: values["events.unavailable"] == [1])
            assert values["reason.input_unobservable"] == [1]
        finally:
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_ai_requests_attempts_queue_and_roundtrip_use_only_numeric_finite_metrics(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service, ai_max_attempts=2) as (runtime, context, clock):
            values = record(context)
            await submit(runtime, context, clock, 1)
            await until(lambda: len(service.responses) == 1)
            clock.now += .03
            service.responses[0].set_result({**answer(), "attempts": [{"private": "secret-one"}, {"private": "secret-two"}]})
            await until(lambda: runtime._ai_evaluation.status == "complete")
            assert values["events.ai_requests"] == [1]
            assert values["events.ai_attempts"] == [2]
            assert values["duration.ai_roundtrip_ms"] == pytest.approx([30])
            assert values["duration.ai_wait_ms"] == [0]
            assert "secret" not in repr(values)

    asyncio.run(run())


def test_latest_ai_queue_replacement_and_expired_responses_are_counted(tmp_path):
    async def run():
        service = ControlledAi()
        async with running_sensor(tmp_path, "direct", service) as (runtime, context, clock):
            values = record(context)
            await submit(runtime, context, clock, 1)
            await until(lambda: len(service.responses) == 1)
            await submit(runtime, context, clock, 2)
            await until(lambda: len(values["events.ai_queued"]) == 2)
            await submit(runtime, context, clock, 3)
            await until(lambda: values["events.ai_replaced"] == [1])
            clock.now += .8
            service.responses[0].set_result(answer())
            await until(lambda: len(values["events.ai_expired"]) == 2)
            assert len(service.calls) == 1
            assert runtime._reducer.snapshot().class_id is None

    asyncio.run(run())


def test_local_worker_counts_only_frames_replaced_after_admission(tmp_path):
    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        values = record(context)
        await runtime.prepare(context)
        entered, release = threading.Event(), threading.Event()
        embed = runtime._backend.embed

        def blocked(image):
            entered.set()
            assert release.wait(3)
            return embed(image)

        runtime._backend.embed = blocked
        task = asyncio.create_task(runtime.run(context))
        try:
            await context.inputs["in"].put(frame(clock, 1))
            await until(entered.is_set)
            for sequence in (2, 3):
                clock.now += .26
                await context.inputs["in"].put(frame(clock, sequence))
                await until(lambda: runtime._last_sample == (0, sequence))
            assert values["events.local_replaced"] == [1]
            assert values["events.input_received"] == [1, 1, 1]
        finally:
            release.set()
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())


def test_existing_authenticated_numeric_api_serves_visual_metrics(client, tmp_path):  # noqa: F811
    create_sensor(client)

    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        context.telemetry_store = client.app.state.pipeline_telemetry_store
        try:
            telemetry = VisualStateTelemetry(context, clock=clock, unix_clock=time.time)
            telemetry.measure("inference", 12.5)
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())
    url = "/api/pipelines/sensor/telemetry/numeric?node_id=state&metric_id=visual_state.duration.inference_ms"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["total_count"] == 1
    assert response.json()["histogram_max"] == 10_000
    client.post("/api/auth/logout")
    assert client.get(url).status_code == 401


def test_metric_names_are_bounded_and_observability_failures_do_not_escape(tmp_path):
    async def run():
        runtime, context, clock, store, _ = await harness(tmp_path)
        context.telemetry_store = PipelineTelemetryStore(max_numeric_series=8)
        values = record(context)
        telemetry = VisualStateTelemetry(context, clock=clock, unix_clock=clock.unix)
        try:
            for index in range(1000):
                telemetry.reason(f"private_description_{index}")
                telemetry.event(f"frame_{index}")
            telemetry.measure("inference", float("nan"))
            telemetry.measure("inference", -1)
            assert set(values) == {"reason.other"}
            assert len(context.telemetry_store._numeric_series) == 1
            for name in DURATIONS:
                telemetry.measure(name, 20)
            for name in EVENTS:
                telemetry.event(name)
            for reason in REASONS:
                telemetry.reason(reason)
            assert len(context.telemetry_store._numeric_series) <= 8
            assert not context.telemetry_store._image_markers_by_pipeline

            def fail(*args, **kwargs):
                raise RuntimeError("diagnostics unavailable")

            context.observe_telemetry_numeric = fail
            telemetry.event("ai_requests")
            telemetry.duration("publication", clock())
        finally:
            await runtime.shutdown()
            await context.scheduler.shutdown()
            store.close()

    asyncio.run(run())
