from __future__ import annotations

from copy import deepcopy

import pytest

from toposync.runtime.config_store import Pipeline, PipelineValidationError
from toposync.runtime.pipelines.editing import prepare_pipeline_duplicate
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.python_dsl import PythonDslCompileError, compile_python_source_to_graph
from toposync_ext_vision.visual_state.operator import register_visual_state_operator

from test_visual_state_integration_mapping import configuration


def source_registry():
    registry = OperatorRegistry()
    registry.register_operator(operator_id='test.source', inputs=[], outputs=[{'name': 'out'}], capabilities=['source'])
    register_visual_state_operator(registry)
    return registry


def test_duplicate_disabled_before_activation_and_each_sensor_gets_own_identity():
    registry = OperatorRegistry()
    register_visual_state_operator(registry)
    value = configuration()
    original = Pipeline(name='duplicate', enabled=True, graph={'schema_version': 2, 'nodes': [
        {'id': identifier, 'operator': 'vision.visual_state', 'config': deepcopy(value)}
        for identifier in ('first', 'second')], 'edges': []})
    before = original.model_dump()
    duplicated = prepare_pipeline_duplicate(original, registry)
    identities = [node['config']['sensor_id'] for node in duplicated.graph['nodes']]
    assert len(set(identities)) == 2 and value['sensor_id'] not in identities
    assert not duplicated.enabled
    assert original.model_dump() == before


@pytest.mark.parametrize('callback', [lambda config: [], lambda config: None])
def test_duplicate_rejects_invalid_callback_contract(callback):
    registry = OperatorRegistry()
    registry.register_operator(operator_id='test.source', duplicate_config=callback)
    pipeline = Pipeline(name='copy', graph={'schema_version': 2, 'nodes': [{'id': 'source', 'operator': 'test.source'}]})
    with pytest.raises(PipelineValidationError, match='configuration patch'):
        prepare_pipeline_duplicate(pipeline, registry)


def test_python_copy_rejects_shadowed_configuration_helper():
    registry = source_registry()
    pipeline = Pipeline(name='copy', editor_mode='python', python_source=(
        f'PIPELINE = test.source() | vision.visual_state(_id="state", **{configuration()!r})\n'
        'configure_node = lambda *args, **kwargs: None\n'))
    with pytest.raises(PipelineValidationError, match='does not reproduce'):
        prepare_pipeline_duplicate(pipeline, registry)


def test_python_configuration_merges_nested_fields_in_order_without_changing_edges():
    registry = OperatorRegistry()
    registry.register_operator(operator_id='test.source', inputs=[], outputs=[{'name': 'out'}],
        defaults={'identity': 'original', 'nested': {'enabled': True, 'mapping': ['a', 'b']}})
    registry.register_operator(operator_id='test.target', inputs=[{'name': 'in'}])
    source = 'PIPELINE = test.source(_id="source") | test.target(_id="target")'
    baseline = compile_python_source_to_graph(python_source=source, pipeline_name='copy', registry=registry)
    compiled = compile_python_source_to_graph(python_source=source + '''
configure_node('source', identity='first', nested={'enabled': False})
configure_node('source', identity='second', nested={'mapping': ['c']})
''', pipeline_name='copy', registry=registry)
    assert compiled['edges'] == baseline['edges']
    assert next(node['config'] for node in compiled['nodes'] if node['id'] == 'source') == {
        'identity': 'second', 'nested': {'enabled': False, 'mapping': ['c']}}
    assert registry.get('test.source').definition.defaults['nested']['mapping'] == ['a', 'b']


@pytest.mark.parametrize('patch, error', [
    ('configure_node("missing", sensor_id="new")', 'missing node'),
    ('configure_node("state", sensor_id="")', 'Invalid configuration'),
])
def test_python_configuration_rejects_invalid_patches(patch, error):
    registry = source_registry()
    source = f'PIPELINE = test.source() | vision.visual_state(_id="state", **{configuration()!r})\n{patch}'
    with pytest.raises(PythonDslCompileError, match=error):
        compile_python_source_to_graph(python_source=source, pipeline_name='copy', registry=registry)


def test_python_configuration_cannot_silently_change_inferred_queue_policy():
    registry = OperatorRegistry()
    registry.register_operator(operator_id='test.source', inputs=[], outputs=[{'name': 'out'}],
        defaults={'split': False}, capabilities=['split_stream'], split_stream_when=lambda config: config['split'])
    registry.register_operator(operator_id='test.target', inputs=[{'name': 'in'}])
    source = '''
PIPELINE = test.source(_id="source") | test.target(_id="target")
configure_node('source', split=True)
'''
    with pytest.raises(PythonDslCompileError, match='operator expression'):
        compile_python_source_to_graph(python_source=source, pipeline_name='copy', registry=registry)


def test_python_duplicate_resolves_destination_name_and_fails_closed_for_stale_node_ids():
    registry = source_registry()
    pipeline = Pipeline(name='copy', editor_mode='python', python_source=(
        f'PIPELINE = test.source() | vision.visual_state(_id=PIPELINE_NAME, **{configuration()!r})\n'))
    duplicate = prepare_pipeline_duplicate(pipeline, registry)
    assert any(node['id'] == 'copy' for node in duplicate.graph['nodes'])
    renamed = duplicate.model_copy(deep=True, update={'name': 'renamed'})
    # Explicit node overrides must be updated when a source changes node identifiers.
    with pytest.raises(PythonDslCompileError, match='missing node'):
        prepare_pipeline_duplicate(renamed, registry)
    renamed.python_source = renamed.python_source.replace("configure_node('copy',", 'configure_node(PIPELINE_NAME,')
    next_copy = prepare_pipeline_duplicate(renamed, registry)
    assert next_copy.graph['nodes'][0]['config']['sensor_id'] != duplicate.graph['nodes'][0]['config']['sensor_id']


def test_ordinary_duplicate_does_not_disable_or_mutate_its_source():
    registry = OperatorRegistry()
    registry.register_operator(operator_id='test.source')
    original = Pipeline(name='ordinary', enabled=True, graph={'schema_version': 2,
        'nodes': [{'id': 'source', 'operator': 'test.source', 'config': {'items': ['one']}}]})
    duplicate = prepare_pipeline_duplicate(original, registry)
    assert duplicate == original and duplicate is not original
    duplicate.graph['nodes'][0]['config']['items'].append('two')
    assert original.graph['nodes'][0]['config']['items'] == ['one']
