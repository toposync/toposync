"""Names are read from the current authorized gallery, never copied into history."""

from functools import partial

from test_identity_api import client
from test_identity_store import curate, enroll, evidence, policy
from test_occurrences import contribution, enrichment


def trio(client):
    history = client.app.state.notifications
    gallery = client.portal.call(partial(client.app.state.services.call, 'vision.identity.store'))
    gallery.register_policy(policy())
    vectors = [[1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14]
    identities = [enroll(gallery, name=f'Synthetic person {index}', event=f'enrollment-{index}', vector=vector)[0]
                  for index, vector in enumerate(vectors)]
    sources = []
    for index in range(3):
        value = contribution(f'open-{index}', camera_id='camera', source_id='camera:source',
            scope={'camera_ids': ['camera']}, participant_id=f'person-{index}', member_id=f'visit-{index}')
        sources.append(value)
        document = client.portal.call(partial(history.record_occurrence, value, retention_seconds=3600.0, request_notice=True))
    for index, value in enumerate(sources):
        closed = value.model_copy(update={'message_id': f'close-{index}', 'evidence_id': f'close-{index}',
                                         'lifecycle': 'CLOSE', 'observed_at': 12.0})
        document = client.portal.call(partial(history.record_occurrence, closed, retention_seconds=3600.0, request_notice=True))
    assert document['status'] == 'CLOSE'
    for index, vector in enumerate(vectors):
        reference = {'occurrence_id': document['id'], 'privacy_revision': 1, 'observed_at': 10.0,
                     'evidence_id': f'open-{index}', 'participant_id': f'person-{index}'}
        sample = evidence(f'private-visit-{index}', vector=vector, observed_at=10.0)
        decision = gallery.observe(sample, policy(), activity_reference=reference)
        assert decision.identity_id == identities[index]
        document = client.portal.call(partial(history.enrich_occurrence, enrichment(document['id'],
            message_id=f'recognition-{index}', evidence_id=f'open-{index}', participant_id=f'person-{index}',
            kind='recognition', text=None, status='observed', identity_occurrence_id=sample.occurrence_id)))
    return history, gallery, document, identities


def test_closed_trio_resolves_two_names_and_one_anonymous_in_detail_and_manifest(client):
    history, gallery, document, identities = trio(client)
    path = '/api/activity/' + document['id']
    response = client.get(path)
    assert response.status_code == 200, response.text
    projected = response.json()['occurrence']
    assert projected['status'] == 'CLOSE' and projected['ended_at'] == 12 and projected['duration_seconds'] == 2
    for index in range(2):
        identity = projected['participants'][f'person-{index}']['identification']
        assert identity['name'] == f'Synthetic person {index}' and identity['identity_id'] == identities[index]
        assert identity['provenance'] == 'automatic' and identity['observed_at'] == 10
    assert projected['participants']['person-2']['identification'] == {'status': 'unknown'}
    manifest = client.get(path + '/manifest').json()
    assert manifest['participants'] == list(projected['participants'].values())
    assert len(response.json()['deliveries']) == 1 and response.json()['deliveries'][0]['simulated']
    assert 'Synthetic person' not in str(history.list_sync())
    assert 'Synthetic person' not in str(history.occurrences.get(document['id'], read_filter=lambda _: True))
    assert 'vector' not in response.text and 'embedding_space' not in response.text
    assert response.headers['cache-control'] == 'private, no-store'


def test_human_rename_and_unassign_are_current_without_changing_observed_time(client):
    history, gallery, document, identities = trio(client)
    observation = gallery.decision('private-visit-0').observation_id
    curate(gallery, 'assign', {'observation_ids': [observation], 'identity_id': identities[0]})
    curate(gallery, 'rename', {'identity_id': identities[0], 'name': 'Synthetic renamed'})
    path = '/api/activity/' + document['id']
    projected = client.get(path).json()['occurrence']
    assert projected['participants']['person-0']['identification']['name'] == 'Synthetic renamed'
    assert projected['participants']['person-0']['identification']['provenance'] == 'human'
    assert projected['revision'] == document['revision'] and projected['ended_at'] == 12
    curate(gallery, 'unassign', {'observation_ids': [observation]})
    assert client.get(path).json()['occurrence']['participants']['person-0']['identification'] == {'status': 'unknown'}


def test_current_identity_permission_and_profile_camera_scope_are_required(client):
    history, gallery, document, identities = trio(client)
    user = client.post('/api/access/users', json={'username': 'limited', 'display_name': 'Limited',
        'role': 'member', 'password': 'password123'}).json()
    auth = client.app.state.auth.store
    auth.upsert_grant(user_id=user['id'], action='core:extension:use', resource_type='core:extension', include=['com.toposync.vision'], exclude=[])
    auth.upsert_grant(user_id=user['id'], action='core:camera:read', resource_type='core:camera', include=['camera'], exclude=[])
    auth.upsert_grant(user_id=user['id'], action='vision:identities:read', resource_type='core:extension', include=[], exclude=['com.toposync.vision'])
    client.post('/api/auth/login', json={'username': 'limited', 'password': 'password123', 'device_label': 'test'})
    path = '/api/activity/' + document['id']
    assert client.get(path).status_code == 200
    for suffix in ('', '/manifest'):
        response = client.get(path + suffix)
        assert 'Synthetic person' not in response.text and identities[0] not in response.text
    auth.upsert_grant(user_id=user['id'], action='vision:identities:read', resource_type='core:extension', include=['com.toposync.vision'], exclude=[])
    assert client.get(path).json()['occurrence']['participants']['person-0']['identification']['name'] == 'Synthetic person 0'
    # A profile can also contain a source outside this occurrence's authorized scope.
    gallery._connection.execute('INSERT INTO identity_camera VALUES(?,?)', (identities[0], 'private-camera'))
    response = client.get(path)
    assert 'Synthetic person 0' not in response.text and identities[0] not in response.text
    assert response.json()['occurrence']['participants']['person-1']['identification']['name'] == 'Synthetic person 1'


def test_revoked_ownership_cannot_be_read_before_background_purge(client):
    history, gallery, document, identities = trio(client)
    # Deliberately miss the asynchronous derivative callback, as after an interrupted purge.
    history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
    assert gallery._connection.execute('SELECT count(*) FROM activity_reference WHERE owner_id=?', (document['id'],)).fetchone()[0] == 2
    assert gallery._connection.execute("SELECT count(*) FROM activity_reference WHERE owner_id='independent'").fetchone()[0] == 2
    assert all(value['status'] == 'unknown' for value in gallery.activity_participants(document, authorize_cameras=lambda _: None).values())
    response = client.get('/api/activity/' + document['id'])
    assert 'Synthetic person' not in response.text


def test_private_projection_rechecks_occurrence_after_await(client):
    history, gallery, document, identities = trio(client)
    async def race(**kwargs):
        history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
        return {'person-0': {'status': 'recognized', 'name': 'must not escape'}}
    client.app.state.services.register('occurrences.participants', race)
    response = client.get('/api/activity/' + document['id'] + '/manifest')
    assert response.status_code == 409 and 'must not escape' not in response.text


def test_marker_cannot_borrow_another_participants_identity_or_stale_policy(client):
    from copy import deepcopy
    history, gallery, document, identities = trio(client)
    altered = deepcopy(document)
    altered['enrichments'][0]['identity_occurrence_id'] = 'private-visit-1'
    projected = gallery.activity_participants(altered, authorize_cameras=lambda _: None)
    assert projected['person-0'] == {'status': 'unknown'}
    gallery.register_policy(policy(automatic=False))
    response = client.get('/api/activity/' + document['id'])
    assert 'Synthetic person' not in response.text and identities[0] not in response.text


def test_disabled_extension_does_not_resolve_private_names(client):
    from toposync.runtime.extension_management import ExtensionManagementConfig, save_extension_management_config
    history, gallery, document, identities = trio(client)
    client.portal.call(save_extension_management_config, client.app.state.config_store,
                       ExtensionManagementConfig(disabled_extension_ids=['com.toposync.vision']))
    response = client.get('/api/activity/' + document['id'])
    assert response.status_code == 200, response.text
    assert 'Synthetic person' not in response.text and identities[0] not in response.text
    assert all(item['identification'] == {'status': 'unavailable'} for item in response.json()['occurrence']['participants'].values())
