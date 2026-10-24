"""Names are read from the current authorized gallery, never copied into history."""

from functools import partial
import pytest

from test_identity_api import client
from test_identity_store import curate, enroll, evidence, policy
from test_occurrences import contribution, enrichment


def saved_policy(client, value):
    config = client.portal.call(client.app.state.config_store.get_config).model_copy(deep=True)
    for intent in config.settings.core['automatic']['intents']:
        intent['condition']['resource_parameters']['policies'] = [value.model_dump()]
    client.portal.call(partial(client.app.state.config_store.save_config, config))


def trio(client, *, third_camera='camera'):
    history = client.app.state.notifications
    gallery = client.portal.call(partial(client.app.state.services.call, 'vision.identity.store'))
    gallery.register_policy(policy())
    config = client.portal.call(client.app.state.config_store.get_config)
    config.settings.core['automatic'] = {'enabled': False, 'intents': [{
        'id': 'presence_garage', 'name': 'Saved recognition', 'capability': 'vision.recognition',
        'scope': {'kind': 'camera', 'camera_ids': ['camera']}, 'requested': False,
        'consents': ['recognition'], 'condition': {'resource_parameters': {'policies': [policy().model_dump()]}}}]}
    from copy import deepcopy
    second = deepcopy(config.settings.core['automatic']['intents'][0])
    second['id'] = 'second'
    config.settings.core['automatic']['intents'].append(second)
    client.portal.call(partial(client.app.state.config_store.save_config, config))
    vectors = [[1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14]
    identities = [enroll(gallery, name=f'Synthetic person {index}', event=f'enrollment-{index}', vector=vector)[0]
                  for index, vector in enumerate(vectors)]
    sources = []
    for index in range(3):
        camera = third_camera if index == 2 else 'camera'
        value = contribution(f'open-{index}', objective_id='presence_garage', camera_id=camera, camera_name=camera.title(), source_id=camera + ':source',
            scope={'camera_ids': sorted({'camera', third_camera}), 'area_ids': ['garage'] if third_camera != 'camera' else []}, participant_id=f'person-{index}', member_id=f'visit-{index}')
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


@pytest.mark.parametrize('change', ['restart', 'remove', 'disable', 'recalibrate', 'revoke'])
def test_history_uses_current_saved_policy_without_new_capture(client, change):
    history, gallery, document, identities = trio(client)
    gallery._policies.clear()
    config = client.portal.call(client.app.state.config_store.get_config)
    state = config.settings.core['automatic']
    if change == 'remove':
        state['intents'] = []
    elif change == 'revoke':
        state['denied_features'] = {'camera': ['recognition']}
    elif change in {'disable', 'recalibrate'}:
        profile = state['intents'][0]['condition']['resource_parameters']['policies'][0]
        profile['automatic_enabled' if change == 'disable' else 'calibration_revision'] = False if change == 'disable' else 'new'
    client.portal.call(partial(client.app.state.config_store.save_config, config))
    # A late producer cannot restore authorization removed from saved configuration.
    if change != 'restart':
        gallery.register_policy(policy())
    projected = client.get('/api/activity/' + document['id']).json()['occurrence']['participants']
    assert projected['person-0']['identification']['status'] == ('recognized' if change == 'restart' else 'unknown')
    if change == 'restart':
        assert projected['person-0']['identification']['name'] == 'Synthetic person 0'
        assert gallery._policies == {}


def test_changed_configuration_during_history_projection_withholds_names(client, monkeypatch):
    history, gallery, document, identities = trio(client)
    original = gallery.activity_participants
    def project(*args, **kwargs):
        result = original(*args, **kwargs)
        config = client.portal.call(client.app.state.config_store.get_config)
        config.settings.core['automatic']['intents'] = []
        client.portal.call(partial(client.app.state.config_store.save_config, config))
        return result
    monkeypatch.setattr(gallery, 'activity_participants', project)
    projected = client.get('/api/activity/' + document['id']).json()['occurrence']['participants']
    assert all(value['identification']['status'] == 'unavailable' for value in projected.values())


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


@pytest.mark.parametrize('later_matches', [False, True])
def test_new_capture_does_not_replace_an_earlier_activitys_owned_decision(client, later_matches):
    history, gallery, document, identities = trio(client)
    original = gallery.decision('private-visit-0').observation_id
    vector = [1.] + [0.] * 15 if later_matches else [0., 0., 1.] + [0.] * 13
    later = history.occurrences.record(contribution('later', objective_id='second', participant_id='person-0',
        observed_at=30, started_at=30), retention_seconds=3600).document
    reference = {'occurrence_id': later['id'], 'privacy_revision': 1, 'evidence_id': 'later',
        'participant_id': 'person-0', 'observed_at': 30}
    gallery.observe(evidence('private-visit-0', capture='later', observed_at=30, vector=vector), policy(),
        activity_reference=reference)
    client.portal.call(partial(history.enrich_occurrence, enrichment(later['id'], message_id='later-name',
        evidence_id='later', participant_id='person-0', observed_at=30, kind='recognition',
        text=None, status='observed', identity_occurrence_id='private-visit-0')))
    path = '/api/activity/' + document['id']
    response = client.get(path)
    assert response.status_code == 200
    identity = response.json()['occurrence']['participants']['person-0']['identification']
    assert identity['name'] == 'Synthetic person 0'
    assert identity['evidence_id'] == 'open-0' and identity['observed_at'] == 10
    matches = client.get(f'/api/vision/identities/{identities[0]}/notifications', params={'association': 'recognized'})
    assert document['id'] in {item['payload']['occurrence_id'] for item in matches.json()['notifications']}
    other = client.get('/api/activity/' + later['id']).json()['occurrence']['participants']['person-0']['identification']
    assert (other['status'] == 'recognized') is later_matches
    curate(gallery, 'assign', {'observation_ids': [original], 'identity_id': identities[0]})
    unassigned = curate(gallery, 'unassign', {'observation_ids': [original]})
    assert client.get(path).json()['occurrence']['participants']['person-0']['identification'] == {'status': 'unknown'}
    gallery.undo(unassigned['operation_id'], expected_revision=gallery.revision)
    assert client.get(path).json()['occurrence']['participants']['person-0']['identification']['name'] == 'Synthetic person 0'
    history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
    assert 'Synthetic person' not in client.get(path).text


def test_nominal_history_uses_current_participant_and_location_before_pagination(client):
    import json
    history, gallery, document, identities = trio(client, third_camera='corridor')
    for index in range(260):
        history.store.upsert(type='event', title='Unrelated', payload={'data': {'camera_id': 'camera'}})
    path = f'/api/vision/identities/{identities[0]}/notifications'
    filters = {'association': 'recognized', 'limit': 1, 'created_after': document['created_at'] - 1,
        'created_before': document['created_at'] + 1, 'data_filter': json.dumps({'camera_id': 'camera', 'area_id': 'garage'})}
    response = client.get(path, params=filters)
    assert response.status_code == 200, response.text
    items = response.json()['notifications']
    assert len(items) == 1 and items[0]['payload']['occurrence_id'] == document['id']
    assert 'Synthetic person' not in response.text
    assert client.get(path, params={**filters, 'association': 'human'}).json()['notifications'] == []
    assert client.get(path, params={**filters, 'created_before': document['created_at']}).json()['notifications'] == []
    assert client.get(path, params={**filters, 'data_filter': json.dumps({'camera_id': 'corridor'})}).json()['notifications'] == []
    assert client.get('/api/notifications', params={'data_filter': json.dumps({'camera_id': 'corridor'})}).json()['notifications'][0]['payload']['occurrence_id'] == document['id']
    options = client.get('/api/notifications/data-options', params={'field': 'camera_id', 'label_field': 'camera_name'}).json()['options']
    assert {'value': 'corridor', 'label': 'Corridor'} in options
    areas = client.get('/api/notifications/data-options', params={'field': 'area_id', 'label_field': 'area_label'}).json()['options']
    assert {'value': 'garage', 'label': None} in areas
    history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
    assert client.get(path, params=filters).json()['notifications'] == []


def test_conflicting_owned_capture_cannot_restore_an_earlier_name(client):
    history, gallery, document, identities = trio(client)
    sample = evidence('private-visit-0', capture='conflicting', observed_at=10,
        vector=[0., 1.] + [0.] * 14)
    reference = {'occurrence_id': document['id'], 'privacy_revision': 1, 'evidence_id': 'open-0',
        'participant_id': 'person-0', 'observed_at': 10}
    decision = gallery.observe(sample, policy(), activity_reference=reference)
    assert decision.reason == 'identity_conflict'
    projected = client.get('/api/activity/' + document['id']).json()['occurrence']
    assert projected['participants']['person-0']['identification']['status'] == 'unknown'
    assert projected['participants']['person-1']['identification']['name'] == 'Synthetic person 1'


def test_capture_decisions_survive_backup_and_follow_capture_deletion(client, tmp_path):
    from toposync_ext_vision.identity.store import IdentityStore
    history, gallery, document, identities = trio(client)
    gallery.backup_to(tmp_path / 'capture-backup')
    restored = IdentityStore.restore_backup(tmp_path / 'capture-backup', tmp_path / 'capture-restored', scope=gallery.scope)
    try:
        restored.activity_authorized = history.occurrences.reference_allowed
        restored.register_policy(policy())
        projected = restored.activity_participants(document, authorize_cameras=lambda _: True)
        assert projected['person-0']['name'] == 'Synthetic person 0'
        original = restored.decision('private-visit-0').observation_id
        restored.delete_identity(identities[0], expected_revision=restored.revision)
        assert restored._connection.execute('SELECT 1 FROM observation_decision WHERE id=?', (original,)).fetchone() is None
        assert restored.activity_participants(document, authorize_cameras=lambda _: True)['person-0']['status'] == 'unknown'
    finally:
        restored.close()


def test_nominal_history_revalidates_private_owner_after_slow_search(client):
    history, gallery, document, identities = trio(client)
    async def race(**kwargs):
        items, _ = history.list_sync(notification_ids=kwargs['notification_ids'], read_filter=kwargs['read_filter'])
        history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
        return {'notifications': items, 'search': {}}
    client.app.state.services.register('ai.search_notifications', race)
    response = client.get(f'/api/vision/identities/{identities[0]}/notifications',
        params={'association': 'recognized', 'query': 'people', 'mode': 'text'})
    assert response.status_code == 409 and 'notifications' not in response.json()


def test_nominal_history_does_not_use_stale_recognition_policy(client):
    _, gallery, _, identities = trio(client)
    gallery.register_policy(policy(automatic=False))
    saved_policy(client, policy(automatic=False))
    response = client.get(f'/api/vision/identities/{identities[0]}/notifications', params={'association': 'recognized'})
    assert response.status_code == 200 and response.json()['notifications'] == []


def test_unknown_clustering_does_not_expire_retained_activity_names(client):
    _, gallery, document, _ = trio(client)
    before = client.get('/api/activity/' + document['id']).json()['occurrence']
    decisions = [gallery.decision(f'private-visit-{index}') for index in range(2)]
    for index in range(2):
        gallery.observe(evidence(f'unknown-{index}', vector=[0., 0., 1.] + [0.] * 13), policy())
    revision = gallery.revision
    gallery.cluster_pending()
    assert gallery.revision > revision
    after = client.get('/api/activity/' + document['id']).json()['occurrence']
    for key in before['participants']:
        assert after['participants'][key]['identification'].get('name') == before['participants'][key]['identification'].get('name')
    for index, old in enumerate(decisions):
        current = gallery.decision(f'private-visit-{index}')
        assert (current.revision, current.evidence_at) == (old.revision, old.evidence_at)
    assert after['revision'] == before['revision'] and after['status'] == 'CLOSE'


def test_nominal_query_and_location_options_do_not_leak_partial_source_access(client):
    _, _, document, identities = trio(client, third_camera='corridor')
    user = client.post('/api/access/users', json={'username': 'partial', 'display_name': 'Partial',
        'role': 'member', 'password': 'password123'}).json()
    auth = client.app.state.auth.store
    auth.upsert_grant(user_id=user['id'], action='core:extension:use', resource_type='core:extension', include=['com.toposync.vision'], exclude=[])
    auth.upsert_grant(user_id=user['id'], action='vision:identities:read', resource_type='core:extension', include=['com.toposync.vision'], exclude=[])
    auth.upsert_grant(user_id=user['id'], action='core:camera:read', resource_type='core:camera', include=['camera'], exclude=[])
    client.post('/api/auth/login', json={'username': 'partial', 'password': 'password123', 'device_label': 'test'})
    response = client.get(f'/api/vision/identities/{identities[0]}/notifications', params={'association': 'recognized'})
    assert response.status_code == 200 and response.json()['notifications'] == []
    assert document['id'] not in response.text
    for field, label in [('camera_id', 'camera_name'), ('area_id', 'area_label')]:
        assert client.get('/api/notifications/data-options', params={'field': field, 'label_field': label}).json() == {'options': []}


def test_activity_nominal_answer_rechecks_owner_after_generation(client):
    from toposync_ext_ai.history_answer import install_history_answer_routes
    history, _, document, identities = trio(client)
    calls = []
    async def search(**kwargs):
        calls.append('search')
        items, _ = history.list_sync(notification_ids=kwargs['notification_ids'], read_filter=kwargs['read_filter'])
        assert len(items) == 1
        return {'notifications': items, 'search': {}}
    class Router:
        async def generate_local_json(self, **kwargs):
            calls.append('answer')
            history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
            return {'citations': []}
    client.app.state.services.register('ai.search_notifications', search)
    install_history_answer_routes(client.app, client.app.state.services, Router())
    response = client.post('/api/ai/history/answer', json={'question': 'people', 'identity_id': identities[0], 'association': 'recognized'})
    assert response.status_code == 409 and calls == ['search', 'answer']
    assert 'notifications' not in response.json()


def test_manifest_reports_retention_classes_and_private_artifacts_without_exporting_vectors(client):
    history, gallery, document, _ = trio(client)
    path = '/api/activity/' + document['id'] + '/manifest'
    response = client.get(path)
    manifest = response.json()
    assert set(manifest['retention']) == {'event_text', 'images', 'private_identity', 'diagnostics', 'video'}
    assert manifest['retention']['event_text']['expires_at'] == document['expires_at']
    assert manifest['retention']['video']['state'] == 'not_recorded'
    assert manifest['retention']['images']['state'] == 'not_retained'
    assert manifest['retention']['diagnostics']['sample_maximum_age_seconds'] == client.app.state.pipeline_inspection.maximum_age
    assert len(manifest['artifacts']) == 2
    assert all(item['uses'] == [document['id']] and item['image_href'] is None for item in manifest['artifacts'])
    assert 'vector' not in response.text and 'embedding_space' not in response.text


def test_manifest_artifact_cannot_borrow_an_authorized_camera_from_its_owner(client):
    _, gallery, document, _ = trio(client)
    observation = gallery.decision('private-visit-0').observation_id
    gallery._connection.execute("UPDATE occurrence SET camera_id='another-camera' WHERE id='private-visit-0'")
    manifest = client.get('/api/activity/' + document['id'] + '/manifest').json()
    assert all(item['id'] != observation for item in manifest['artifacts'])


def test_manifest_image_link_requires_live_activity_owner_and_preserves_independent_photo(client):
    from io import BytesIO
    from PIL import Image
    history, gallery, document, _ = trio(client)
    sample = evidence('private-visit-0', observed_at=10.0)
    image = BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(image, format='JPEG')
    observation = gallery.decision(sample.occurrence_id).observation_id
    gallery._connection.execute('UPDATE observation SET crop=? WHERE id=?', (gallery._cipher.encrypt(image.getvalue()), observation))
    # Explicit independent use shares the exact capture, not the entire episode.
    gallery.observe(sample, policy())
    response = client.get('/api/activity/' + document['id'] + '/manifest', headers={'x-ingress-path': '/fixture-ingress'})
    artifact = next(item for item in response.json()['artifacts'] if item['id'] == observation)
    assert response.json()['retention']['images']['state'] == 'retained'
    assert artifact['image_href'].startswith('/fixture-ingress/api/vision/identities/observations/')
    link = artifact['image_href'].removeprefix('/fixture-ingress')
    loaded = client.get(link)
    assert loaded.status_code == 200 and loaded.content == image.getvalue()
    assert loaded.headers['cache-control'] == 'private, no-store'
    assert client.get(link.replace(observation, '..%2F..%2Fsecret')).status_code == 404
    assert client.request('DELETE', '/api/activity/' + document['id'], json={'expected_revision': document['revision']}).status_code == 200
    assert client.get(link).status_code == 404
    assert gallery.image(observation) == image.getvalue()


def test_manifest_image_discarded_when_activity_revoked_during_image_read(client, monkeypatch):
    history, gallery, document, _ = trio(client)
    observation = gallery.decision('private-visit-0').observation_id
    gallery._connection.execute('UPDATE observation SET crop=? WHERE id=?', (gallery._cipher.encrypt(b'synthetic-bytes'), observation))
    artifact = next(item for item in client.get('/api/activity/' + document['id'] + '/manifest').json()['artifacts'] if item['id'] == observation)
    def revoked(_):
        history.occurrences.revoke_enrichment(document['id'], expected_revision=document['revision'])
        return b'must-not-escape'
    monkeypatch.setattr(gallery, 'image', revoked)
    response = client.get(artifact['image_href'])
    assert response.status_code == 404 and b'must-not-escape' not in response.content


def test_manifest_private_expiry_can_precede_event_expiry_without_claiming_continuous_video(client, monkeypatch):
    import time
    history, gallery, document, _ = trio(client)
    gallery.configure_retention(enabled=True, expected_revision=gallery.revision)
    observation = gallery._stored_decision('private-visit-0').observation_id
    now = time.time()
    gallery._connection.execute('UPDATE observation SET created_at=? WHERE id=?', (now - 7 * 86400 + 30, observation))
    path = '/api/activity/' + document['id'] + '/manifest'
    before = client.get(path).json()
    item = next(item for item in before['artifacts'] if item['id'] == observation)
    assert item['expires_at'] == now + 30 < before['retention']['event_text']['expires_at']
    monkeypatch.setattr(time, 'time', lambda: now + 31)
    after = client.get(path).json()
    assert len(after['artifacts']) == 1 and all(item['id'] != observation for item in after['artifacts'])
    assert after['retention']['event_text']['state'] == 'retained'
    assert after['retention']['private_identity']['gallery_retention']['enabled'] is True
    assert after['retention']['video'] == {'state': 'not_recorded', 'exported': False}
    assert after['coverage']['continuous_video'] is False


def test_physical_purge_of_one_participant_preserves_other_retained_recognition(client):
    import time
    gallery = client.portal.call(partial(client.app.state.services.call, 'vision.identity.store'))
    gallery.configure_retention(enabled=True, expected_revision=gallery.revision)
    history, gallery, document, identities = trio(client)
    survivor = gallery.decision('private-visit-1')
    removed = gallery.decision('private-visit-0').observation_id
    gallery._connection.execute('UPDATE observation SET created_at=? WHERE id=?', (time.time() - 7 * 86400 - 1, removed))
    assert gallery.apply_retention()['observations'] == 1
    current = gallery.validated_decision('private-visit-1', decision_revision=survivor.revision, observed_at=10, max_age_seconds=0)
    assert current is not None and current.identity_id == identities[1]
    assert current.evidence_at == survivor.evidence_at and current.revision == survivor.revision
    response = client.get('/api/activity/' + document['id']).json()['occurrence']
    assert response['participants']['person-0']['identification']['status'] == 'unknown'
    assert response['participants']['person-1']['identification']['name'] == 'Synthetic person 1'
    assert response['status'] == 'CLOSE' and response['revision'] == document['revision']


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
        projection = response.json().get('occurrence', response.json())
        assert projection['artifacts'] == []
        assert projection['retention']['private_identity'] == {'state': 'restricted'}
        assert projection['retention']['images']['state'] == 'restricted'
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
        return {'participants': {'person-0': {'status': 'recognized', 'name': 'must not escape'}}, 'private_evidence': {'state': 'retained', 'artifacts': []}}
    client.app.state.services.register('occurrences.private_projection', race)
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
    saved_policy(client, policy(automatic=False))
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


def test_manual_history_policy_follows_only_its_enrichment_branch():
    from toposync.runtime.config_store import AppConfig, Pipeline
    from toposync_ext_vision.identity.api import activity_policies
    profile = {'enabled': True, 'policies': [policy().model_dump()]}
    nodes = [
        {'id': 'a', 'operator': 'core.record_occurrence', 'config': {'objective_id': 'a'}},
        {'id': 'b', 'operator': 'core.record_occurrence', 'config': {'objective_id': 'b'}},
        {'id': 'recognize_a', 'operator': 'vision.recognize_identity', 'config': profile},
        {'id': 'recognize_b', 'operator': 'vision.recognize_identity', 'config': profile},
        {'id': 'disconnected', 'operator': 'vision.recognize_identity', 'config': profile},
    ]
    edge = lambda a, b: {'from': {'node': a, 'port': 'out'}, 'to': {'node': b, 'port': 'in'}}
    config = AppConfig(pipelines=[Pipeline(name='manual', enabled=False, graph={
        'schema_version': 2, 'nodes': nodes, 'edges': [edge('a', 'recognize_a'), edge('b', 'recognize_b'), edge('recognize_a', 'a')],
    })])
    document = {'objective_id': 'a', 'scope': {'camera_ids': ['camera']}}
    assert len(activity_policies(config, document)) == 1
    config.pipelines[0].graph['edges'] = [edge('b', 'recognize_b')]
    assert activity_policies(config, document) == {}
    # Different calibration on a connected branch fails closed; unrelated branches do not.
    nodes[-1]['config'] = {'enabled': True, 'policies': [policy(automatic=False).model_dump()]}
    config.pipelines[0].graph['edges'] = [edge('a', 'recognize_a'), edge('a', 'disconnected')]
    assert activity_policies(config, document) == {}
