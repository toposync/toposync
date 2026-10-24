"""Synthetic bounded ground calibration and passive zone summaries; no devices."""

import asyncio
import time

import numpy as np
import pytest

from test_inspection_behavior import Context
from toposync.runtime.pipelines import Artifact, PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
from toposync.runtime.pipelines.inspection import InspectionVisual, inspect_packet_visuals
from toposync.runtime.pipelines.runtime import Packet
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
from toposync_ext_cameras.pipelines.inspection import mapping_visual
from toposync_ext_cameras.pipelines.postprocess import AreaRestrictionRuntime, CameraMappingRuntime


CALIBRATION = {"composition_id": "synthetic", "calibrated_views": [{
    "id": "fixed", "stream_scope": {"physical_view_id": "fixed", "compatible_source_ids": ["main"]},
    "projection_model": {"type": "camera_ray_ground_v2", "source_geometry": {"width": 640, "height": 480},
        "lens": {"type": "identity_rectilinear_v1"}, "correspondences": [
            {"id": f"p{index}", "role": role, "image": {"x": u, "y": v}, "world": {"x": u * 10, "z": v * 10}}
            for index, (role, u, v) in enumerate([
                ("fit", .1, .1), ("fit", .9, .1), ("fit", .9, .9), ("fit", .1, .9),
                ("fit", .5, .1), ("fit", .5, .9), ("check", .3, .3), ("check", .7, .7)])]},
    "projection_quality": {"status": "ready"}}]}
AREA = {"id": "square", "name": "Synthetic square", "points": [
    {"x": 4, "z": 4}, {"x": 6, "z": 4}, {"x": 6, "z": 6}, {"x": 4, "z": 6}]}


def packet(sequence=1, x=.5):
    capture = {"capture_instance": "synthetic", "generation": 0, "sequence": sequence, "published_at": time.time()}
    return Packet.create(stream_id="synthetic", payload={"camera_id": "camera", "frame_ts": float(sequence),
        "frame_width": 640, "frame_height": 480, "source": {"device_id": "camera", "source_id": "main", "view_id": "fixed"},
        "capture_evidence": capture, "subject": {"id": "person-one", "type": "event", "category": "person",
            "bbox01": [x-.01, .3, x+.01, .5]}}, artifacts={
        "main": Artifact("main", np.zeros((480, 640, 3), np.uint8), metadata={"image_geometry": image_geometry(640, 480, capture)})})


def test_mapping_runtime_reports_its_current_anchor_and_clears_outside_calibration():
    async def scenario():
        runtime = CameraMappingRuntime(CALIBRATION, PipelineRuntimeDependencies())
        context = Context("camera.camera_mapping")
        try:
            source = packet()
            output = (await runtime.process_packet(source, context))[0]
            assert output.payload["world"] == pytest.approx({"x": 5, "z": 5})
            drawing = context.snapshot().visuals[0]
            assert drawing.state == "mapping_estimate" and drawing.properties["calibrated_view_id"] == "fixed"
            assert drawing.points[0] == pytest.approx([319.5/640, 239.5/480])
            assert drawing.score is None and "confidence" not in drawing.properties
            outside = (await runtime.process_packet(packet(2, .98), context))[0]
            assert outside.payload["mapping"]["status"] == "unmapped"
            drawing = context.snapshot().visuals[0]
            assert drawing.state == "mapping_unavailable" and drawing.points == []
            assert "world_x" not in drawing.properties
        finally:
            context.close()
    asyncio.run(scenario())


def test_mapping_projection_rejects_stale_anchors_unsafe_pose_and_carried_input():
    source = packet()
    source.payload.update(world={"x": 5, "z": 5}, mapping={"status": "mapped", "u": .5, "v": .5,
        "calibrated_view_id": "fixed", "map_revision": "revision"})
    assert InspectionVisual.model_validate(mapping_visual(source)).points
    assert mapping_visual(source, produced=False)["state"] == "mapping_unavailable"
    source.payload["pan_tilt_zoom_state"] = {"geometry_safe": False, "move_status": "moving"}
    assert mapping_visual(source)["points"] == []
    source.payload.pop("pan_tilt_zoom_state")
    source.payload["subject"]["source_anchor"] = {"uv": [.5, .5], "capture_evidence": {}}
    assert mapping_visual(source)["points"] == []
    source.payload["subject"].pop("source_anchor")
    artifact = source.artifacts["main"]
    metadata = transformed_geometry(artifact, [[0,-.5,240],[.5,0,0],[0,0,1]], 240, 320)
    rotated = source.with_artifact(Artifact("main", np.zeros((320,240,3), np.uint8), metadata=metadata))
    assert mapping_visual(rotated)["points"][0] == pytest.approx([120.25/240, 159.75/320])
    metadata["image_geometry"]["capture_evidence"]["sequence"] += 1
    assert mapping_visual(rotated)["points"] == []


def test_visual_panorama_mapping_remains_visible_during_motion_only_for_its_exact_frame():
    source = packet()
    source.payload.update(world={"x": 5, "z": 5},
        pan_tilt_zoom_state={"geometry_safe": False, "move_status": "moving"},
        mapping={"status": "mapped", "u": .5, "v": .5, "calibrated_view_id": "panorama",
            "panorama_revision": 2, "pose_evidence": "visual_frame_rotation",
            "visual_localization": {"source_artifact_id": "source", "geometry_digest": "geometry",
                "capture_evidence": dict(source.payload["capture_evidence"])}})
    assert mapping_visual(source)["state"] == "mapping_estimate"
    source.payload["mapping"]["visual_localization"]["capture_evidence"]["sequence"] += 1
    visual = mapping_visual(source)
    assert visual["state"] == "mapping_unavailable" and not visual["points"]
    assert visual["properties"]["mapping_reason"] == "mapping_frame_mismatch"


@pytest.mark.parametrize("reason", [
    "panorama_visual_support_insufficient", "panorama_frame_binding_mismatch",
    "panorama_projection_outside_support",
])
def test_mapping_inspection_preserves_operator_failure_during_motion(reason):
    source = packet()
    source.payload.update(
        mapping={"status": "unmapped", "reason": reason, "composition_id": "synthetic"},
        pan_tilt_zoom_state={"geometry_safe": False, "move_status": "moving"},
    )
    visual = InspectionVisual.model_validate(mapping_visual(source))
    assert visual.state == "mapping_unavailable" and not visual.points
    assert visual.properties["mapping_reason"] == reason
    assert "world_x" not in visual.properties


@pytest.mark.parametrize("reason", [None, "", "invalid reason", "x" * 65])
def test_mapping_inspection_uses_pose_fallback_without_a_valid_operator_reason(reason):
    source = packet()
    source.payload.update(
        mapping={"status": "unmapped", "reason": reason},
        pan_tilt_zoom_state={"geometry_safe": False, "move_status": "moving"},
    )
    visual = InspectionVisual.model_validate(mapping_visual(source))
    assert visual.state == "mapping_unavailable" and not visual.points
    assert visual.properties["mapping_reason"] == "unsafe_pose"


def test_zone_filter_and_occurrence_report_observed_membership_without_reprojecting_boundaries():
    async def scenario():
        source = packet()
        source.payload.update(world={"x": 5, "z": 5}, mapping={"status": "mapped", "u": .5, "v": .5,
            "calibrated_view_id": "fixed", "composition_id": "synthetic"})
        for runtime, expected, count in [
            (AreaRestrictionRuntime({"areas": [AREA]}), "zone_inside", 1),
            (AreaRestrictionRuntime({"areas": [AREA], "exclude_area_names": [AREA["name"]]}), "zone_excluded", 0),
            (AreaOccurrenceRuntime({"area": AREA, "minimum_duration_seconds": 0}), "zone_inside", 1),
        ]:
            context = Context("camera.area_restriction")
            try:
                outputs = await runtime.process_packet(source, context)
                assert len(outputs) == count
                visual = context.snapshot().visuals[0]
                assert visual.state == expected and visual.properties["zone_name"] == AREA["name"]
                assert visual.kind == "points" and len(visual.points) == 1
            finally:
                context.close()
    asyncio.run(scenario())


def test_diagnostic_builder_failure_is_isolated_and_inactive_inspection_does_no_work():
    async def scenario():
        calls = []
        def broken():
            calls.append(True)
            raise ValueError("synthetic diagnostic failure")
        await inspect_packet_visuals(None, packet(), broken)
        assert not calls
        context = Context("camera.camera_mapping")
        try:
            await inspect_packet_visuals(context, packet(), broken)
            assert calls == [True] and not context.snapshot().visuals
        finally:
            context.close()
    asyncio.run(scenario())
