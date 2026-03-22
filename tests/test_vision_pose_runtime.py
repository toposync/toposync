from __future__ import annotations

import asyncio

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.pipelines import ModelRegistry, PoseObject, VisionPoseEstimateRuntime
from toposync_ext_vision.registry import ModelManifest


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        kwargs = dict(kwargs)
        kwargs.pop("concurrency_key", None)
        return func(*args, **kwargs)


def _build_registry() -> ModelRegistry:
    return ModelRegistry(
        [
            ModelManifest(
                model_id="fake.pose",
                display_name="Fake Pose",
                task="pose",
                runtime="onnxruntime",
                artifact_format="onnx",
                artifact_path="fake://pose",
            )
        ]
    )


def test_vision_pose_estimate_runtime_annotates_packet_and_links_tracking_ids() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake_pose"

            def estimate_pose(self, frame, *, detections=None):  # noqa: ANN001
                _ = frame
                assert isinstance(detections, list)
                assert detections and detections[0].label == "person"
                return [
                    PoseObject(
                        label="Person",
                        score=0.88,
                        bbox01=(0.2, 0.25, 0.8, 0.75),
                        keypoints=[(0.25, 0.25, 0.95), (0.75, 0.75, 0.80)],
                        model_id="",
                        metadata={"source": "fake"},
                    )
                ]

        deps = PipelineRuntimeDependencies(
            pose_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionPoseEstimateRuntime({"model_id": "fake.pose"}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "frame_crop": {
                    "bbox01": [0.25, 0.1, 0.75, 0.9],
                    "set_stream_frame": True,
                },
                "vision": {
                    "task": "tracking",
                    "detections": [
                        {
                            "label": "person",
                            "label_id": 0,
                            "score": 0.93,
                            "bbox01": [0.2, 0.25, 0.8, 0.75],
                            "model_id": "fake.detector",
                        }
                    ],
                    "tracks": [
                        {
                            "tracking_id": "trk:camera:test:7",
                            "label": "person",
                            "bbox01": [0.35, 0.3, 0.65, 0.7],
                            "model_id": "fake.detector",
                            "tracker_id": "simple_iou_kalman",
                        }
                    ],
                },
            },
            artifacts={
                "frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw"),
                "frame": Artifact(name="frame", data=object(), mime_type="image/raw"),
            },
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        vision = out.payload.get("vision", {})
        assert vision.get("task") == "pose"
        assert vision.get("model_id") == "fake.pose"
        assert vision.get("runtime") == "fake_pose"
        poses = vision.get("poses")
        assert isinstance(poses, list)
        assert poses[0]["tracking_id"] == "trk:camera:test:7"
        assert poses[0]["bbox01"] == [0.35, 0.30000000000000004, 0.65, 0.7000000000000001]
        assert poses[0]["keypoints"] == [
            [0.375, 0.30000000000000004, 0.95],
            [0.625, 0.7000000000000001, 0.8],
        ]
        assert out.payload.get("tracking_id") == "trk:camera:test:7"
        assert out.payload.get("object_category_label") == "person"
        assert out.payload.get("object_bbox01") == [0.35, 0.30000000000000004, 0.65, 0.7000000000000001]

    asyncio.run(scenario())
