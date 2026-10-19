"""Passive group/gesture projections: synthetic observations, no models or devices."""

import asyncio
import json
import time
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines import Artifact, PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.inspection import InspectionVisual, PipelineInspectionStore
from toposync_ext_vision.association import AssociationStore
from toposync_ext_vision.association_runtime import AssociationRuntime, observation_from_packet
from toposync_ext_vision.processing.inspection import association_visual, gesture_visuals
from test_vision_association import config
from test_vision_association_runtime import packet as association_packet
from test_vision_gestures import packet as gesture_packet, runtime as gesture_runtime
from test_vision_gesture_partial_evidence import obscure


def with_image(packet, size=(1001, 1001)):
    width, height = size
    packet.payload["capture_evidence"]["published_at"] = time.time()
    packet.payload["source"].update(device_id=packet.payload["camera_id"], source_id="main")
    return packet.with_artifact(Artifact("main", np.zeros((height, width, 3), np.uint8), metadata={
        "image_geometry": image_geometry(width, height, packet.payload["capture_evidence"])}))


class Context:
    def __init__(self, operator):
        self.inspection_store = PipelineInspectionStore()
        self.token = self.inspection_store.open("test", "revision", {"node": operator})
        self.operator = operator
        self.outputs = []

    def inspection_active(self):
        return bool(self.inspection_store.sessions)

    async def run_blocking(self, function, *args, **kwargs):
        assert kwargs.pop("mode", None) in (None, "thread_pool")
        return function(*args, **kwargs)

    async def emit(self, packet):
        self.outputs.append(packet)

    def inspect(self, state, packet, **kwargs):
        self.inspection_store.observe(pipeline="test", revision="revision", node="node", physical="physical",
            operator=self.operator, producer_epoch="runtime", state=state, packet=packet, **kwargs)

    def snapshot(self):
        return self.inspection_store.snapshot(self.token).nodes[0]

    def close(self):
        self.inspection_store.close(self.token)


def test_group_runtime_projects_all_observed_members_of_the_same_capture_without_collective_identity():
    async def scenario():
        cfg = config()
        runtime = AssociationRuntime(cfg.model_dump(), PipelineRuntimeDependencies())
        runtime.store = AssociationStore(cfg)
        context = Context("vision.associate_entities")
        try:
            packets = []
            published_at = time.time()
            for track in (1, 2, 3):
                value = with_image(association_packet(track=track, x=(track - 1) * .7), (640, 360))
                value.payload["source"]["source_id"] = "a"
                value.payload["capture_evidence"]["published_at"] = published_at
                value.payload["subject"]["bbox01"] = [.1 * track, .2, .1 * track + .05, .7]
                packets.append(value)
            await runtime._flush([(value, observation_from_packet(value, cfg)) for value in packets], context)
            node = context.snapshot()
            assert len(context.outputs) == len(node.visuals) == 3
            assert {value.subject_id for value in node.visuals} == {"1", "2", "3"}
            assert len({value.segment_id for value in node.visuals}) == 1
            assert all(value.state == "group_member" and len(value.points) == 4 for value in node.visuals)
            assert all(value.properties["member_count"] == 3 and len(value.related_subject_ids) == 3 for value in node.visuals)
            assert node.frame["sequence"] == 1
            assert "identity_id" not in node.model_dump_json()
            assert "probability" not in node.model_dump_json()
            # Reading diagnostics cannot change the stored association decisions.
            before = json.dumps(runtime.store.observations, sort_keys=True)
            context.snapshot()
            assert json.dumps(runtime.store.observations, sort_keys=True) == before
        finally:
            context.close()
    asyncio.run(scenario())


def test_group_projection_bounds_members_and_never_invents_a_collective_contour():
    value = with_image(association_packet(), (640, 360))
    value.payload["subject"]["bbox01"] = [.1, .2, .3, .4]
    value.payload["association"] = {"entity_id": "entity", "revision": 7, "reason": "ambiguous_competitors",
        "components": {"candidate_count": 2, "residual_metres": .4, "error_bound_metres": .1, "private": "hidden"},
        "group": {"id": "group", "revision": 6, "members": [f"member-{index}" for index in range(40)]}}
    visual = InspectionVisual.model_validate(association_visual(value))
    assert len(visual.related_subject_ids) == 32 and visual.properties["member_count"] == 40
    assert visual.properties["candidate_count"] == 2 and "private" not in visual.properties
    assert visual.points[0] == pytest.approx([.1 * 639 / 640, .2 * 359 / 360])
    missing = value.with_artifact(replace(value.artifacts["main"], metadata={}))
    assert association_visual(missing)["points"] == []
    value.payload["association"]["group"] = None
    assert association_visual(value)["state"] == "ungrouped" and association_visual(value)["points"] == []


@pytest.mark.parametrize("mode", ["annotate", "events"])
def test_gesture_inspection_uses_current_individual_wrists_and_preserves_functional_output(mode):
    async def scenario():
        runtime = gesture_runtime(output_mode=mode)
        baseline = gesture_runtime(output_mode=mode)
        context = Context("vision.gesture_recognize")
        try:
            for timestamp in (0, .1, .2):
                source = with_image(gesture_packet(timestamp, "both"))
                actual = await runtime.process_packet(source, context)
                expected = await baseline.process_packet(source, None)
                assert len(actual) == len(expected)
                assert [item.lifecycle for item in actual] == [item.lifecycle for item in expected]
            node = context.snapshot()
            active = [value for value in node.visuals if value.label == "both_hands_raised"]
            assert len(active) == 1 and active[0].state == "gesture_active"
            assert active[0].subject_id == "person:1" and len(active[0].points) == 2
            assert active[0].points[0] == pytest.approx([300 / 1001, 60 / 1001])
            assert active[0].score is None and active[0].properties["evidence"] == "heuristic_2d"
            assert node.frame["sequence"] == source.payload["capture_evidence"]["sequence"]
            unknown = with_image(obscure(gesture_packet(.3, "both"), "right"))
            await runtime.process_packet(unknown, context)
            retained = [value for value in context.snapshot().visuals if value.state == "gesture_retained"]
            assert len(retained) == 1 and retained[0].points == []
            assert any(value.state == "gesture_unknown" for value in context.snapshot().visuals)
            wrong_actor = with_image(gesture_packet(.4, "both"))
            wrong_actor.payload["vision"]["poses"][0]["actor_subject_id"] = "another-person"
            await runtime.process_packet(wrong_actor, context)
            assert all(not value.points for value in context.snapshot().visuals)
        finally:
            context.close()
            await runtime.shutdown()
            await baseline.shutdown()
    asyncio.run(scenario())


def test_gesture_projection_requires_declared_capture_geometry_and_projects_crop_rotation():
    value = with_image(gesture_packet(0, "raised"))
    result = {"actor_subject_id": "person:1", "status": "none", "reason": "evaluated", "active": [],
        "candidates": [{"name": "hand_raised", "side": "left", "evidence": "heuristic_2d"}], "side_evidence": {}}
    observed = {"left_wrist": (300, 60)}
    forward = [[0, -.5, 400], [.5, 0, 0], [0, 0, 1]]
    metadata = transformed_geometry(value.artifacts["main"], forward, 500, 500)
    rotated = value.with_artifact(Artifact("main", np.zeros((500, 500, 3), np.uint8), metadata=metadata))
    assert gesture_visuals(rotated, result, observed)[1]["points"][0] == pytest.approx([370/500, 150/500])
    metadata["image_geometry"]["capture_evidence"]["sequence"] += 1
    assert gesture_visuals(rotated, result, observed)[1]["points"] == []


def test_two_individuals_on_one_capture_survive_received_and_started_then_clear_on_new_capture():
    context = Context("vision.gesture_recognize")
    source = with_image(gesture_packet(0, "raised"))
    artifact = source.artifacts["main"]
    def inspect(subject):
        context.inspect("received", source)
        context.inspect("started", source)
        context.inspect("analyzed", source, frame=artifact.data, geometry=artifact.metadata["image_geometry"],
            visuals=[{"layer": "gestures", "kind": "points", "points": [[.3,.06]], "subject_id": subject}])
    try:
        inspect("first")
        inspect("second")
        assert {value.subject_id for value in context.snapshot().visuals} == {"first", "second"}
        inspect("first")
        assert len(context.snapshot().visuals) == 2
        source.payload["capture_evidence"]["sequence"] += 1
        artifact.metadata["image_geometry"]["capture_evidence"]["sequence"] += 1
        inspect("second")
        assert [value.subject_id for value in context.snapshot().visuals] == ["second"]
    finally:
        context.close()


@pytest.mark.parametrize("changed", ["geometry", "model", "producer", "missing_capture"])
def test_individual_visuals_are_not_merged_across_incompatible_provenance(changed):
    context = Context("vision.gesture_recognize")
    source = with_image(gesture_packet(0, "raised"))
    artifact = source.artifacts["main"]
    values = dict(pipeline="test", revision="revision", node="node", physical="physical",
        operator=context.operator, producer_epoch="runtime", state="analyzed", packet=source,
        frame=artifact.data, geometry=artifact.metadata["image_geometry"], model_id="model-one")
    if changed == "missing_capture":
        values.update(packet=None, frame=None, geometry=None)
    def observe(subject):
        context.inspection_store.observe(**values,
            visuals=[{"layer": "gestures", "kind": "points", "points": [], "subject_id": subject}])
    try:
        observe("first")
        if changed == "geometry":
            values["geometry"] = transformed_geometry(artifact, [[1,0,1],[0,1,0],[0,0,1]], 1001, 1001)["image_geometry"]
        elif changed == "model":
            values["model_id"] = "model-two"
        elif changed == "producer":
            values["producer_epoch"] = "replacement"
        observe("second")
        assert [visual.subject_id for visual in context.snapshot().visuals] == ["second"]
        if changed == "missing_capture":
            assert context.snapshot().geometry is None and not context.snapshot().image_available
    finally:
        context.close()


def test_group_member_references_are_bounded_and_metadata_without_pixels_expires():
    drawing = {"layer": "groups", "kind": "points", "points": [], "related_subject_ids": ["person"] * 33}
    with pytest.raises(ValidationError):
        InspectionVisual.model_validate(drawing)
    drawing["related_subject_ids"] = ["person"]
    now = [100.0]
    store = PipelineInspectionStore(clock=lambda: now[0])
    token = store.open("test", "revision", {"node": "vision.associate_entities"})
    store.observe(pipeline="test", revision="revision", node="node", physical="physical",
        operator="vision.associate_entities", producer_epoch="runtime", state="analyzed", visuals=[drawing])
    assert len(store.snapshot(token).nodes[0].visuals) == 1
    now[0] += 1.01
    assert not store.snapshot(token).nodes[0].visuals
    store.close(token)
