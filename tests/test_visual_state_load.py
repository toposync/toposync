"""Shared-executor saturation invariants; synthetic ONNX and controlled clocks, not capacity benchmarks."""
from __future__ import annotations

import asyncio
from collections import Counter
import threading

import pytest

from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler

from test_visual_state_runtime import frame, harness, send, until
from test_visual_state_telemetry import record


@pytest.mark.parametrize("sensor_count", [1, 4, 8])
@pytest.mark.parametrize("finish", ["recover", "cancel"])
def test_shared_executor_bounds_backlog_expires_and_recovers_or_stops(tmp_path, sensor_count, finish):
    async def run():
        scheduler = ExecutionScheduler(thread_pool_max_workers=2)
        sensors, tasks, metrics = [], [], []
        release = threading.Event()
        lock = threading.Lock()
        calls = Counter()
        active = peak = 0

        def wrap(index, original):
            def embed(image):
                nonlocal active, peak
                with lock:
                    calls[index] += 1
                    active += 1
                    peak = max(peak, active)
                try:
                    assert release.wait(10), "test did not release the native inference barrier"
                    return original(image)
                finally:
                    with lock:
                        active -= 1
            return embed

        try:
            for index in range(sensor_count):
                directory = tmp_path / str(index)
                directory.mkdir()
                runtime, context, clock, store, _ = await harness(
                    directory, sensor_id=f"sensor_{index}", scheduler=scheduler)
                sensors.append((runtime, context, clock, store))
                metrics.append(record(context))
                await runtime.prepare(context)
                tasks.append(asyncio.create_task(runtime.run(context)))
            for sequence in (1, 2):
                await asyncio.gather(*(send(runtime, context, clock, sequence) for runtime, context, clock, _ in sensors))
            assert all(runtime._reducer.snapshot().class_id == "red" for runtime, *_ in sensors)
            for index, (runtime, context, clock, _) in enumerate(sensors):
                runtime._backend.embed = wrap(index, runtime._backend.embed)
                clock.now += .26
                await context.inputs["in"].put(frame(clock, 3, "blue"))
            await until(lambda: active == min(2, sensor_count))
            await until(lambda: all(runtime._last_sample == (0, 3) for runtime, *_ in sensors))

            for sequence in range(4, 104):
                for _, context, clock, _ in sensors:
                    clock.now += .002
                    await context.inputs["in"].put(frame(clock, sequence, "blue"))
                    assert context.inputs["in"].depth <= 1
                await until(lambda: all(runtime._last_sample == (0, sequence) for runtime, *_ in sensors))
            # One submitted native job per sensor, regardless of the 100 newer inputs.
            assert scheduler._thread_pool._work_queue.qsize() + active <= sensor_count
            assert sum(calls.values()) == min(2, sensor_count)
            assert all(sum(values["events.local_replaced"]) == 99 for values in metrics)

            for _, _, clock, _ in sensors:
                clock.now += 2.1
            await until(lambda: all(context.packets[-1].payload["visual_state"]["reason"] == "observation_expired"
                                    for _, context, *_ in sensors))
            assert all(context.packets[-1].payload["visual_state"]["class_id"] is None
                       and context.packets[-1].payload["visual_state"]["valid_for_ms"] == 0
                       for _, context, *_ in sensors)
            assert active == min(2, sensor_count), "expiration must not wait for native inference"

            if finish == "cancel":
                for _, context, *_ in sensors:
                    context.cancel_event.set()
                async with asyncio.timeout(3):
                    await asyncio.gather(*tasks)
                assert active == min(2, sensor_count), "logical shutdown must not wait for native inference"
                assert sum(not runtime.resources_released for runtime, *_ in sensors) == min(2, sensor_count)
                return

            release.set()
            await until(lambda: active == 0 and all(values["events.local_expired"] for values in metrics))
            assert all(runtime._reducer.snapshot().class_id is None for runtime, *_ in sensors)
            assert calls == Counter({index: 1 for index in range(sensor_count)})
            for sequence in (104, 105):
                await asyncio.gather(*(send(runtime, context, clock, sequence) for runtime, context, clock, _ in sensors))
                if sequence == 104:
                    assert all(runtime._reducer.snapshot().status == "pending" for runtime, *_ in sensors)
            assert all(runtime._reducer.snapshot().class_id == "red"
                       and runtime._reducer.snapshot().sample.sequence == 105 for runtime, *_ in sensors)
            assert calls == Counter({index: 3 for index in range(sensor_count)})
            assert peak <= 2
        finally:
            release.set()
            for _, context, *_ in sensors:
                context.cancel_event.set()
            await asyncio.gather(*tasks)
            pool = scheduler._thread_pool
            await scheduler.shutdown()
            if pool is not None:
                await asyncio.to_thread(pool.shutdown, wait=True)
            for runtime, _, _, store in sensors:
                await runtime.shutdown()
                store.close()
            assert all(runtime.resources_released for runtime, *_ in sensors)

    asyncio.run(run())
