"""Mapping snapshots are inspection data, never proof of physical identity."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import numpy as np

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_distributed import _deserialize_packet, _serialize_packet
from toposync.runtime.pipelines.runtime import Artifact
from toposync_ext_vision.identity.extraction import ExtractionResult
from toposync_ext_vision.identity.pipelines import (
    IdentityEvidenceRuntime,
    PRIVATE_EVIDENCE_ARTIFACT,
    occurrence_key,
)
from toposync_ext_vision.identity.spatial import spatial_context
from toposync_ext_vision.identity.store import IdentityStore
from test_identity_pipeline import Context, packet
from test_identity_store import enroll, evidence, policy


def mapped(*, composition="ground", x=1, z=2):
    original = packet()
    return replace(
        original,
        payload={
            **original.payload,
            "subject": {
                **original.payload["subject"],
                "world_anchor": {"x": x, "z": z, "confidence": 1},
            },
            "mapping": {
                "status": "mapped",
                "composition_id": composition,
                "calibrated_view_id": "calibration",
                "panorama_revision": 3,
                "quality": {"status": "ready"},
            },
            "capture_evidence": {
                "capture_instance": "decoder",
                "sequence": 4,
                "generation": 1,
                "physical_timestamp_verified": True,
            },
        },
    )


def test_spatial_snapshot_never_promotes_confidence_or_published_time_to_physical_proof():
    context = spatial_context(mapped())
    assert context.status == "estimate" and context.position == (1, 2)
    assert context.use == "inspection_only"
    assert context.reason == "physical_bounds_unavailable"
    assert context.clock_reason == "clock_uncertainty_unavailable"
    assert context.capture_instance == "decoder" and context.capture_sequence == 4
    assert context.capture_generation == 1
    assert context.calibration_revision == "3"
    assert "confidence" not in context.model_dump()


def test_group_packet_anchor_and_stale_capture_cannot_become_individual_estimates():
    original = mapped()
    group = replace(
        original,
        payload={
            **original.payload,
            "subject": {"id": "group", "type": "group_event", "world_anchor": {"x": 1, "z": 2}},
        },
    )
    assert spatial_context(group).reason == "individual_anchor_required"
    no_individual = replace(
        original,
        payload={
            **original.payload,
            "subject": {"id": "event", "type": "event"},
            "world_anchor": {"x": 1, "z": 2},
        },
    )
    assert spatial_context(no_individual).position is None
    stale = replace(
        original,
        payload={
            **original.payload,
            "mapping": {
                **original.payload["mapping"],
                "visual_localization": {"capture_evidence": {"sequence": 3}},
            },
        },
    )
    assert spatial_context(stale).reason == "mapping_frame_mismatch"
    assert spatial_context(stale).position is None
    invalid = mapped(x=float("nan"))
    assert spatial_context(invalid).reason == "individual_anchor_invalid"
    missing = replace(original, payload={**original.payload, "mapping": {"status": "unmapped"}})
    assert spatial_context(missing).reason == "mapping_provenance_incomplete"


def test_overlapping_distant_and_different_compositions_do_not_override_visual_identity(tmp_path):
    store = IdentityStore(tmp_path, scope="test")
    identity, _, _ = enroll(store)
    profile = policy()
    for index, value in enumerate(
        [mapped(), mapped(), mapped(x=90000), mapped(composition="other")]
    ):
        sample = evidence(f"visit-{index}").model_copy(
            update={"camera_id": f"camera-{index}", "spatial_context": spatial_context(value)}
        )
        decision = store.observe(sample, profile)
        assert decision.identity_id == identity and decision.provenance == "automatic"
        assert store.observation_context(decision.observation_id)["use"] == "inspection_only"
    assert store.list_identities()[0]["references"] == 1
    store.close()


def test_spatial_snapshot_is_private_transport_data_and_does_not_multiply_evidence(monkeypatch):
    async def scenario():
        original = mapped().with_artifact(Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8)))
        sample = evidence(occurrence_key(original), species="cat")
        runtime = IdentityEvidenceRuntime({"enabled": True}, PipelineRuntimeDependencies())
        monkeypatch.setattr(
            "toposync_ext_vision.identity.pipelines.extract_in_process",
            lambda *args, **kwargs: ExtractionResult("pending", "evidence_ready", sample),
        )
        result = (await runtime.process_packet(original, Context()))[0]
        transported = _deserialize_packet(_serialize_packet(result))
        artifact = transported.artifacts[PRIVATE_EVIDENCE_ARTIFACT]
        assert artifact.private
        decoded = json.loads(artifact.data)
        assert decoded["evidence"]["spatial_context"]["composition_id"] == "ground"
        assert "physical_bounds_unavailable" not in json.dumps(transported.payload)
        with_context = sample.model_copy(update={"spatial_context": spatial_context(original)})
        assert sample.observation_id("test") == with_context.observation_id("test")

    asyncio.run(scenario())


def test_reprojection_cannot_attach_new_calibration_to_stale_individual_anchor():
    original = mapped(x=99, z=99)
    projected = replace(original, payload={
        **original.payload,
        "world_anchor": {"x": 2, "z": 5},
        "mapping": {**original.payload["mapping"], "calibrated_view_id": "new"},
    })
    context = spatial_context(projected)
    assert context.reason == "mapping_anchor_mismatch"
    assert context.position is None and context.calibrated_view_id is None
    assert original.payload["subject"]["world_anchor"]["x"] == 99


def test_capture_generation_distinguishes_restarted_decoder_sequence():
    original = mapped()
    restarted = replace(original, payload={
        **original.payload,
        "capture_evidence": {**original.payload["capture_evidence"], "generation": 2},
    })
    first, second = spatial_context(original), spatial_context(restarted)
    assert first.capture_instance == second.capture_instance
    assert first.capture_sequence == second.capture_sequence
    assert first.capture_generation == 1 and second.capture_generation == 2


@pytest.mark.parametrize("field", ["coordinate", "revision", "generation"])
def test_malformed_optional_context_does_not_discard_visual_inference(field, monkeypatch):
    async def scenario():
        original = mapped(x=10**400 if field == "coordinate" else 1)
        if field == "revision":
            original.payload["mapping"]["panorama_revision"] = 10**600
        if field == "generation":
            original.payload["capture_evidence"]["generation"] = 10**400
        original = original.with_artifact(Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8)))
        sample = evidence(occurrence_key(original), species="cat")
        runtime = IdentityEvidenceRuntime({"enabled": True}, PipelineRuntimeDependencies())
        monkeypatch.setattr(
            "toposync_ext_vision.identity.pipelines.extract_in_process",
            lambda *args, **kwargs: ExtractionResult("pending", "evidence_ready", sample),
        )
        result = (await runtime.process_packet(original, Context()))[0]
        decoded = json.loads(result.artifacts[PRIVATE_EVIDENCE_ARTIFACT].data)
        assert decoded["evidence"]["vector"] == list(sample.vector)
        context = decoded["evidence"]["spatial_context"]
        if field == "coordinate":
            assert context["status"] == "unavailable" and context["position"] is None
        elif field == "revision":
            assert context["calibration_revision"] is None
        else:
            assert context["capture_generation"] is None

    asyncio.run(scenario())


def test_actual_mapping_reprojection_rejects_stale_subject_provenance():
    from toposync_ext_cameras.pipelines.postprocess import CameraMappingRuntime

    async def scenario():
        runtime = CameraMappingRuntime({
            "composition_id": "new-ground",
            "control_point_sets": [{
                "id": "new-calibration", "label": "New calibration",
                "control_points": [
                    {"image": {"x": x, "y": y}, "world": {"x": x * 10, "z": y * 10}}
                    for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))
                ],
            }],
        }, PipelineRuntimeDependencies())
        original = mapped(x=99, z=99)
        original.payload["subject"]["bbox01"] = [.1, .1, .3, .5]
        result = (await runtime.process_packet(original, None))[0]
        assert result.payload["world"] == pytest.approx({"x": 2, "z": 5})
        assert result.payload["subject"]["world_anchor"] == original.payload["subject"]["world_anchor"]
        context = spatial_context(result)
        assert context.reason == "mapping_anchor_mismatch"
        assert context.position is None and context.calibrated_view_id is None

    asyncio.run(scenario())


def test_panorama_mapping_another_detection_does_not_place_the_subject_at_its_old_position():
    from test_camera_panorama_runtime import _job, _packet, _run

    original = _packet()
    original.payload["image_uv"] = {"u": .5, "v": 0}
    original.payload["subject"] = {"type": "event", "id": "at-horizon", "category": "cat",
                                   "world_anchor": {"x": 99, "z": 99}}
    result = _run(original, {"applies": True, "job": _job()})
    assert result.payload["mapping"]["status"] == "mapped"
    assert result.payload["vision"]["detections"][0]["world_anchor"]["x"] == pytest.approx(3)
    assert result.payload["world_projection"]["reason"] == "no_ground_intersection"
    assert "world_anchor" not in result.payload
    assert spatial_context(result).position is None


def test_equal_coordinates_in_another_calibration_do_not_validate_the_old_subject_mapping():
    original = mapped()
    original.payload["subject"]["mapping"] = {**original.payload["mapping"], "panorama_revision": 2}
    original.payload["world_anchor"] = {"x": 1, "z": 2}
    assert spatial_context(original).position is None


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("preserved", [False, True])
def test_extraction_uses_the_bound_individual_for_pixels_and_position(monkeypatch, late, preserved):
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime

    async def allowed(*args):
        return True

    async def scenario():
        original = mapped()
        original.payload["source"] = {"source_id": "wide", "device_id": "camera"}
        original.payload["media"] = {"ts": 1.0}
        binding_runtime = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, PipelineRuntimeDependencies())
        bound, = await binding_runtime.process_packet(original, Context())
        subject = original.payload["subject"]
        other = {**subject, "id": "area-event", "bbox01": [.5, .5, .9, .9], "world_anchor": {"x": 7, "z": 8}}
        selected = replace(bound, payload={**bound.payload, "subject": other, "occurrence_reference": {"occurrence_id": "owner"},
            "area_occurrence": {"observed_subject": subject if preserved else other}})
        selected = selected.with_artifact(Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8)))
        sample = evidence(occurrence_key(selected), species="cat")
        regions = []
        def extract(*args, **kwargs):
            regions.append(kwargs["region"])
            return ExtractionResult("pending", "evidence_ready", sample)
        monkeypatch.setattr("toposync_ext_vision.identity.pipelines.extract_in_process", extract)
        monkeypatch.setattr("toposync_ext_vision.identity.pipelines.occurrence_enrichment_allowed", allowed)
        runtime = IdentityEvidenceRuntime({"enabled": True, "late_enrichment": late}, PipelineRuntimeDependencies())
        result, = await runtime.process_packet(selected, Context())
        assert regions == [tuple(subject["bbox01"])]
        ev = json.loads(result.artifacts[PRIVATE_EVIDENCE_ARTIFACT].data)["evidence"]
        assert ev["occurrence_id"] == occurrence_key(bound) and ev["vector"]
        assert ev["spatial_context"]["position"] == ([1., 2.] if preserved else None)
        assert result.payload["subject"] == other
    asyncio.run(scenario())

@pytest.mark.parametrize("descriptor", ["area_occurrence", "frame_occurrence", "region_occurrence"])
def test_bound_area_individual_survives_entity_projection(descriptor, tmp_path):
    from test_camera_area_occurrences import AREA, observation
    from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
    from toposync_ext_vision.entity_activity import EntityActivityRuntime
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime, individual_packet, identity_binding

    async def scenario():
        tracked = observation(1)
        tracked.payload.update(
            camera_id="camera", source={"device_id": "camera", "source_id": "wide"},
            association={"scope": "local", "entity_id": "animal"}, mapping=mapped().payload["mapping"],
        )
        tracked.payload["subject"].update(bbox01=(.1, .2, .4, .9) if descriptor == "frame_occurrence" else [.1, .2, .4, .9], world_anchor={"x": 1, "z": 1})
        area = AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0})
        occurrence, = await area.process_packet(tracked, None)
        context = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, PipelineRuntimeDependencies())
        bound, = await context.process_packet(occurrence, None)
        activity, = await EntityActivityRuntime({"scope": "local"}).process_packet(bound, None)
        binding = identity_binding(activity)
        assert tracked.payload["subject"]["id"] != binding["subject_id"] != activity.payload["subject"]["id"]
        assert spatial_context(individual_packet(activity, binding)).position == (1., 1.)

        # The same entity can receive another member, in the very same frame and box.
        concurrent = replace(bound, payload={**bound.payload, "subject": {
            **bound.payload["subject"], "id": "other-member", "world_anchor": {"x": 7, "z": 8},
        }})
        assembler = EntityActivityRuntime({"scope": "local"})
        first, = await assembler.process_packet(bound, None)
        second, = await assembler.process_packet(concurrent, None)
        assert first.payload["subject"]["id"] == second.payload["subject"]["id"]
        mixed = replace(second, payload={**second.payload, "identity_binding": first.payload["identity_binding"]})
        from toposync.runtime.services import ServiceRegistry
        from toposync_ext_vision.identity.pipelines import RecognizeIdentityRuntime
        from test_identity_pipeline import with_evidence
        services = ServiceRegistry()
        store = IdentityStore(tmp_path, scope="two-members")
        services.register("vision.identity.store", lambda: store)
        deps = PipelineRuntimeDependencies(services=services)
        try:
            mixed = mixed.with_artifact(with_evidence(first).artifacts[PRIVATE_EVIDENCE_ARTIFACT])
            resolved, = await RecognizeIdentityRuntime({"enabled": True}, deps).process_packet(mixed, Context())
            assert resolved.payload["recognition"]["reason"] == "individual_occurrence_required"
            assert not store.observations()
            extracted, = await IdentityEvidenceRuntime({"enabled": True}, deps).process_packet(mixed, Context())
            assert extracted.payload["recognition"]["reason"] == "individual_occurrence_required"
            assert PRIVATE_EVIDENCE_ARTIFACT not in extracted.artifacts
        finally:
            store.close()

        # A later occurrence can retain the original subject under any of these descriptors.
        other = {**activity.payload["subject"], "id": "other", "world_anchor": {"x": 7, "z": 8}}
        projected = replace(activity, payload={
            **activity.payload, "subject": other, descriptor: {"observed_subject": occurrence.payload["subject"]},
        })
        assert spatial_context(individual_packet(projected, binding)).position == (1., 1.)
        projected.payload[descriptor] = {"observed_subject": other}
        assert spatial_context(individual_packet(projected, binding)).position is None

    asyncio.run(scenario())


@pytest.mark.parametrize("defect", ["missing", "boolean_sequence", "stale_subject", "mutable_box"])
def test_context_requires_current_capture_and_freezes_the_bound_region(defect):
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime, identity_binding

    async def scenario():
        original = mapped()
        original.payload["source"] = {"source_id": "wide", "device_id": "camera"}
        capture = original.payload["capture_evidence"]
        if defect == "missing":
            capture.clear()
        elif defect == "boolean_sequence":
            capture["sequence"] = True
        elif defect == "stale_subject":
            original.payload["subject"]["source_anchor"] = {"capture_evidence": {**capture, "sequence": 99}}
        context = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, PipelineRuntimeDependencies())
        bound, = await context.process_packet(original, None)
        if defect == "mutable_box":
            original.payload["subject"]["bbox01"][0] = .3
            assert identity_binding(bound)["bbox01"][0] == .1
        else:
            assert occurrence_key(bound) is None
            assert bound.payload["recognition"]["status"] == "unobservable"

    asyncio.run(scenario())


@pytest.mark.parametrize("late", [False, True])
def test_resolver_rejects_evidence_from_another_frame_with_the_same_timestamp(monkeypatch, tmp_path, late):
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime, RecognizeIdentityRuntime

    async def scenario():
        dependencies = PipelineRuntimeDependencies()
        context = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, dependencies)
        original = mapped()
        original.payload["source"] = {"source_id": "wide", "device_id": "camera"}
        original = original.with_artifact(Artifact(name="main", data=np.zeros((128, 128, 3), dtype=np.uint8)))
        first, = await context.process_packet(original, None)
        next_frame = {**original.payload["capture_evidence"], "sequence": 5}
        second, = await context.process_packet(replace(original, payload={
            **original.payload, "capture_evidence": next_frame, "occurrence_reference": {"occurrence_id": "owner"},
        }), None)
        key = occurrence_key(first)
        assert key == occurrence_key(second)
        sample = evidence(key, species="cat").model_copy(update={"source_id": "wide"})
        monkeypatch.setattr("toposync_ext_vision.identity.pipelines.extract_in_process",
                            lambda *args, **kwargs: ExtractionResult("pending", "evidence_ready", sample))
        observed, = await IdentityEvidenceRuntime({"enabled": True}, dependencies).process_packet(first, Context())
        store = IdentityStore(tmp_path, scope="two-frames")
        try:
            resolver = RecognizeIdentityRuntime({"enabled": True, "late_enrichment": late}, dependencies)
            with pytest.raises(ValueError, match="evidence_provenance_mismatch"):
                resolver._resolve_active(second, store, key, observed.artifacts[PRIVATE_EVIDENCE_ARTIFACT])
            assert not store.observations()
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("retained", [False, True])
def test_activity_cannot_stipulate_an_unrelated_binding_on_the_same_capture(retained):
    from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime

    async def scenario():
        packet = mapped()
        packet.payload.update(source={"source_id": "wide", "device_id": "camera"}, media={"ts": 1.0})
        bound, = await IdentityContextRuntime({"enabled": True, "late_enrichment": True},
                                             PipelineRuntimeDependencies()).process_packet(packet, None)
        original = bound.payload["subject"]
        altered = replace(bound, payload={**bound.payload, "subject": {**original, "id": "other"},
            "frame_occurrence": {"observed_subject": original} if retained else {}})
        output, = await ActivityEvidenceRuntime().process_packet(altered, None)
        assert output.payload["activity_observation"]["episode_id"] == "other"
        assert occurrence_key(output, require_binding=True) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["area", "frame", "region"])
def test_identity_bound_before_an_occurrence_reaches_the_admitted_activity(kind):
    from test_camera_area_occurrences import AREA, observation
    from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
    from toposync_ext_cameras.pipelines.frame_occurrence import FrameOccurrenceRuntime
    from toposync_ext_cameras.pipelines.region_occurrence import RegionOccurrenceRuntime
    from toposync_ext_vision.activity_evidence import ActivityEvidenceRuntime
    from toposync_ext_vision.identity.pipelines import IdentityContextRuntime

    async def scenario():
        packet = observation(1)
        packet.payload.update(camera_id="camera", source={"source_id": "wide", "device_id": "camera"},
                              media={"ts": 1.}, correlation_id="original",
                              region_observation={"geometry_key": "region", "classification": "inside"})
        packet.payload["subject"].update(type="event", bbox01=[.1, .2, .4, .9])
        context = IdentityContextRuntime({"enabled": True, "late_enrichment": True}, PipelineRuntimeDependencies())
        bound, = await context.process_packet(packet, None)
        config = {"categories": ["cat"], "minimum_duration_seconds": 0}
        operators = {"area": AreaOccurrenceRuntime({**config, "area": AREA}), "frame": FrameOccurrenceRuntime(config),
                     "region": RegionOccurrenceRuntime(config)}
        projected, = await operators[kind].process_packet(bound, None)
        assert projected.payload["subject"]["id"] != packet.payload["subject"]["id"]
        admitted, = await ActivityEvidenceRuntime().process_packet(projected, None)
        assert occurrence_key(admitted) == occurrence_key(bound) is not None
        assert admitted.payload["identity_binding"]["activity_evidence_id"] == admitted.payload["activity_observation"]["evidence_id"]

    asyncio.run(scenario())
