from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines.postprocess import (
    CameraMappingRuntime,
    _camera_mapping_diagnostics,
)


def _job() -> dict[str, Any]:
    # Independent pinhole oracle: camera at height 3, world plane (x, z).
    # A plane point produces direction (x, z, -3) in the camera's fixed frame.
    return {
        "id": "panorama-one",
        "revision": 3,
        "camera_id": "camera-one",
        "source_id": "wide",
        "composition_id": "yard",
        "profile": {
            "lens": {"width": 101, "height": 101, "fx": 50, "fy": 50, "cx": 50, "cy": 50},
            "pan_axis": {
                "position_min": -1,
                "position_max": 1,
                "angle_min_radians": -math.pi,
                "angle_max_radians": math.pi,
            },
            "tilt_axis": {
                "position_min": -1,
                "position_max": 1,
                "angle_min_radians": -math.pi / 2,
                "angle_max_radians": math.pi / 2,
            },
            "zoom": 0,
            "position_tolerance": 0.01,
        },
        "solution": {
            "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, -3]],
            "inverse_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, -1 / 3]],
            "support_polygon": [[-20, -20], [20, -20], [20, 20], [-20, 20]],
            "quality": {"status": "ready"},
        },
    }


def _packet() -> Packet:
    return Packet.create(
        stream_id="camera:camera-one:wide",
        payload={
            "source": {"device_id": "camera-one", "source_id": "wide", "view_id": "wide"},
            "media": {"width": 101, "height": 101},
            "frame_width": 101,
            "frame_height": 101,
            "image_uv": {"u": 0.5, "v": 0.5},
            "world": {"x": 99, "z": 99},
            "world_anchor": {"x": 99, "z": 99},
            "pan_tilt_zoom_state": {
                "pan": 0,
                "tilt": -0.5,
                "zoom": 0,
                "geometry_safe": True,
                "move_status": "IDLE",
                "motion_epoch": 7,
            },
            "vision": {
                key: [{"bbox01": [0.4, 0.4, 0.6, 0.5], "world_anchor": {"x": 99, "z": 99}}]
                for key in ("detections", "tracks", "segmentations")
            },
        },
        metadata={"ptz_geometry_safe": True, "ptz_motion_epoch": 7, "calibrated_view_id": "old"},
    )


def _services(active: Any) -> ServiceRegistry:
    services = ServiceRegistry()

    def get_active(**kwargs: Any) -> Any:
        assert kwargs["camera_id"] == "camera-one"
        assert kwargs["source_id"] == "wide"
        assert kwargs["composition_id"] == "yard"
        return active

    services.register("cameras.panorama.get_active", get_active)
    return services


def _run(packet: Packet, active: Any) -> Packet:
    runtime = CameraMappingRuntime(
        {"composition_id": "yard"},
        PipelineRuntimeDependencies(services=_services(active)),
    )
    return asyncio.run(runtime.process_packet(packet, None))[0]


def _assert_unmapped(result: Packet, reason: str) -> None:
    assert result.payload["mapping"]["status"] == "unmapped"
    assert result.payload["mapping"]["reason"] == reason
    assert "world" not in result.payload
    assert "world_anchor" not in result.payload
    assert "calibrated_view_id" not in result.metadata
    for key in ("detections", "tracks", "segmentations"):
        assert "world_anchor" not in result.payload["vision"][key][0]


@pytest.mark.parametrize("pan", [0.0, 0.5, -0.5, 1.0, -1.0])
def test_panorama_projects_the_current_pose_without_inventing_confidence(pan: float) -> None:
    packet = _packet()
    packet.payload["pan_tilt_zoom_state"]["pan"] = pan
    original = copy.deepcopy(packet.payload)
    result = _run(packet, {"applies": True, "job": _job()})

    expected = {"x": 3 * math.cos(pan * math.pi), "z": 3 * math.sin(pan * math.pi)}
    assert result.payload["world"] == pytest.approx(expected)
    assert result.payload["world_anchor"] == pytest.approx(expected)
    for key in ("detections", "tracks", "segmentations"):
        assert result.payload["vision"][key][0]["world_anchor"] == pytest.approx(expected)
    assert result.payload["mapping"]["status"] == "mapped"
    assert result.payload["mapping"]["panorama_revision"] == 3
    assert "confidence" not in result.payload["mapping"]
    assert "confidence" not in result.payload["world_anchor"]
    assert result.metadata["calibrated_view_id"] == "panorama-one"
    assert packet.payload == original


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"geometry_safe": False}, "panorama_pose_not_safe"),
        ({"move_status": "MOVING"}, "panorama_pose_not_safe"),
        ({"move_status": "UNKNOWN"}, "panorama_pose_not_safe"),
        ({"error": "unavailable"}, "panorama_pose_not_safe"),
        ({"pan": None}, "panorama_pose_not_safe"),
        ({"pan": True}, "panorama_pose_not_safe"),
        ({"tilt": math.nan}, "panorama_pose_not_safe"),
        ({"zoom": 0.1}, "panorama_zoom_changed"),
        ({"pan": 1.1}, "panorama_pose_outside_profile"),
        ({"motion_epoch": 8}, "panorama_pose_not_safe"),
    ],
)
def test_panorama_removes_stale_coordinates_when_pose_is_unsafe(
    changes: dict[str, Any], reason: str
) -> None:
    packet = _packet()
    packet.payload["pan_tilt_zoom_state"].update(changes)
    _assert_unmapped(_run(packet, {"applies": True, "job": _job()}), reason)


def test_panorama_never_fetches_a_new_pose_for_a_queued_frame_without_pose_evidence() -> None:
    packet = _packet()
    packet.payload.pop("pan_tilt_zoom_state")
    _assert_unmapped(_run(packet, {"applies": True, "job": _job()}), "panorama_pose_not_safe")


@pytest.mark.parametrize("geometry", ["missing", "size", "crop", "warp"])
def test_panorama_rejects_unknown_or_changed_source_geometry(geometry: str) -> None:
    packet = _packet()
    if geometry == "missing":
        for key in ("media", "frame_width", "frame_height"):
            packet.payload.pop(key)
    elif geometry == "size":
        packet.payload["frame_width"] = 102
    else:
        packet.payload[f"frame_{geometry}"] = {"applied": True}
    _assert_unmapped(
        _run(packet, {"applies": True, "job": _job()}), "panorama_source_geometry_changed"
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("source_id", "other", "panorama_source_mismatch"),
        ("composition_id", "other", "panorama_composition_mismatch"),
        ("camera_id", "other", "panorama_source_mismatch"),
        ("profile", {}, "panorama_invalid_calibration"),
        ("solution", {}, "panorama_calibration_not_ready"),
    ],
)
def test_panorama_requires_the_exact_source_composition_and_valid_profile(
    field: str, value: Any, reason: str
) -> None:
    job = _job()
    job[field] = value
    _assert_unmapped(_run(_packet(), {"applies": True, "job": job}), reason)


def test_panorama_excludes_horizon_and_outside_support_without_discarding_valid_detections() -> (
    None
):
    packet = _packet()
    packet.payload["image_uv"] = {"u": 0.5, "v": 0.0}  # Optical top points at the horizon.
    result = _run(packet, {"applies": True, "job": _job()})
    assert "world" not in result.payload
    assert result.payload["vision"]["detections"][0]["world_anchor"] == pytest.approx(
        {"x": 3, "z": 0}
    )

    job = _job()
    job["solution"]["support_polygon"] = [[-1, -1], [1, -1], [1, 1], [-1, 1]]
    _assert_unmapped(_run(_packet(), {"applies": True, "job": job}), "outside_calibrated_ground")


@pytest.mark.parametrize(
    "active", [None, {"applies": True}, {"applies": True, "reason": "source_changed"}]
)
def test_panorama_invalid_active_never_falls_back_to_legacy(active: Any) -> None:
    _assert_unmapped(
        _run(_packet(), active),
        active.get("reason", "panorama_unavailable")
        if isinstance(active, dict)
        else "panorama_unavailable",
    )


@pytest.mark.parametrize("service_present", [False, True])
def test_no_panorama_preserves_the_existing_legacy_mapping(service_present: bool) -> None:
    services = _services({"applies": False}) if service_present else ServiceRegistry()
    runtime = CameraMappingRuntime(
        {
            "composition_id": "yard",
            "control_point_sets": [
                {
                    "id": "legacy",
                    "control_points": [
                        {
                            "id": str(index),
                            "image": {"x": u, "y": v},
                            "world": {"x": 10 * u, "z": 10 * v},
                        }
                        for index, (u, v) in enumerate(((0, 0), (1, 0), (1, 1), (0, 1)))
                    ],
                }
            ],
        },
        PipelineRuntimeDependencies(services=services),
    )
    packet = _packet()
    packet.payload.pop("pan_tilt_zoom_state")
    result = asyncio.run(runtime.process_packet(packet, None))[0]
    assert result.payload["world"] == pytest.approx({"x": 5, "z": 5})
    assert result.payload["mapping"]["calibrated_view_id"] == "legacy"


def test_panorama_active_reference_is_recognized_by_pipeline_diagnostics() -> None:
    reference = {"job_id": "panorama-one", "revision": 3, "source_id": "wide", "status": "ready"}
    context = {
        "compositions": [
            {
                "id": "yard",
                "elements": [{"props": {"camera_id": "camera-one", "panorama_mapping": reference}}],
            }
        ]
    }
    config = {"camera_id": "camera-one", "composition_id": "yard"}
    assert _camera_mapping_diagnostics(config, context) == []
    reference["status"] = "draft"
    assert (
        _camera_mapping_diagnostics(config, context)[0].code
        == "camera_mapping_control_points_missing"
    )


@pytest.mark.parametrize(
    "error", [KeyError("private state unavailable"), RuntimeError("read failed")]
)
def test_active_service_failure_clears_coordinates_without_leaking_error_details(
    error: Exception,
) -> None:
    services = ServiceRegistry()

    def unavailable(**_kwargs: Any) -> Any:
        raise error

    services.register("cameras.panorama.get_active", unavailable)
    runtime = CameraMappingRuntime(
        {"composition_id": "yard"}, PipelineRuntimeDependencies(services=services)
    )
    result = asyncio.run(runtime.process_packet(_packet(), None))[0]
    _assert_unmapped(result, "panorama_unavailable")


def _visual_runtime_case(*, localization_failure=None, mutate=None, change_packet=None):
    import numpy as np
    from toposync.runtime.pipelines.runtime import Artifact

    job = _job()
    lens = job.pop("profile")["lens"]
    job["source_panorama"] = {"id": "artifact", "geometry": {"model_digest": "geometry"}}
    packet = _packet().with_artifact(Artifact(name="main", data=np.zeros((101, 101, 3), np.uint8)))
    packet.payload.pop("pan_tilt_zoom_state")
    packet.metadata["ptz_geometry_safe"] = False
    packet.payload["capture_evidence"] = {"capture_instance": "decoder-one", "generation": 1, "sequence": 50}
    from toposync.runtime.pipelines.image_geometry import image_geometry
    packet = packet.with_artifact(Artifact(name="main", data=packet.artifacts["main"].data,
        metadata={"image_geometry": image_geometry(101, 101, packet.payload["capture_evidence"])}))
    if change_packet:
        packet = change_packet(packet)
    services = _services({"applies": True, "job": job})
    calls = []

    def localize(**arguments):
        calls.append(arguments)
        assert arguments["image"] is packet.artifacts["main"].data
        if localization_failure:
            return {"status": "unlocalized", "reason": localization_failure}
        angle = -math.pi / 4
        result = {"status": "localized", "lens": lens,
                  "_coverage_mask": np.ones((16, 32), np.uint8),
                  "rotation_matrix": [[1, 0, 0], [0, math.cos(angle), -math.sin(angle)], [0, math.sin(angle), math.cos(angle)]],
                  "capture_evidence": dict(arguments["capture_evidence"]), "source_artifact_id": "artifact",
                  "geometry_digest": "geometry", "source_id": "wide", "revision": 3}
        if mutate:
            mutate(result)
        return result

    services.register("cameras.panorama.localize_frame", localize)
    runtime = CameraMappingRuntime({"composition_id": "yard"}, PipelineRuntimeDependencies(services=services))
    return asyncio.run(runtime.process_packet(packet, None))[0], calls


def test_visual_mapping_localizes_exact_frame_once_for_all_detections_without_numeric_pose():
    result, calls = _visual_runtime_case()
    assert len(calls) == 1
    assert result.payload["mapping"]["pose_evidence"] == "visual_frame_rotation"
    assert result.payload["world"] == pytest.approx({"x": 3, "z": 0})
    for kind in ("detections", "tracks", "segmentations"):
        assert result.payload["vision"][kind][0]["world_anchor"] == pytest.approx({"x": 3, "z": 0})


@pytest.mark.parametrize("field,value", [("capture_evidence", {}), ("source_artifact_id", "different"), ("geometry_digest", "different"), ("revision", 4), ("source_id", "different")])
def test_visual_mapping_rejects_cross_frame_or_cross_geometry_evidence(field, value):
    result, _ = _visual_runtime_case(mutate=lambda response: response.update({field: value}))
    _assert_unmapped(result, "panorama_frame_binding_mismatch")


def test_visual_localization_failure_preserves_raw_detections_and_removes_stale_world():
    result, _ = _visual_runtime_case(localization_failure="panorama_visual_localization_ambiguous")
    _assert_unmapped(result, "panorama_visual_localization_ambiguous")
    assert result.payload["vision"]["detections"][0]["bbox01"] == [.4, .4, .6, .5]


def test_visual_mapping_does_not_fill_a_panorama_coverage_hole():
    import numpy as np
    result, _ = _visual_runtime_case(mutate=lambda response: response.update(_coverage_mask=np.zeros((16, 32), np.uint8)))
    _assert_unmapped(result, "outside_calibrated_ground")


@pytest.mark.parametrize("stale", [False, True])
def test_visual_mapping_uses_transformed_anchor_only_for_its_own_frame(stale):
    import numpy as np
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync.runtime.pipelines.image_geometry import image_geometry
    def change(packet):
        evidence = packet.payload['capture_evidence']
        geometry = image_geometry(101, 101, evidence)
        geometry.update(image_size=[51, 51], to_source=[[2, 0, 0], [0, 2, 0], [0, 0, 1]])
        packet = packet.with_artifact(Artifact(name='main', data=np.zeros((51, 51, 3), np.uint8), metadata={
            'image_geometry': geometry, 'resized_from': {'width': 101, 'height': 101}}))
        for values in packet.payload['vision'].values():
            values[0]['source_anchor'] = {'uv': [.5, .5], 'capture_evidence': {**evidence, 'sequence': 49} if stale else evidence}
        return packet
    result, calls = _visual_runtime_case(change_packet=change)
    assert len(calls) == 1
    if stale:
        _assert_unmapped(result, 'outside_calibrated_ground')
    else:
        assert result.payload['mapping']['status'] == 'mapped'
        assert 'world_anchor' not in result.payload  # unbound top-level point
        assert result.payload['vision']['detections'][0]['world_anchor'] == pytest.approx({'x': 3, 'z': 0})


def test_visual_mapping_refuses_same_sized_image_without_pixel_provenance():
    from toposync.runtime.pipelines.runtime import Artifact
    def change(packet):
        return packet.with_artifact(Artifact(name="main", data=packet.artifacts["main"].data))
    result, calls = _visual_runtime_case(change_packet=change)
    assert not calls
    _assert_unmapped(result, "panorama_source_geometry_changed")
