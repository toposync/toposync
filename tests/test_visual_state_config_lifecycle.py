from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_identity_api import client  # noqa: F401
from test_visual_state_api import _configured_pipeline
from test_visual_state_integration_mapping import configuration
from test_pipeline_resource_guards import store_at
from toposync.runtime.config_store import Pipeline, PipelineRevisionConflictError
from toposync.runtime.pipelines.editing import PipelineRead, pipeline_revision


def create_sensor(api_client):
    from toposync_ext_cameras.pipelines.operators import register_camera_pipeline_operators
    register_camera_pipeline_operators(api_client.app.state.pipeline_operator_registry)
    response = api_client.post('/api/pipelines', json=_configured_pipeline(configuration()))
    assert response.status_code == 201, response.text
    return response.json()


def test_two_editors_keep_winner_and_recover_with_current_revision(client):  # noqa: F811
    original = create_sensor(client)
    listed = next(item for item in client.get('/api/pipelines').json()['pipelines'] if item['name'] == 'sensor')
    assert listed == client.get('/api/pipelines/sensor').json() == original
    first, second = deepcopy(original), deepcopy(original)
    first['graph']['nodes'][0]['config']['classes'][0]['label'] = 'Primeira edição'
    second['graph']['nodes'][0]['config']['classes'][1]['label'] = 'Segunda edição'
    saved = client.put('/api/pipelines/sensor', json=first)
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] != original['revision']
    rejected = client.put('/api/pipelines/sensor', json=second)
    assert rejected.status_code == 409 and rejected.json()['detail'] == 'pipeline_revision_conflict'
    latest = client.get('/api/pipelines/sensor').json()
    assert latest == saved.json()
    latest['graph']['nodes'][0]['config']['classes'][1]['label'] = 'Segunda edição revisada'
    recovered = client.put('/api/pipelines/sensor', json=latest)
    assert recovered.status_code == 200, recovered.text
    labels = [category['label'] for category in recovered.json()['graph']['nodes'][0]['config']['classes']]
    assert labels[:2] == ['Primeira edição', 'Segunda edição revisada']
    stored = json.loads(client.app.state.config_store.paths.config_path.read_text())
    assert 'revision' not in next(item for item in stored['pipelines'] if item['name'] == 'sensor')


@pytest.mark.parametrize('remove_sensor', [False, True])
def test_visual_edit_requires_precondition_even_when_removing_the_operator(client, remove_sensor):  # noqa: F811
    original = create_sensor(client)
    missing = deepcopy(original)
    missing.pop('revision')
    if remove_sensor:
        missing['graph']['nodes'] = missing['graph']['nodes'][1:]
        missing['graph']['edges'] = []
    response = client.put('/api/pipelines/sensor', json=missing)
    assert response.status_code == 428, response.text
    assert client.get('/api/pipelines/sensor').json() == original
    missing['revision'] = 'malformed'
    assert client.put('/api/pipelines/sensor', json=missing).status_code == 422


def test_external_file_edit_changes_revision_but_unrelated_settings_do_not(client):  # noqa: F811
    original = create_sensor(client)
    path = client.app.state.config_store.paths.config_path
    saved = json.loads(path.read_text())
    saved['settings']['core']['unrelated_test'] = True
    path.write_text(json.dumps(saved))
    assert client.get('/api/pipelines/sensor').json()['revision'] == original['revision']
    saved['pipelines'][0]['display_name'] = 'Edição externa'
    path.write_text(json.dumps(saved))
    response = client.put('/api/pipelines/sensor', json=original)
    assert response.status_code == 409
    assert client.get('/api/pipelines/sensor').json()['display_name'] == 'Edição externa'


def test_legacy_pipeline_accepts_old_client_but_honors_supplied_revision(client):  # noqa: F811
    value = {'name': 'ordinary', 'enabled': False, 'graph': {'schema_version': 2, 'nodes': [], 'edges': []}}
    original = client.post('/api/pipelines', json=value)
    assert original.status_code == 201, original.text
    value['display_name'] = 'Legacy client'
    assert client.put('/api/pipelines/ordinary', json=value).status_code == 200
    assert client.put('/api/pipelines/ordinary', json=original.json()).status_code == 409


def test_atomic_expected_snapshot_rejects_race_during_validation(tmp_path):
    async def run():
        store = store_at(tmp_path)
        original = Pipeline(name='sensor', enabled=False, graph={'schema_version': 2})
        await store.create_pipeline(original)
        waiting, proceed = asyncio.Event(), asyncio.Event()
        normal_validate = store.validate_pipeline_activation

        async def delayed(candidate, **kwargs):
            if candidate.display_name == 'Loser':
                waiting.set()
                await proceed.wait()
            await normal_validate(candidate, **kwargs)

        store.validate_pipeline_activation = delayed
        loser = asyncio.create_task(store.replace_pipeline('sensor', original.model_copy(update={'display_name': 'Loser'}), expected_pipeline=original))
        await waiting.wait()
        winner = await store.replace_pipeline('sensor', original.model_copy(update={'display_name': 'Winner'}), expected_pipeline=original)
        proceed.set()
        with pytest.raises(PipelineRevisionConflictError):
            await loser
        assert await store.get_pipeline('sensor') == winner
    asyncio.run(run())


def test_revision_is_canonical_content_and_response_does_not_mutate_pipeline():
    left = Pipeline(name='sensor', enabled=False, graph={'schema_version': 2, 'nodes': [], 'edges': []})
    right = Pipeline(name='sensor', enabled=False, graph={'edges': [], 'nodes': [], 'schema_version': 2})
    assert pipeline_revision(left) == pipeline_revision(right)
    before = left.model_dump()
    response = PipelineRead.from_pipeline(left)
    assert len(response.revision) == 64 and left.model_dump() == before
    assert pipeline_revision(left.model_copy(update={'enabled': True})) != response.revision


def visual_config(pipeline):
    return next(node['config'] for node in pipeline['graph']['nodes'] if node['operator'] == 'vision.visual_state')


@pytest.mark.parametrize('python_editor', [False, True])
def test_duplicate_has_new_identity_and_preserves_configuration_after_save(client, python_editor):  # noqa: F811
    original = create_sensor(client)
    if python_editor:
        original['editor_mode'] = 'python'
        original['python_source'] = (
            'sensor = camera.source(_id="source", rtsp_url="rtsp://synthetic-never-opened") '
            f'| vision.visual_state(_id="state", **{configuration()!r})'
        )
        response = client.put('/api/pipelines/sensor', json=original)
        assert response.status_code == 200, response.text
        original = response.json()
    identities = {visual_config(original)['sensor_id']}
    source_name = 'sensor'
    for name in ('copy', 'copy_again'):
        response = client.post(f'/api/pipelines/{source_name}/duplicate', json={'new_name': name})
        assert response.status_code == 201, response.text
        duplicate = response.json()
        actual = visual_config(duplicate)
        assert actual['sensor_id'] not in identities
        identities.add(actual['sensor_id'])
        assert duplicate['enabled'] is False and actual['home_assistant']['enabled'] is False
        expected = deepcopy(visual_config(original))
        expected.update(sensor_id=actual['sensor_id'])
        expected['home_assistant']['enabled'] = False
        assert actual == expected
        assert duplicate['graph']['uid'] == name
        # Saving a Python copy recompiles its source and must retain the new identity.
        duplicate['display_name'] = f'Reviewed {name}'
        saved = client.put(f'/api/pipelines/{name}', json=duplicate)
        assert saved.status_code == 200, saved.text
        assert visual_config(saved.json()) == actual
        assert client.get(f'/api/pipelines/{name}').json() == saved.json()
        source_name = name
    assert client.get('/api/pipelines/sensor').json() == original
    if python_editor:
        latest = client.get('/api/pipelines/copy_again').json()
        latest['python_source'] = latest['python_source'].replace("'description': 'Aberta'", "'description': 'Porta aberta'")
        saved = client.put('/api/pipelines/copy_again', json=latest)
        assert saved.status_code == 200, saved.text
        assert visual_config(saved.json())['classes'][0]['description'] == 'Porta aberta'
        assert visual_config(saved.json())['sensor_id'] == visual_config(latest)['sensor_id']


def test_duplicate_failure_does_not_create_partial_pipeline_or_modify_source(client, monkeypatch):  # noqa: F811
    original = create_sensor(client)
    registry = client.app.state.pipeline_operator_registry
    registered = registry.get('vision.visual_state')
    monkeypatch.setitem(registry._items, 'vision.visual_state', replace(registered, duplicate_config=lambda config: {'sensor_id': ''}))
    rejected = client.post('/api/pipelines/sensor/duplicate', json={'new_name': 'broken'})
    assert rejected.status_code == 400, rejected.text
    assert client.get('/api/pipelines/broken').status_code == 404
    assert client.get('/api/pipelines/sensor').json() == original
