from __future__ import annotations

import asyncio

import pytest


def test_camera_image_perspective_crop_operator_warps_frame_and_maps_yolo_bbox_back() -> None:
    async def scenario() -> None:
        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("numpy is required for this test") from exc

        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectDetectionYOLORuntime, YoloObject
        from toposync_ext_cameras.pipelines.postprocess import ImagePerspectiveCropRuntime

        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_width": 200,
                "frame_height": 100,
            },
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        deps = PipelineRuntimeDependencies()
        warp = ImagePerspectiveCropRuntime(
            {
                "units": "pixels",
                "points": [(50, 20), (150, 20), (150, 70), (50, 70)],
                "output_ratio_preset": "auto",
                "min_output_edge_px": 8,
            },
            deps,
        )
        out_packets = await warp.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]

        assert "main" in out.artifacts
        assert "frame" not in out.artifacts

        warped = out.artifacts["main"].data
        assert warped is not None
        assert tuple(getattr(warped, "shape", ())) == (50, 100, 3)
        assert out.payload.get("frame_width") == 100
        assert out.payload.get("frame_height") == 50

        warp_payload = out.payload.get("frame_warp")
        assert isinstance(warp_payload, dict)
        assert warp_payload.get("source_frame_width") == 200
        assert warp_payload.get("source_frame_height") == 100
        assert warp_payload.get("dest_frame_width") == 100
        assert warp_payload.get("dest_frame_height") == 50
        assert isinstance(warp_payload.get("homography"), list)
        assert isinstance(warp_payload.get("homography_inv"), list)

        yolo = ObjectDetectionYOLORuntime({}, deps)
        normalized = yolo._normalize_objects(  # noqa: SLF001
            [YoloObject(tracking_id=None, category="person", confidence=0.9, bbox01=(0.0, 0.0, 1.0, 1.0))],
            packet=out,
        )
        assert normalized[0].bbox01 == pytest.approx(
            (50 / 199.0, 20 / 99.0, 150 / 199.0, 70 / 99.0),
            abs=2e-3,
        )

    asyncio.run(scenario())


def test_camera_image_perspective_crop_operator_handles_skewed_quad_ordering() -> None:
    async def scenario() -> None:
        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("numpy is required for this test") from exc

        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ImagePerspectiveCropRuntime

        frame = np.zeros((1620, 2880, 3), dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_width": 2880,
                "frame_height": 1620,
            },
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        deps = PipelineRuntimeDependencies()
        warp = ImagePerspectiveCropRuntime(
            {
                "units": "percent",
                "points": [(70.5, 17.5), (100.0, 24.0), (100.0, 39.0), (64.5, 26.5)],
                "output_ratio_preset": "auto",
                "min_output_edge_px": 8,
            },
            deps,
        )
        out_packets = await warp.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]

        warp_payload = out.payload.get("frame_warp")
        assert isinstance(warp_payload, dict)
        assert warp_payload.get("source_frame_width") == 2880
        assert warp_payload.get("source_frame_height") == 1620
        assert int(warp_payload.get("dest_frame_width") or 0) < 2880
        assert int(warp_payload.get("dest_frame_height") or 0) < 1620

        warped = out.artifacts["main"].data
        assert warped is not None
        warped_shape = tuple(getattr(warped, "shape", ()))
        assert warped_shape
        assert warped_shape[0] < 1620
        assert warped_shape[1] < 2880

    asyncio.run(scenario())
