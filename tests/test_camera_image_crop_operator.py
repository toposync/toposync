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
                "main": Artifact(name="main", data=frame, mime_type="image/raw", metadata={"source": "test"}),
                "aux": Artifact(name="aux", data=frame, mime_type="image/raw", metadata={"source": "test", "derived_from": "main"}),
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
                "min_crop_size_px": 8,
            },
            deps,
        )
        out_packets = await crop.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]

        assert "main" in out.artifacts
        assert "frame" not in out.artifacts

        cropped = out.artifacts["main"].data
        assert cropped is not None
        assert tuple(getattr(cropped, "shape", ())) == (50, 100, 3)
        assert out.payload.get("frame_width") == 100
        assert out.payload.get("frame_height") == 50

        crop_payload = out.payload.get("frame_crop")
        assert isinstance(crop_payload, dict)
        assert crop_payload.get("bbox01") == pytest.approx([0.25, 0.10, 0.75, 0.60], abs=1e-6)

        meta = out.artifacts["main"].metadata
        assert isinstance(meta, dict)
        assert meta.get("bbox_px_total") == [50, 10, 150, 60]

        yolo = ObjectDetectionYOLORuntime({}, deps)
        normalized = yolo._normalize_objects(  # noqa: SLF001
            [YoloObject(tracking_id=None, category="person", confidence=0.9, bbox01=(0.0, 0.0, 1.0, 1.0))],
            packet=out,
        )
        assert normalized[0].bbox01 == pytest.approx((0.25, 0.10, 0.75, 0.60), abs=1e-6)

    asyncio.run(scenario())


def test_snapshot_bound_crop_rejects_changed_or_unknown_provenance() -> None:
    from copy import deepcopy
    from unittest.mock import Mock
    from types import SimpleNamespace

    import numpy as np

    from toposync.runtime.pipelines.execution import NodeRuntimeMetrics, PipelineRuntimeDependencies
    from toposync.runtime.pipelines.image_geometry import image_geometry
    from toposync.runtime.pipelines.runtime import Artifact, Packet
    from toposync_ext_cameras.pipelines.postprocess import ImageCropConfig, ImageCropRuntime

    binding = dict(camera_id="camera", source_id="water", capture_instance="decoder", generation=1,
                   width=200, height=100)
    config = dict(left=25, top=10, right=75, bottom=60, snapshot_binding=binding)
    evidence = dict(capture_instance="decoder", generation=1, sequence=3)
    payload = dict(source=dict(device_id="camera", source_id="water"), capture_evidence=evidence)
    metadata = dict(image_geometry=image_geometry(200, 100, evidence))

    async def scenario() -> None:
        snapshots = Mock()
        metrics = NodeRuntimeMetrics()
        context = SimpleNamespace(metrics=metrics)
        runtime = ImageCropRuntime(config, PipelineRuntimeDependencies(pipeline_snapshot_store=snapshots))
        for failure in (None, "camera", "source", "session", "generation", "missing", "ptz",
                        "size", "geometry", "transform", "artifact_evidence", "boolean_generation", None):
            current_payload, current_metadata = deepcopy(payload), deepcopy(metadata)
            frame = np.zeros((100, 200, 3), dtype=np.uint8)
            if failure == "camera":
                current_payload["source"]["device_id"] = "other"
            elif failure == "source":
                current_payload["source"]["source_id"] = "other"
            elif failure == "session":
                current_payload["capture_evidence"]["capture_instance"] = "restarted"
            elif failure == "generation":
                current_payload["capture_evidence"]["generation"] = 2
            elif failure == "boolean_generation":
                current_payload["capture_evidence"]["generation"] = True
            elif failure == "missing":
                current_payload.pop("capture_evidence")
            elif failure == "ptz":
                current_payload["pan_tilt_zoom_state"] = None
            elif failure == "size":
                frame = np.zeros((101, 200, 3), dtype=np.uint8)
            elif failure == "geometry":
                current_metadata = {}
            elif failure == "transform":
                current_metadata["image_geometry"]["to_source"][0][2] = 20
            elif failure == "artifact_evidence":
                current_metadata["image_geometry"]["capture_evidence"]["sequence"] = 2
            packet = Packet.create(stream_id="camera:water", payload=current_payload,
                                   artifacts={"main": Artifact(name="main", data=frame,
                                                               mime_type="image/raw", metadata=current_metadata)})
            snapshots.reset_mock()
            output = await runtime.process_packet(packet, context)
            if failure is None:
                assert output[0].artifacts["main"].data.shape == (50, 100, 3)
                snapshots.schedule_input_snapshot.assert_called_once()
                assert metrics.unavailable_reason is None
            else:
                assert output == [], failure
                assert metrics.unavailable_reason == ("ext.cameras.cat.regionPtzUnavailable" if failure == "ptz" else "ext.cameras.cat.regionReconfirm")
                snapshots.schedule_input_snapshot.assert_not_called()
            assert "original" not in packet.artifacts
        assert metrics.error_count == 0
        assert metrics.dropped_packets == 11
        assert metrics.snapshot()["unavailable_reason"] is None

    asyncio.run(scenario())
    for field, value in (("generation", True), ("generation", -1), ("width", 0),
                         ("capture_instance", " "), ("source_id", "")):
        with pytest.raises(ValueError):
            ImageCropConfig.model_validate({**config, "snapshot_binding": {**binding, field: value}})
    for coords in (dict(right=101), dict(left=75), dict(top=60)):
        with pytest.raises(ValueError):
            ImageCropConfig.model_validate({**config, **coords})
