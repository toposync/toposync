from __future__ import annotations

import asyncio


def test_camera_artifact_privacy_operator_strips_selected_image_artifacts() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ArtifactPrivacyRuntime

        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "images": {
                    "original": "frame_original",
                    "treated": "frame",
                    "best_frame": "best_frame",
                },
                "classification_label": "NSFW",
                "classification_label_normalized": "nsfw",
                "classification_score": 0.97,
            },
            artifacts={
                "frame_original": Artifact(name="frame_original", data=object(), mime_type="image/raw"),
                "frame": Artifact(name="frame", data=object(), mime_type="image/raw"),
                "best_frame": Artifact(name="best_frame", data=object(), mime_type="image/raw"),
                "debug_thumb": Artifact(name="debug_thumb", data=object(), mime_type="image/raw"),
            },
        )

        op = ArtifactPrivacyRuntime(
            {
                "expression": 'payload.classification_label_normalized in ["nsfw"] and payload.classification_score is not None and payload.classification_score >= 0.85',
                "artifact_names": ["best_frame", "original", "treated"],
            }
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "frame_original" not in out.artifacts
        assert "frame" not in out.artifacts
        assert "best_frame" not in out.artifacts
        assert "debug_thumb" in out.artifacts
        assert out.payload.get("images") == {}
        assert out.payload.get("artifact_names") == ["debug_thumb"]

        artifact_privacy = out.payload.get("artifact_privacy")
        assert isinstance(artifact_privacy, dict)
        assert artifact_privacy.get("applied") is True
        assert artifact_privacy.get("mode") == "strip"
        assert artifact_privacy.get("removed_artifact_names") == ["best_frame", "frame_original", "frame"]

    asyncio.run(scenario())


def test_camera_artifact_privacy_operator_is_noop_when_expression_does_not_match() -> None:
    async def scenario() -> None:
        from toposync.runtime.pipelines.runtime import Artifact, Packet
        from toposync_ext_cameras.pipelines.postprocess import ArtifactPrivacyRuntime

        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "images": {"treated": "frame"},
                "classification_label_normalized": "safe",
                "classification_score": 0.21,
            },
            artifacts={
                "frame": Artifact(name="frame", data=object(), mime_type="image/raw"),
            },
        )

        op = ArtifactPrivacyRuntime(
            {
                "expression": 'payload.classification_label_normalized in ["nsfw"] and payload.classification_score is not None and payload.classification_score >= 0.85',
                "artifact_names": ["treated"],
            }
        )

        out_packets = await op.process_packet(packet, None)
        assert len(out_packets) == 1
        out = out_packets[0]
        assert "frame" in out.artifacts
        assert out.payload.get("artifact_privacy") is None
        assert out.payload.get("images") == {"treated": "frame"}

    asyncio.run(scenario())
