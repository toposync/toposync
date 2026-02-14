from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .compiler import CompiledPipeline
from .operator_registry import OperatorRegistry
from .runtime import (
    BoundedChannel,
    ChannelMetricsSnapshot,
    Packet,
    QueueOperationStatus,
)


class PipelineExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class PipelineRuntimeDependencies:
    config_store: Any | None = None
    logger: logging.Logger | None = None
    yolo_backend_factory: Callable[[Any], Any] | None = None
    files_dir: Path | None = None
    notifications_upsert: Callable[..., Any] | None = None
    origin_inbox: BoundedChannel[dict[str, Any]] | None = None
    processing_emit_projected_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None


@dataclass(slots=True)
class NodeRuntimeMetrics:
    processed_packets: int = 0
    emitted_packets: int = 0
    dropped_packets: int = 0
    timeout_count: int = 0
    canceled_count: int = 0
    error_count: int = 0
    process_latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=4096))

    def record_latency(self, latency_ms: float) -> None:
        self.process_latency_ms.append(max(0.0, float(latency_ms)))

    def snapshot(self) -> dict[str, Any]:
        samples = list(self.process_latency_ms)
        avg = (sum(samples) / len(samples)) if samples else 0.0
        p95 = _percentile(samples, 95.0)
        return {
            "processed_packets": self.processed_packets,
            "emitted_packets": self.emitted_packets,
            "dropped_packets": self.dropped_packets,
            "timeout_count": self.timeout_count,
            "canceled_count": self.canceled_count,
            "error_count": self.error_count,
            "avg_process_latency_ms": avg,
            "p95_process_latency_ms": p95,
        }


class OperatorRuntime(Protocol):
    async def run(self, context: "NodeExecutionContext") -> None:
        ...

    async def shutdown(self) -> None:
        ...


@dataclass(slots=True)
class NodeExecutionContext:
    node_id: str
    pipeline_name: str
    inputs: dict[str, BoundedChannel[Packet]]
    outputs: dict[str, list[BoundedChannel[Packet]]]
    cancel_event: asyncio.Event
    metrics: NodeRuntimeMetrics
    logger: logging.Logger

    async def read(self, *, port: str = "in", timeout_s: float = 0.2) -> Packet | None:
        channel = self.inputs.get(port)
        if channel is None:
            raise PipelineExecutionError(f"Input port '{port}' not found for node '{self.node_id}'")

        result = await channel.get(timeout_s=timeout_s, cancel_event=self.cancel_event)
        if result.status == QueueOperationStatus.ACCEPTED:
            self.metrics.processed_packets += 1
            return result.item
        if result.status == QueueOperationStatus.TIMEOUT:
            self.metrics.timeout_count += 1
            return None
        if result.status == QueueOperationStatus.CANCELED:
            self.metrics.canceled_count += 1
            return None
        self.metrics.dropped_packets += 1
        return None

    async def emit(
        self,
        packet: Packet,
        *,
        port: str = "out",
        timeout_s: float = 0.1,
    ) -> int:
        channels = self.outputs.get(port, [])
        if not channels:
            return 0
        accepted = 0
        for channel in channels:
            result = await channel.put(packet, timeout_s=timeout_s, cancel_event=self.cancel_event)
            if result.status == QueueOperationStatus.ACCEPTED:
                accepted += 1
                self.metrics.emitted_packets += 1
            elif result.status == QueueOperationStatus.DROPPED:
                self.metrics.dropped_packets += 1
            elif result.status == QueueOperationStatus.TIMEOUT:
                self.metrics.timeout_count += 1
            elif result.status == QueueOperationStatus.CANCELED:
                self.metrics.canceled_count += 1
        return accepted

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self.cancel_event.wait(), timeout=max(0.0, float(seconds)))
        except TimeoutError:
            return

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()


class BaseOperatorRuntime:
    async def run(self, context: NodeExecutionContext) -> None:  # pragma: no cover - interface default
        raise NotImplementedError

    async def shutdown(self) -> None:
        return None


class SourceOperatorRuntime(BaseOperatorRuntime):
    async def produce(self, context: NodeExecutionContext) -> Packet | None:  # pragma: no cover - interface default
        raise NotImplementedError

    async def idle_sleep(self, context: NodeExecutionContext) -> None:
        await context.sleep(0.01)

    async def run(self, context: NodeExecutionContext) -> None:
        while not context.is_cancelled():
            started_ns = time.monotonic_ns()
            packet = await self.produce(context)
            latency_ms = _elapsed_ms(started_ns)
            context.metrics.record_latency(latency_ms)
            if packet is None:
                await self.idle_sleep(context)
                continue
            await context.emit(packet, port="out")


class TransformOperatorRuntime(BaseOperatorRuntime):
    input_port: str = "in"
    output_port: str = "out"
    read_timeout_s: float = 0.2

    async def process_packet(self, packet: Packet, context: NodeExecutionContext) -> list[Packet]:
        return [packet]

    async def run(self, context: NodeExecutionContext) -> None:
        while not context.is_cancelled():
            packet = await context.read(port=self.input_port, timeout_s=self.read_timeout_s)
            if packet is None:
                continue
            started_ns = time.monotonic_ns()
            try:
                out_packets = await self.process_packet(packet, context)
            except Exception:
                context.metrics.error_count += 1
                context.logger.exception("Node '%s' failed to process packet", context.node_id)
                continue
            context.metrics.record_latency(_elapsed_ms(started_ns))
            for out_packet in out_packets:
                await context.emit(out_packet, port=self.output_port)


class PassThroughRuntime(TransformOperatorRuntime):
    async def process_packet(self, packet: Packet, context: NodeExecutionContext) -> list[Packet]:
        return [packet]


class SinkRuntime(TransformOperatorRuntime):
    output_port = "out"

    async def process_packet(self, packet: Packet, context: NodeExecutionContext) -> list[Packet]:
        return []


@dataclass(slots=True)
class PipelineRuntime:
    compiled: CompiledPipeline
    registry: OperatorRegistry
    dependencies: PipelineRuntimeDependencies = field(default_factory=PipelineRuntimeDependencies)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("toposync.pipelines.runtime"))
    channel_map: dict[str, BoundedChannel[Packet]] = field(init=False, default_factory=dict)
    node_metrics: dict[str, NodeRuntimeMetrics] = field(init=False, default_factory=dict)
    _runtime_by_node: dict[str, BaseOperatorRuntime] = field(init=False, default_factory=dict)
    _context_by_node: dict[str, NodeExecutionContext] = field(init=False, default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(init=False, default_factory=list)
    _runtimes: list[BaseOperatorRuntime] = field(init=False, default_factory=list)
    _cancel_event: asyncio.Event = field(init=False, default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._build_runtime()

    async def start(self) -> None:
        if self._tasks:
            return
        for node_id in self.compiled.topological_order:
            runtime = self._runtime_by_node[node_id]
            context = self._context_by_node[node_id]
            task = asyncio.create_task(self._run_node(runtime, context), name=f"pipeline[{self.compiled.name}].{node_id}")
            self._tasks.append(task)

    async def stop(self) -> None:
        self._cancel_event.set()
        for runtime in self._runtimes:
            try:
                await runtime.shutdown()
            except Exception:
                self.logger.exception("Failed to shutdown runtime")

        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=2.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
        self._tasks.clear()

    async def run_for(self, duration_s: float) -> dict[str, Any]:
        await self.start()
        await asyncio.sleep(max(0.0, float(duration_s)))
        await self.stop()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        channels = {name: _snapshot_to_dict(channel.metrics_snapshot()) for name, channel in self.channel_map.items()}
        nodes = {node_id: metrics.snapshot() for node_id, metrics in self.node_metrics.items()}
        return {
            "pipeline_name": self.compiled.name,
            "channels": channels,
            "nodes": nodes,
        }

    async def _run_node(self, runtime: BaseOperatorRuntime, context: NodeExecutionContext) -> None:
        try:
            await runtime.run(context)
        except asyncio.CancelledError:
            raise
        except Exception:
            context.metrics.error_count += 1
            self.logger.exception("Node '%s' runtime crashed", context.node_id)

    def _build_runtime(self) -> None:
        self._runtime_by_node = {}
        self._context_by_node = {}

        outputs_by_node_port: dict[tuple[str, str], list[BoundedChannel[Packet]]] = {}
        inputs_by_node_port: dict[tuple[str, str], BoundedChannel[Packet]] = {}

        for edge in self.compiled.edges:
            channel_name = f"{edge.source_node_id}.{edge.source_port}->{edge.target_node_id}.{edge.target_port}"
            channel = BoundedChannel[Packet](
                name=channel_name,
                maxsize=edge.channel_maxsize,
                drop_policy=edge.channel_drop_policy,
            )
            self.channel_map[channel_name] = channel
            outputs_by_node_port.setdefault((edge.source_node_id, edge.source_port), []).append(channel)
            inputs_by_node_port[(edge.target_node_id, edge.target_port)] = channel

        node_by_id = {node.node_id: node for node in self.compiled.nodes}
        for node_id in self.compiled.topological_order:
            node = node_by_id[node_id]
            registered = self.registry.get(node.operator_id)
            if registered is None:
                raise PipelineExecutionError(f"Unknown operator in runtime: {node.operator_id}")
            runtime_factory = getattr(registered, "runtime_factory", None)
            if runtime_factory is None:
                raise PipelineExecutionError(f"Operator has no runtime factory: {node.operator_id}")

            runtime = runtime_factory(node.normalized_config, self.dependencies)
            if not isinstance(runtime, BaseOperatorRuntime):
                raise PipelineExecutionError(
                    f"Runtime factory for '{node.operator_id}' must return BaseOperatorRuntime",
                )
            self._runtimes.append(runtime)
            self._runtime_by_node[node_id] = runtime

            node_inputs: dict[str, BoundedChannel[Packet]] = {}
            node_outputs: dict[str, list[BoundedChannel[Packet]]] = {}
            for (target_node_id, target_port), channel in inputs_by_node_port.items():
                if target_node_id == node_id:
                    node_inputs[target_port] = channel
            for (source_node_id, source_port), channels in outputs_by_node_port.items():
                if source_node_id == node_id:
                    node_outputs[source_port] = channels

            metrics = NodeRuntimeMetrics()
            self.node_metrics[node_id] = metrics
            context = NodeExecutionContext(
                node_id=node_id,
                pipeline_name=self.compiled.name,
                inputs=node_inputs,
                outputs=node_outputs,
                cancel_event=self._cancel_event,
                metrics=metrics,
                logger=self.logger,
            )
            self._context_by_node[node_id] = context


def _snapshot_to_dict(snapshot: ChannelMetricsSnapshot) -> dict[str, Any]:
    return {
        "name": snapshot.name,
        "maxsize": snapshot.maxsize,
        "depth": snapshot.depth,
        "max_depth_seen": snapshot.max_depth_seen,
        "put_attempts": snapshot.put_attempts,
        "put_accepted": snapshot.put_accepted,
        "get_accepted": snapshot.get_accepted,
        "dropped_oldest": snapshot.dropped_oldest,
        "dropped_newest": snapshot.dropped_newest,
        "dropped_total": snapshot.dropped_total,
        "timed_out": snapshot.timed_out,
        "canceled": snapshot.canceled,
        "avg_queue_wait_ms": snapshot.avg_queue_wait_ms,
        "p95_queue_wait_ms": snapshot.p95_queue_wait_ms,
        "utilization": snapshot.utilization,
    }


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (float(time.monotonic_ns()) - float(started_ns)) / 1_000_000.0)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = int(math.ceil((pct / 100.0) * len(sorted_values))) - 1
    idx = max(0, min(len(sorted_values) - 1, idx))
    return float(sorted_values[idx])
