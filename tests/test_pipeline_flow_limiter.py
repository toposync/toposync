from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    FlowLimiter,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SourceOperatorRuntime,
    TransformOperatorRuntime,
    profile_for_name,
    register_core_operators,
)


def _packet(*, lifecycle: Lifecycle = Lifecycle.UPDATE, age_ms: float = 0.0) -> Packet:
    packet = Packet.create(stream_id="camera", lifecycle=lifecycle)
    if age_ms <= 0:
        return packet
    return replace(
        packet,
        created_monotonic_ns=time.monotonic_ns() - int(float(age_ms) * 1_000_000.0),
    )


def test_flow_limiter_accepts_and_finishes_packet() -> None:
    limiter = FlowLimiter(profile_for_name("low_latency_ai"))

    decision = limiter.acquire(_packet())
    assert decision.accepted is True
    assert decision.permit is not None
    assert limiter.metrics.in_flight == 1

    decision.permit.finish()
    snapshot = limiter.metrics.snapshot()
    assert snapshot["accepted"] == 1
    assert snapshot["finished"] == 1
    assert snapshot["in_flight"] == 0
    assert snapshot["last_event_ts"] is not None


def test_low_latency_profile_drops_updates_when_busy_and_preserves_lifecycle() -> None:
    limiter = FlowLimiter(profile_for_name("low_latency_ai"))
    first = limiter.acquire(_packet())
    assert first.permit is not None

    busy_update = limiter.acquire(_packet())
    assert busy_update.accepted is False
    assert busy_update.reason == "busy"

    lifecycle = limiter.acquire(_packet(lifecycle=Lifecycle.CLOSE))
    assert lifecycle.accepted is True
    assert lifecycle.permit is not None

    first.permit.finish()
    lifecycle.permit.finish()
    snapshot = limiter.metrics.snapshot()
    assert snapshot["accepted"] == 2
    assert snapshot["dropped"] == 1
    assert snapshot["finished"] == 2


def test_flow_limiter_skips_stale_updates_before_compute() -> None:
    limiter = FlowLimiter(profile_for_name("low_latency_ai"))

    stale = limiter.acquire(_packet(age_ms=2_000.0))

    assert stale.accepted is False
    assert stale.reason == "stale"
    snapshot = limiter.metrics.snapshot()
    assert snapshot["dropped"] == 1
    assert snapshot["skipped_stale"] == 1
    assert snapshot["in_flight"] == 0


def test_finished_callback_records_error_and_cancel() -> None:
    limiter = FlowLimiter(profile_for_name("lossless"))

    errored = limiter.acquire(_packet())
    canceled = limiter.acquire(_packet())
    assert errored.permit is not None
    assert canceled.permit is not None

    errored.permit.finish(error=True)
    canceled.permit.finish(canceled=True)

    snapshot = limiter.metrics.snapshot()
    assert snapshot["finished"] == 2
    assert snapshot["errors"] == 1
    assert snapshot["canceled"] == 1
    assert snapshot["in_flight"] == 0


class _OnePacketSourceRuntime(SourceOperatorRuntime):
    def __init__(self, packet: Packet) -> None:
        self._packet = packet
        self._done = False

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if self._done:
            return None
        self._done = True
        return self._packet


class _CountingTransformRuntime(TransformOperatorRuntime):
    def __init__(self) -> None:
        self.processed = 0

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self.processed += 1
        return [packet]


def _registry_with_probe_operators(
    *,
    source_packet: Packet,
    transform_runtime: _CountingTransformRuntime,
    pressure_behavior: str,
) -> OperatorRegistry:
    registry = OperatorRegistry()
    registry.register_operator(
        operator_id="test.source",
        inputs=[],
        outputs=[{"name": "out"}],
        runtime_factory=lambda _config, _deps: _OnePacketSourceRuntime(source_packet),
    )
    registry.register_operator(
        operator_id="test.heavy",
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        resource_kind="vision_model",
        pressure_behavior=pressure_behavior,  # type: ignore[arg-type]
        runtime_factory=lambda _config, _deps: transform_runtime,
    )
    return registry


def _pipeline() -> Pipeline:
    return Pipeline(
        name="flow_limiter_runtime_probe",
        graph={
            "schema_version": 1,
            "nodes": [
                {"id": "source", "operator": "test.source", "config": {}},
                {"id": "heavy", "operator": "test.heavy", "config": {}},
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "heavy", "port": "in"},
                    "maxsize": 4,
                    "drop_policy": "drop_oldest",
                }
            ],
        },
    )


def test_runtime_applies_flow_limiter_to_skip_before_compute_operator() -> None:
    async def scenario() -> None:
        transform = _CountingTransformRuntime()
        registry = _registry_with_probe_operators(
            source_packet=_packet(age_ms=2_000.0),
            transform_runtime=transform,
            pressure_behavior="skip_before_compute",
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(_pipeline())
        runtime = PipelineRuntime(compiled=compiled, registry=registry)

        await runtime.start()
        await asyncio.sleep(0.05)
        snapshot = runtime.snapshot()
        await runtime.stop()

        flow_metrics = snapshot["nodes"]["heavy"]["flow_limiter"]
        assert transform.processed == 0
        assert snapshot["nodes"]["heavy"]["dropped_packets"] == 1
        assert flow_metrics["profile"] == "low_latency_ai"
        assert flow_metrics["skipped_stale"] == 1
        assert flow_metrics["in_flight"] == 0

    asyncio.run(scenario())


def test_runtime_does_not_apply_flow_limiter_to_regular_operator() -> None:
    async def scenario() -> None:
        transform = _CountingTransformRuntime()
        registry = _registry_with_probe_operators(
            source_packet=_packet(age_ms=2_000.0),
            transform_runtime=transform,
            pressure_behavior="ignore",
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(_pipeline())
        runtime = PipelineRuntime(compiled=compiled, registry=registry)

        await runtime.start()
        await asyncio.sleep(0.05)
        snapshot = runtime.snapshot()
        await runtime.stop()

        assert transform.processed == 1
        assert snapshot["nodes"]["heavy"]["flow_limiter"] is None

    asyncio.run(scenario())


def test_core_flow_limiter_operator_is_registered() -> None:
    registry = OperatorRegistry()

    register_core_operators(registry)

    registered = registry.get("core.flow_limiter")
    assert registered is not None
    assert registered.definition.defaults["profile"] == "low_latency_ai"
    assert registered.definition.capabilities


def test_core_flow_limiter_uses_configured_profile() -> None:
    registry = OperatorRegistry()
    register_core_operators(registry)
    registered = registry.get("core.flow_limiter")
    assert registered is not None

    limiter = FlowLimiter.for_operator(registered.definition, {"profile": "lossless"})
    assert limiter is not None

    decision = limiter.acquire(_packet(age_ms=2_000.0))
    assert decision.accepted is True
    assert decision.permit is not None
    decision.permit.finish()
    assert limiter.metrics.snapshot()["profile"] == "lossless"
    assert limiter.metrics.snapshot()["skipped_stale"] == 0
