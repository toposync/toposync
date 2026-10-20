import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from toposync.runtime.notifications.occurrences import Contribution, Enrichment, OccurrenceConflict, OccurrenceStore
from toposync.runtime.notifications.runtime import NotificationsRuntime


@pytest.fixture(autouse=True)
def synthetic_clock(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1000.0)


def contribution(message='open', **changes):
    return Contribution.model_validate({
        'message_id': message, 'objective_id': 'presence-garage', 'episode_id': 'group-episode',
        'title': 'People in the garage', 'kind': 'presence',
        'scope': {'camera_ids': ['front', 'garage'], 'area_ids': ['garage-area']},
        'participant_id': 'entity-1', 'member_id': 'visit-1', 'category': 'person',
        'camera_id': 'front', 'source_id': 'front-main', 'lifecycle': 'OPEN', 'observed_at': 10.0,
        'clock_domain': 'fixture-shared-clock', 'evidence_id': message,
        'capture_instance': 'fixture-capture', 'capture_generation': 1, 'frame_sequence': 1,
        'position': [1.0, 2.0], 'coordinate_reference': 'fixture-metres', 'mapping_revision': 'fixture-map-1', **changes,
    })


def enrichment(identifier, **changes):
    return Enrichment.model_validate({
        'message_id': 'late-result', 'occurrence_id': identifier,
        'evidence_id': 'open', 'observed_at': 10.0, 'participant_id': 'entity-1',
        'privacy_revision': 1, 'kind': 'context', 'text': 'A parcel is visible.', **changes,
    })


def record(store, value=None, **kwargs):
    return store.record(value or contribution(), retention_seconds=60.0, now=1000.0, **kwargs)


@pytest.mark.parametrize('category', ['person', 'car'])
def test_independent_participants_two_cameras_one_occurrence_and_one_simulated_notice(tmp_path, category):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        async def send(value):
            return await runtime.record_occurrence(value.model_copy(update={'category': category}), retention_seconds=60.0, request_notice=True, now=1000.0)
        first = await send(contribution())
        second = await send(contribution('second', participant_id='entity-2', member_id='visit-2'))
        camera = await send(contribution('garage', camera_id='garage', source_id='garage-main',
            member_id='visit-1b', observed_at=12.0))
        assert first['id'] == second['id'] == camera['id']
        assert set(camera['participants']) == {'entity-1', 'entity-2'}
        assert all(item['category'] == category for item in camera['participants'].values())
        assert len(runtime.occurrences.deliveries(first['id'])) == 1
        assert runtime.occurrences.deliveries(first['id'])[0]['status'] == 'confirmed'
        assert runtime.occurrences.deliveries(first['id'])[0]['simulated'] is True
        for member in ['visit-1', 'visit-1b', 'visit-2']:
            closed = await send(contribution('close-' + member, member_id=member,
                participant_id='entity-2' if member == 'visit-2' else 'entity-1',
                lifecycle='CLOSE', observed_at=15.0, reason='left_scope'))
        assert closed['status'] == 'CLOSE' and closed['close_reason'] == 'left_scope'
        notifications, _ = runtime.list_sync()
        assert len(notifications) == 1 and notifications[0]['payload']['occurrence_id'] == first['id']
        assert len(list(tmp_path.rglob('*.sqlite3'))) == 1
    asyncio.run(scenario())


def test_overlapping_objectives_keep_distinct_effects_and_replay_is_idempotent(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    first = record(store, request_notice=True)
    other = record(store, contribution(objective_id='person-near-pool'), request_notice=True)
    assert first.document['id'] != other.document['id']
    repeated = record(store, contribution(replay=True), request_notice=True)
    assert not repeated.changed and repeated.requested_delivery is None
    assert len(store.deliveries(first.document['id'])) == len(store.deliveries(other.document['id'])) == 1
    replay_only = record(store, contribution(episode_id='recorded-episode', replay=True), request_notice=True)
    assert store.deliveries(replay_only.document['id']) == []


def test_late_admission_is_explicit_atomic_and_cannot_bypass_expiry(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    value = contribution(lifecycle='UPDATE', started_at=1.0, observed_at=10.0)
    for changes in ({}, {'admit_live_update': True, 'replay': True}):
        replay = changes.get('replay', False)
        with pytest.raises(OccurrenceConflict, match='requires_open'):
            record(store, value.model_copy(update={'replay': replay}),
                   admit_live_update=changes.get('admit_live_update', False))
    first = record(store, value, admit_live_update=True)
    assert first.document['admitted_at'] == 10
    assert not record(store, value, admit_live_update=True).changed
    with pytest.raises(OccurrenceConflict, match='message_changed'):
        record(store, value.model_copy(update={'observed_at': 11.0}), admit_live_update=True)
    with pytest.raises(OccurrenceConflict, match='unavailable'):
        store.record(value, retention_seconds=60.0, admit_live_update=True, now=1060.0)


def test_late_enrichment_after_close_preserves_observed_time_and_does_not_repeat_notice(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        first = await runtime.record_occurrence(contribution(), retention_seconds=60.0, request_notice=True, now=1000.0)
        closed = await runtime.record_occurrence(contribution('close', lifecycle='CLOSE', observed_at=12.0),
            retention_seconds=60.0, request_notice=True, now=1002.0)
        result = await runtime.enrich_occurrence(enrichment(first['id']), now=1010.0)
        repeated = await runtime.enrich_occurrence(enrichment(first['id']), now=1011.0)
        for field in ('status', 'started_at', 'last_observed_at', 'ended_at', 'duration_seconds', 'evidence', 'expires_at'):
            assert result[field] == closed[field]
        assert result['revision'] == closed['revision'] + 1 == repeated['revision']
        assert len(runtime.occurrences.deliveries(first['id'])) == 1
        original = runtime.store.list()[0][0]
        assert original.payload['status'] == 'closed'
        assert original.payload['data']['occurrence']['enrichments'][0]['observed_at'] == 10.0
        ignored = await runtime.record_occurrence(contribution('stale-update', lifecycle='UPDATE', observed_at=99.0),
            retention_seconds=60.0, request_notice=True, now=1012.0)
        assert ignored['revision'] == result['revision']
    asyncio.run(scenario())


def test_fast_path_finishes_before_slow_enrichment(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        release = asyncio.Event()
        identifier = OccurrenceStore.identifier('presence-garage', 'group-episode')
        async def slow():
            await release.wait()
            return await runtime.enrich_occurrence(enrichment(identifier), now=1005.0)
        task = asyncio.create_task(slow())
        first = await asyncio.wait_for(runtime.record_occurrence(contribution(),
            retention_seconds=60.0, request_notice=True, now=1000.0), timeout=1)
        assert not task.done()
        assert runtime.occurrences.deliveries(first['id'])[0]['status'] == 'confirmed'
        release.set()
        assert (await task)['revision'] == 2
    asyncio.run(scenario())


def test_concurrent_duplicate_records_request_only_one_effect(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: record(store, request_notice=True), range(8)))
    assert sum(result.changed for result in results) == 1
    assert sum(result.requested_delivery is not None for result in results) == 1
    assert len(store.revisions(results[0].document['id'])) == 1


def test_restart_retains_identifiers_closes_at_last_observation_and_does_not_retry_unknown_delivery(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    first = record(runtime.occurrences, request_notice=True)
    restored = NotificationsRuntime(data_dir=tmp_path).occurrences
    assert restored.recover(now=1010.0) == 1
    document = restored.get(first.document['id'], read_filter=lambda _: True, now=1010.0)
    assert document['status'] == 'CLOSE' and document['ended_at'] == 10.0
    assert document['duration_seconds'] == 0 and document['close_reason'] == 'runtime_restart'
    assert restored.deliveries(document['id'])[0]['status'] == 'unknown'
    assert not record(restored, request_notice=True).changed
    with pytest.raises(OccurrenceConflict, match='already_resolved'):
        restored.finish_delivery(first.requested_delivery, 'confirmed')


@pytest.mark.parametrize('status', ['confirmed', 'failed', 'unknown'])
def test_delivery_resolution_is_final_and_idempotent(tmp_path, status):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    first = record(store, request_notice=True)
    receipt = store.finish_delivery(first.requested_delivery, status, reason='synthetic', now=1001.0)
    assert receipt == store.finish_delivery(first.requested_delivery, status, reason='synthetic', now=1002.0)
    with pytest.raises(OccurrenceConflict, match='already_resolved'):
        store.finish_delivery(first.requested_delivery, 'failed' if status == 'confirmed' else 'confirmed')


def test_enrichment_revocation_and_event_expiry_remove_projection_and_block_late_results(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    store = runtime.occurrences
    first = record(store)
    rich = store.enrich(enrichment(first.document['id']), now=1002.0)
    store.publish(first.document['id'], now=1002.0)
    revoked = store.revoke_enrichment(first.document['id'], expected_revision=rich.document['revision'], now=1003.0)
    assert revoked.document['enrichments'] == [] and revoked.document['privacy_revision'] == 2
    assert runtime.store.list()[0] == []
    with pytest.raises(OccurrenceConflict, match='privacy_changed'):
        store.enrich(enrichment(first.document['id'], message_id='late-queued'), now=1004.0)
    store.publish(first.document['id'], now=1004.0)
    assert store.get(first.document['id'], read_filter=lambda _: True, now=1060.0)['status'] == 'expired'
    assert len(store.expire(now=1060.0)) == 1
    assert runtime.store.list()[0] == []
    tombstone = json.loads(runtime.store._conn.execute('SELECT document_json FROM occurrence').fetchone()[0])
    assert 'participants' not in tombstone and 'enrichments' not in tombstone and 'evidence' not in tombstone
    assert tombstone['created_at'] == tombstone['expires_at'] == 1060.0
    assert store.publish(first.document['id'], now=1061.0) is None
    with pytest.raises(OccurrenceConflict, match='unavailable'):
        record(store)


def test_read_scope_precedes_limit_and_queries_by_place_participant_and_time(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    permitted = record(store).document
    record(store, contribution(objective_id='private', scope={'camera_ids': ['back']}, camera_id='back'))
    read = lambda document: 'back' not in document['access_scope']['camera_ids']
    documents, cursor = store.list(read_filter=read, limit=1, now=1001.0, camera_id='front', participant_id='entity-1', created_after=999.0, created_before=1001.0)
    assert [item['id'] for item in documents] == [permitted['id']] and cursor is None
    assert store.list(read_filter=read, area_id='elsewhere', now=1001.0)[0] == []


def test_transaction_rolls_back_event_receipt_and_revision_on_full_disk(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    runtime.store._conn.execute("CREATE TRIGGER synthetic_full_disk BEFORE INSERT ON occurrence_revision BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
    with pytest.raises(sqlite3.DatabaseError, match='disk is full'):
        record(runtime.occurrences, request_notice=True)
    for table in ('occurrence', 'occurrence_revision', 'occurrence_delivery', 'occurrence_message', 'notification'):
        assert runtime.store._conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('change', [{'camera_id': 'outside'}, {'observed_at': float('nan')},
    {'position': [float('inf'), 0]}, {'position': [1, 2], 'coordinate_reference': None}])
def test_invalid_provenance_is_rejected(change):
    with pytest.raises(ValidationError):
        contribution(**change)


def test_evidence_changes_and_incompatible_clocks_do_not_invent_duration(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    first = record(store)
    with pytest.raises(OccurrenceConflict, match='message_changed'):
        record(store, contribution(observed_at=20.0))
    with pytest.raises(OccurrenceConflict, match='evidence_changed'):
        record(store, contribution('other', evidence_id='open', observed_at=20.0))
    changed = record(store, contribution('clock', lifecycle='UPDATE', clock_domain='unrelated-clock', observed_at=999.0))
    assert changed.document['duration_seconds'] is None
    assert changed.document['duration_status'] == 'incomparable_clocks'
    assert len(store.revisions(first.document['id'])) == 2


def test_recognition_arrival_is_revisioned_without_copying_private_names(tmp_path):
    store = NotificationsRuntime(data_dir=tmp_path).occurrences
    first = record(store)
    with pytest.raises(ValidationError, match='names are not copied'):
        enrichment(first.document['id'], kind='recognition', text='Private name')
    reviewed = store.enrich(enrichment(first.document['id'], kind='recognition', text=None), now=1005.0)
    assert reviewed.document['revision'] == 2
    assert reviewed.document['enrichments'][0]['kind'] == 'recognition'
    assert reviewed.document['enrichments'][0]['text'] is None
    assert reviewed.document['participants']['entity-1']['recognition_status'] == 'unknown'


def test_record_only_is_activity_without_notification_or_delivery(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        queue = runtime.broadcaster.subscribe()
        document = await runtime.record_occurrence(contribution(), retention_seconds=60.0, now=1000.0)
        assert runtime.list_sync()[0] == []
        assert runtime.occurrences.deliveries(document['id']) == []
        assert runtime.occurrences.list(read_filter=lambda _: True, now=1001.0)[0][0]['id'] == document['id']
        assert queue.empty()
        runtime.broadcaster.unsubscribe(queue)
    asyncio.run(scenario())


def test_periodic_retention_expires_without_read_and_awaits_pending_purge_on_stop(tmp_path, monkeypatch):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        first = await runtime.record_occurrence(contribution(), retention_seconds=60, request_notice=True, now=1000)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def purge(identifier):
            calls.append(identifier)
            started.set()
            await release.wait()
        runtime.invalidate_occurrence_derivatives = purge
        original_sleep = asyncio.sleep
        delays = []
        async def advance(seconds):
            delays.append(seconds)
            assert seconds == 60
            await original_sleep(0)
        monkeypatch.setattr(asyncio, 'sleep', advance)
        monkeypatch.setattr('time.time', lambda: 1061)
        task = asyncio.create_task(runtime.maintain_occurrences())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await original_sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert calls == [first['id']] and delays == [60]
        assert runtime.store.list()[0] == []
        assert runtime.occurrences.get(first['id'], read_filter=lambda _: True)['status'] == 'expired'
    asyncio.run(scenario())


def test_retention_failure_waits_for_next_interval_instead_of_retrying_in_a_loop(tmp_path, monkeypatch):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        waiting = asyncio.Event()
        delays, calls = [], []
        original_sleep = asyncio.sleep
        async def advance(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                waiting.set()
                await asyncio.Event().wait()
            await original_sleep(0)
        async def fail():
            calls.append(1)
            raise OSError('synthetic storage failure')
        monkeypatch.setattr(asyncio, 'sleep', advance)
        monkeypatch.setattr(runtime, 'expire_occurrences', fail)
        task = asyncio.create_task(runtime.maintain_occurrences())
        await asyncio.wait_for(waiting.wait(), 1)
        assert delays == [60, 60] and calls == [1]
        assert runtime.retention_error == 'occurrence_retention_unavailable'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_revocation_scrubs_projection_without_new_notification_time_unread_or_notice(tmp_path, monkeypatch):
    from toposync_ext_ai.text_embeddings import TextHistorySearch
    runtime = NotificationsRuntime(data_dir=tmp_path)
    async def scenario():
        first = await runtime.record_occurrence(contribution(), retention_seconds=60.0, request_notice=True, now=1000.0)
        rich = await runtime.enrich_occurrence(enrichment(first['id']), now=1005.0)
        original = runtime.list_sync()[0][0]
        await runtime.mark_all_viewed()
        search = TextHistorySearch(runtime, None)
        search.cache('synthetic', {original['id']: ('hash', 'private context')}, {original['id']: [1.0, 0.0]})
        queue = runtime.broadcaster.subscribe()
        runtime.broadcaster.publish({'op': 'update', 'notification': original})
        monkeypatch.setattr('time.time', lambda: 1006.0)
        await runtime.revoke_occurrence_enrichment(first['id'], expected_revision=rich['revision'])
        current = runtime.list_sync()[0][0]
        assert current['id'] == original['id'] and current['createdAt'] == original['createdAt']
        assert current['updatedAt'] != original['updatedAt']
        assert current['payload']['event'] == original['payload']['event']
        assert current['payload']['data']['occurrence']['enrichments'] == []
        assert runtime.count_summary_sync()['unread_total'] == 0
        assert len(runtime.occurrences.deliveries(first['id'])) == 1
        assert runtime.store._conn.execute('SELECT count(*) FROM ai_notification_embedding').fetchone()[0] == 0
        assert queue.get_nowait() == {'op': 'invalidate', 'notification_id': original['id'], 'occurrence_id': first['id']}
        event = queue.get_nowait()
        assert queue.empty() and event['op'] == 'update' and event['notification']['id'] == original['id']
        assert event['notification']['payload']['data']['occurrence']['enrichments'] == []
        runtime.broadcaster.unsubscribe(queue)
    asyncio.run(scenario())
