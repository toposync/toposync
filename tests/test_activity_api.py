from functools import partial
from urllib.parse import urljoin

import pytest

from test_auth_local import _create_client, _setup_owner
from test_occurrences import contribution, enrichment


@pytest.fixture
def activity(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        result = client.portal.call(partial(client.app.state.services.call, 'occurrences.record',
            contribution=contribution(), retention_seconds=60.0, request_notice=True))
        yield client, result


def test_activity_api_exposes_persisted_history_and_controlled_relative_manifest(activity):
    client, result = activity
    response = client.get('/api/activity', params={'participant_id': 'entity-1', 'camera_id': 'front'})
    assert response.status_code == 200
    assert response.json()['occurrences'][0]['id'] == result['id']
    assert response.json()['coverage']['complete_day'] is False
    detail = client.get('/api/activity/' + result['id']).json()
    assert detail['permissions']['manage'] is True
    assert detail['revisions'][0]['kind'] == 'OPEN'
    assert detail['deliveries'][0]['status'] == 'confirmed' and detail['deliveries'][0]['simulated']
    manifest = client.get('/api/activity/' + result['id'] + '/manifest')
    assert manifest.headers['cache-control'] == 'private, no-store'
    assert manifest.json()['name'] == 'People in the garage'
    assert manifest.json()['coverage']['continuous_video'] is False
    reference = manifest.json()['evidence'][0]
    assert reference['uses'] == [result['id']] and reference['kind'] == 'observation_metadata'
    assert client.get(urljoin(str(manifest.request.url), reference['href'])).json()['id'] == 'open'
    assert urljoin('https://local.invalid/api/hassio_ingress/test/api/activity/id/manifest', reference['href']).startswith(
        'https://local.invalid/api/hassio_ingress/test/api/activity/id/evidence?')
    assert client.get('/api/activity/' + result['id'] + '/evidence', params={'evidence_id': '../../secret'}).status_code == 404


def test_retention_maintenance_failure_is_visible_without_exposing_internal_exception(activity):
    client, result = activity
    client.app.state.notifications.retention_error = 'occurrence_retention_unavailable'
    for path in ('/api/activity', '/api/activity/' + result['id'], '/api/activity/' + result['id'] + '/manifest'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()['maintenance_error'] == 'occurrence_retention_unavailable'


def test_renaming_labels_preserves_occurrence_evidence_links_and_delivery(activity):
    client, document = activity
    history = client.app.state.notifications
    path = '/api/activity/' + document['id']
    original = client.get(path + '/manifest').json()
    labels = {'title': 'Renamed activity', 'camera_name': 'Renamed front', 'source_name': 'Renamed source'}
    duplicate = client.portal.call(partial(history.record_occurrence, contribution(**labels), retention_seconds=60.0, request_notice=True))
    assert duplicate['id'] == document['id'] and duplicate['revision'] == document['revision']
    updated = client.portal.call(partial(history.record_occurrence, contribution('renamed', lifecycle='UPDATE',
        observed_at=11.0, frame_sequence=2, **labels), retention_seconds=60.0, request_notice=True))
    assert updated['id'] == document['id']
    manifest = client.get(path + '/manifest').json()
    assert manifest['name'] == labels['title'] and manifest['deliveries'] == original['deliveries']
    assert manifest['evidence'][0] == original['evidence'][0]
    assert 'Renamed front' in manifest['evidence'][1]['name']
    reference = original['evidence'][0]
    assert client.get(urljoin(str(client.base_url) + path + '/manifest', reference['href'])).json()['id'] == 'open'
    closed = contribution('renamed-close', evidence_id='renamed', lifecycle='CLOSE', observed_at=11.0, frame_sequence=2,
        **{**labels, 'camera_name': 'Another label'})
    final = client.portal.call(partial(history.record_occurrence, closed, retention_seconds=60.0, request_notice=True))
    assert final['status'] == 'CLOSE' and final['duration_seconds'] == 1
    assert final['evidence']['renamed']['camera_name'] == 'Renamed front'


def test_api_rechecks_all_cameras_for_counts_metadata_export_and_mutation(activity):
    client, result = activity
    user = client.post('/api/access/users', json={'username': 'limited', 'display_name': 'Limited',
        'role': 'member', 'password': 'password123'}).json()
    assert client.post(f"/api/access/users/{user['id']}/grants", json={'action': 'core:camera:read',
        'resource_type': 'core:camera', 'include': ['front'], 'exclude': []}).status_code == 200
    assert client.post('/api/auth/login', json={'username': 'limited', 'password': 'password123', 'device_label': 'test'}).status_code == 200
    assert client.get('/api/activity').json()['occurrences'] == []
    for suffix in ('', '/manifest', '/evidence?evidence_id=open'):
        assert client.get('/api/activity/' + result['id'] + suffix).status_code == 404
    assert client.get('/api/notifications/count').json()['total'] == 0
    response = client.request('DELETE', '/api/activity/' + result['id'], json={'expected_revision': 1})
    assert response.status_code == 403
    client.app.state.auth.store.upsert_grant(user_id=user['id'], action='core:camera:read',
        resource_type='core:camera', include=['front', 'garage'], exclude=[])
    visible = client.get('/api/activity/' + result['id'])
    assert visible.status_code == 200 and visible.json()['permissions']['manage'] is False
    client.app.state.auth.store.upsert_grant(user_id=user['id'], action='core:camera:read',
        resource_type='core:camera', include=['front', 'garage'], exclude=['garage'])
    assert client.get('/api/activity/' + result['id'] + '/manifest').status_code == 404


def test_delete_requires_current_revision_removes_derived_history_and_blocks_late_enrichment(activity):
    from toposync.runtime.notifications.occurrences import OccurrenceConflict
    client, result = activity
    path = '/api/activity/' + result['id']
    assert client.request('DELETE', path, json={'expected_revision': 99}).status_code == 409
    assert client.request('DELETE', path, json={'expected_revision': 1}).json()['external_messages_retracted'] is False
    assert client.get(path).status_code == 410
    assert client.get(path + '/manifest').status_code == 410
    assert client.get('/api/notifications').json()['notifications'] == []
    assert client.get('/api/activity').json()['occurrences'] == []
    with pytest.raises(OccurrenceConflict, match='unavailable'):
        client.portal.call(partial(client.app.state.services.call, 'occurrences.enrich', enrichment=enrichment(result['id'])))


def test_delete_purges_pending_delivery_and_embedding_cache(activity):
    from toposync_ext_ai.text_embeddings import TextHistorySearch
    client, result = activity
    runtime = client.app.state.notifications
    notification = runtime.list_sync()[0][0]
    search = TextHistorySearch(runtime, None)
    search.cache('synthetic-space', {notification['id']: ('hash', 'synthetic text')}, {notification['id']: [1.0, 0.0]})
    async def queue_pending():
        queue = runtime.broadcaster.subscribe()
        runtime.broadcaster.publish({'op': 'update', 'notification': notification})
        return queue
    queue = client.portal.call(queue_pending)
    assert queue.qsize() == 1
    assert client.request('DELETE', '/api/activity/' + result['id'], json={'expected_revision': 1}).status_code == 200
    assert queue.get_nowait() == {'op': 'invalidate', 'notification_id': notification['id'], 'occurrence_id': result['id']}
    assert queue.empty()
    assert runtime.store._conn.execute('SELECT count(*) FROM ai_notification_embedding').fetchone()[0] == 0
    client.portal.call(runtime.broadcaster.unsubscribe, queue)


def test_revoke_keeps_observed_facts_and_rejects_pending_enrichment(activity):
    from toposync.runtime.notifications.occurrences import OccurrenceConflict
    client, result = activity
    rich = client.portal.call(partial(client.app.state.services.call, 'occurrences.enrich', enrichment=enrichment(result['id'])))
    path = '/api/activity/' + result['id']
    response = client.post(path + '/revoke-enrichment', json={'expected_revision': rich['revision']})
    assert response.status_code == 200
    document = response.json()['occurrence']
    assert document['participants'] == result['participants'] and document['evidence'] == result['evidence']
    assert document['enrichments'] == [] and document['privacy_revision'] == 2
    with pytest.raises(OccurrenceConflict, match='privacy_changed'):
        client.portal.call(partial(client.app.state.services.call, 'occurrences.enrich',
            enrichment=enrichment(result['id'], message_id='pending')))


def test_activity_expiration_is_enforced_before_sweep(activity, monkeypatch):
    client, result = activity
    monkeypatch.setattr('time.time', lambda: result['expires_at'])
    # Notification reads cannot expose an expired projection even before a sweep runs.
    assert client.get('/api/notifications').json()['notifications'] == []
    assert client.get('/api/notifications/count').json()['total'] == 0
    assert client.get('/api/activity/' + result['id']).status_code == 410
    assert client.app.state.notifications.store._conn.execute('SELECT count(*) FROM notification').fetchone()[0] == 0


@pytest.mark.parametrize('query', [{'created_after': 10, 'created_before': 10},
    {'created_after': 'nan'}, {'camera_id': '*'}, {'limit': 0}])
def test_activity_rejects_invalid_queries(activity, query):
    client, _ = activity
    assert client.get('/api/activity', params=query).status_code == 422


def test_observed_queries_match_one_authorized_sample_not_configured_cameras_or_receipt_time(activity):
    client, result = activity
    query = {'observed_after': 10, 'observed_before': 11, 'clock_domain': 'fixture-shared-clock',
             'participant_id': 'entity-1', 'camera_id': 'front'}
    response = client.get('/api/activity', params=query)
    assert response.status_code == 200, response.text
    assert [item['id'] for item in response.json()['occurrences']] == [result['id']]
    assert response.json()['coverage']['time_filter'] == 'observed_at'
    assert response.json()['coverage']['matches'] == 'individual_observations'
    combined = client.get('/api/activity', params={**query, 'created_after': result['created_at'] - 1})
    assert combined.json()['coverage']['time_filter'] == 'recorded_and_observed_at'
    assert [item['id'] for item in combined.json()['occurrences']] == [result['id']]
    for changes in ({'observed_after': 9, 'observed_before': 10}, {'clock_domain': 'other-clock'}, {'camera_id': 'garage'}):
        assert client.get('/api/activity', params={**query, **changes}).json()['occurrences'] == []
    # Another participant's visit must not make a camera/participant/time conjunction true.
    later = contribution('second', participant_id='entity-2', member_id='visit-2',
                         camera_id='garage', source_id='garage-main', observed_at=12.0)
    client.portal.call(partial(client.app.state.notifications.record_occurrence, later, retention_seconds=60.0))
    assert client.get('/api/activity', params={**query, 'camera_id': 'garage', 'observed_before': 13}).json()['occurrences'] == []
    matched = client.get('/api/activity', params={**query, 'camera_id': 'garage', 'participant_id': 'entity-2', 'observed_before': 13})
    assert [item['id'] for item in matched.json()['occurrences']] == [result['id']]


@pytest.mark.parametrize('query', [{'observed_after': 10}, {'observed_before': 11},
    {'observed_after': 10, 'observed_before': 10, 'clock_domain': 'clock'},
    {'observed_after': 'nan', 'clock_domain': 'clock'}, {'clock_domain': '*'}])
def test_observed_interval_requires_a_comparable_clock_and_valid_bounds(activity, query):
    client, _ = activity
    assert client.get('/api/activity', params=query).status_code == 422


def test_manifest_retains_human_source_labels_under_the_same_scope_gate(activity):
    client, result = activity
    sample = contribution('labelled', camera_name='Frente', source_name='Principal')
    client.portal.call(partial(client.app.state.notifications.record_occurrence, sample, retention_seconds=60.0))
    response = client.get('/api/activity/' + result['id'] + '/manifest')
    entry = next(item for item in response.json()['evidence'] if item['id'] == 'labelled')
    assert entry['name'] == 'Frente · Principal · 10.0'
    assert entry['camera_id'] == 'front' and entry['source_id'] == 'front-main'
