from __future__ import annotations

import asyncio
import os
import multiprocessing
import math
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
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
    _serial_process_pools: dict[str, ProcessPoolExecutor] = field(init=False, default_factory=dict)
    _terminable_process_pools: set[ProcessPoolExecutor] = field(init=False, default_factory=set)
    _semaphores: dict[str, asyncio.Semaphore] = field(init=False, default_factory=dict)
    _semaphore_limits: dict[str, int] = field(init=False, default_factory=dict)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def _ensure_thread_pool(self) -> ThreadPoolExecutor:
        if self._thread_pool is not None:
            return self._thread_pool
        default_workers = min(32, (os.cpu_count() or 4) + 4)
        workers = int(self.thread_pool_max_workers or default_workers)
        self._thread_pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="toposync-pipelines")
        return self._thread_pool

    def _ensure_process_pool(self, key: str | None = None) -> ProcessPoolExecutor:
        if key is not None:
            # Um trabalhador por chave preserva caches nativos sem multiplicar modelos.
            if key not in self._serial_process_pools:
                self._serial_process_pools[key] = ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn")
                )
            return self._serial_process_pools[key]
        if self._process_pool is not None:
            return self._process_pool
        default_workers = max(1, (os.cpu_count() or 4) // 2)
        workers = int(self.process_pool_max_workers or default_workers)
        self._process_pool = ProcessPoolExecutor(max_workers=max(1, workers))
        return self._process_pool

    def _discard_broken_process_pool(self, pool: ProcessPoolExecutor) -> None:
        # Só a chamada seguinte cria outro executor; não repetir trabalho com efeitos.
        if self._process_pool is pool:
            self._process_pool = None
        for key, existing in tuple(self._serial_process_pools.items()):
            if existing is pool:
                del self._serial_process_pools[key]
        self._terminable_process_pools.discard(pool)
        pool.shutdown(wait=False, cancel_futures=True)

    def _terminate_process_pool(self, pool: ProcessPoolExecutor) -> None:
        # API pública desde Python 3.14; compatibilidade localizada para 3.11–3.13.
        kill_workers = getattr(pool, "kill_workers", None)
        if callable(kill_workers):
            kill_workers()
        else:
            processes = getattr(pool, "_processes", None)
            if processes is not None:
                for process in tuple(processes.values()):
                    if process.is_alive():
                        process.kill()
        self._discard_broken_process_pool(pool)

    async def run_sync(
        self,
        func: Callable[..., T],
        /,
        *args: Any,
        mode: ExecutionMode,
        concurrency_key: str | None = None,
        max_concurrency: int | None = None,
        cancel_event: asyncio.Event | None = None,
        process_pool_key: str | None = None,
        process_pool_deadline_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        if process_pool_key is not None:
            if (
                mode != "process_pool"
                or not process_pool_key.strip()
                or concurrency_key != process_pool_key
                or max_concurrency != 1
            ):
                raise ValueError("Named process pools require the same concurrency key and limit one")
        if process_pool_deadline_seconds is not None:
            if (
                process_pool_key is None
                or concurrency_key != process_pool_key
                or max_concurrency != 1
                or not math.isfinite(process_pool_deadline_seconds)
                or process_pool_deadline_seconds <= 0
            ):
                raise ValueError("Process deadline requires an exclusive named pool and positive finite seconds")
        semaphore = await self._maybe_get_semaphore(
            concurrency_key=str(concurrency_key or "").strip() or None,
            max_concurrency=max_concurrency,
            enforce_limit=process_pool_key is not None,
        )
        acquired = False
        fut: asyncio.Future[Any] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        process_pool: ProcessPoolExecutor | None = None
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
                process_pool = self._ensure_process_pool(process_pool_key)
                if process_pool_deadline_seconds is not None:
                    if not callable(getattr(process_pool, "kill_workers", None)) and not isinstance(
                        getattr(process_pool, "_processes", None), dict
                    ):
                        raise RuntimeError("This Python executor cannot enforce a process deadline")
                    self._terminable_process_pools.add(process_pool)
                try:
                    fut = loop.run_in_executor(process_pool, call)
                except BrokenProcessPool:
                    self._discard_broken_process_pool(process_pool)
                    raise

                def observe_process_failure(completed: asyncio.Future[Any]) -> None:
                    if not completed.cancelled() and isinstance(completed.exception(), BrokenProcessPool):
                        self._discard_broken_process_pool(process_pool)

                # Também recuperar quando o chamador já recebeu timeout/cancelamento.
                fut.add_done_callback(observe_process_failure)
                if process_pool_deadline_seconds is not None:
                    def enforce_process_deadline() -> None:
                        if not fut.done():
                            self._terminate_process_pool(process_pool)

                    deadline = loop.call_later(process_pool_deadline_seconds, enforce_process_deadline)
                    fut.add_done_callback(lambda _completed: deadline.cancel())
            else:
                raise RuntimeError(f"Unknown execution_mode: {mode}")

            if cancel_event is None:
                # Cancelar o chamador não interrompe a função na thread/processo.
                return await asyncio.shield(fut)

            cancel_task = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                [fut, cancel_task], return_when=asyncio.FIRST_COMPLETED
            )

            if cancel_task in done:
                raise asyncio.CancelledError

            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            if fut is not None:
                held_semaphore = semaphore if acquired else None

                def release_when_finished(completed: asyncio.Future[Any]) -> None:
                    if held_semaphore is not None:
                        held_semaphore.release()
                    if not completed.cancelled():
                        completed.exception()  # O chamador cancelado não observará o resultado.

                fut.add_done_callback(release_when_finished)
                acquired = False
            raise
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
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
        for pool in tuple(self._serial_process_pools.values()):
            if pool in self._terminable_process_pools:
                self._terminate_process_pool(pool)
            else:
                pool.shutdown(wait=False, cancel_futures=False)
        self._serial_process_pools.clear()

    async def _maybe_get_semaphore(self, *, concurrency_key: str | None, max_concurrency: int | None, enforce_limit: bool = False) -> asyncio.Semaphore | None:
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
                if enforce_limit and self._semaphore_limits[concurrency_key] != limit:
                    raise ValueError("Named process pool concurrency key has an incompatible limit")
                return existing
            sem = asyncio.Semaphore(limit)
            self._semaphores[concurrency_key] = sem
            self._semaphore_limits[concurrency_key] = limit
            return sem

    async def _acquire(self, semaphore: asyncio.Semaphore, *, cancel_event: asyncio.Event | None) -> None:
        if cancel_event is None:
            await semaphore.acquire()
            return

        acquire_task = asyncio.create_task(semaphore.acquire())
        cancel_task = asyncio.create_task(cancel_event.wait())
        transferred = False
        try:
            done, _pending = await asyncio.wait(
                [acquire_task, cancel_task], return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                raise asyncio.CancelledError
            await acquire_task
            transferred = True
        finally:
            for task in (acquire_task, cancel_task):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
            except asyncio.CancelledError:
                transferred = False
                raise
            finally:
                # Aquisição e cancelamento podem acontecer no mesmo ciclo do event loop.
                if not transferred and acquire_task.done() and not acquire_task.cancelled() and acquire_task.result():
                    semaphore.release()
