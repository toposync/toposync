import pytest
import asyncio
import json
from starlette.requests import Request

from test_auth_local import _create_client, _setup_owner


@pytest.fixture
def restricted_history(tmp_path, monkeypatch):
    with _create_client(tmp_path, monkeypatch) as client:
        _setup_owner(client)
        user = client.post('/api/access/users', json={
            'username': 'limited', 'display_name': 'Limited',
            'role': 'member', 'password': 'password123',
        }).json()
        grant = {'action': 'core:camera:read', 'resource_type': 'core:camera',
                 'include': ['front'], 'exclude': []}
        assert client.post(f"/api/access/users/{user['id']}/grants", json=grant).status_code == 200
        records = {}
        for name, cameras in [('allowed', ['front']), ('private', ['back']),
                              ('mixed', ['front', 'back']), ('unknown', [])]:
            records[name], _ = client.app.state.notifications.store.upsert(
                type='pipelines.event', title=name, image_path=f'evidence/{name}.jpg',
                payload={'access_scope': {'camera_ids': cameras, 'area_ids': [], 'complete': bool(cameras)},
                         'data': {'camera_id': name, 'camera_name': name}})
            files = client.app.state.config_store.paths.files_dir / 'evidence'
            files.mkdir(parents=True, exist_ok=True)
            (files / f'{name}.jpg').write_bytes(b'synthetic-file')
        assert client.post('/api/auth/login', json={
            'username': 'limited', 'password': 'password123', 'device_label': 'test',
        }).status_code == 200
        yield client, records, user['id']


@pytest.mark.parametrize('surface', ['list', 'detail', 'count', 'options', 'file', 'viewed'])
def test_notification_surfaces_require_all_source_permissions(restricted_history, surface):
    client, records, _ = restricted_history
    if surface == 'list':
        response = client.get('/api/notifications', params={'limit': 1})
        assert response.status_code == 200
        assert [item['id'] for item in response.json()['notifications']] == [records['allowed'].id]
    elif surface == 'detail':
        for name in ('private', 'mixed', 'unknown'):
            assert client.get(f'/api/notifications/{records[name].id}').status_code == 404
        assert client.get(f"/api/notifications/{records['allowed'].id}").status_code == 200
    elif surface == 'count':
        assert client.get('/api/notifications/count').json()['total'] == 1
    elif surface == 'options':
        assert client.get('/api/notifications/data-options', params={
            'field': 'camera_id', 'label_field': 'camera_name',
        }).json()['options'] == [{'value': 'allowed', 'label': 'allowed'}]
    elif surface == 'file':
        assert client.get('/files/evidence/private.jpg').status_code == 404
        assert client.get('/files/evidence/mixed.jpg').status_code == 404
        assert client.get('/files/evidence/allowed.jpg').content == b'synthetic-file'
    elif surface == 'viewed':
        assert client.post('/api/notifications/viewed').json()['total'] == 1


def test_access_rechecks_grants_and_shared_file_references(restricted_history):
    client, records, user = restricted_history
    store = client.app.state.notifications.store
    store._conn.execute('UPDATE notification SET image_path = ? WHERE id = ?',
                        ('evidence/allowed.jpg', records['private'].id))
    assert client.get('/files/evidence/allowed.jpg').status_code == 404
    client.app.state.auth.store.upsert_grant(user_id=user, action='core:camera:read',
        resource_type='core:camera', include=['front'], exclude=['front'])
    assert client.get('/api/notifications').json()['notifications'] == []
    assert client.get('/api/notifications/count').json()['total'] == 0
    assert client.get(f"/api/notifications/{records['allowed'].id}").status_code == 404


def test_history_read_does_not_rotate_refresh_token_for_every_record(restricted_history, monkeypatch):
    client, records, _ = restricted_history
    auth = client.app.state.auth
    rotations = []
    original = auth.store.rotate_refresh_token
    def rotate(*args, **kwargs):
        rotations.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(auth.store, 'rotate_refresh_token', rotate)
    client.cookies.delete(auth.access_cookie_name)
    response = client.get('/api/notifications')
    assert response.status_code == 200
    assert [item['id'] for item in response.json()['notifications']] == [records['allowed'].id]
    assert response.headers['cache-control'] == 'private, no-store'
    assert len(rotations) == 1


def stream_request(client, path):
    request = Request({'type': 'http', 'app': client.app, 'path': path, 'method': 'GET',
                       'headers': [(b'cookie', '; '.join(f'{key}={value}' for key, value in client.cookies.items()).encode())]})
    request.state.auth_context = client.app.state.auth.resolve_request(request)
    request.is_disconnected = lambda: asyncio.sleep(0, result=False)
    return request


@pytest.mark.parametrize('selected', [False, True])
def test_queued_stream_content_is_not_delivered_after_revocation(restricted_history, selected):
    client, records, user = restricted_history
    async def scenario():
        runtime = client.app.state.notifications
        path = '/api/notifications/{notification_id}/stream' if selected else '/api/notifications/stream'
        route = next(route for route in client.app.routes if getattr(route, 'path', '') == path)
        request = stream_request(client, path)
        response = await route.endpoint(request, records['allowed'].id) if selected else await route.endpoint(request)
        iterator = response.body_iterator
        assert await anext(iterator) == 'retry: 1000\n\n'
        assert 'event: ready' in await anext(iterator)
        runtime.broadcaster.publish({'op': 'update', 'notification': await runtime.get(records['allowed'].id)})
        client.app.state.auth.store.upsert_grant(user_id=user, action='core:notifications:stream',
            resource_type='core:global', include=['*'], exclude=['*'])
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(iterator), timeout=1)
        assert not runtime.broadcaster._subscribers
    client.portal.call(scenario)


@pytest.mark.parametrize('selected', [False, True])
def test_activity_deletion_invalidates_open_clients_without_disclosing_other_scopes(restricted_history, selected):
    from test_occurrences import contribution
    from toposync.runtime.notifications.access import NotificationAccess
    client, _, _ = restricted_history
    async def scenario():
        runtime = client.app.state.notifications
        documents = {}
        identifiers = {}
        for camera in ('front', 'back'):
            document = await runtime.record_occurrence(contribution(objective_id=camera, camera_id=camera,
                scope={'camera_ids': [camera], 'area_ids': []}), retention_seconds=60, request_notice=True)
            documents[camera] = document
            identifiers[camera] = next(item['id'] for item in runtime.list_sync()[0]
                if item['payload'].get('occurrence_id') == document['id'])
        path = '/api/notifications/{notification_id}/stream' if selected else '/api/notifications/stream'
        route = next(route for route in client.app.routes if getattr(route, 'path', '') == path)
        request = stream_request(client, path)
        access = NotificationAccess(request)
        response = await route.endpoint(request, identifiers['front']) if selected else await route.endpoint(request)
        iterator = response.body_iterator
        assert await anext(iterator) == 'retry: 1000\n\n'
        assert 'event: ready' in await anext(iterator)
        for camera in ('back', 'front'):
            await runtime.erase_occurrence(documents[camera]['id'], expected_revision=documents[camera]['revision'])
            visible = await runtime.event_visible({'op': 'invalidate', 'notification_id': identifiers[camera],
                'occurrence_id': documents[camera]['id']}, read_filter=access)
            assert visible is (camera == 'front')
        message = await asyncio.wait_for(anext(iterator), timeout=1)
        assert json.loads(message.removeprefix('data: ')) == {'op': 'invalidate', 'notification_id': identifiers['front']}
        assert await runtime.event_visible({'op': 'resync'}, read_filter=access)
        assert not await runtime.event_visible({'op': 'resync', 'notification_id': identifiers['back']}, read_filter=access)
        await iterator.aclose()
        assert not runtime.broadcaster._subscribers
    client.portal.call(scenario)


def test_history_answer_only_receives_authorized_evidence_and_discards_revoked_result(restricted_history):
    from toposync_ext_ai.history_answer import install_history_answer_routes
    client, records, user = restricted_history
    model_inputs = []
    class Services:
        async def call(self, name, **parameters):
            assert name == 'ai.search_notifications'
            parameters.pop('query')
            items, _ = await client.app.state.notifications.list(**parameters)
            return {'notifications': items, 'search': {}}
    class Router:
        async def generate_local_json(self, **parameters):
            model_inputs.append(parameters)
            client.app.state.auth.store.upsert_grant(user_id=user, action='core:camera:read',
                resource_type='core:camera', include=['front'], exclude=['front'])
            return {'citations': [{'notification_id': records['allowed'].id, 'field': 'title', 'excerpt': 'allowed'}]}
    install_history_answer_routes(client.app, Services(), Router())
    response = client.post('/api/ai/history/answer', json={'question': 'What happened?'})
    assert response.status_code == 200, response.text
    assert len(model_inputs) == 1
    evidence = model_inputs[0]['messages'][-1]['content']
    assert 'allowed' in evidence and 'private' not in evidence and 'mixed' not in evidence
    assert response.json()['notifications'] == []
    assert response.json()['answer']['citations'] == []


def test_notify_retains_all_origins_even_when_projection_excludes_them():
    from test_pipeline_notify_timing import CONTEXT, sample, sink
    from toposync.runtime.pipelines.runtime import Lifecycle
    async def scenario():
        runtime, emitted = sink({'update_interval_seconds': 0, 'include_payload_paths': ['subject']})
        first = sample(1, Lifecycle.OPEN)
        first.payload.update(camera_id='front', area_id='entrance',
            entity_activity={'origins': [('front', 's1'), ('back', 's2')]})
        await runtime.process_packet(first, CONTEXT)
        later = sample(2)
        later.payload.update(camera_id='front', area_id='hall')
        await runtime.process_packet(later, CONTEXT)
        assert emitted[0]['payload']['access_scope'] == {
            'camera_ids': ['front', 'back'], 'area_ids': ['entrance'], 'complete': True}
        assert emitted[-1]['payload']['access_scope'] == {
            'camera_ids': ['front', 'back'], 'area_ids': ['entrance', 'hall'], 'complete': True}
        await runtime.shutdown()
        assert emitted[-1]['payload']['access_scope']['camera_ids'] == ['front', 'back']
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['delete', 'edit', 'revoke'])
def test_queued_snapshot_cannot_survive_current_evidence_change(restricted_history, change):
    from toposync.runtime.notifications.access import NotificationAccess
    client, records, user = restricted_history
    async def scenario():
        runtime = client.app.state.notifications
        access = NotificationAccess(stream_request(client, '/api/notifications/stream'))
        event = {'op': 'update', 'notification': await runtime.get(records['allowed'].id)}
        assert await runtime.event_visible(event, read_filter=access)
        if change == 'delete':
            runtime.store._conn.execute('DELETE FROM notification WHERE id = ?', (records['allowed'].id,))
        elif change == 'edit':
            runtime.store._conn.execute('UPDATE notification SET title = ? WHERE id = ?', ('Corrected', records['allowed'].id))
        else:
            client.app.state.auth.store.upsert_grant(user_id=user, action='core:camera:read',
                resource_type='core:camera', include=['front'], exclude=['front'])
        assert not await runtime.event_visible(event, read_filter=access)
        assert not await runtime.event_visible({'op': 'resync', 'notification_id': records['private'].id}, read_filter=access)
    client.portal.call(scenario)


def test_text_embedding_rechecks_access_after_model_preparation(restricted_history, monkeypatch):
    import json
    import httpx
    from types import SimpleNamespace
    from toposync.runtime.notifications.access import NotificationAccess
    from toposync_ext_ai.text_embeddings import TextHistorySearch, MODEL
    client, _, user = restricted_history
    inputs = []
    class Services:
        async def call(self, name):
            return SimpleNamespace(providers=[SimpleNamespace(enabled=True, kind='ollama', local=True, host='http://localhost:11434')])
    def handler(request):
        if request.url.path == '/api/tags':
            client.app.state.auth.store.upsert_grant(user_id=user, action='core:camera:read',
                resource_type='core:camera', include=['front'], exclude=['front'])
            return httpx.Response(200, json={'models': [{'name': MODEL, 'digest': 'a' * 64}]})
        batch = json.loads(request.content)['input']
        inputs.append(batch)
        return httpx.Response(200, json={'embeddings': [[1.0, 0.0] for _ in batch]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    async def scenario():
        result = await TextHistorySearch(client.app.state.notifications, Services()).search(
            query='gato', read_filter=NotificationAccess(stream_request(client, '/api/ai/history/search')))
        assert result['notifications'] == []
        assert inputs == [['gato']]
    client.portal.call(scenario)
