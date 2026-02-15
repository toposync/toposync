from __future__ import annotations

import asyncio


def test_camera_image_adjust_operator_can_desaturate_frame() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ImageAdjustRuntime

        frame = np.array(
            [
                [[10, 50, 200], [20, 180, 60]],
                [[200, 40, 30], [120, 120, 10]],
            ],
            dtype=np.uint8,
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 2, "frame_height": 2},
            artifacts={
                "frame_original": Artifact(name="frame_original", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "frame": Artifact(name="frame", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "frame_original"}),
            },
        )

        op = ImageAdjustRuntime(
            {
                "input_artifact_names": ["frame_original"],
                "fallback_to_stream_frame": True,
                "output_artifact_name": "frame_adjusted",
                "saturation": 0.0,
                "brightness": 0.0,
                "contrast": 1.0,
                "gamma": 1.0,
                "set_stream_frame": True,
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "frame_original" in out.artifacts
        assert "frame" in out.artifacts
        assert "frame_adjusted" in out.artifacts

        adjusted = out.artifacts["frame"].data
        assert adjusted is not None
        assert tuple(getattr(adjusted, "shape", ())) == (2, 2, 3)
        assert (adjusted[..., 0] == adjusted[..., 1]).all()
        assert (adjusted[..., 1] == adjusted[..., 2]).all()

    asyncio.run(scenario())
