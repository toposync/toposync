"""Instrumento de latência: persistência real fora do loop, sem inferência biométrica."""

import asyncio
import threading
import time

from scripts.identity.benchmark_pipeline import persist_notification
from toposync.runtime.notifications.store import NotificationStore


def test_notification_storage_does_not_block_event_loop(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    worker_threads = []

    class SlowStore(NotificationStore):
        def upsert(self, **values):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("The event loop could not release storage")
            return super().upsert(**values)

    async def scenario():
        store = SlowStore(tmp_path / "notifications.sqlite3")
        loop_thread = threading.get_ident()
        task = asyncio.create_task(
            persist_notification(store, {"type": "event", "title": "Benchmark fixture"})
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            released_at = time.perf_counter()
            release.set()
            record, created, started, completed = await asyncio.wait_for(task, 2)
            assert len(worker_threads) == 1
            assert worker_threads[0] != loop_thread
            assert started <= released_at <= completed <= time.perf_counter()
            assert created and store.get(record.id).title == "Benchmark fixture"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
