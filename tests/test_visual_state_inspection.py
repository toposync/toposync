from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines.inspection import InspectionVisual, PipelineInspectionStore
from toposync_ext_vision.visual_state.inspection import decision_visuals
from toposync_ext_vision.visual_state.schemas import PublishedState

from test_visual_state_runtime import frame, harness, send, until
from test_visual_state_temporal import sensor, confirmed, observe  # noqa: F401


def test_last_analysis_is_distinct_from_retained_value_and_survives_only_as_history(sensor):  # noqa: F811
    confirmed(sensor)
    state = observe(sensor, .5, 3, "b", reference_ids=("reference_b",), duration_ms=20)
    assert state.class_id == "a" and state.candidate.class_id == "b"
    assert state.last_analysis.class_id == "b" and state.last_analysis.sample.sequence == 3
    assert state.sample.sequence == 2 and state.last_confirmed.class_id == "a"
    assert state.last_analysis.reference_ids == ("reference_b",)
    repeated = observe(sensor, .6, 3, "c")
    assert repeated.last_analysis == state.last_analysis and repeated.analysis_age_ms == 100
    sensor[0].now = 3
    expired = sensor[1].tick()
    assert expired.status == "unknown" and expired.class_id is None
    assert expired.last_confirmed.class_id == "a" and expired.last_analysis.class_id == "b"
    assert expired.analysis_age_ms == 2500
    unavailable = sensor[1].invalidate("input_unobservable")
    assert unavailable.status == "unavailable" and unavailable.last_analysis == expired.last_analysis
    initial = sensor[1].__class__(sensor[1].config, runtime_epoch="new", clock=sensor[0]).tick()
    assert initial.last_analysis is None and initial.analysis_age_ms is None and initial.last_confirmed is None


@pytest.mark.parametrize("field", ["runtime_epoch", "reference_revision", "model_revision", "analysis_age_ms"])
def test_last_analysis_cannot_be_attached_to_different_configuration_or_without_age(sensor, field):  # noqa: F811
    state = confirmed(sensor).model_dump(mode="json")
    state[field] = None if field == "analysis_age_ms" else "different"
    with pytest.raises(ValidationError):
        PublishedState.model_validate(state)


def test_inspection_runtime_exposes_bounded_technical_evidence_without_images_or_descriptions(tmp_path):
    async def scenario():
        runtime, context, clock, references, _ = await harness(tmp_path)
        inspection = PipelineInspectionStore(clock=clock.unix)
        token = inspection.open("sensor", "pipeline-revision", {"state": "vision.visual_state"})
        context.inspection_store = inspection
        context.inspection_revisions = {"sensor": "pipeline-revision"}
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        try:
            await send(runtime, context, clock, 1)
            await send(runtime, context, clock, 2)
            await until(lambda: inspection.sessions[token].nodes["state"].reason == "confirmed")
            node = inspection.sessions[token].nodes["state"]
            assert not node.image_available and not node.detections and not node.geometry
            assert inspection.sessions[token].image_bytes == 0 and not inspection.sessions[token].images
            state = runtime._reducer.tick()
            assert node.visuals == [InspectionVisual.model_validate(value) for value in decision_visuals(state)]
            projection = node.model_dump(mode="json")
            assert projection["visuals"][0]["properties"]["reference_revision"] == runtime.config.reference_revision
            assert projection["visuals"][1]["properties"]["sequence"] == 2
            assert projection["visuals"][1]["score_kind"] == "cosine_similarity"
            assert projection["visuals"][1]["related_subject_ids"] == list(state.last_analysis.reference_ids)
            assert all(not packet.artifacts for packet in context.packets)
            assert not any(key in json.dumps(projection) for key in ("description", "vector", "data:image"))
            clock.now += 2.1
            await until(lambda: inspection.sessions[token].nodes["state"].reason == "observation_expired")
            assert inspection.sessions[token].nodes["state"].visuals[0].state == "unknown"
        finally:
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            references.close()

    asyncio.run(scenario())


def test_native_failure_never_copies_error_details_into_metrics_or_state(tmp_path):
    async def scenario():
        runtime, context, clock, references, _ = await harness(tmp_path)
        await runtime.prepare(context)

        def fail(_):
            raise RuntimeError("private-description rtsp://secret:password@camera/private")

        runtime._backend.embed = fail
        task = asyncio.create_task(runtime.run(context))
        try:
            await context.inputs["in"].put(frame(clock, 1))
            await until(lambda: context.metrics.error_count == 1)
            await until(lambda: context.packets[-1].payload["visual_state"]["reason"] == "local_analysis_failed")
            assert context.metrics.last_error == "RuntimeError: visual_state_local_analysis_failed"
            output = json.dumps([packet.payload for packet in context.packets])
            assert "private-description" not in output and "secret" not in output
            assert context.packets[-1].payload["visual_state"]["status"] == "unavailable"
        finally:
            context.cancel_event.set()
            await task
            await context.scheduler.shutdown()
            references.close()

    asyncio.run(scenario())
