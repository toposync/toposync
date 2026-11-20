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


def test_two_sources_deliver_distinct_visits_to_shared_subscriber(tmp_path, monkeypatch):
    import hashlib
    from PIL import Image
    from scripts.identity import benchmark_pipeline as benchmark

    photos = []
    for index, species in enumerate(("person", "cat", "dog")):
        path = tmp_path / f"{species}.png"
        Image.new("RGB", (16, 16), (index, 0, 0)).save(path)
        photos.append((species, path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    monkeypatch.setattr(benchmark, "SOURCES", photos)
    report = asyncio.run(benchmark.trial(
        tmp_path, tmp_path / "results", enabled=False, published=True, sources=2, visits=3, interval=.02
    ))
    assert report["measured_visits"] == report["warmup_visits_excluded"] == 6
    assert report["measured_packets"] == 18
    for source in range(2):
        rows = [row for row in report["per_packet_timings"] if row["source_index"] == source]
        assert len(rows) == 9
        rates = report["actual_rates_per_source"][source]
        for field, rate in (("emitted_s", "emitted_hz"), ("delivered_s", "delivered_hz")):
            times = [row[field] for row in rows]
            assert rates[rate] == (len(rows) - 1) / (max(times) - min(times))
        assert {row["species"] for row in rows} == {"person", "cat", "dog"}
        assert [row["phase"] for row in rows] == ["open", "update", "close"] * 3

    # Coalescing all visits into one record must fail even when every callback arrives.
    import json
    import pytest

    persist = NotificationStore.upsert

    def coalesce(store, **values):
        return persist(store, **{**values, "dedupe_key": "one-key", "payload": {}})

    monkeypatch.setattr(NotificationStore, "upsert", coalesce)
    output = tmp_path / "coalesced"
    with pytest.raises(AssertionError):
        asyncio.run(benchmark.trial(tmp_path, output, enabled=False, sources=2, visits=3, interval=.02))
    failure = json.loads((output / "failure.json").read_text())
    assert failure["warmup_complete"] and failure["source_packets_emitted"] == 36
    assert failure["notification_callbacks"] == 36 and failure["notification_incomplete_visits"] == {}
    assert failure["notifications_created"] == failure["distinct_notifications"] == 1
    assert failure["recognition_incomplete_visits"] is None
