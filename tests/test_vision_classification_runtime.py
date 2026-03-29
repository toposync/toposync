from __future__ import annotations

import asyncio

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.runtime import Artifact, Packet
from toposync_ext_vision.pipelines import ImageClassificationResult, ModelRegistry, VisionClassifyImageRuntime
from toposync_ext_vision.registry import ModelManifest


class _Context:
    async def run_blocking(self, func, /, *args, **kwargs):  # noqa: ANN001
        _ = kwargs
        return func(*args)


def _build_registry() -> ModelRegistry:
    return ModelRegistry(
        [
            ModelManifest(
                model_id="fake.classifier",
                display_name="Fake Classifier",
                task="classification",
                runtime="fake",
                artifact_format="fake",
                artifact_path="fake://classifier",
            )
        ]
    )


def test_vision_classify_image_attaches_ranked_payload() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def classify(self, frame):  # noqa: ANN001
                _ = frame
                return ImageClassificationResult(
                    labels=[
                        {"label": "safe", "label_id": 0, "score": 0.08},
                        {"label": "nsfw", "label_id": 1, "score": 0.92},
                    ],
                    model_id="",
                    metadata={"source": "unit"},
                )

        deps = PipelineRuntimeDependencies(
            classifier_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionClassifyImageRuntime({"model_id": "fake.classifier", "top_k": 2}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={"frame_width": 200, "frame_height": 100},
            artifacts={"frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        assert len(out_packets) == 1
        out = out_packets[0]
        assert out.payload.get("vision", {}).get("task") == "classification"
        assert out.payload.get("vision", {}).get("model_id") == "fake.classifier"
        assert out.payload.get("vision", {}).get("runtime") == "fake"
        classification = out.payload.get("vision", {}).get("classification")
        assert isinstance(classification, dict)
        assert classification.get("top_label") == "nsfw"
        assert classification.get("top_score") == 0.92
        assert classification.get("scores", {}).get("nsfw") == 0.92
        labels = classification.get("labels")
        assert isinstance(labels, list)
        assert labels[0]["label"] == "nsfw"
        assert out.payload.get("classification_label") == "nsfw"
        assert out.payload.get("classification_score") == 0.92
        assert out.metadata.get("vision_task") == "classification"

    asyncio.run(scenario())


def test_vision_classify_image_respects_top_k() -> None:
    async def scenario() -> None:
        class _Backend:
            backend_id = "fake"

            def classify(self, frame):  # noqa: ANN001
                _ = frame
                return {
                    "labels": [
                        {"label": "clear", "label_id": 0, "score": 0.5},
                        {"label": "warning", "label_id": 1, "score": 0.3},
                        {"label": "critical", "label_id": 2, "score": 0.2},
                    ],
                    "model_id": "fake.classifier",
                }

        deps = PipelineRuntimeDependencies(
            classifier_backend_factory=lambda manifest: _Backend(),
            vision_model_registry=_build_registry(),
        )
        runtime = VisionClassifyImageRuntime({"model_id": "fake.classifier", "top_k": 2}, deps)
        packet = Packet.create(
            stream_id="camera:test",
            payload={},
            artifacts={"frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw")},
        )

        out_packets = await runtime.process_packet(packet, _Context())
        labels = out_packets[0].payload["vision"]["classification"]["labels"]
        scores = out_packets[0].payload["vision"]["classification"]["scores"]
        assert [item["label"] for item in labels] == ["clear", "warning"]
        assert set(scores) == {"clear", "warning"}

    asyncio.run(scenario())
