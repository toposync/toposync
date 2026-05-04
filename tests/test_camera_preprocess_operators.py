from __future__ import annotations

import asyncio


def test_camera_local_contrast_clahe_operator_increases_dynamic_range() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import LocalContrastCLAHERuntime

        base = np.linspace(110, 120, 64, dtype=np.uint8)
        frame = np.repeat(base[None, :, None], 64, axis=0)
        frame = np.repeat(frame, 3, axis=2)

        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 64, "frame_height": 64},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        op = LocalContrastCLAHERuntime(
            {
                "clip_limit": 2.0,
                "tile_grid_size": [8, 8],
                "colorspace": "lab",
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "frame" not in out.artifacts

        clahe = out.artifacts["main"].data
        assert clahe is not None
        assert tuple(getattr(clahe, "shape", ())) == (64, 64, 3)

        in_range = int(frame.max()) - int(frame.min())
        out_range = int(clahe.max()) - int(clahe.min())
        assert out_range >= in_range
        assert out_range > 0

    asyncio.run(scenario())


def test_camera_unsharp_mask_operator_restores_high_frequency_detail() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        import cv2  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import UnsharpMaskRuntime

        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        frame[:, 32:, :] = 200
        blurred = cv2.GaussianBlur(frame, (0, 0), 2.0)

        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 64, "frame_height": 64},
            artifacts={
                "main": Artifact(
                    name="main",
                    data=blurred,
                    mime_type="image/raw",
                    metadata={"source": "test"},
                ),
                "aux": Artifact(
                    name="aux",
                    data=blurred,
                    mime_type="image/raw",
                    metadata={"source": "test", "derived_from": "main"},
                ),
            },
        )

        op = UnsharpMaskRuntime(
            {
                "amount": 1.0,
                "sigma": 1.2,
                "threshold": 0,
                "luma_only": True,
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        sharpened = out.artifacts["main"].data
        assert sharpened is not None

        blurred_gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
        sharp_gray = cv2.cvtColor(sharpened, cv2.COLOR_BGR2GRAY)
        var_before = float(cv2.Laplacian(blurred_gray, cv2.CV_64F).var())
        var_after = float(cv2.Laplacian(sharp_gray, cv2.CV_64F).var())
        assert var_after > var_before

    asyncio.run(scenario())


def test_camera_denoise_luma_operator_reduces_noise_variance() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        import cv2  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import DenoiseLumaRuntime

        rng = np.random.default_rng(0)
        noise = rng.normal(0.0, 35.0, size=(64, 64, 1)).astype(np.float32)
        base = np.full((64, 64, 1), 128.0, dtype=np.float32) + noise
        y = np.clip(np.round(base), 0.0, 255.0).astype(np.uint8)
        frame = np.repeat(y, 3, axis=2)

        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 64, "frame_height": 64},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        op = DenoiseLumaRuntime(
            {
                "method": "bilateral",
                "bilateral_diameter": 9,
                "bilateral_sigma_color": 90.0,
                "bilateral_sigma_space": 90.0,
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        denoised = out.artifacts["main"].data
        assert denoised is not None

        before = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        after = cv2.cvtColor(denoised, cv2.COLOR_BGR2GRAY).astype(np.float32)
        assert float(after.var()) < float(before.var())

    asyncio.run(scenario())


def test_camera_auto_gamma_operator_targets_configured_luminance() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import AutoGammaRuntime

        frame = np.full((8, 8, 3), 50, dtype=np.uint8)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 8, "frame_height": 8},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        op = AutoGammaRuntime(
            {
                "measurement": "mean",
                "target_luma": 0.5,
                "min_gamma": 0.1,
                "max_gamma": 5.0,
                "smoothing": 0.0,
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        adjusted = out.artifacts["main"].data
        assert adjusted is not None
        assert int(adjusted[0, 0, 0]) in range(120, 137)

        meta = out.artifacts["main"].metadata
        assert isinstance(meta, dict)
        assert float(meta.get("gamma") or 0.0) > 1.0

    asyncio.run(scenario())


def test_camera_global_stabilize_operator_reduces_translation_error() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import GlobalStabilizeRuntime

        base = np.zeros((64, 64, 3), dtype=np.uint8)
        base[20:44, 18:40, :] = 255

        shifted = np.zeros_like(base)
        dx, dy = 3, 2
        shifted[dy:, dx:, :] = base[:-dy, :-dx, :]

        op = GlobalStabilizeRuntime(
            {
                "response_threshold": 0.05,
                "max_translation_px": 20.0,
                "smoothing": 0.0,
                "interpolation": "nearest",
                "border_mode": "constant",
                "border_value": 0,
            },
        )

        packet1 = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 64, "frame_height": 64},
            artifacts={
                "main": Artifact(name="main", data=base, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=base, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )
        _ = await op.process_packet(packet1, None)

        packet2 = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 64, "frame_height": 64},
            artifacts={
                "main": Artifact(name="main", data=shifted, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=shifted, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )
        out_packets = await op.process_packet(packet2, None)
        assert len(out_packets) == 1
        stabilized = out_packets[0].artifacts["main"].data
        assert stabilized is not None

        diff_before = float(np.mean(np.abs(shifted.astype(np.int16) - base.astype(np.int16))))
        diff_after = float(np.mean(np.abs(stabilized.astype(np.int16) - base.astype(np.int16))))
        assert diff_after < diff_before

    asyncio.run(scenario())


def test_camera_lens_undistort_operator_is_noop_with_zero_distortion() -> None:
    async def scenario() -> None:
        import numpy as np  # type: ignore

        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import LensUndistortRuntime

        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        frame[8:24, 10:22, :] = 200

        fx = 100.0
        fy = 100.0
        cx = 16.0
        cy = 16.0
        K = [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]]

        op = LensUndistortRuntime(
            {
                "camera_matrix": K,
                "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
                "alpha": 0.0,
                "use_optimal_new_camera_matrix": True,
                "crop_to_valid_roi": False,
            },
        )

        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 32, "frame_height": 32},
            artifacts={
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
            },
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0].artifacts["main"].data
        assert out is not None
        assert tuple(getattr(out, "shape", ())) == (32, 32, 3)

        max_delta = int(np.max(np.abs(out.astype(np.int16) - frame.astype(np.int16))))
        assert max_delta <= 1

    asyncio.run(scenario())

