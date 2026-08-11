from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from collections import deque
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_cameras.processing import mapping as camera_mapping
from toposync_ext_cameras.processing.mapping import ControlPointBoundaryRefinementPoint, ControlPointMapper, ControlPointPair
from toposync_ext_cameras.pipelines.postprocess import (
    CameraMappingConfig,
    CameraMappingRuntime,
    VelocityEstimationRuntime,
)


class _SequenceSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"


class _CollectSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_name: str = "sink"


class _SequenceSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[dict[str, Any]]) -> None:
        parsed = _SequenceSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id
        self._sequence = deque(sequence)

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if not self._sequence:
            return None
        item = self._sequence.popleft()
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=item["lifecycle"],
            payload=dict(item["payload"]),
            artifacts=dict(item.get("artifacts", {})),
            metadata=dict(item.get("metadata", {})),
        )


class _CollectSinkRuntime(SinkRuntime):
    def __init__(self, config: dict[str, Any], collector: dict[str, list[Packet]]) -> None:
        parsed = _CollectSinkConfig.model_validate(config)
        self._sink_name = parsed.sink_name
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        packets = self._collector.setdefault(self._sink_name, [])
        packets.append(packet)
        return []


def _register_test_source_and_sink(
    registry: OperatorRegistry,
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
) -> None:
    registry.register_operator(
        operator_id="test.sequence_source",
        config_model=_SequenceSourceConfig,
        inputs=[],
        outputs=[{"name": "out"}],
        defaults=_SequenceSourceConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _SequenceSourceRuntime(config, sequence),
    )
    registry.register_operator(
        operator_id="test.collect_sink",
        config_model=_CollectSinkConfig,
        inputs=[{"name": "in", "required": True}],
        outputs=[],
        defaults=_CollectSinkConfig().model_dump(),
        share_strategy="never",
        runtime_factory=lambda config, _deps: _CollectSinkRuntime(config, collector),
    )


def test_camera_mapping_annotates_detection_world_anchors_before_tracking() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "control_point_sets": [
                    {
                        "id": "main",
                        "label": "Main",
                        "control_points": [
                            {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                            {"image": {"x": 1.0, "y": 1.0}, "world": {"x": 10.0, "z": 10.0}},
                            {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                        ],
                    }
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "vision": {
                    "task": "detection",
                    "detections": [
                        {
                            "label": "person",
                            "label_id": 0,
                            "score": 0.9,
                            "bbox01": [0.10, 0.10, 0.30, 0.50],
                            "model_id": "fake.detector",
                        },
                        {
                            "label": "person",
                            "label_id": 0,
                            "score": 0.8,
                            "bbox01": [0.60, 0.60, 0.80, 0.80],
                            "model_id": "fake.detector",
                        },
                    ],
                },
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload
        detections = payload["vision"]["detections"]

        assert payload["world"] == pytest.approx({"x": 2.0, "z": 5.0}, abs=1e-6)
        assert payload["world_anchor"]["confidence"] > 0.0
        assert detections[0]["world_anchor"]["x"] == pytest.approx(2.0, abs=1e-6)
        assert detections[0]["world_anchor"]["z"] == pytest.approx(5.0, abs=1e-6)
        assert detections[1]["world_anchor"]["x"] == pytest.approx(7.0, abs=1e-6)
        assert detections[1]["world_anchor"]["z"] == pytest.approx(8.0, abs=1e-6)

    asyncio.run(scenario())


def _calibrated_view_with_stream_scope(
    *,
    compatible_source_ids: list[str],
    compatible_roles: list[str],
    view_id: str = "scoped-view",
    world_offset: float = 0.0,
    pose_reference: dict[str, Any] | None = None,
    visual_pose_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": view_id,
        "label": "Scoped view",
        "pose_reference": pose_reference,
        "stream_scope": {
            "compatible_source_ids": compatible_source_ids,
            "compatible_roles": compatible_roles,
        },
        "projection_model": {
            "type": "image_quad_on_world",
            "image_region": {
                "top_left": {"x": 0.0, "y": 0.0},
                "bottom_right": {"x": 1.0, "y": 1.0},
            },
            "world_quad": {
                "top_left": {"x": world_offset, "z": world_offset},
                "top_right": {"x": world_offset + 10.0, "z": world_offset},
                "bottom_right": {"x": world_offset + 10.0, "z": world_offset + 10.0},
                "bottom_left": {"x": world_offset, "z": world_offset + 10.0},
            },
            **(
                {"visual_pose_signature": visual_pose_signature}
                if visual_pose_signature is not None
                else {}
            ),
        },
    }


def _synthetic_visual_pose_frame(seed: int) -> np.ndarray:
    import cv2

    rng = np.random.default_rng(seed)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (18, 24, 31)
    for index in range(90):
        center = (int(rng.integers(25, 615)), int(rng.integers(25, 455)))
        radius = int(rng.integers(3, 15))
        color = tuple(int(value) for value in rng.integers(70, 256, size=3))
        cv2.circle(frame, center, radius, color, 1 + index % 3, cv2.LINE_AA)
    for index in range(25):
        start = (int(rng.integers(0, 640)), int(rng.integers(0, 480)))
        end = (int(rng.integers(0, 640)), int(rng.integers(0, 480)))
        cv2.line(frame, start, end, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(
        frame,
        f"TOPOSYNC {seed}",
        (55, 245),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.4,
        (255, 255, 255),
        3,
        cv2.LINE_AA,
    )
    return frame


def _orb_visual_pose_signature(frame: np.ndarray) -> dict[str, Any]:
    import cv2

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=320)
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    assert descriptors is not None
    ordered = sorted(
        range(len(keypoints)),
        key=lambda index: (
            -float(keypoints[index].response),
            float(keypoints[index].pt[1]),
            float(keypoints[index].pt[0]),
            float(keypoints[index].size),
            float(keypoints[index].angle),
            int(keypoints[index].octave),
            int(keypoints[index].class_id),
        ),
    )[:320]
    keypoint_values = np.asarray(
        [
            (
                round(max(0.0, min(1.0, keypoints[index].pt[0] / (width - 1))) * 65535.0),
                round(max(0.0, min(1.0, keypoints[index].pt[1] / (height - 1))) * 65535.0),
            )
            for index in ordered
        ],
        dtype="<u2",
    )
    keypoint_bytes = keypoint_values.tobytes()
    descriptor_bytes = np.ascontiguousarray(descriptors[ordered], dtype=np.uint8).tobytes()
    count = len(ordered)
    digest = hashlib.sha256(
        b"orb_hamming_v1\0"
        + struct.pack("<III", width, height, count)
        + keypoint_bytes
        + descriptor_bytes
    ).hexdigest()
    return {
        "algorithm": "orb_hamming_v1",
        "keypoint_count": count,
        "keypoints_base64": base64.b64encode(keypoint_bytes).decode("ascii"),
        "descriptors_base64": base64.b64encode(descriptor_bytes).decode("ascii"),
        "original_width": width,
        "original_height": height,
        "digest_sha256": digest,
    }


@pytest.mark.parametrize("encoded_size", [(50001, 2), (10000, 5001)])
def test_visual_pose_frame_rejects_oversized_encoded_header_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    encoded_size: tuple[int, int],
) -> None:
    import cv2
    from PIL import Image

    class _ImageHeader:
        size = encoded_size

        def __enter__(self) -> "_ImageHeader":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(Image, "open", lambda _buffer: _ImageHeader())

    def _unexpected_decode(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("cv2.imdecode must not run for an oversized encoded image")

    monkeypatch.setattr(cv2, "imdecode", _unexpected_decode)

    assert camera_mapping._extract_orb_visual_pose_features(b"synthetic-header") is None


def test_visual_pose_frame_rejects_oversized_array_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cv2

    base = np.zeros((1,), dtype=np.float32)
    oversized = np.lib.stride_tricks.as_strided(
        base,
        shape=(8000, 7000, 3),
        strides=(0, 0, 0),
        writeable=False,
    )

    def _unexpected_allocation(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("pixel conversion must not run for an oversized array")

    monkeypatch.setattr(cv2, "cvtColor", _unexpected_allocation)
    monkeypatch.setattr(np, "clip", _unexpected_allocation)

    assert camera_mapping._extract_orb_visual_pose_features(oversized) is None


def test_visual_pose_signature_decoder_requires_minimum_matchable_keypoints() -> None:
    keypoint_count = 15
    keypoint_bytes = bytes(keypoint_count * 4)
    descriptor_bytes = bytes(keypoint_count * 32)
    width = 640
    height = 480
    digest = hashlib.sha256(
        b"orb_hamming_v1\0"
        + struct.pack("<III", width, height, keypoint_count)
        + keypoint_bytes
        + descriptor_bytes
    ).hexdigest()
    signature = camera_mapping.VisualPoseSignature(
        algorithm="orb_hamming_v1",
        keypoint_count=keypoint_count,
        keypoints_base64=base64.b64encode(keypoint_bytes).decode("ascii"),
        descriptors_base64=base64.b64encode(descriptor_bytes).decode("ascii"),
        original_width=width,
        original_height=height,
        digest_sha256=digest,
    )

    assert camera_mapping._decode_visual_pose_signature(signature) is None


def test_camera_mapping_runtime_applies_calibrated_view_for_compatible_source_scope() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["secondary"],
                        compatible_roles=["sub"],
                    )
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "secondary", "role": "sub"},
                "image_uv": {"u": 0.5, "v": 0.5},
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert payload["world"] == pytest.approx({"x": 5.0, "z": 5.0}, abs=1e-6)
        assert payload["mapping"]["calibrated_view_id"] == "scoped-view"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "source_payload",
    [
        pytest.param({"source": {"source_id": "secondary", "role": "main"}}, id="nested-source-id"),
        pytest.param({"camera_source_id": "secondary"}, id="camera-source-id"),
        pytest.param({"source": {"source_id": "primary", "role": "zoom"}}, id="source-role"),
    ],
)
def test_camera_mapping_runtime_skips_calibrated_view_for_incompatible_source_scope(
    source_payload: dict[str, Any],
) -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["primary"],
                        compatible_roles=["main"],
                    )
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "image_uv": {"u": 0.5, "v": 0.5},
                **source_payload,
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert "world" not in payload
        assert "world_anchor" not in payload
        assert "mapping" not in payload

    asyncio.run(scenario())


def test_camera_mapping_runtime_skips_scoped_view_when_source_identity_is_missing() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["primary"],
                        compatible_roles=["main"],
                    )
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "image_uv": {"u": 0.5, "v": 0.5},
            },
        )

        outputs = await runtime.process_packet(packet, None)

        assert "world" not in outputs[0].payload
        assert "mapping" not in outputs[0].payload

    asyncio.run(scenario())


def test_camera_mapping_runtime_skips_role_scoped_view_when_source_role_is_missing() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=[],
                        compatible_roles=["main"],
                    )
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "camera_source_id": "secondary",
                "image_uv": {"u": 0.5, "v": 0.5},
            },
        )

        outputs = await runtime.process_packet(packet, None)

        assert "world" not in outputs[0].payload
        assert "mapping" not in outputs[0].payload

    asyncio.run(scenario())


def test_camera_mapping_runtime_selects_preset_only_view() -> None:
    async def scenario() -> None:
        frame_a = _synthetic_visual_pose_frame(41)
        frame_b = _synthetic_visual_pose_frame(73)
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-a",
                        pose_reference={"preset_token": "A", "preset_name": "A"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_a),
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-b",
                        world_offset=100.0,
                        pose_reference={"preset_token": "B", "preset_name": "B"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_b),
                    ),
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "wide_main", "role": "main"},
                "image_uv": {"u": 0.5, "v": 0.5},
                "pan_tilt_zoom_state": {
                    "move_status": "IDLE",
                    "preset_token": "B",
                    "preset_name": "B",
                },
            },
            artifacts=_main_frame_artifacts(frame_b),
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert payload["world"] == pytest.approx({"x": 105.0, "z": 105.0}, abs=1e-4)
        assert payload["mapping"]["calibrated_view_id"] == "preset-b"
        assert payload["mapping"]["pose_axes_used"] == ["visual"]
        assert payload["mapping"]["pose_evidence"] == "visual_signature"

    asyncio.run(scenario())


def test_camera_mapping_runtime_fetches_active_preset_for_exact_source() -> None:
    async def scenario() -> None:
        frame_a = _synthetic_visual_pose_frame(41)
        frame_b = _synthetic_visual_pose_frame(73)
        services = ServiceRegistry()
        calls: list[tuple[str, str | None]] = []

        async def get_status(
            *, camera_id: str, camera_source_id: str | None = None
        ) -> dict[str, Any]:
            calls.append((camera_id, camera_source_id))
            return {
                "move_status": "IDLE",
                "preset_token": "B",
                "preset_name": "B",
            }

        services.register("cameras.ptz.get_status", get_status)
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-a",
                        pose_reference={"preset_token": "A"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_a),
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-b",
                        world_offset=100.0,
                        pose_reference={"preset_token": "B"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_b),
                    ),
                ]
            },
            PipelineRuntimeDependencies(services=services),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "wide_main", "role": "main"},
                "image_uv": {"u": 0.5, "v": 0.5},
            },
            artifacts=_main_frame_artifacts(frame_b),
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert calls == [("camera-main", "wide_main")]
        assert payload["mapping"]["calibrated_view_id"] == "preset-b"
        assert payload["pan_tilt_zoom_state"]["preset_token"] == "B"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "pose_state",
    [
        pytest.param(None, id="restart-without-token"),
        pytest.param(
            {"move_status": "IDLE", "preset_token": "A"},
            id="stale-command-history-token",
        ),
    ],
)
def test_camera_mapping_runtime_recovers_preset_only_view_from_visual_pose(
    pose_state: dict[str, Any] | None,
) -> None:
    async def scenario() -> None:
        frame_a = _synthetic_visual_pose_frame(41)
        frame_b = _synthetic_visual_pose_frame(73)
        runtime = CameraMappingRuntime(
            {
                "ptz_state_fetch": {"enabled": False},
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-a",
                        pose_reference={"preset_token": "A"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_a),
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-b",
                        world_offset=100.0,
                        pose_reference={"preset_token": "B"},
                        visual_pose_signature=_orb_visual_pose_signature(frame_b),
                    ),
                ],
            },
            PipelineRuntimeDependencies(),
        )
        payload: dict[str, Any] = {
            "camera_id": "camera-main",
            "source": {"source_id": "wide_main", "role": "main"},
            "image_uv": {"u": 0.5, "v": 0.5},
        }
        if pose_state is not None:
            payload["pan_tilt_zoom_state"] = pose_state
        packet = Packet.create(
            stream_id="camera:test",
            payload=payload,
            artifacts=_main_frame_artifacts(frame_b),
        )

        output = (await runtime.process_packet(packet, None))[0]

        assert output.payload["mapping"]["calibrated_view_id"] == "preset-b"
        assert output.payload["mapping"]["pose_evidence"] == "visual_signature"
        assert output.payload["world"] == pytest.approx({"x": 105.0, "z": 105.0})

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "evidence_mode",
    [
        "missing-frame",
        "missing-signature",
        "missing-source",
        "shifted-frame",
        "ambiguous",
        "moving",
    ],
)
def test_camera_mapping_runtime_abstains_without_unique_visual_pose(evidence_mode: str) -> None:
    async def scenario() -> None:
        frame_a = _synthetic_visual_pose_frame(41)
        frame_b = _synthetic_visual_pose_frame(73)
        signature_a = _orb_visual_pose_signature(frame_a)
        signature_b = _orb_visual_pose_signature(frame_b)
        if evidence_mode == "ambiguous":
            signature_a = signature_b
        runtime = CameraMappingRuntime(
            {
                "ptz_state_fetch": {"enabled": False},
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-a",
                        pose_reference={"preset_token": "A"},
                        visual_pose_signature=signature_a,
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-b",
                        world_offset=100.0,
                        pose_reference={"preset_token": "B"},
                        visual_pose_signature=(
                            None if evidence_mode == "missing-signature" else signature_b
                        ),
                    ),
                ],
            },
            PipelineRuntimeDependencies(),
        )
        artifacts: dict[str, Artifact] = {}
        if evidence_mode == "shifted-frame":
            shifted = np.roll(frame_b, shift=80, axis=1)
            artifacts = _main_frame_artifacts(shifted)
        elif evidence_mode != "missing-frame":
            artifacts = _main_frame_artifacts(frame_b)
        source_payload = (
            {}
            if evidence_mode == "missing-source"
            else {"source": {"source_id": "wide_main", "role": "main"}}
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                **source_payload,
                "image_uv": {"u": 0.5, "v": 0.5},
                "pan_tilt_zoom_state": {
                    "move_status": "MOVING" if evidence_mode == "moving" else "IDLE",
                    "preset_token": "B",
                },
            },
            artifacts=artifacts,
        )

        output = (await runtime.process_packet(packet, None))[0]

        assert "mapping" not in output.payload
        assert "world" not in output.payload

    asyncio.run(scenario())


@pytest.mark.parametrize("digest_mode", ["missing", "mismatch"])
def test_camera_mapping_config_rejects_invalid_visual_pose_signature_digest(
    digest_mode: str,
) -> None:
    frame = _synthetic_visual_pose_frame(73)
    signature = _orb_visual_pose_signature(frame)
    if digest_mode == "missing":
        signature.pop("digest_sha256")
    else:
        signature["digest_sha256"] = "0" * 64
    view = _calibrated_view_with_stream_scope(
        compatible_source_ids=["wide_main"],
        compatible_roles=["main"],
        view_id="preset-b",
        pose_reference={"preset_token": "B"},
        visual_pose_signature=signature,
    )

    with pytest.raises(ValueError, match="digest"):
        CameraMappingConfig.model_validate({"calibrated_views": [view]})


def test_camera_mapping_runtime_keeps_numeric_pose_selection_without_frame() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="numeric-a",
                        pose_reference={"pan": 0.1, "tilt": 0.2, "zoom": 0.3},
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="numeric-b",
                        world_offset=100.0,
                        pose_reference={"pan": 0.8, "tilt": 0.2, "zoom": 0.3},
                    ),
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "wide_main", "role": "main"},
                "image_uv": {"u": 0.5, "v": 0.5},
                "pan_tilt_zoom_state": {
                    "pan": 0.8,
                    "move_status": "IDLE",
                    "preset_token": "stale-a",
                },
            },
        )

        output = (await runtime.process_packet(packet, None))[0]

        assert output.payload["mapping"]["calibrated_view_id"] == "numeric-b"
        assert output.payload["mapping"]["pose_evidence"] == "numeric_pose"
        assert output.payload["mapping"]["pose_axes_used"] == ["pan"]

    asyncio.run(scenario())


@pytest.mark.parametrize("include_frame", [False, True])
def test_camera_mapping_runtime_requires_visual_proof_for_ambiguous_partial_numeric_pose(
    include_frame: bool,
) -> None:
    async def scenario() -> None:
        frame_a = _synthetic_visual_pose_frame(41)
        frame_b = _synthetic_visual_pose_frame(73)
        runtime = CameraMappingRuntime(
            {
                "ptz_state_fetch": {"enabled": False},
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="numeric-a",
                        pose_reference={
                            "pan": 0.25,
                            "tilt": -0.5,
                            "preset_token": "A",
                        },
                        visual_pose_signature=_orb_visual_pose_signature(frame_a),
                    ),
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="numeric-b",
                        world_offset=100.0,
                        pose_reference={
                            "pan": 0.25,
                            "tilt": 0.5,
                            "preset_token": "B",
                        },
                        visual_pose_signature=_orb_visual_pose_signature(frame_b),
                    ),
                ],
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "wide_main", "role": "main"},
                "image_uv": {"u": 0.5, "v": 0.5},
                "pan_tilt_zoom_state": {
                    "pan": 0.25,
                    "move_status": "IDLE",
                    "preset_token": "A",
                },
            },
            artifacts=_main_frame_artifacts(frame_b) if include_frame else {},
        )

        output = (await runtime.process_packet(packet, None))[0]

        if include_frame:
            assert output.payload["mapping"]["calibrated_view_id"] == "numeric-b"
            assert output.payload["mapping"]["pose_evidence"] == "visual_signature"
            assert output.payload["world"] == pytest.approx(
                {"x": 105.0, "z": 105.0},
                abs=1e-4,
            )
        else:
            assert "mapping" not in output.payload
            assert "world" not in output.payload

    asyncio.run(scenario())


def test_camera_mapping_runtime_aligns_current_frame_point_before_world_projection() -> None:
    async def scenario() -> None:
        import cv2

        reference_frame = _synthetic_visual_pose_frame(73)
        height, width = reference_frame.shape[:2]
        translation_x = 8.0
        translation_y = -4.0
        current_frame = cv2.warpAffine(
            reference_frame,
            np.asarray(
                [[1.0, 0.0, translation_x], [0.0, 1.0, translation_y]],
                dtype=np.float32,
            ),
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        reference_u = 0.42
        reference_v = 0.56
        current_u = reference_u + translation_x / float(width - 1)
        current_v = reference_v + translation_y / float(height - 1)
        runtime = CameraMappingRuntime(
            {
                "ptz_state_fetch": {"enabled": False},
                "calibrated_views": [
                    _calibrated_view_with_stream_scope(
                        compatible_source_ids=["wide_main"],
                        compatible_roles=["main"],
                        view_id="preset-b",
                        pose_reference={"preset_token": "B"},
                        visual_pose_signature=_orb_visual_pose_signature(reference_frame),
                    )
                ],
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "source": {"source_id": "wide_main", "role": "main"},
                "image_uv": {"u": current_u, "v": current_v},
                "vision": {
                    "detections": [
                        {
                            "label": "edge",
                            "bbox01": [0.0, 0.2, 0.01, 0.5],
                        }
                    ]
                },
                "pan_tilt_zoom_state": {
                    "move_status": "IDLE",
                    "preset_token": "B",
                },
            },
            artifacts=_main_frame_artifacts(current_frame),
        )

        output = (await runtime.process_packet(packet, None))[0]
        mapping = output.payload["mapping"]

        assert mapping["pose_evidence"] == "visual_signature"
        assert float(mapping["aligned_u"]) == pytest.approx(reference_u, abs=0.003)
        assert float(mapping["aligned_v"]) == pytest.approx(reference_v, abs=0.003)
        assert output.payload["world"] == pytest.approx(
            {"x": reference_u * 10.0, "z": reference_v * 10.0},
            abs=0.03,
        )
        assert mapping["visual_alignment"]["p95_reprojection_error_px"] <= 6.0
        assert "world_anchor" not in output.payload["vision"]["detections"][0]

    asyncio.run(scenario())


def test_camera_mapping_runtime_applies_calibrated_view_refinement_to_world_payload() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    {
                        "id": "main",
                        "label": "Main",
                        "projection_model": {
                            "type": "image_quad_on_world",
                            "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
                            "world_quad": {
                                "top_left": {"x": 0.0, "z": 0.0},
                                "top_right": {"x": 10.0, "z": 0.0},
                                "bottom_right": {"x": 10.0, "z": 10.0},
                                "bottom_left": {"x": 0.0, "z": 10.0},
                            },
                            "refinement": {
                                "model": "local_rbf_v1",
                                "points": [
                                    {
                                        "id": "center",
                                        "image": {"x": 0.5, "y": 0.5},
                                        "world": {"x": 7.0, "z": 3.0},
                                    }
                                ],
                            },
                        },
                    }
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "subject": {"bbox01": [0.40, 0.10, 0.60, 0.50]},
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert payload["world"]["x"] == pytest.approx(7.0, abs=1e-6)
        assert payload["world"]["z"] == pytest.approx(3.0, abs=1e-6)
        assert payload["mapping"]["quality"]["number_of_points"] == 4

    asyncio.run(scenario())


def test_control_point_mapper_applies_boundary_refinement_on_edge() -> None:
    mapper = ControlPointMapper(
        [
            ControlPointPair(image_u=0.0, image_v=0.0, world_x=0.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.0, world_x=10.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=1.0, world_x=10.0, world_z=10.0),
            ControlPointPair(image_u=0.0, image_v=1.0, world_x=0.0, world_z=10.0),
        ],
        boundary_refinement_points=[
            ControlPointBoundaryRefinementPoint(
                id="left-mid",
                edge="left",
                t=0.5,
                image_u=0.0,
                image_v=0.5,
                world_x=-2.0,
                world_z=5.0,
            )
        ],
    )

    edge = mapper.map(0.0, 0.5)
    near_edge = mapper.map(0.1, 0.5)
    far_inside = mapper.map(0.8, 0.5)
    inverse = mapper.map_world_to_image(-2.0, 5.0)

    assert edge == pytest.approx((-2.0, 5.0), abs=1e-6)
    assert near_edge is not None
    assert far_inside is not None
    assert near_edge[0] < 1.0
    assert far_inside[0] == pytest.approx(8.0, abs=0.05)
    assert inverse == pytest.approx((0.0, 0.5), abs=1e-5)


def test_camera_mapping_runtime_applies_calibrated_view_boundary_refinement_to_world_payload() -> None:
    async def scenario() -> None:
        runtime = CameraMappingRuntime(
            {
                "calibrated_views": [
                    {
                        "id": "main",
                        "label": "Main",
                        "projection_model": {
                            "type": "image_quad_on_world",
                            "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
                            "world_quad": {
                                "top_left": {"x": 0.0, "z": 0.0},
                                "top_right": {"x": 10.0, "z": 0.0},
                                "bottom_right": {"x": 10.0, "z": 10.0},
                                "bottom_left": {"x": 0.0, "z": 10.0},
                            },
                            "boundary_refinement": {
                                "model": "edge_handles_v1",
                                "points": [
                                    {
                                        "id": "left-mid",
                                        "edge": "left",
                                        "t": 0.5,
                                        "image": {"x": 0.0, "y": 0.5},
                                        "world": {"x": -2.0, "z": 5.0},
                                    }
                                ],
                            },
                        },
                    }
                ]
            },
            PipelineRuntimeDependencies(),
        )
        packet = Packet.create(
            stream_id="camera:test",
            payload={
                "camera_id": "camera-main",
                "subject": {"bbox01": [0.0, 0.1, 0.0, 0.5]},
            },
        )

        outputs = await runtime.process_packet(packet, None)
        payload = outputs[0].payload

        assert payload["world"]["x"] == pytest.approx(-2.0, abs=1e-6)
        assert payload["world"]["z"] == pytest.approx(5.0, abs=1e-6)

    asyncio.run(scenario())


def test_camera_mapping_config_rejects_invalid_boundary_refinement() -> None:
    base_view = {
        "id": "main",
        "label": "Main",
        "projection_model": {
            "type": "image_quad_on_world",
            "image_region": {"top_left": {"x": 0.0, "y": 0.0}, "bottom_right": {"x": 1.0, "y": 1.0}},
            "world_quad": {
                "top_left": {"x": 0.0, "z": 0.0},
                "top_right": {"x": 10.0, "z": 0.0},
                "bottom_right": {"x": 10.0, "z": 10.0},
                "bottom_left": {"x": 0.0, "z": 10.0},
            },
            "boundary_refinement": {
                "model": "edge_handles_v1",
                "points": [{"id": "bad", "edge": "middle", "t": 0.5, "world": {"x": 0.0, "z": 5.0}}],
            },
        },
    }

    with pytest.raises(Exception):
        CameraMappingConfig.model_validate({"calibrated_views": [base_view]})

    invalid_t_view = dict(base_view)
    invalid_t_view["projection_model"] = dict(base_view["projection_model"])
    invalid_t_view["projection_model"]["boundary_refinement"] = {
        "model": "edge_handles_v1",
        "points": [{"id": "bad", "edge": "left", "t": 1.5, "world": {"x": 0.0, "z": 5.0}}],
    }
    with pytest.raises(Exception):
        CameraMappingConfig.model_validate({"calibrated_views": [invalid_t_view]})


def _pipeline_runtime(
    *,
    graph: dict[str, Any],
    sequence: list[dict[str, Any]],
    collector: dict[str, list[Packet]],
    dependencies: PipelineRuntimeDependencies | None = None,
) -> PipelineRuntime:
    registry = OperatorRegistry()
    register_builtin_operators(registry)
    register_camera_pipeline_operators(registry)
    _register_test_source_and_sink(registry, sequence, collector)
    compiled = PipelineGraphCompiler(registry).compile_pipeline(
        Pipeline(name="stage6_postprocess_test", graph=graph),
    )
    return PipelineRuntime(
        compiled=compiled,
        registry=registry,
        dependencies=dependencies or PipelineRuntimeDependencies(),
    )


def _frame_artifacts(frame: Any) -> dict[str, Artifact]:
    return {
        "main": Artifact(name="main", data=frame, mime_type="image/raw"),
    }


def _main_frame_artifacts(frame: Any) -> dict[str, Artifact]:
    return {
        "main": Artifact(name="main", data=frame, mime_type="image/raw"),
    }


def test_object_crop_reprojects_bbox_for_cropped_stream_frame() -> None:
    async def scenario() -> None:
        main = np.zeros((100, 100, 3), dtype=np.uint8)
        main[30:50, 30:50] = 123

        # Simulates a stream crop of the center area [0.25..0.75] applied as the stream frame.
        stream_frame = main[25:75, 25:75].copy()

        sequence: list[dict[str, Any]] = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-1",
                    # Use exact binary fractions to avoid borderline rounding in int/ceil conversions.
                    "subject": {"bbox01": [0.3125, 0.3125, 0.50, 0.50]},
                    "frame_crop": {
                        "bbox01": [0.25, 0.25, 0.75, 0.75],
                        "output_artifact_name": "main",
                    },
                },
                "artifacts": _main_frame_artifacts(stream_frame),
            },
        ]

        graph = {
            "schema_version": 2,
            "uid": "graph_object_crop_stream_frame",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_crop",
                    "id": "crop",
                    "operator": "vision.crop_objects",
                    "config": {
                        "output_artifact_name": "main",
                        "bbox_field": "subject.bbox01",
                        "padding_ratio": 0.0,
                        "min_crop_size_px": 1,
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_crop",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "crop", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_crop_sink",
                    "from": {"node": "crop", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        main = packet.artifacts["main"].data
        assert main is not None

        # The crop metadata is applied only because it explicitly targets the selected main artifact.
        assert tuple(main.shape[:2]) == (19, 19)
        assert int(main[0, 0, 0]) == 123

    asyncio.run(scenario())


def test_object_crop_reprojects_bbox_for_perspective_warped_stream_frame() -> None:
    async def scenario() -> None:
        try:
            import cv2  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("opencv-python-headless is required for this test") from exc

        main = np.zeros((100, 100, 3), dtype=np.uint8)
        main[30:50, 30:50] = 123

        src = np.asarray([[25, 25], [75, 25], [75, 75], [25, 75]], dtype=np.float32)
        dst_w = 51
        dst_h = 51
        dst = np.asarray([[0, 0], [50, 0], [50, 50], [0, 50]], dtype=np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        stream_frame = cv2.warpPerspective(main, H, (dst_w, dst_h))

        sequence: list[dict[str, Any]] = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-1",
                    "subject": {"bbox01": [0.30, 0.30, 0.50, 0.50]},
                    "frame_warp": {
                        "kind": "perspective",
                        "source_frame_width": 100,
                        "source_frame_height": 100,
                        "dest_frame_width": dst_w,
                        "dest_frame_height": dst_h,
                        "homography": [[float(v) for v in row] for row in H.tolist()],
                        "output_artifact_name": "main",
                    },
                },
                "artifacts": _main_frame_artifacts(stream_frame),
            },
        ]

        graph = {
            "schema_version": 2,
            "uid": "graph_object_crop_perspective_frame",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_crop",
                    "id": "crop",
                    "operator": "vision.crop_objects",
                    "config": {
                        "output_artifact_name": "main",
                        "bbox_field": "subject.bbox01",
                        "padding_ratio": 0.0,
                        "min_crop_size_px": 1,
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_crop",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "crop", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_crop_sink",
                    "from": {"node": "crop", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        main = packet.artifacts["main"].data
        assert main is not None

        meta = packet.artifacts["main"].metadata
        assert isinstance(meta, dict)
        assert "reproject:frame_warp" in str(meta.get("bbox_source", ""))

        assert int(main.max()) == 123

    asyncio.run(scenario())


def test_image_resize_downscales_selected_artifacts_in_place() -> None:
    async def scenario() -> None:
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "frame_ts": 1.0,
                    "tracking_id": "trk-resize",
                },
                "artifacts": _frame_artifacts(frame),
            },
        ]

        graph = {
            "schema_version": 2,
            "uid": "graph_image_resize_downscale",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_resize",
                    "id": "resize",
                    "operator": "camera.image_resize",
                    "config": {
                        "max_edge_px": 50,
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_resize",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "resize", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_resize_sink",
                    "from": {"node": "resize", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packets = collector.get("sink", [])
        assert len(packets) == 1
        packet = packets[0]
        assert "main" in packet.artifacts
        image = packet.artifacts["main"].data
        assert image is not None
        assert tuple(image.shape[:2]) == (25, 50)
        meta = packet.artifacts["main"].metadata
        assert meta.get("resized_from") == {"width": 200, "height": 100}
        assert meta.get("resized_to") == {"width": 50, "height": 25}

    asyncio.run(scenario())


def test_mapping_area_and_velocity_chain_filters_on_stopped_object() -> None:
    async def scenario() -> None:
        frame = np.zeros((50, 50, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 1.0,
                    "subject": {"bbox01": [0.48, 0.48, 0.52, 0.52]},
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 2.0,
                    "subject": {"bbox01": [0.70, 0.48, 0.74, 0.52]},
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-1",
                    "tracking_id": "velocity-1",
                    "frame_ts": 3.0,
                    "subject": {"bbox01": [0.70, 0.48, 0.74, 0.52]},
                },
                "artifacts": _frame_artifacts(frame),
            },
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_mapping_area_velocity",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_mapping",
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "bbox_field": "subject.bbox01",
                        "control_point_sets": [
                            {
                                "id": "main",
                                "label": "Main",
                                "pose_reference": None,
                                "control_points": [
                                    {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                    {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 10.0, "z": 10.0},
                                    },
                                    {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                ],
                            }
                        ],
                    },
                },
                {
                    "uid": "node_area",
                    "id": "area",
                    "operator": "camera.area_restriction",
                    "config": {
                        "areas": [
                            {
                                "name": "front",
                                "points": [
                                    {"x": 0.0, "z": 0.0},
                                    {"x": 10.0, "z": 0.0},
                                    {"x": 10.0, "z": 10.0},
                                    {"x": 0.0, "z": 10.0},
                                ],
                            },
                        ],
                        "include_area_names": ["front"],
                    },
                },
                {
                    "uid": "node_velocity",
                    "id": "velocity",
                    "operator": "camera.velocity_estimation",
                    "config": {
                        "stopped_speed_threshold": 0.15,
                        "filter_mode": "annotate",
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_mapping",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_mapping_area",
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "area", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_area_velocity",
                    "from": {"node": "area", "port": "out"},
                    "to": {"node": "velocity", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_velocity_sink",
                    "from": {"node": "velocity", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.25)

        packets = collector.get("sink", [])
        assert len(packets) == 3
        packet = packets[-1]
        assert packet.payload.get("area_label") == "front"
        velocity = packet.payload.get("velocity")
        assert isinstance(velocity, dict)
        assert velocity.get("ever_stopped") is True
        assert velocity.get("moving") is False
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 7.2
        assert round(float(world.get("z")), 1) == 5.2
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "main"
        quality = mapping.get("quality")
        assert isinstance(quality, dict)
        assert quality.get("number_of_points") == 4

    asyncio.run(scenario())


def test_velocity_stopped_now_drops_close_when_first_valid_world_sample_arrives_on_close() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.OPEN,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-close-only",
                    "tracking_id": "velocity-close-only",
                    "frame_ts": 1.0,
                },
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.CLOSE,
                "payload": {
                    "camera_id": "camera-main",
                    "event_id": "velocity-close-only",
                    "tracking_id": "velocity-close-only",
                    "frame_ts": 9.0,
                    "world": {"x": 2.0, "z": 3.0},
                },
                "artifacts": _frame_artifacts(frame),
            },
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_velocity_close_only",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_velocity",
                    "id": "velocity",
                    "operator": "camera.velocity_estimation",
                    "config": {
                        "filter_mode": "stopped_now",
                        "min_elapsed_seconds": 0.05,
                        "stopped_speed_threshold": 0.07,
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_velocity",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "velocity", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_velocity_sink",
                    "from": {"node": "velocity", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 8, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        assert collector.get("sink", []) == []

    asyncio.run(scenario())


def test_mapping_selects_pose_bound_set_when_ptz_state_matches() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                    "pan_tilt_zoom_state": {
                        "pan": 0.12,
                        "tilt": -0.08,
                        "zoom": 0.33,
                        "move_status": "IDLE",
                    },
                },
                "artifacts": _frame_artifacts(frame),
            }
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_mapping_pose_payload",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_mapping",
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
                                "pose_reference": None,
                                "control_points": [
                                    {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                    {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 10.0, "z": 10.0},
                                    },
                                    {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                ],
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ]
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_mapping",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_mapping_sink",
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packet = collector["sink"][-1]
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 105.0
        assert round(float(world.get("z")), 1) == 105.0
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "door_zoom"
        assert round(float(mapping.get("pose_distance")), 3) == 0.0
        assert mapping.get("move_status") == "idle"

    asyncio.run(scenario())


def test_mapping_fetches_ptz_state_from_service_when_payload_missing() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                },
                "artifacts": _frame_artifacts(frame),
            }
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_mapping_fetch_pose_service",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_mapping",
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
                                "pose_reference": None,
                                "control_points": [
                                    {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                    {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 10.0, "z": 10.0},
                                    },
                                    {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                ],
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ]
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_mapping",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_mapping_sink",
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
            ],
        }

        services = ServiceRegistry()
        call_count = {"value": 0}

        async def _get_status(*, camera_id: str) -> dict[str, Any]:
            assert camera_id == "camera-main"
            call_count["value"] += 1
            return {"pan": 0.12, "tilt": -0.08, "zoom": 0.33, "move_status": "IDLE"}

        services.register("cameras.ptz.get_status", _get_status)

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            sequence=sequence,
            collector=collector,
            dependencies=PipelineRuntimeDependencies(services=services),
        )
        await runtime.run_for(0.2)

        assert call_count["value"] == 1
        packet = collector["sink"][-1]
        world = packet.payload.get("world")
        assert isinstance(world, dict)
        assert round(float(world.get("x")), 1) == 105.0
        assert round(float(world.get("z")), 1) == 105.0
        mapping = packet.payload.get("mapping")
        assert isinstance(mapping, dict)
        assert mapping.get("control_point_set_id") == "door_zoom"
        pose_state = packet.payload.get("pan_tilt_zoom_state")
        assert isinstance(pose_state, dict)
        assert pose_state.get("source") == "cameras.ptz.get_status"

    asyncio.run(scenario())


def test_mapping_caches_fetched_ptz_state_between_packets() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {"camera_id": "camera-main", "image_uv": {"u": 0.5, "v": 0.5}},
                "artifacts": _frame_artifacts(frame),
            },
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {"camera_id": "camera-main", "image_uv": {"u": 0.5, "v": 0.5}},
                "artifacts": _frame_artifacts(frame),
            },
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_mapping_cached_pose_service",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_mapping",
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "ptz_state_fetch": {"cache_ttl_seconds": 60.0},
                        "control_point_sets": [
                            {
                                "id": "default",
                                "label": "Default",
                                "pose_reference": None,
                                "control_points": [
                                    {"image": {"x": 0.0, "y": 0.0}, "world": {"x": 0.0, "z": 0.0}},
                                    {"image": {"x": 1.0, "y": 0.0}, "world": {"x": 10.0, "z": 0.0}},
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 10.0, "z": 10.0},
                                    },
                                    {"image": {"x": 0.0, "y": 1.0}, "world": {"x": 0.0, "z": 10.0}},
                                ],
                            },
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            },
                        ],
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_mapping",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_mapping_sink",
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
            ],
        }

        services = ServiceRegistry()
        call_count = {"value": 0}

        async def _get_status(*, camera_id: str) -> dict[str, Any]:
            assert camera_id == "camera-main"
            call_count["value"] += 1
            return {"pan": 0.12, "tilt": -0.08, "zoom": 0.33, "move_status": "IDLE"}

        services.register("cameras.ptz.get_status", _get_status)

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(
            graph=graph,
            sequence=sequence,
            collector=collector,
            dependencies=PipelineRuntimeDependencies(services=services),
        )
        await runtime.run_for(0.2)

        assert call_count["value"] == 1
        packets = collector["sink"]
        assert len(packets) == 2
        assert all(isinstance(packet.payload.get("mapping"), dict) for packet in packets)

    asyncio.run(scenario())


def test_mapping_skips_when_ptz_state_reports_moving() -> None:
    async def scenario() -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        sequence = [
            {
                "lifecycle": Lifecycle.UPDATE,
                "payload": {
                    "camera_id": "camera-main",
                    "image_uv": {"u": 0.5, "v": 0.5},
                    "pan_tilt_zoom_state": {
                        "pan": 0.12,
                        "tilt": -0.08,
                        "zoom": 0.33,
                        "move_status": "MOVING",
                    },
                },
                "artifacts": _frame_artifacts(frame),
            }
        ]
        graph = {
            "schema_version": 2,
            "uid": "graph_mapping_moving_pose",
            "revision": 1,
            "nodes": [
                {
                    "uid": "node_source",
                    "id": "source",
                    "operator": "test.sequence_source",
                    "config": {"stream_id": "camera:test"},
                },
                {
                    "uid": "node_mapping",
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "control_point_sets": [
                            {
                                "id": "door_zoom",
                                "label": "Door",
                                "pose_reference": {"pan": 0.12, "tilt": -0.08, "zoom": 0.33},
                                "control_points": [
                                    {
                                        "image": {"x": 0.0, "y": 0.0},
                                        "world": {"x": 100.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 0.0},
                                        "world": {"x": 110.0, "z": 100.0},
                                    },
                                    {
                                        "image": {"x": 1.0, "y": 1.0},
                                        "world": {"x": 110.0, "z": 110.0},
                                    },
                                    {
                                        "image": {"x": 0.0, "y": 1.0},
                                        "world": {"x": 100.0, "z": 110.0},
                                    },
                                ],
                            }
                        ]
                    },
                },
                {
                    "uid": "node_sink",
                    "id": "sink",
                    "operator": "test.collect_sink",
                    "config": {"sink_name": "sink"},
                },
            ],
            "edges": [
                {
                    "uid": "edge_source_mapping",
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
                {
                    "uid": "edge_mapping_sink",
                    "from": {"node": "mapping", "port": "out"},
                    "to": {"node": "sink", "port": "in"},
                    "traffic": {"modality": "video.frame", "semantic_class": "frame"},
                    "queue": {"max_items": 4, "drop_policy": "drop_oldest"},
                },
            ],
        }

        collector: dict[str, list[Packet]] = {}
        runtime = _pipeline_runtime(graph=graph, sequence=sequence, collector=collector)
        await runtime.run_for(0.2)

        packet = collector["sink"][-1]
        assert "world" not in packet.payload
        assert "mapping" not in packet.payload

    asyncio.run(scenario())


def test_control_point_mapper_rejects_single_outlier_with_robust_homography() -> None:
    mapper = ControlPointMapper(
        [
            ControlPointPair(image_u=0.0, image_v=0.0, world_x=0.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.0, world_x=10.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=1.0, world_x=10.0, world_z=10.0),
            ControlPointPair(image_u=0.0, image_v=1.0, world_x=0.0, world_z=10.0),
            ControlPointPair(image_u=0.5, image_v=0.0, world_x=5.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.5, world_x=10.0, world_z=5.0),
            ControlPointPair(image_u=0.5, image_v=1.0, world_x=5.0, world_z=10.0),
            ControlPointPair(image_u=0.0, image_v=0.5, world_x=0.0, world_z=5.0),
            ControlPointPair(image_u=0.25, image_v=0.75, world_x=2.5, world_z=7.5),
            ControlPointPair(image_u=0.9, image_v=0.1, world_x=42.0, world_z=17.0),
        ]
    )
    mapped = mapper.map(0.5, 0.5)
    assert mapped is not None
    assert mapped[0] == pytest.approx(5.0, abs=0.25)
    assert mapped[1] == pytest.approx(5.0, abs=0.25)
    assert mapper.quality.number_of_inliers >= 8


def test_velocity_filter_mode_stopped_once_emits_only_after_object_stops() -> None:
    async def scenario() -> None:
        runtime = VelocityEstimationRuntime(
            {
                "stopped_speed_threshold": 0.2,
                "filter_mode": "stopped_once",
            },
        )

        def make_packet(frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id="obj:velocity",
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "event_id": "velocity-track",
                    "tracking_id": "velocity-track",
                    "frame_ts": frame_ts,
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        outputs: list[Packet] = []
        for packet in [make_packet(1.0, 0.0), make_packet(2.0, 1.0), make_packet(3.0, 1.0)]:
            output_packets = await runtime.process_packet(packet, context=None)
            outputs.extend(output_packets)

        assert len(outputs) == 1
        velocity = outputs[0].payload.get("velocity")
        assert isinstance(velocity, dict)
        assert velocity.get("ever_stopped") is True
        assert velocity.get("moving") is False

    asyncio.run(scenario())


def test_velocity_state_is_namespaced_when_tracking_id_repeats_across_streams() -> None:
    async def scenario() -> None:
        runtime = VelocityEstimationRuntime(
            {
                "stopped_speed_threshold": 0.2,
                "filter_mode": "annotate",
            },
        )

        def make_packet(*, stream_id: str, frame_ts: float, world_x: float) -> Packet:
            return Packet.create(
                stream_id=stream_id,
                lifecycle=Lifecycle.UPDATE,
                payload={
                    "event_id": "1",
                    "tracking_id": "1",
                    "frame_ts": frame_ts,
                    "world": {"x": world_x, "z": 0.0},
                },
            )

        # If state was keyed only by tracking_id ("1"), packet2 would see a large speed from packet1.
        packet1 = make_packet(stream_id="cam:one", frame_ts=1.0, world_x=0.0)
        packet2 = make_packet(stream_id="cam:two", frame_ts=2.0, world_x=10.0)

        out1 = (await runtime.process_packet(packet1, context=None))[0]
        out2 = (await runtime.process_packet(packet2, context=None))[0]

        v1 = out1.payload.get("velocity")
        v2 = out2.payload.get("velocity")
        assert isinstance(v1, dict)
        assert isinstance(v2, dict)
        assert v1.get("valid") is False
        assert v2.get("valid") is False

    asyncio.run(scenario())
