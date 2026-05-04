from __future__ import annotations

import asyncio


def test_camera_image_privacy_operator_applies_black_region() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ImagePrivacyRuntime

        frame = np.array(
            [
                [[10, 20, 30], [40, 50, 60], [70, 80, 90], [100, 110, 120]],
                [[15, 25, 35], [45, 55, 65], [75, 85, 95], [105, 115, 125]],
                [[20, 30, 40], [50, 60, 70], [80, 90, 100], [110, 120, 130]],
                [[25, 35, 45], [55, 65, 75], [85, 95, 105], [115, 125, 135]],
            ],
            dtype=np.uint8,
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 4, "frame_height": 4},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        op = ImagePrivacyRuntime(
            {
                "units": "percent",
                "left": 25,
                "top": 25,
                "right": 75,
                "bottom": 75,
                "effect": "black",
                "min_region_size_px": 1,
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "main" in out.artifacts
        assert "frame" not in out.artifacts

        redacted = out.artifacts["main"].data
        assert redacted is not None
        assert tuple(getattr(redacted, "shape", ())) == (4, 4, 3)
        assert (redacted[1:3, 1:3] == 0).all()
        assert (redacted[0, 0] == frame[0, 0]).all()
        assert (redacted[3, 3] == frame[3, 3]).all()

        privacy = out.payload.get("frame_privacy")
        assert isinstance(privacy, dict)
        assert privacy.get("enabled") is True
        assert privacy.get("effect") == "black"

    asyncio.run(scenario())


def test_camera_image_privacy_operator_is_noop_without_valid_region() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ImagePrivacyRuntime

        frame = np.full((4, 4, 3), 90, dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 4, "frame_height": 4},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        op = ImagePrivacyRuntime(
            {
                "left": 0,
                "top": 0,
                "right": 0,
                "bottom": 0,
                "effect": "blur_medium",
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "main" in out.artifacts
        privacy = out.payload.get("frame_privacy")
        assert isinstance(privacy, dict)
        assert privacy.get("enabled") is False

    asyncio.run(scenario())
