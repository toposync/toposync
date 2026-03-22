from __future__ import annotations

import asyncio

import pytest


def test_camera_image_crop_operator_crops_frame_and_keeps_original() -> None:
    async def scenario() -> None:
        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("numpy is required for this test") from exc

        from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.operators import ObjectDetectionYOLORuntime, YoloObject
        from toposync_ext_cameras.pipelines.postprocess import ImageCropRuntime

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
        crop = ImageCropRuntime(
            {
                "units": "percent",
                "left": 25.0,
                "top": 10.0,
                "right": 75.0,
                "bottom": 60.0,
                "output_artifact_name": "frame_cropped",
                "set_stream_frame": True,
                "min_crop_size_px": 8,
            },
            deps,
        )
        out_packets = await crop.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]

        assert "frame_original" in out.artifacts
        assert "frame" in out.artifacts
        assert "frame_cropped" in out.artifacts

        cropped = out.artifacts["frame"].data
        assert cropped is not None
        assert tuple(getattr(cropped, "shape", ())) == (50, 100, 3)
        assert out.payload.get("frame_width") == 100
        assert out.payload.get("frame_height") == 50

        crop_payload = out.payload.get("frame_crop")
        assert isinstance(crop_payload, dict)
        assert crop_payload.get("set_stream_frame") is True
        assert crop_payload.get("bbox01") == pytest.approx([0.25, 0.10, 0.75, 0.60], abs=1e-6)

        meta = out.artifacts["frame_cropped"].metadata
        assert isinstance(meta, dict)
        assert meta.get("bbox_px_total") == [50, 10, 150, 60]

        yolo = ObjectDetectionYOLORuntime({}, deps)
        normalized = yolo._normalize_objects(  # noqa: SLF001
            [YoloObject(tracking_id=None, category="person", confidence=0.9, bbox01=(0.0, 0.0, 1.0, 1.0))],
            packet=out,
        )
        assert normalized[0].bbox01 == pytest.approx((0.25, 0.10, 0.75, 0.60), abs=1e-6)

    asyncio.run(scenario())
