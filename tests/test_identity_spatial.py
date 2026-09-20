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
