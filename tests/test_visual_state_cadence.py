from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_core import VelocityThrottleRuntime
from toposync.runtime.pipelines.runtime import Lifecycle
from toposync_ext_cameras.pipelines.operators import MotionGateRuntime
from toposync_ext_vision.visual_state.operator import visual_state_cadence_diagnostics, visual_state_diagnostics
from toposync_ext_vision.visual_state.schemas import VisualStateConfig

from test_visual_state_contracts import configuration  # noqa: F401
from test_visual_state_runtime import frame, harness, until


def node(operator, **settings):
    return {"node_id": operator, "operator_id": operator, "normalized_config": settings}


@pytest.mark.parametrize("upstream,code", [
    (node("camera.motion_gate"), "visual_state_motion_dependency"),
    (node("camera.motion_bgsub_adaptive"), "visual_state_motion_dependency"),
    (node("camera.motion_sample_bg"), "visual_state_motion_dependency"),
    (node("camera.onvif_state_gate", fail_open=True), "visual_state_event_dependency"),
    (node("camera.source", observation_window_id="window", keep_capture_when_gate_closed=True), "visual_state_event_dependency"),
    (node("core.filter", expression="true"), "visual_state_event_dependency"),
    (node("core.debounce"), "visual_state_event_dependency"),
    (node("core.throttle", interval_seconds=5), "visual_state_cadence_too_slow"),
    (node("core.velocity_throttle", moving_interval_seconds=.1, stopped_interval_seconds=2), "visual_state_cadence_too_slow"),
    (node("core.fps_reducer", target_fps=1), "visual_state_cadence_too_slow"),
])
def test_manual_pipeline_keeps_configuration_and_reports_unqualified_response(configuration, upstream, code):  # noqa: F811
    before = json.dumps(upstream, sort_keys=True)
    diagnostics = visual_state_cadence_diagnostics(VisualStateConfig.model_validate(configuration), {"upstream_nodes": [upstream]})
    assert code in {item.code for item in diagnostics}
    assert all(item.details["rapid_response_qualified"] is False for item in diagnostics)
    assert json.dumps(upstream, sort_keys=True) == before


def test_cadence_checks_remain_visible_before_model_setup_and_do_not_expose_source_credentials(configuration):  # noqa: F811
    context = {"upstream_nodes": [node("camera.source", fps=1, rtsp_url="rtsp://secret:password@private"),
                                  node("core.throttle", interval_seconds=.5), node("core.fps_reducer", target_fps=4)]}
    diagnostics = visual_state_diagnostics({**configuration, "model_id": "missing"}, context)
    assert {"visual_state_not_ready", "visual_state_serial_limits", "visual_state_cadence_too_slow"} <= {item.code for item in diagnostics}
    assert "secret" not in json.dumps([item.model_dump() for item in diagnostics])


def test_continuous_input_and_idle_delivery_are_not_rejected_as_event_dependent(configuration):  # noqa: F811
    context = {"upstream_nodes": [node("camera.source", fps=10), node("camera.motion_gate", emit_when_idle=True),
        node("core.velocity_throttle", moving_interval_seconds=.1, stopped_interval_seconds=.5,
             moving_field="metadata.motion_gate_open", key_field="stream_id")]}
    assert not visual_state_cadence_diagnostics(VisualStateConfig.model_validate(configuration), context)


def test_compiled_editor_analysis_reaches_the_visual_sensor_with_upstream_context(configuration):  # noqa: F811
    from toposync.runtime.config_store import Pipeline
    from toposync.runtime.pipelines import OperatorRegistry, PipelineGraphCompiler, register_builtin_operators
    from toposync.runtime.pipelines.recommendations import analyze_compiled_pipeline
    from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
    from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
    from toposync_ext_vision.visual_state.operator import register_visual_state_operator

    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    register_visual_state_operator(registry)
    pipeline = Pipeline(name="cadence", enabled=False, graph=build_pipeline_graph_v2(graph_uid="cadence",
        nodes=[{"id": "source", "operator": "camera.source", "config": {"fps": 1}},
               {"id": "motion", "operator": "camera.motion_gate", "config": {"emit_when_idle": False}},
               {"id": "sensor", "operator": "vision.visual_state", "config": configuration}],
        edges=[{"from": {"node": source, "port": "out"}, "to": {"node": target, "port": "in"},
                "traffic": {"modality": "video"}} for source, target in [("source", "motion"), ("motion", "sensor")]]))
    before = pipeline.model_dump_json()
    compiled = PipelineGraphCompiler(registry).compile_pipeline(pipeline)
    alerts = analyze_compiled_pipeline(pipeline=compiled, registry=registry)
    owned = {alert.code for alert in alerts if alert.node_id == "sensor"}
    assert {"visual_state_motion_dependency", "visual_state_cadence_too_slow"} <= owned
    assert pipeline.model_dump_json() == before


def test_real_sensor_reconciles_without_motion_or_onvif_and_expires_after_input_stops(tmp_path, monkeypatch):
    import toposync_ext_cameras.pipelines.operators as camera_operators
    import toposync.runtime.pipelines.operators_core as core_operators

    class NoMotion:
        def __init__(self, **kwargs):
            pass

        def process(self, *args, **kwargs):
            return SimpleNamespace(active=False, score=0., bboxes01=(), last_latency_ms=0., fps=10.)

    monkeypatch.setattr(camera_operators, "MotionDetector", NoMotion)

    async def scenario():
        runtime, context, clock, references, _ = await harness(tmp_path)
        local_time = SimpleNamespace(monotonic=clock, time=clock.unix)
        monkeypatch.setattr(camera_operators, "time", local_time)
        monkeypatch.setattr(core_operators, "time", local_time)
        motion = MotionGateRuntime({"emit_when_idle": True}, PipelineRuntimeDependencies())
        throttle = VelocityThrottleRuntime({"moving_interval_seconds": .1, "stopped_interval_seconds": .5,
                                            "moving_field": "metadata.motion_gate_open", "key_field": "stream_id"})
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        seen = []
        try:
            for sequence in range(1, 25):
                clock.now += .1
                packet = frame(clock, sequence, "red" if sequence < 12 else "blue").with_lifecycle(Lifecycle.UPDATE)
                gated = await motion.process_packet(packet, None)
                assert gated and gated[0].metadata["motion_gate_open"] is False
                for accepted in await throttle.process_packet(gated[0], None):
                    await context.inputs["in"].put(accepted)
                    await until(lambda: runtime._reducer._last_seen is not None
                                and runtime._reducer._last_seen.sample.sequence == sequence)
                    seen.append(runtime._reducer.tick().class_id)
            assert "red" in seen and seen[-1] == "blue"
            assert not any(packet.payload.get("onvif") for packet in context.packets)
            clock.now += 2.1
            await until(lambda: context.packets[-1].payload["visual_state"]["reason"] == "observation_expired")
            assert context.packets[-1].payload["visual_state"]["class_id"] is None
        finally:
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            references.close()

    asyncio.run(scenario())
