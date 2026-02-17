from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import time
import tracemalloc
from collections import deque

from toposync.runtime.pipelines import BoundedChannel, DropPolicy, Lifecycle, Packet, QueueOperationStatus


@dataclasses.dataclass(slots=True)
class DemoStats:
    produced: int = 0
    produced_dropped: int = 0
    produced_timeout: int = 0
    processed: int = 0
    consumed: int = 0
    e2e_samples_ms: deque[float] = dataclasses.field(default_factory=lambda: deque(maxlen=8000))

    def p95_e2e_ms(self) -> float:
        if not self.e2e_samples_ms:
            return 0.0
        values = sorted(self.e2e_samples_ms)
        idx = max(0, int(0.95 * len(values)) - 1)
        return float(values[idx])

    def avg_e2e_ms(self) -> float:
        if not self.e2e_samples_ms:
            return 0.0
        return float(sum(self.e2e_samples_ms) / len(self.e2e_samples_ms))


async def _producer(
    *,
    ingress: BoundedChannel[Packet],
    stop_event: asyncio.Event,
    stats: DemoStats,
    target_rate_hz: float,
    put_timeout_s: float,
) -> None:
    interval_s = 1.0 / target_rate_hz if target_rate_hz > 0 else 0.0
    next_tick = time.monotonic()
    sequence = 0
    while not stop_event.is_set():
        packet = Packet.create(
            stream_id="demo:stream",
            lifecycle=Lifecycle.UPDATE,
            payload={"sequence": sequence},
        )
        sequence += 1
        stats.produced += 1
        result = await ingress.put(packet, timeout_s=put_timeout_s, cancel_event=stop_event)
        if result.status == QueueOperationStatus.DROPPED:
            stats.produced_dropped += 1
        elif result.status == QueueOperationStatus.TIMEOUT:
            stats.produced_timeout += 1
        if interval_s <= 0:
            continue
        next_tick += interval_s
        sleep_s = next_tick - time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        else:
            next_tick = time.monotonic()


async def _worker(
    *,
    ingress: BoundedChannel[Packet],
    egress: BoundedChannel[Packet],
    stop_event: asyncio.Event,
    stats: DemoStats,
    process_time_ms: float,
    get_timeout_s: float,
    put_timeout_s: float,
) -> None:
    sleep_s = max(0.0, float(process_time_ms)) / 1000.0
    while True:
        result = await ingress.get(timeout_s=get_timeout_s, cancel_event=stop_event)
        if result.status == QueueOperationStatus.TIMEOUT:
            if stop_event.is_set():
                break
            continue
        if result.status == QueueOperationStatus.CANCELED:
            break
        packet = result.item
        if packet is None:
            continue

        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

        processed = packet.with_artifact(
            artifact_name_to_stage(packet=packet, stage_name="worker"),
        )
        stats.processed += 1
        await egress.put(processed, timeout_s=put_timeout_s, cancel_event=stop_event)

        if stop_event.is_set() and ingress.depth <= 0:
            break


async def _sink(
    *,
    egress: BoundedChannel[Packet],
    stop_event: asyncio.Event,
    stats: DemoStats,
    get_timeout_s: float,
) -> None:
    while True:
        result = await egress.get(timeout_s=get_timeout_s, cancel_event=stop_event)
        if result.status == QueueOperationStatus.TIMEOUT:
            if stop_event.is_set():
                break
            continue
        if result.status == QueueOperationStatus.CANCELED:
            break
        packet = result.item
        if packet is None:
            continue
        stats.consumed += 1
        stats.e2e_samples_ms.append(packet.age_ms())
        if stop_event.is_set() and egress.depth <= 0:
            break


async def _monitor(
    *,
    ingress: BoundedChannel[Packet],
    egress: BoundedChannel[Packet],
    stats: DemoStats,
    stop_event: asyncio.Event,
    interval_s: float,
) -> None:
    started = time.monotonic()
    while not stop_event.is_set():
        await asyncio.sleep(interval_s)
        ingress_snapshot = ingress.metrics_snapshot()
        egress_snapshot = egress.metrics_snapshot()
        elapsed = time.monotonic() - started
        print(
            (
                "[%.1fs] in=%d/%d drop=%d p95q=%.2fms | out=%d/%d drop=%d p95q=%.2fms | "
                "prod=%d proc=%d cons=%d p95e2e=%.2fms"
            )
            % (
                elapsed,
                ingress_snapshot.depth,
                ingress_snapshot.maxsize,
                ingress_snapshot.dropped_total,
                ingress_snapshot.p95_queue_wait_ms,
                egress_snapshot.depth,
                egress_snapshot.maxsize,
                egress_snapshot.dropped_total,
                egress_snapshot.p95_queue_wait_ms,
                stats.produced,
                stats.processed,
                stats.consumed,
                stats.p95_e2e_ms(),
            ),
        )


def artifact_name_to_stage(*, packet: Packet, stage_name: str):
    from toposync.runtime.pipelines import Artifact

    return Artifact(
        name=f"stage_{stage_name}",
        data={"packet_id": packet.packet_id, "stage": stage_name},
    )


async def _run_demo(args: argparse.Namespace) -> int:
    drop_policy = DropPolicy(args.drop_policy)
    ingress = BoundedChannel[Packet](name="demo_ingress", maxsize=args.queue_size, drop_policy=drop_policy)
    egress = BoundedChannel[Packet](
        name="demo_egress",
        maxsize=max(1, int(args.egress_queue_size)),
        drop_policy=DropPolicy(args.egress_drop_policy),
    )

    stats = DemoStats()
    stop_event = asyncio.Event()
    tracemalloc.start()
    start_current, start_peak = tracemalloc.get_traced_memory()
    start_monotonic = time.monotonic()

    producer_task = asyncio.create_task(
        _producer(
            ingress=ingress,
            stop_event=stop_event,
            stats=stats,
            target_rate_hz=float(args.producer_rate_hz),
            put_timeout_s=float(args.put_timeout_s),
        ),
    )
    worker_task = asyncio.create_task(
        _worker(
            ingress=ingress,
            egress=egress,
            stop_event=stop_event,
            stats=stats,
            process_time_ms=float(args.worker_ms),
            get_timeout_s=float(args.get_timeout_s),
            put_timeout_s=float(args.put_timeout_s),
        ),
    )
    sink_task = asyncio.create_task(
        _sink(
            egress=egress,
            stop_event=stop_event,
            stats=stats,
            get_timeout_s=float(args.get_timeout_s),
        ),
    )
    monitor_task = asyncio.create_task(
        _monitor(
            ingress=ingress,
            egress=egress,
            stats=stats,
            stop_event=stop_event,
            interval_s=float(args.monitor_interval_s),
        ),
    )

    await asyncio.sleep(float(args.duration_s))
    stop_event.set()

    for task in [producer_task, worker_task, sink_task]:
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)

    ingress_snapshot = ingress.metrics_snapshot()
    egress_snapshot = egress.metrics_snapshot()
    elapsed = max(0.001, time.monotonic() - start_monotonic)
    end_current, end_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    memory_growth_kib = (float(end_current) - float(start_current)) / 1024.0
    output = {
        "elapsed_s": round(elapsed, 3),
        "producer_rate_hz": float(args.producer_rate_hz),
        "worker_ms": float(args.worker_ms),
        "stats": {
            "produced": stats.produced,
            "produced_dropped": stats.produced_dropped,
            "produced_timeout": stats.produced_timeout,
            "processed": stats.processed,
            "consumed": stats.consumed,
            "throughput_consumed_hz": round(float(stats.consumed) / elapsed, 2),
            "e2e_avg_ms": round(stats.avg_e2e_ms(), 3),
            "e2e_p95_ms": round(stats.p95_e2e_ms(), 3),
        },
        "channels": {
            "ingress": dataclasses.asdict(ingress_snapshot),
            "egress": dataclasses.asdict(egress_snapshot),
        },
        "memory": {
            "tracemalloc_start_current_bytes": start_current,
            "tracemalloc_start_peak_bytes": start_peak,
            "tracemalloc_end_current_bytes": end_current,
            "tracemalloc_end_peak_bytes": end_peak,
            "growth_kib": round(memory_growth_kib, 3),
        },
        "bounded_guarantee": {
            "ingress_max_depth_le_maxsize": ingress_snapshot.max_depth_seen <= ingress_snapshot.maxsize,
            "egress_max_depth_le_maxsize": egress_snapshot.max_depth_seen <= egress_snapshot.maxsize,
        },
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("\nDemo summary")
        print(json.dumps(output, ensure_ascii=False, indent=2))

    checks_ok = True
    if ingress_snapshot.max_depth_seen > ingress_snapshot.maxsize:
        checks_ok = False
    if egress_snapshot.max_depth_seen > egress_snapshot.maxsize:
        checks_ok = False
    if args.max_e2e_p95_ms > 0 and stats.p95_e2e_ms() > float(args.max_e2e_p95_ms):
        checks_ok = False
    if args.max_memory_growth_kib > 0 and memory_growth_kib > float(args.max_memory_growth_kib):
        checks_ok = False

    return 0 if checks_ok else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipelines-runtime-demo")
    parser.add_argument("--duration-s", type=float, default=8.0)
    parser.add_argument("--producer-rate-hz", type=float, default=450.0)
    parser.add_argument("--worker-ms", type=float, default=4.0)
    parser.add_argument("--queue-size", type=int, default=64)
    parser.add_argument("--egress-queue-size", type=int, default=64)
    parser.add_argument(
        "--drop-policy",
        type=str,
        choices=[v.value for v in DropPolicy],
        default=DropPolicy.DROP_OLDEST.value,
    )
    parser.add_argument(
        "--egress-drop-policy",
        type=str,
        choices=[v.value for v in DropPolicy],
        default=DropPolicy.DROP_OLDEST.value,
    )
    parser.add_argument("--put-timeout-s", type=float, default=0.03)
    parser.add_argument("--get-timeout-s", type=float, default=0.2)
    parser.add_argument("--monitor-interval-s", type=float, default=1.0)
    parser.add_argument("--max-e2e-p95-ms", type=float, default=350.0)
    parser.add_argument("--max-memory-growth-kib", type=float, default=4096.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    code = asyncio.run(_run_demo(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
