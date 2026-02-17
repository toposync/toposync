from __future__ import annotations

import asyncio
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Literal, TypeVar


T = TypeVar("T")

ExecutionMode = Literal["in_event_loop", "thread_pool", "process_pool", "external"]


@dataclass(slots=True)
class ExecutionScheduler:
    thread_pool_max_workers: int | None = None
    process_pool_max_workers: int | None = None
    _thread_pool: ThreadPoolExecutor | None = field(init=False, default=None)
    _process_pool: ProcessPoolExecutor | None = field(init=False, default=None)
    _semaphores: dict[str, asyncio.Semaphore] = field(init=False, default_factory=dict)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def _ensure_thread_pool(self) -> ThreadPoolExecutor:
        if self._thread_pool is not None:
            return self._thread_pool
        default_workers = min(32, (os.cpu_count() or 4) + 4)
        workers = int(self.thread_pool_max_workers or default_workers)
        self._thread_pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="toposync-pipelines")
        return self._thread_pool

    def _ensure_process_pool(self) -> ProcessPoolExecutor:
        if self._process_pool is not None:
            return self._process_pool
        default_workers = max(1, (os.cpu_count() or 4) // 2)
        workers = int(self.process_pool_max_workers or default_workers)
        self._process_pool = ProcessPoolExecutor(max_workers=max(1, workers))
        return self._process_pool

    async def run_sync(
        self,
        func: Callable[..., T],
        /,
        *args: Any,
        mode: ExecutionMode,
        concurrency_key: str | None = None,
        max_concurrency: int | None = None,
        cancel_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> T:
        semaphore = await self._maybe_get_semaphore(
            concurrency_key=str(concurrency_key or "").strip() or None,
            max_concurrency=max_concurrency,
        )
        acquired = False
        try:
            if semaphore is not None:
                await self._acquire(semaphore, cancel_event=cancel_event)
                acquired = True

            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError

            if mode == "in_event_loop":
                return func(*args, **kwargs)
            if mode == "external":
                raise RuntimeError("execution_mode='external' is not supported by the local runtime scheduler")

            loop = asyncio.get_running_loop()
            call = partial(func, *args, **kwargs)
            if mode == "thread_pool":
                fut = loop.run_in_executor(self._ensure_thread_pool(), call)
            elif mode == "process_pool":
                fut = loop.run_in_executor(self._ensure_process_pool(), call)
            else:
                raise RuntimeError(f"Unknown execution_mode: {mode}")

            if cancel_event is None:
                return await fut

            cancel_task = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait([fut, cancel_task], return_when=asyncio.FIRST_COMPLETED)

            if cancel_task in done:
                # Cancellation does not stop thread/process work. Keep the semaphore held until the
                # underlying work completes to avoid oversubscribing the same resource during shutdown.
                if acquired and semaphore is not None:
                    fut.add_done_callback(lambda _f: semaphore.release())
                    acquired = False
                raise asyncio.CancelledError

            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            return await fut
        finally:
            if acquired and semaphore is not None:
                try:
                    semaphore.release()
                except Exception:
                    pass

    async def shutdown(self) -> None:
        # Best-effort: executors are global-ish and may be shared by multiple runtimes.
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False, cancel_futures=False)
            self._thread_pool = None
        if self._process_pool is not None:
            self._process_pool.shutdown(wait=False, cancel_futures=False)
            self._process_pool = None

    async def _maybe_get_semaphore(self, *, concurrency_key: str | None, max_concurrency: int | None) -> asyncio.Semaphore | None:
        if concurrency_key is None:
            return None
        if max_concurrency is None:
            return None
        limit = int(max_concurrency)
        if limit <= 0:
            return None
        async with self._lock:
            existing = self._semaphores.get(concurrency_key)
            if existing is not None:
                return existing
            sem = asyncio.Semaphore(limit)
            self._semaphores[concurrency_key] = sem
            return sem

    async def _acquire(self, semaphore: asyncio.Semaphore, *, cancel_event: asyncio.Event | None) -> None:
        if cancel_event is None:
            await semaphore.acquire()
            return

        acquire_task = asyncio.create_task(semaphore.acquire())
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait(
            [acquire_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if cancel_task in done:
            acquire_task.cancel()
            raise asyncio.CancelledError
        await asyncio.gather(*pending, return_exceptions=True)
        if acquire_task.cancelled():
            raise asyncio.CancelledError
