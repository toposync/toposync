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
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "frame": Artifact(name="frame", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "frame_original"}),
            },
        )

        deps = PipelineRuntimeDependencies()
        warp = ImagePerspectiveCropRuntime(
            {
                "units": "pixels",
                "points": [(50, 20), (150, 20), (150, 70), (50, 70)],
                "output_ratio_preset": "auto",
                "output_artifact_name": "frame_warped",
                "set_stream_frame": True,
                "min_output_edge_px": 8,
            },
            deps,
        )
        out_packets = await warp.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]

        assert "frame_original" in out.artifacts
        assert "frame" in out.artifacts
        assert "frame_warped" in out.artifacts

        warped = out.artifacts["frame"].data
        assert warped is not None
        assert tuple(getattr(warped, "shape", ())) == (50, 100, 3)
        assert out.payload.get("frame_width") == 100
        assert out.payload.get("frame_height") == 50

        warp_payload = out.payload.get("frame_warp")
        assert isinstance(warp_payload, dict)
        assert warp_payload.get("set_stream_frame") is True
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
