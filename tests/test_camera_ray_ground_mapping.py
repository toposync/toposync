from __future__ import annotations

import asyncio
import time
from typing import Literal

import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Packet
from toposync_ext_cameras.area_events import AreaOccurrenceRuntime
from toposync_ext_vision.processing.tasks.tracking import VisionTrackRuntime
from toposync_ext_cameras.pipelines.postprocess import (
    CameraMappingRuntime,
    _parse_calibrated_views_as_control_point_sets,
)
from toposync_ext_cameras.processing.mapping import (
    GroundCalibrationPoint,
    GroundLens,
    GroundPlaneMapper,
    GroundProjectionSpec,
)


def _point(identifier: str, role: Literal["fit", "check"], u: float, v: float) -> GroundCalibrationPoint:
    return GroundCalibrationPoint(
        id=identifier,
        role=role,
        image_u=u,
        image_v=v,
        world_x=u * 10.0,
        world_z=v * 10.0,
    )


def test_ray_ground_mapping_maps_only_the_proven_ground_region() -> None:
    projection = GroundProjectionSpec(
        lens=GroundLens(type="identity_rectilinear_v1"),
        points=(
            _point("p1", "fit", 0.1, 0.1),
            _point("p2", "fit", 0.9, 0.1),
            _point("p3", "fit", 0.9, 0.9),
            _point("p4", "fit", 0.1, 0.9),
            _point("p5", "fit", 0.5, 0.1),
            _point("p6", "fit", 0.5, 0.9),
            _point("c1", "check", 0.3, 0.3),
            _point("c2", "check", 0.7, 0.7),
        ),
    )

    mapper = GroundPlaneMapper(projection)

    assert mapper.quality.status == "ready"
    assert mapper.map(0.5, 0.5) == pytest.approx((5.0, 5.0))
    assert mapper.map(0.05, 0.5) is None
    assert mapper.map_world_to_image(5.0, 5.0) == pytest.approx((0.5, 0.5))
    assert mapper.map_world_to_image(0.5, 5.0) is None


def test_tracking_does_not_place_an_unmapped_person_at_another_person_position() -> None:
    class Context:
        async def run_blocking(self, function, /, *args, **kwargs):
            kwargs.pop("concurrency_key", None)
            return function(*args, **kwargs)

    async def scenario() -> None:
        dependencies = PipelineRuntimeDependencies()
        mapping = CameraMappingRuntime(
            {"calibrated_views": [{
                "id": "fixed",
                "stream_scope": {"physical_view_id": "fixed", "compatible_source_ids": ["main"]},
                "projection_model": {
                    "type": "camera_ray_ground_v2",
                    "source_geometry": {"width": 640, "height": 480},
                    "lens": {"type": "identity_rectilinear_v1"},
                    "correspondences": [
                        {"id": f"p{index}", "role": role,
                         "image": {"x": u, "y": v}, "world": {"x": u * 10, "z": v * 10}}
                        for index, (role, u, v) in enumerate([
                            ("fit", .1, .1), ("fit", .9, .1), ("fit", .9, .9),
                            ("fit", .1, .9), ("fit", .5, .1), ("fit", .5, .9),
                            ("check", .3, .3), ("check", .7, .7),
                        ])
                    ],
                },
                "projection_quality": {"status": "ready"},
            }]}, dependencies,
        )
        tracking = VisionTrackRuntime({"default_interval_seconds": 0}, dependencies)
        area = AreaOccurrenceRuntime({
            "area": {"id": "inside", "name": "Inside", "points": [
                {"x": 2, "z": 4}, {"x": 4, "z": 4},
                {"x": 4, "z": 6}, {"x": 2, "z": 6},
            ]},
            "minimum_duration_seconds": 0,
        })
        packet = Packet.create(stream_id="camera:test", payload={
            "camera_id": "camera", "frame_ts": 100,
            "frame_width": 640, "frame_height": 480,
            "source": {"source_id": "main", "view_id": "fixed"},
            "capture_evidence": {"capture_instance": "test", "generation": 1,
                                 "sequence": 1, "published_at": time.time()},
            "vision": {"detections": [
                {"label": "person", "label_id": 1, "model_id": "fixture",
                 "score": .95, "bbox01": [.2, .2, .4, .5]},
                {"label": "person", "label_id": 1, "model_id": "fixture",
                 "score": .9, "bbox01": [.92, .2, .98, .5]},
            ]},
        })
        try:
            mapped = (await mapping.process_packet(packet, Context()))[0]
            detections = mapped.payload["vision"]["detections"]
            assert "world_anchor" in detections[0]
            assert "world_anchor" not in detections[1]
            tracks = await tracking.process_packet(mapped, Context())
            assert len(tracks) == 2
            unlocated = next(p for p in tracks if p.payload["subject"]["bbox01"][0] > .9)
            assert "world" not in unlocated.payload
            assert "world_anchor" not in unlocated.payload
            assert "world_anchor" not in unlocated.payload["subject"]
            assert unlocated.payload.get("mapping", {}).get("status") != "mapped"
            events = []
            for tracked in tracks:
                events.extend(await area.process_packet(tracked, Context()))
            assert len(events) == 1
        finally:
            await tracking.shutdown()

    asyncio.run(scenario())


def test_ray_ground_mapping_supports_opencv_eight_term_brown_profile() -> None:
    projection = GroundProjectionSpec(
        lens=GroundLens(
            type="rectilinear_brown_v1",
            fx=1.0,
            fy=1.0,
            cx=0.5,
            cy=0.5,
            coefficients=(0.01, -0.002, 0.0, 0.0, 0.0001, 0.001, -0.0002, 0.00001),
        ),
        points=(
            _point("p1", "fit", 0.2, 0.2),
            _point("p2", "fit", 0.8, 0.2),
            _point("p3", "fit", 0.8, 0.8),
            _point("p4", "fit", 0.2, 0.8),
            _point("p5", "fit", 0.5, 0.2),
            _point("p6", "fit", 0.2, 0.5),
            _point("c1", "check", 0.5, 0.5),
            _point("c2", "check", 0.6, 0.6),
        ),
    )

    mapper = GroundPlaneMapper(projection)

    assert mapper.quality.status == "ready"
    assert mapper.map_world_to_image(5.0, 5.0) == pytest.approx((0.5, 0.5), abs=0.01)


def test_ray_ground_view_requires_a_ready_revision_and_matching_physical_view() -> None:
    view = {
        "id": "wide",
        "label": "Wide",
        "stream_scope": {
            "physical_view_id": "wide",
            "compatible_source_ids": ["wide_main"],
            "compatible_roles": ["main"],
        },
        "projection_model": {
            "type": "camera_ray_ground_v2",
            "source_geometry": {"width": 1920, "height": 1080},
            "lens": {"type": "identity_rectilinear_v1"},
            "correspondences": [
                {
                    "id": f"p{index}",
                    "role": "fit",
                    "image": {"x": u, "y": v},
                    "world": {"x": u * 10.0, "z": v * 10.0},
                }
                for index, (u, v) in enumerate(
                    ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9), (0.5, 0.1), (0.5, 0.9)),
                    start=1,
                )
            ]
            + [
                {
                    "id": "c1",
                    "role": "check",
                    "image": {"x": 0.3, "y": 0.3},
                    "world": {"x": 3.0, "z": 3.0},
                },
                {
                    "id": "c2",
                    "role": "check",
                    "image": {"x": 0.7, "y": 0.7},
                    "world": {"x": 7.0, "z": 7.0},
                },
            ],
        },
        "projection_quality": {"status": "ready"},
    }

    parsed = _parse_calibrated_views_as_control_point_sets([view])

    assert len(parsed) == 1
    assert parsed[0].physical_view_id == "wide"
    assert parsed[0].ground_projection is not None
    assert _parse_calibrated_views_as_control_point_sets(
        [{**view, "projection_quality": {"status": "incomplete"}}]
    ) == []


def test_ray_ground_runtime_maps_inside_the_hull_and_clears_stale_anchors() -> None:
    async def scenario() -> None:
        calibrated_view = {
            "id": "wide",
            "label": "Wide",
            "stream_scope": {
                "physical_view_id": "wide",
                "compatible_source_ids": ["wide_main"],
                "compatible_roles": ["main"],
            },
            "projection_model": {
                "type": "camera_ray_ground_v2",
                "source_geometry": {"width": 1920, "height": 1080},
                "lens": {"type": "identity_rectilinear_v1"},
                "correspondences": [
                    {
                        "id": f"p{index}",
                        "role": "fit",
                        "image": {"x": u, "y": v},
                        "world": {"x": u * 10.0, "z": v * 10.0},
                    }
                    for index, (u, v) in enumerate(
                        ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9), (0.5, 0.1), (0.5, 0.9)),
                        start=1,
                    )
                ]
                + [
                    {"id": "c1", "role": "check", "image": {"x": 0.3, "y": 0.3}, "world": {"x": 3.0, "z": 3.0}},
                    {"id": "c2", "role": "check", "image": {"x": 0.7, "y": 0.7}, "world": {"x": 7.0, "z": 7.0}},
                ],
            },
            "projection_quality": {"status": "ready"},
        }
        runtime = CameraMappingRuntime(
            {"calibrated_views": [calibrated_view]}, PipelineRuntimeDependencies()
        )
        source = {"source_id": "wide_main", "role": "main", "view_id": "wide"}
        mapped = await runtime.process_packet(
            Packet.create(
                stream_id="camera:wide",
                payload={"camera_id": "front", "source": source, "image_uv": {"u": 0.5, "v": 0.5}},
            ),
            None,
        )
        assert mapped[0].payload["world"] == pytest.approx({"x": 5.0, "z": 5.0})
        assert mapped[0].payload["mapping"]["status"] == "mapped"

        unmapped = await runtime.process_packet(
            Packet.create(
                stream_id="camera:wide",
                payload={
                    "camera_id": "front",
                    "source": source,
                    "image_uv": {"u": 0.02, "v": 0.5},
                    "world": {"x": 99.0, "z": 99.0},
                    "world_anchor": {"x": 99.0, "z": 99.0},
                    "vision": {"detections": [{"bbox01": [0.0, 0.4, 0.04, 0.5], "world_anchor": {"x": 99.0, "z": 99.0}}]},
                },
            ),
            None,
        )
        payload = unmapped[0].payload
        assert "world" not in payload
        assert "world_anchor" not in payload
        assert "world_anchor" not in payload["vision"]["detections"][0]
        assert payload["mapping"]["status"] == "unmapped"
        assert payload["mapping"]["reason"] == "outside_calibrated_ground"

        pose_required_runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    {**calibrated_view, "requires_pose_evidence": True}
                ]
            },
            PipelineRuntimeDependencies(),
        )
        pose_required = await pose_required_runtime.process_packet(
            Packet.create(
                stream_id="camera:wide",
                payload={"camera_id": "front", "source": source, "image_uv": {"u": 0.5, "v": 0.5}},
            ),
            None,
        )
        assert pose_required[0].payload["mapping"]["reason"] == "calibration_requires_numeric_pose"

    asyncio.run(scenario())


def test_ray_ground_view_takes_priority_over_a_legacy_quad_for_the_same_source() -> None:
    async def scenario() -> None:
        legacy = {
            "id": "legacy",
            "label": "Legacy",
            "projection_model": {
                "type": "image_quad_on_world",
                "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
                "world_quad": {
                    "top_left": {"x": 100.0, "z": 100.0},
                    "top_right": {"x": 110.0, "z": 100.0},
                    "bottom_right": {"x": 110.0, "z": 110.0},
                    "bottom_left": {"x": 100.0, "z": 110.0},
                },
            },
        }
        ray = {
            "id": "wide",
            "label": "Wide",
            "stream_scope": {"physical_view_id": "wide", "compatible_source_ids": ["wide_main"], "compatible_roles": ["main"]},
            "projection_model": {
                "type": "camera_ray_ground_v2",
                "source_geometry": {"width": 1920, "height": 1080},
                "lens": {"type": "identity_rectilinear_v1"},
                "correspondences": [
                    {"id": f"p{index}", "role": "fit", "image": {"x": u, "y": v}, "world": {"x": u * 10.0, "z": v * 10.0}}
                    for index, (u, v) in enumerate(((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9), (0.5, 0.1), (0.5, 0.9)), start=1)
                ] + [
                    {"id": "c1", "role": "check", "image": {"x": 0.3, "y": 0.3}, "world": {"x": 3.0, "z": 3.0}},
                    {"id": "c2", "role": "check", "image": {"x": 0.7, "y": 0.7}, "world": {"x": 7.0, "z": 7.0}},
                ],
            },
            "projection_quality": {"status": "ready"},
        }
        runtime = CameraMappingRuntime(
            {"calibrated_views": [legacy, ray]}, PipelineRuntimeDependencies()
        )
        result = await runtime.process_packet(
            Packet.create(
                stream_id="camera:wide",
                payload={
                    "camera_id": "front",
                    "source": {"source_id": "wide_main", "role": "main", "view_id": "wide"},
                    "image_uv": {"u": 0.5, "v": 0.5},
                },
            ),
            None,
        )
        assert result[0].payload["world"] == pytest.approx({"x": 5.0, "z": 5.0})
        assert result[0].payload["mapping"]["calibrated_view_id"] == "wide"

    asyncio.run(scenario())
