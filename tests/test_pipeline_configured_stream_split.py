"""Configured stream splitting must agree in diagnostics, DSL and runtime channels."""
import pytest

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry, PipelineGraphCompiler, PipelineRuntime,
    PipelineRuntimeDependencies, register_builtin_operators,
)
from toposync.runtime.pipelines.python_dsl import compile_python_source_to_graph
from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
from toposync.runtime.pipelines.runtime import KeyedBoundedChannel
from toposync_ext_vision.pipelines.operators import register_vision_pipeline_operators


@pytest.mark.parametrize("mode", ["annotate", "filter", "events"])
@pytest.mark.parametrize("bound_target", [False, True])
def test_configured_detection_preserves_real_split_warnings_and_channel_types(mode, bound_target):
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_vision_pipeline_operators(registry)
    pose = 'vision.pose_estimate(model_id="mediapipe_pose_33", _id="pose")'
    source = (f'target = {pose}\n' if bound_target else '') + (
        'PIPELINE = core.synthetic_source(_id="source") | '
        f'vision.detect(model_id="rfdetr_det_medium", emit_mode="{mode}", _id="detect") | '
        f'{"target" if bound_target else pose} | '
        + ('core.passthrough(_id="track")' if mode == 'events' else 'vision.track(_id="track")')
        + ' | core.passthrough(_id="after")'
    )
    graph = compile_python_source_to_graph(
        python_source=source, pipeline_name="configured_split", registry=registry,
    )
    detector_edge = next(e for e in graph['edges'] if e['from']['node'] == 'detect')
    assert detector_edge['queue']['drop_policy'] == (
        'keyed_latest_only' if mode == 'events' else 'latest_only'
    )
    # Deliberately unsafe queues: warnings must start at the actual splitter.
    for edge in graph['edges']:
        edge['queue'] = {'max_items': 1, 'drop_policy': 'latest_only'}
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name='configured_split', enabled=False, graph=graph)
    )
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)
    warned = {a.edge['from']['node'] for a in alerts if a.code == 'split_stream_latest_only_channel'}
    assert warned == ({'detect', 'pose', 'track'} if mode == 'events' else {'track'})
    runtime = PipelineRuntime(
        compiled=compiled, registry=registry, dependencies=PipelineRuntimeDependencies(),
    )
    assert isinstance(runtime.channel_map['detect.out->pose.in'], KeyedBoundedChannel) == (mode == 'events')
    assert isinstance(runtime.channel_map['pose.out->track.in'], KeyedBoundedChannel) == (mode == 'events')
    assert isinstance(runtime.channel_map['track.out->after.in'], KeyedBoundedChannel)
    for edge in graph['edges']:
        edge['queue'] = {'max_items': 256, 'drop_policy': 'keyed_latest_only'}
    safe = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name='configured_split', enabled=False, graph=graph)
    )
    assert not any(a.code.startswith('split_stream_') for a in analyze_compiled_pipeline(pipeline=safe, registry=registry))
    assert registry.get('vision.detect').splits_stream({})  # Existing default is events.
