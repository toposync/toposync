from toposync.runtime.notifications import NotificationsRuntime


def test_recovery_limit_applies_to_top_level_open_pipeline_records(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    for key, payload, timestamp in [
        ("open", {"source": "pipelines", "status": "open"}, 1),
        ("closed", {"source": "pipelines", "status": "closed", "evidence": {"status": "open"}}, 2),
        ("other", {"source": "other", "status": "open"}, 3),
    ]:
        runtime.store.upsert(
            type="pipelines.event", title=key, payload=payload, dedupe_key=key, now=timestamp
        )
    records = runtime.store.list_open_pipeline_notifications(limit=1)
    assert [record.dedupe_key for record in records] == ["open"]


def test_recovery_drains_all_batches_and_is_idempotent(tmp_path):
    import asyncio

    async def scenario():
        runtime = NotificationsRuntime(data_dir=tmp_path)
        for index in range(3):
            runtime.store.upsert(
                type="pipelines.event",
                title=str(index),
                payload={
                    "source": "pipelines",
                    "status": "open",
                    "event": {"duration_seconds": 1},
                    "reference": f"clip-{index}",
                },
                dedupe_key=f"event-{index}",
            )
        assert await runtime.close_open_pipeline_notifications(batch_size=1) == 3
        records, _ = runtime.store.list()
        assert len(records) == 3
        for record in records:
            assert record.payload["status"] == "closed"
            assert record.payload["event"] == {"duration_seconds": 1}
            assert record.payload["reference"] == f"clip-{record.title}"
        assert await runtime.close_open_pipeline_notifications(batch_size=1) == 0

    asyncio.run(scenario())
