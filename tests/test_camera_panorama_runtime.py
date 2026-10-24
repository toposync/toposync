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
            "world_projection": {"status": "stale"},
            "pan_tilt_zoom_state": {
                "pan": 0,
                "tilt": -0.5,
                "zoom": 0,
                "geometry_safe": True,
                "move_status": "IDLE",
                "motion_epoch": 7,
            },
            "vision": {
                key: [{"bbox01": [0.4, 0.4, 0.6, 0.5], "world_anchor": {"x": 99, "z": 99}, "metadata": {"world_projection": {"status": "stale"}, "preserved": True}}]
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
    assert result.payload.get("world_projection", {}).get("status") != "stale"
    assert "calibrated_view_id" not in result.metadata
    for key in ("detections", "tracks", "segmentations"):
        assert "world_anchor" not in result.payload["vision"][key][0]
        assert result.payload["vision"][key][0].get("metadata", {}).get("world_projection", {}).get("status") != "stale"
        assert result.payload["vision"][key][0]["metadata"]["preserved"] is True


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


def test_panorama_preserves_horizon_observations_and_extrapolates_positions() -> (
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
    result = _run(_packet(), {"applies": True, "job": job})
    assert result.payload["mapping"]["status"] == "mapped"
    assert result.payload["vision"]["detections"][0]["world_anchor"] == pytest.approx({"x": 3, "z": 0})
    assert result.payload["vision"]["detections"][0]["metadata"]["world_projection"]["extrapolated"] is True
    assert result.payload["world_projection"]["extrapolated"] is True


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


def test_mapping_delivers_same_frame_geometry_for_subject_extent_without_projecting_head_to_ground():
    import numpy as np
    from toposync_ext_ptz_attention.framing import observe_framing
    from toposync_ext_ptz_attention.models import AttentionTarget

    def change(packet):
        packet.payload["capture_evidence"]["published_at"] = 1000.
        packet.artifacts["main"].metadata["image_geometry"]["capture_evidence"] = dict(packet.payload["capture_evidence"])
        packet.payload["subject"] = {"bbox01": [.4, .4, .6, .5]}
        return packet

    localizations = []
    result, calls = _visual_runtime_case(change_packet=change, mutate=localizations.append)
    assert len(calls) == 1, "Framing must not run another localization or consult current device pose"
    localizations[0]["rotation_matrix"][0][0] = 2
    assert result.payload["mapping"]["visual_localization"]["rotation_matrix"][0][0] == 1
    observation = observe_framing(result, result.payload["subject"]["bbox01"],
        AttentionTarget(world_anchor=result.payload["world_anchor"]))
    expected = np.array([1.1, 0, -.9])
    expected /= np.linalg.norm(expected)
    assert observation.panorama.center_ray == pytest.approx(expected)
    assert observation.panorama.job_id == "panorama-one" and observation.panorama.revision == 3
    assert result.payload["world_anchor"] == pytest.approx({"x": 3, "z": 0})


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


def test_mixed_observations_keep_individual_positions_and_remove_stale_anchors():
    packet = _packet()
    job = _job()
    job["solution"]["support_polygon"] = [[2, -1], [4, -1], [4, 1], [2, 1]]
    # Optical centre maps to x=3, the lower edge to x=0, the top to the horizon.
    packet.payload["vision"]["detections"] = [
        {"id": label, "bbox01": [.4, 0, .6, v], "world_anchor": {"x": 99, "z": 99},
         "metadata": {"world_projection": {"status": "stale"}, "preserved": True}}
        for label, v in [("inside", .5), ("outside", 1), ("horizon", 0)]
    ]
    original = copy.deepcopy(packet.payload)
    result = _run(packet, {"applies": True, "job": job})
    inside, outside, horizon = result.payload["vision"]["detections"]
    assert inside["world_anchor"] == pytest.approx({"x": 3, "z": 0})
    assert inside["metadata"]["world_projection"]["extrapolated"] is False
    assert outside["world_anchor"] == pytest.approx({"x": 0, "z": 0}, abs=1e-8)
    assert outside["metadata"]["world_projection"]["extrapolated"] is True
    assert "world_anchor" not in horizon
    assert horizon["metadata"]["world_projection"]["reason"] == "no_ground_intersection"
    assert horizon["metadata"]["world_projection"]["direction_reference"] == "panorama"
    assert len(horizon["metadata"]["world_projection"]["panorama_ray"]) == 3
    assert [item["id"] for item in (inside, outside, horizon)] == ["inside", "outside", "horizon"]
    assert all(item["metadata"]["preserved"] for item in (inside, outside, horizon))
    assert packet.payload == original


def test_mapped_detection_metadata_survives_installed_tracker():
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime

    class Context:
        async def run_blocking(self, function, /, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    packet = _packet().with_artifact(Artifact("main", object()))
    packet.payload["vision"] = {"detections": [
        {"label": "person", "label_id": 0, "score": .95,
         "model_id": "test.detector", "bbox01": [.4, v - .2, .6, v],
         "metadata": {"identity": identity}}
        for identity, v in [("inside", .5), ("outside", 1)]
    ]}
    job = _job()
    job["solution"]["support_polygon"] = [[2, -1], [4, -1], [4, 1], [2, 1]]
    mapped = _run(packet, {"applies": True, "job": job})
    tracker = VisionTrackRuntime({"default_interval_seconds": 0}, PipelineRuntimeDependencies())
    outputs = asyncio.run(tracker.process_packet(mapped, Context()))
    tracks = [item for output in outputs for item in output.payload["vision"]["tracks"]]
    assert len(tracks) == 2
    by_identity = {item["metadata"]["identity"]: item for item in tracks}
    for identity, x, extrapolated in [("inside", 3, False), ("outside", 0, True)]:
        track = by_identity[identity]
        assert track["world_anchor"] == pytest.approx({"x": x, "z": 0}, abs=1e-8)
        assert track["metadata"]["world_projection"]["extrapolated"] is extrapolated
        assert track["metadata"]["world_projection"]["panorama_revision"] == job["revision"]


@pytest.mark.parametrize("category", ["person", "car"])
def test_moving_subject_keeps_world_trail_while_camera_rotates(category):
    import numpy as np
    import time
    from toposync.runtime.pipelines.spatial_history import SpatialHistory
    from toposync_ext_cameras.processing.panorama_mapping import map_world_to_ray, ray_to_image_pixel
    from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime

    class Context:
        async def run_blocking(self, function, /, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    async def scenario():
        tracker = VisionTrackRuntime({"tracker_id": "byte_world", "use_world_anchor": "always",
            "default_interval_seconds": 0, "world_match_distance_meters": 3}, PipelineRuntimeDependencies())
        history, identities, image_points = SpatialHistory(), set(), []
        expected = [(3., 0.), (3.3, .2), (3.6, .4)]
        started = time.time() - 1.5
        for index, ((x, z), yaw) in enumerate(zip(expected, [0, .15, -.15], strict=True)):
            angle = -math.pi / 4
            rotation = np.array([[math.cos(yaw), -math.sin(yaw), 0],
                [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]]) @ np.array([
                [1, 0, 0], [0, math.cos(angle), -math.sin(angle)], [0, math.sin(angle), math.cos(angle)]])
            job = _job()
            pixel = ray_to_image_pixel(map_world_to_ray(job["solution"], x, z),
                job["profile"]["lens"], rotation_matrix=rotation)
            assert pixel is not None
            u, v = pixel[0] / 100, pixel[1] / 100
            image_points.append((u, v))

            def update_packet(packet):
                packet.payload["capture_evidence"]["sequence"] += index
                packet.payload["capture_evidence"]["published_at"] = started + index * .5
                packet.artifacts["main"].metadata["image_geometry"]["capture_evidence"] = dict(packet.payload["capture_evidence"])
                packet.payload.update(frame_ts=started + index * .5, media={"ts": started + index * .5,
                    "width": 101, "height": 101},
                    pan_tilt_zoom_state={"geometry_safe": False, "move_status": "MOVING"},
                    vision={"detections": [{"label": category, "label_id": 0, "score": .95,
                        "model_id": "synthetic", "bbox01": [u-.04, v-.15, u+.04, v]}]})
                return packet

            # Mapping is exercised synchronously outside this tracker's event loop below.
            mapped, _ = await asyncio.to_thread(_visual_runtime_case,
                mutate=lambda result: result.update(rotation_matrix=rotation.tolist()),
                change_packet=update_packet)
            outputs = await tracker.process_packet(mapped, Context())
            assert len(outputs) == 1
            output = outputs[0]
            identities.add(output.payload["subject"]["id"])
            assert output.payload["subject"]["world_anchor"] == pytest.approx({"x": x, "z": z})
            history.observe(output)
        assert len(identities) == 1
        assert len(set(image_points)) == 3
        trail = history.snapshot()["trails"][0]
        np.testing.assert_allclose([(p["x"], p["z"]) for p in trail["points"]], expected, atol=1e-8)
        assert not history.snapshot()["had_mapping_gaps"]

    asyncio.run(scenario())


def _static_runtime_case(change=None):
    import numpy as np
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync.runtime.pipelines.image_geometry import image_geometry
    from toposync_ext_cameras.processing.panorama_mapping import estimate_image_plane_mapping

    job = _job()
    job.pop("profile")
    job["source_panorama"] = {"id": "artifact", "geometry": {"projection": "image_plane", "framing": "fixed", "source_size": [101, 101]}}
    job["solution"] = estimate_image_plane_mapping([
        {"id": str(index), "image_u": u, "image_v": v, "world_x": u * 10, "world_z": v * 10}
        for index, (u, v) in enumerate([(.1, .1), (.9, .1), (.9, .9), (.1, .9)])])
    packet = _packet()
    packet.payload.pop("pan_tilt_zoom_state")
    packet.metadata.clear()
    evidence = {"capture_instance": "static", "generation": 1, "sequence": 5}
    packet.payload["capture_evidence"] = evidence
    packet = packet.with_artifact(Artifact(name="main", data=np.zeros((101, 101, 3), np.uint8),
        metadata={"image_geometry": image_geometry(101, 101, evidence)}))
    if change:
        change(job, packet)
    return _run(packet, {"applies": True, "job": job})


def test_static_runtime_projects_without_pose_lens_or_visual_rotation():
    result = _static_runtime_case()
    assert result.payload["world_anchor"] == pytest.approx({"x": 5, "z": 5})
    assert result.payload["mapping"]["pose_evidence"] == "fixed_source_frame"
    assert result.payload["mapping"]["projection_model"] == "panorama_image_ground_v1"
    for key in ("detections", "tracks", "segmentations"):
        assert result.payload["vision"][key][0]["world_anchor"] == pytest.approx({"x": 5, "z": 5})
    assert "panorama_ray" not in result.payload["world_projection"]


@pytest.mark.parametrize("change,reason", [
    (lambda job, packet: job["source_panorama"]["geometry"].update(framing="movable"), "panorama_fixed_framing_required"),
    (lambda job, packet: packet.payload.update(capture_evidence={}), "panorama_frame_identity_missing"),
    (lambda job, packet: job["source_panorama"]["geometry"].update(source_size=[200, 100]), "panorama_source_geometry_changed"),
    (lambda job, packet: packet.artifacts["main"].metadata["image_geometry"].update(capture_evidence={}), "panorama_frame_binding_mismatch"),
])
def test_static_runtime_discards_stale_positions_when_reference_is_unusable(change, reason):
    _assert_unmapped(_static_runtime_case(change), reason)


@pytest.mark.parametrize("has_anchor", [True, False])
def test_static_runtime_uses_source_anchors_on_transformed_frames(has_anchor):
    import numpy as np
    from toposync.runtime.pipelines.runtime import Artifact
    from toposync.runtime.pipelines.image_geometry import image_geometry

    def change(job, packet):
        evidence = packet.payload["capture_evidence"]
        geometry = image_geometry(101, 101, evidence)
        geometry.update(image_size=[51, 51], to_source=[[.5, 0, .2], [0, .5, .3], [0, 0, 1]])
        packet.artifacts["main"] = Artifact(name="main", data=np.zeros((51, 51, 3), np.uint8), metadata={"image_geometry": geometry})
        packet.payload["frame_crop"] = {"synthetic": True}
        if has_anchor:
            packet.payload["subject"] = {"source_anchor": {"uv": [.4, .6], "capture_evidence": evidence}}
            for key in ("detections", "tracks", "segmentations"):
                packet.payload["vision"][key][0]["source_anchor"] = {"uv": [.4, .6], "capture_evidence": evidence}

    result = _static_runtime_case(change)
    if has_anchor:
        assert result.payload["world_anchor"] == pytest.approx({"x": 4, "z": 6})
    else:
        _assert_unmapped(result, "outside_calibrated_ground")
