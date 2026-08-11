from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import replace
import hashlib
from importlib.metadata import EntryPoint
import json
import math
from pathlib import Path
import threading
from typing import Any

import cv2
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.app import create_app
import toposync.extensions.manager as ext_manager_mod
import toposync_ext_cameras.plugin as cameras_plugin
import toposync_ext_cameras.processing.visual_calibration as visual_calibration
from toposync_ext_cameras.pipelines.postprocess import CameraMappingVisualPoseSignature
from toposync_ext_cameras.plugin import (
    CameraVisualCalibrationResponse,
    CameraVisualPoseSignature,
)
from toposync_ext_cameras.processing.mapping import (
    ControlPointBoundaryRefinementPoint,
    ControlPointPair,
    ControlPointRefinementPoint,
    ControlPointSet,
    VisualPoseSignature as RuntimeVisualPoseSignature,
)
from toposync_ext_cameras.processing.visual_calibration import (
    _extract_visual_pose_signature,
    propagate_visual_calibration,
)


IMAGE_WIDTH = 640
IMAGE_HEIGHT = 480
WORLD_WIDTH = 20.0
WORLD_DEPTH = 12.0


def _source_control_point_set() -> ControlPointSet:
    return ControlPointSet(
        id="reference",
        label="Reference",
        pose_reference=None,
        control_points=(
            ControlPointPair(image_u=0.0, image_v=0.0, world_x=0.0, world_z=0.0),
            ControlPointPair(image_u=1.0, image_v=0.0, world_x=WORLD_WIDTH, world_z=0.0),
            ControlPointPair(
                image_u=1.0,
                image_v=1.0,
                world_x=WORLD_WIDTH,
                world_z=WORLD_DEPTH,
            ),
            ControlPointPair(image_u=0.0, image_v=1.0, world_x=0.0, world_z=WORLD_DEPTH),
        ),
    )


def _source_control_point_set_for_image_region(
    *,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> ControlPointSet:
    return ControlPointSet(
        id="partial-reference",
        label="Partial reference",
        pose_reference=None,
        control_points=(
            ControlPointPair(image_u=left, image_v=top, world_x=0.0, world_z=0.0),
            ControlPointPair(
                image_u=right,
                image_v=top,
                world_x=WORLD_WIDTH,
                world_z=0.0,
            ),
            ControlPointPair(
                image_u=right,
                image_v=bottom,
                world_x=WORLD_WIDTH,
                world_z=WORLD_DEPTH,
            ),
            ControlPointPair(
                image_u=left,
                image_v=bottom,
                world_x=0.0,
                world_z=WORLD_DEPTH,
            ),
        ),
    )


def _source_view_payload() -> dict[str, Any]:
    return {
        "id": "reference",
        "label": "Reference",
        "pose_reference": None,
        "stream_scope": {"compatible_roles": ["main"], "compatible_source_ids": ["main"]},
        "projection_model": {
            "type": "image_quad_on_world",
            "image_region": {
                "top_left": {"x": 0.0, "y": 0.0},
                "bottom_right": {"x": 1.0, "y": 1.0},
            },
            "world_quad": {
                "top_left": {"x": 0.0, "z": 0.0},
                "top_right": {"x": WORLD_WIDTH, "z": 0.0},
                "bottom_right": {"x": WORLD_WIDTH, "z": WORLD_DEPTH},
                "bottom_left": {"x": 0.0, "z": WORLD_DEPTH},
            },
            "refinement": None,
            "boundary_refinement": None,
        },
        "projection_quality": {"status": "ready", "estimated": False},
    }


def _create_camera_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")

    def _entry_points(_group: str) -> list[EntryPoint]:
        return [
            EntryPoint(
                name="cameras",
                value="toposync_ext_cameras.plugin:CamerasExtension",
                group="toposync.extensions",
            )
        ]

    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", _entry_points)
    return TestClient(create_app())


def _textured_scene(seed: int = 20260810) -> np.ndarray:
    rng = np.random.default_rng(seed)
    texture = rng.integers(0, 256, (IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (5, 5), 0)
    image = cv2.cvtColor(texture, cv2.COLOR_GRAY2BGR)

    for index in range(72):
        x = int(rng.integers(24, IMAGE_WIDTH - 24))
        y = int(rng.integers(24, IMAGE_HEIGHT - 24))
        radius = int(rng.integers(5, 18))
        color = tuple(int(value) for value in rng.integers(20, 236, size=3))
        cv2.circle(image, (x, y), radius, color, thickness=2)
        cv2.line(
            image,
            (max(0, x - radius * 2), y),
            (min(IMAGE_WIDTH - 1, x + radius * 2), y + int(rng.integers(-12, 13))),
            color,
            thickness=1,
        )
        if index % 8 == 0:
            cv2.putText(
                image,
                f"R{index}",
                (max(0, x - 18), min(IMAGE_HEIGHT - 8, y + 28)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                thickness=1,
                lineType=cv2.LINE_AA,
            )
    return image


def _repeated_facade() -> np.ndarray:
    image = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 214, dtype=np.uint8)
    for y in range(20, IMAGE_HEIGHT - 20, 48):
        for x in range(20, IMAGE_WIDTH - 20, 48):
            cv2.rectangle(image, (x, y), (x + 24, y + 30), (55, 55, 55), thickness=2)
            cv2.line(image, (x + 12, y), (x + 12, y + 30), (55, 55, 55), thickness=1)
            cv2.line(image, (x, y + 15), (x + 24, y + 15), (55, 55, 55), thickness=1)
    return image


def _known_warp() -> np.ndarray:
    source = np.float32(
        [
            [0.0, 0.0],
            [IMAGE_WIDTH - 1.0, 0.0],
            [IMAGE_WIDTH - 1.0, IMAGE_HEIGHT - 1.0],
            [0.0, IMAGE_HEIGHT - 1.0],
        ]
    )
    target = np.float32(
        [
            [18.0, 12.0],
            [IMAGE_WIDTH - 22.0, 7.0],
            [IMAGE_WIDTH - 9.0, IMAGE_HEIGHT - 16.0],
            [11.0, IMAGE_HEIGHT - 7.0],
        ]
    )
    return cv2.getPerspectiveTransform(source, target)


def _jpeg_bytes(image: np.ndarray, *, quality: int = 95) -> bytes:
    encoded, buffer = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    assert encoded
    return buffer.tobytes()


def _post_visual_calibration(
    client: TestClient,
    *,
    source_bytes: bytes,
    target_bytes: bytes,
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/api/cameras/projection/propagate",
        data={
            "source_view_json": json.dumps(_source_view_payload()),
            "source_id": "main",
        },
        files={
            "source_image": ("reference.jpg", source_bytes, "image/jpeg"),
            "target_image": ("current.jpg", target_bytes, "image/jpeg"),
        },
        headers=headers,
    )


def _expected_world_quad(source_to_target: np.ndarray) -> dict[str, tuple[float, float]]:
    target_to_source = np.linalg.inv(source_to_target)
    corners = {
        "top_left": (0.0, 0.0),
        "top_right": (IMAGE_WIDTH - 1.0, 0.0),
        "bottom_right": (IMAGE_WIDTH - 1.0, IMAGE_HEIGHT - 1.0),
        "bottom_left": (0.0, IMAGE_HEIGHT - 1.0),
    }
    output: dict[str, tuple[float, float]] = {}
    for name, (target_x, target_y) in corners.items():
        source = target_to_source @ np.asarray([target_x, target_y, 1.0], dtype=np.float64)
        source_x = float(source[0] / source[2])
        source_y = float(source[1] / source[2])
        output[name] = (
            source_x / (IMAGE_WIDTH - 1.0) * WORLD_WIDTH,
            source_y / (IMAGE_HEIGHT - 1.0) * WORLD_DEPTH,
        )
    return output


def _assert_visual_pose_signature(
    signature: dict[str, Any],
    *,
    expected_width: int,
    expected_height: int,
) -> None:
    assert set(signature) == {
        "algorithm",
        "keypoint_count",
        "keypoints_base64",
        "descriptors_base64",
        "original_width",
        "original_height",
        "digest_sha256",
    }
    assert signature["algorithm"] == "orb_hamming_v1"
    assert signature["original_width"] == expected_width
    assert signature["original_height"] == expected_height
    assert 16 <= signature["keypoint_count"] <= 320

    keypoint_bytes = base64.b64decode(signature["keypoints_base64"], validate=True)
    descriptor_bytes = base64.b64decode(signature["descriptors_base64"], validate=True)
    assert len(keypoint_bytes) == signature["keypoint_count"] * 4
    assert len(descriptor_bytes) == signature["keypoint_count"] * 32

    normalized_keypoints = np.frombuffer(keypoint_bytes, dtype="<u2").reshape((-1, 2))
    assert normalized_keypoints.shape == (signature["keypoint_count"], 2)
    assert np.all(normalized_keypoints >= 0)
    assert np.all(normalized_keypoints <= 65535)

    digest = hashlib.sha256()
    digest.update(b"orb_hamming_v1\0")
    digest.update(int(expected_width).to_bytes(4, "little"))
    digest.update(int(expected_height).to_bytes(4, "little"))
    digest.update(int(signature["keypoint_count"]).to_bytes(4, "little"))
    digest.update(keypoint_bytes)
    digest.update(descriptor_bytes)
    assert signature["digest_sha256"] == digest.hexdigest()
    assert CameraVisualPoseSignature.model_validate(signature).model_dump() == signature


def _truncate_visual_pose_signature(
    signature: dict[str, Any],
    *,
    keypoint_count: int,
) -> dict[str, Any]:
    keypoint_bytes = base64.b64decode(signature["keypoints_base64"], validate=True)[
        : keypoint_count * 4
    ]
    descriptor_bytes = base64.b64decode(signature["descriptors_base64"], validate=True)[
        : keypoint_count * 32
    ]
    digest = hashlib.sha256()
    digest.update(b"orb_hamming_v1\0")
    digest.update(int(signature["original_width"]).to_bytes(4, "little"))
    digest.update(int(signature["original_height"]).to_bytes(4, "little"))
    digest.update(int(keypoint_count).to_bytes(4, "little"))
    digest.update(keypoint_bytes)
    digest.update(descriptor_bytes)
    return {
        **signature,
        "keypoint_count": keypoint_count,
        "keypoints_base64": base64.b64encode(keypoint_bytes).decode("ascii"),
        "descriptors_base64": base64.b64encode(descriptor_bytes).decode("ascii"),
        "digest_sha256": digest.hexdigest(),
    }


def _source_control_point_set_with_visual_signature(
    signature: dict[str, Any],
) -> ControlPointSet:
    return replace(
        _source_control_point_set(),
        visual_pose_signature=RuntimeVisualPoseSignature(**signature),
    )


def _visual_pose_signature_from_image_bytes(image_bytes: bytes) -> dict[str, Any]:
    grayscale = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    assert grayscale is not None
    signature = _extract_visual_pose_signature(
        grayscale,
        original_width=int(grayscale.shape[1]),
        original_height=int(grayscale.shape[0]),
    )
    assert signature is not None
    return signature


def _warp_normalized_point(
    source_to_target: np.ndarray,
    source_u: float,
    source_v: float,
) -> tuple[float, float]:
    target = source_to_target @ np.asarray(
        [
            source_u * (IMAGE_WIDTH - 1.0),
            source_v * (IMAGE_HEIGHT - 1.0),
            1.0,
        ],
        dtype=np.float64,
    )
    return (
        float(target[0] / target[2]) / (IMAGE_WIDTH - 1.0),
        float(target[1] / target[2]) / (IMAGE_HEIGHT - 1.0),
    )


def _assert_quality_is_self_consistent(quality: dict[str, Any]) -> None:
    for key, value in quality.items():
        if isinstance(value, (int, float)):
            assert math.isfinite(float(value)), key

    assert quality["source_keypoints"] >= 0
    assert quality["target_keypoints"] >= 0
    assert 0 <= quality["inliers"] <= quality["candidate_matches"]
    assert 0.0 <= quality["inlier_ratio"] <= 1.0
    assert 0.0 <= quality["source_coverage_ratio"] <= 1.0
    assert 0.0 <= quality["target_coverage_ratio"] <= 1.0
    assert 0.0 <= quality["overlap_ratio"] <= 1.0

    expected_ratio = quality["inliers"] / max(1, quality["candidate_matches"])
    assert quality["inlier_ratio"] == pytest.approx(expected_ratio)
    for key in ("median_reprojection_error_px", "p95_reprojection_error_px"):
        if quality[key] is not None:
            assert quality[key] >= 0.0


def test_visual_calibration_propagates_known_warp_to_world_quad() -> None:
    source_image = _textured_scene()
    source_to_target = _known_warp()
    target_image = cv2.warpPerspective(
        source_image,
        source_to_target,
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is True, result
    assert result["reason"] is None
    assert result["projection_model"] is not None
    _assert_visual_pose_signature(
        result["source_visual_pose_signature"],
        expected_width=IMAGE_WIDTH,
        expected_height=IMAGE_HEIGHT,
    )
    _assert_visual_pose_signature(
        result["projection_model"]["visual_pose_signature"],
        expected_width=IMAGE_WIDTH,
        expected_height=IMAGE_HEIGHT,
    )
    malformed_signature = dict(result["source_visual_pose_signature"])
    malformed_signature["keypoints_base64"] = base64.b64encode(
        base64.b64decode(malformed_signature["keypoints_base64"], validate=True)[:-4]
    ).decode("ascii")
    with pytest.raises(ValueError, match="keypoint payload has an invalid size"):
        CameraVisualPoseSignature.model_validate(malformed_signature)
    malformed_digest = dict(result["source_visual_pose_signature"])
    malformed_digest["digest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest does not match"):
        CameraVisualPoseSignature.model_validate(malformed_digest)
    quality = result["quality"]
    _assert_quality_is_self_consistent(quality)
    assert quality["candidate_matches"] >= 12
    assert quality["inliers"] >= 10
    assert quality["inlier_ratio"] >= 0.5
    assert min(quality["source_coverage_ratio"], quality["target_coverage_ratio"]) >= 0.05
    assert quality["p95_reprojection_error_px"] <= 8.0

    expected_quad = _expected_world_quad(source_to_target)
    actual_quad = result["projection_model"]["world_quad"]
    for name, (expected_x, expected_z) in expected_quad.items():
        assert actual_quad[name]["x"] == pytest.approx(expected_x, abs=0.2)
        assert actual_quad[name]["z"] == pytest.approx(expected_z, abs=0.2)


def test_visual_pose_signature_is_deterministic_and_contains_no_image_payload() -> None:
    source_image = _textured_scene()
    grayscale = cv2.cvtColor(source_image, cv2.COLOR_BGR2GRAY)

    first = _extract_visual_pose_signature(
        grayscale,
        original_width=IMAGE_WIDTH,
        original_height=IMAGE_HEIGHT,
    )
    second = _extract_visual_pose_signature(
        grayscale,
        original_width=IMAGE_WIDTH,
        original_height=IMAGE_HEIGHT,
    )

    assert first is not None
    assert first == second
    _assert_visual_pose_signature(
        first,
        expected_width=IMAGE_WIDTH,
        expected_height=IMAGE_HEIGHT,
    )
    assert set(first) == {
        "algorithm",
        "keypoint_count",
        "keypoints_base64",
        "descriptors_base64",
        "original_width",
        "original_height",
        "digest_sha256",
    }
    serialized = json.dumps(first, sort_keys=True)
    raw_image_base64 = base64.b64encode(_jpeg_bytes(source_image)).decode("ascii")
    assert raw_image_base64 not in serialized
    assert len(serialized) < len(raw_image_base64)


def test_visual_pose_signature_contract_rejects_fewer_than_sixteen_keypoints() -> None:
    source_image = _textured_scene()
    signature = _extract_visual_pose_signature(
        cv2.cvtColor(source_image, cv2.COLOR_BGR2GRAY),
        original_width=IMAGE_WIDTH,
        original_height=IMAGE_HEIGHT,
    )

    assert signature is not None
    short_signature = _truncate_visual_pose_signature(signature, keypoint_count=15)
    for signature_model in (CameraVisualPoseSignature, CameraMappingVisualPoseSignature):
        with pytest.raises(ValueError, match="greater than or equal to 16"):
            signature_model.model_validate(short_signature)

    with pytest.raises(ValueError, match="requires source and target signatures"):
        CameraVisualCalibrationResponse.model_validate({"accepted": True})


def test_visual_calibration_rejects_when_sift_succeeds_but_orb_signature_is_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_image = _textured_scene()
    source_to_target = _known_warp()
    target_image = cv2.warpPerspective(
        source_image,
        source_to_target,
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )
    original_orb_create = cv2.ORB_create

    class _ShortOrb:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate

        def detectAndCompute(self, image: Any, mask: Any) -> tuple[list[Any], Any]:
            keypoints, descriptors = self._delegate.detectAndCompute(image, mask)
            if descriptors is None:
                return list(keypoints or [])[:15], None
            return list(keypoints or [])[:15], descriptors[:15]

    def _create_short_orb(*args: Any, **kwargs: Any) -> _ShortOrb:
        return _ShortOrb(original_orb_create(*args, **kwargs))

    monkeypatch.setattr(cv2, "ORB_create", _create_short_orb)
    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False
    assert result["reason"] == "visual_pose_signature_failed"
    assert result["projection_model"] is None
    assert result["source_visual_pose_signature"] is None
    assert result["quality"]["candidate_matches"] >= 12
    assert result["quality"]["inliers"] >= 10


def test_visual_calibration_rejects_unrelated_source_before_geometry() -> None:
    reference_image = _textured_scene()
    source_signature = _visual_pose_signature_from_image_bytes(_jpeg_bytes(reference_image))
    unrelated_source = _textured_scene(20260811)
    unrelated_target = cv2.warpPerspective(
        unrelated_source,
        _known_warp(),
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(unrelated_source),
        _jpeg_bytes(unrelated_target),
        _source_control_point_set_with_visual_signature(source_signature),
    ).as_dict()

    assert result["accepted"] is False
    assert result["reason"] == "source_visual_pose_mismatch"
    assert result["projection_model"] is None
    assert result["source_visual_pose_signature"] is None
    assert result["quality"]["source_width"] == IMAGE_WIDTH
    assert result["quality"]["source_height"] == IMAGE_HEIGHT
    assert result["quality"]["target_width"] == 0
    assert result["quality"]["source_keypoints"] == 0
    assert result["quality"]["candidate_matches"] == 0


def test_visual_calibration_accepts_recompressed_signed_source_without_replacing_signature() -> (
    None
):
    reference_image = _textured_scene()
    source_signature = _visual_pose_signature_from_image_bytes(_jpeg_bytes(reference_image))
    target_image = cv2.warpPerspective(
        reference_image,
        _known_warp(),
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(reference_image, quality=88),
        _jpeg_bytes(target_image),
        _source_control_point_set_with_visual_signature(source_signature),
    ).as_dict()

    assert result["accepted"] is True, result
    assert result["reason"] is None
    assert result["source_visual_pose_signature"] == source_signature


def test_visual_calibration_accepts_known_warp_with_photometric_change_and_outliers() -> None:
    source_image = _textured_scene()
    source_to_target = _known_warp()
    target_image = cv2.warpPerspective(
        source_image,
        source_to_target,
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )
    target_image = cv2.convertScaleAbs(target_image, alpha=1.12, beta=14.0)

    cv2.rectangle(target_image, (48, 58), (168, 154), (18, 18, 18), thickness=-1)
    cv2.line(target_image, (48, 58), (168, 154), (244, 244, 244), thickness=5)
    cv2.line(target_image, (168, 58), (48, 154), (244, 244, 244), thickness=5)
    cv2.circle(target_image, (492, 330), 46, (20, 210, 245), thickness=-1)
    cv2.circle(target_image, (492, 330), 24, (210, 32, 64), thickness=5)
    cv2.putText(
        target_image,
        "NEW",
        (451, 337),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        thickness=2,
        lineType=cv2.LINE_AA,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is True, result
    assert result["reason"] is None
    assert result["projection_model"] is not None
    quality = result["quality"]
    _assert_quality_is_self_consistent(quality)
    assert quality["candidate_matches"] >= 12
    assert quality["inliers"] >= 10
    assert quality["candidate_matches"] > quality["inliers"]
    assert quality["inlier_ratio"] >= 0.5
    assert min(quality["source_coverage_ratio"], quality["target_coverage_ratio"]) >= 0.05
    assert quality["overlap_ratio"] >= 0.6
    assert quality["median_displacement_ratio"] >= 0.01
    assert quality["p95_reprojection_error_px"] <= 8.0

    expected_quad = _expected_world_quad(source_to_target)
    actual_quad = result["projection_model"]["world_quad"]
    for name, (expected_x, expected_z) in expected_quad.items():
        assert actual_quad[name]["x"] == pytest.approx(expected_x, abs=0.25)
        assert actual_quad[name]["z"] == pytest.approx(expected_z, abs=0.25)


def test_visual_calibration_rejects_large_translation_extrapolation() -> None:
    source_image = _textured_scene()
    source_to_target = np.asarray(
        [
            [1.0, 0.0, 150.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    target_image = cv2.warpPerspective(
        source_image,
        source_to_target,
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False, result
    assert result["reason"] == "excessive_source_extrapolation"
    assert result["projection_model"] is None
    assert 0.6 <= result["quality"]["overlap_ratio"] < 0.8


def test_visual_calibration_partial_image_region_does_not_report_full_overlap() -> None:
    source_image = _textured_scene()
    target_image = cv2.warpPerspective(
        source_image,
        _known_warp(),
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        _source_control_point_set_for_image_region(
            left=0.2,
            top=0.2,
            right=0.8,
            bottom=0.8,
        ),
    ).as_dict()

    assert result["accepted"] is False, result
    assert result["reason"] == "insufficient_overlap"
    assert result["projection_model"] is None
    assert 0.25 < result["quality"]["overlap_ratio"] < 0.45


def test_visual_calibration_discards_refinement_that_would_fold_mesh() -> None:
    base_set = _source_control_point_set()
    source_set = ControlPointSet(
        id=base_set.id,
        label=base_set.label,
        pose_reference=base_set.pose_reference,
        control_points=base_set.control_points,
        refinement_points=(
            ControlPointRefinementPoint(
                id="folding-center",
                image_u=0.5,
                image_v=0.5,
                world_x=-5.0,
                world_z=6.0,
            ),
        ),
    )
    source_image = _textured_scene()
    target_image = cv2.warpPerspective(
        source_image,
        _known_warp(),
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        source_set,
    ).as_dict()

    assert result["accepted"] is True, result
    assert result["projection_model"]["refinement"] is None
    assert result["quality"]["refinement_points"] == 0
    assert result["quality"]["discarded_refinement_points"] > 0


def test_visual_calibration_rejects_uniform_target() -> None:
    uniform_target = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8)

    result = propagate_visual_calibration(
        _jpeg_bytes(_textured_scene()),
        _jpeg_bytes(uniform_target),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False
    assert result["reason"] == "insufficient_visual_features"
    assert result["projection_model"] is None
    _assert_quality_is_self_consistent(result["quality"])
    assert result["quality"]["target_keypoints"] == 0


def test_visual_calibration_rejects_unchanged_view() -> None:
    image_bytes = _jpeg_bytes(_textured_scene())

    result = propagate_visual_calibration(
        image_bytes,
        image_bytes,
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False, result
    assert result["reason"] == "insufficient_view_change"
    assert result["projection_model"] is None
    assert result["quality"]["median_displacement_ratio"] < 0.01


def test_visual_calibration_prioritizes_missing_coverage_over_view_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_bytes = _jpeg_bytes(_textured_scene())
    monkeypatch.setattr(visual_calibration, "_coverage_ratio", lambda _points: 0.0)

    result = propagate_visual_calibration(
        image_bytes,
        image_bytes,
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False, result
    assert result["reason"] == "insufficient_spatial_coverage"
    assert result["quality"]["median_displacement_ratio"] == 0.0


def test_visual_calibration_preserves_local_but_not_boundary_refinement() -> None:
    base_set = _source_control_point_set()
    local_refinement = ControlPointRefinementPoint(
        id="local-center",
        image_u=0.45,
        image_v=0.55,
        world_x=9.4,
        world_z=6.2,
    )
    source_set = ControlPointSet(
        id=base_set.id,
        label=base_set.label,
        pose_reference=base_set.pose_reference,
        control_points=base_set.control_points,
        refinement_points=(local_refinement,),
        boundary_refinement_points=(
            ControlPointBoundaryRefinementPoint(
                id="top-edge",
                edge="top",
                t=0.5,
                image_u=0.5,
                image_v=0.0,
                world_x=10.0,
                world_z=0.0,
            ),
        ),
    )
    source_image = _textured_scene()
    source_to_target = _known_warp()
    target_image = cv2.warpPerspective(
        source_image,
        source_to_target,
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )

    result = propagate_visual_calibration(
        _jpeg_bytes(source_image),
        _jpeg_bytes(target_image),
        source_set,
    ).as_dict()

    assert result["accepted"] is True, result
    projection_model = result["projection_model"]
    assert projection_model["boundary_refinement"] is None
    assert result["quality"]["refinement_points"] == 1
    assert projection_model["refinement"] is not None
    assert len(projection_model["refinement"]["points"]) == 1

    transported = projection_model["refinement"]["points"][0]
    expected_u, expected_v = _warp_normalized_point(
        source_to_target,
        local_refinement.image_u,
        local_refinement.image_v,
    )
    assert transported["id"] == local_refinement.id
    assert transported["image"]["x"] == pytest.approx(expected_u, abs=0.01)
    assert transported["image"]["y"] == pytest.approx(expected_v, abs=0.01)
    assert transported["world"] == {
        "x": local_refinement.world_x,
        "z": local_refinement.world_z,
    }


def test_visual_calibration_rejects_repeated_facade_without_unique_evidence() -> None:
    source = _repeated_facade()
    target = np.roll(source, shift=48, axis=1)

    result = propagate_visual_calibration(
        _jpeg_bytes(source),
        _jpeg_bytes(target),
        _source_control_point_set(),
    ).as_dict()

    assert result["accepted"] is False, result
    assert result["projection_model"] is None
    _assert_quality_is_self_consistent(result["quality"])


def test_visual_calibration_api_accepts_and_rejects_without_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_image = _textured_scene()
    target_image = cv2.warpPerspective(
        source_image,
        _known_warp(),
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )
    uniform_target = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8)
    form = {
        "source_view_json": json.dumps(_source_view_payload()),
        "source_id": "main",
    }
    source_bytes = _jpeg_bytes(source_image)
    configuration_path = tmp_path / "data" / "config.json"

    with _create_camera_client(tmp_path, monkeypatch) as client:
        before_configuration = (
            configuration_path.read_bytes() if configuration_path.exists() else None
        )
        before_composition_response = client.get("/api/composition")
        assert before_composition_response.status_code == 200
        before_composition = before_composition_response.json()

        accepted = client.post(
            "/api/cameras/projection/propagate",
            data=form,
            files={
                "source_image": ("reference.jpg", source_bytes, "image/jpeg"),
                "target_image": ("current.jpg", _jpeg_bytes(target_image), "image/jpeg"),
            },
        )
        rejected = client.post(
            "/api/cameras/projection/propagate",
            data=form,
            files={
                "source_image": ("reference.jpg", source_bytes, "image/jpeg"),
                "target_image": ("current.jpg", _jpeg_bytes(uniform_target), "image/jpeg"),
            },
        )
        invalid = client.post(
            "/api/cameras/projection/propagate",
            data=form,
            files={
                "source_image": ("reference.jpg", source_bytes, "image/jpeg"),
                "target_image": ("current.jpg", b"not an image", "image/jpeg"),
            },
        )
        after_composition_response = client.get("/api/composition")
        assert after_composition_response.status_code == 200
        after_composition = after_composition_response.json()
        after_configuration = (
            configuration_path.read_bytes() if configuration_path.exists() else None
        )

    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["accepted"] is True
    assert accepted_body["projection_model"]["type"] == "image_quad_on_world"
    _assert_visual_pose_signature(
        accepted_body["source_visual_pose_signature"],
        expected_width=IMAGE_WIDTH,
        expected_height=IMAGE_HEIGHT,
    )
    _assert_visual_pose_signature(
        accepted_body["projection_model"]["visual_pose_signature"],
        expected_width=IMAGE_WIDTH,
        expected_height=IMAGE_HEIGHT,
    )
    quality = accepted_body["quality"]
    assert quality["inliers"] >= 10
    assert quality["source_width"] == IMAGE_WIDTH
    assert quality["source_height"] == IMAGE_HEIGHT
    assert quality["target_width"] == IMAGE_WIDTH
    assert quality["target_height"] == IMAGE_HEIGHT
    assert quality["refinement_points"] == 0
    assert quality["discarded_refinement_points"] == 0
    assert quality["median_displacement_ratio"] >= 0.01

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["accepted"] is False
    assert rejected.json()["projection_model"] is None
    assert rejected.json()["source_visual_pose_signature"] is None

    assert invalid.status_code == 400, invalid.text
    assert after_configuration == before_configuration
    assert after_composition == before_composition


def test_visual_calibration_api_requires_approved_compatible_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_image = _textured_scene()
    source_bytes = _jpeg_bytes(source_image)
    target_bytes = _jpeg_bytes(
        cv2.warpPerspective(
            source_image,
            _known_warp(),
            (IMAGE_WIDTH, IMAGE_HEIGHT),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
    )

    def files() -> dict[str, tuple[str, bytes, str]]:
        return {
            "source_image": ("reference.jpg", source_bytes, "image/jpeg"),
            "target_image": ("current.jpg", target_bytes, "image/jpeg"),
        }

    with _create_camera_client(tmp_path, monkeypatch) as client:
        missing_source = client.post(
            "/api/cameras/projection/propagate",
            data={"source_view_json": json.dumps(_source_view_payload())},
            files=files(),
        )

        estimated_view = _source_view_payload()
        estimated_view["projection_quality"] = {
            "status": "estimated",
            "estimated": True,
        }
        estimated = client.post(
            "/api/cameras/projection/propagate",
            data={
                "source_view_json": json.dumps(estimated_view),
                "source_id": "main",
            },
            files=files(),
        )

        estimated_flag_view = _source_view_payload()
        estimated_flag_view["projection_quality"] = {
            "status": "ready",
            "estimated": True,
        }
        estimated_flag = client.post(
            "/api/cameras/projection/propagate",
            data={
                "source_view_json": json.dumps(estimated_flag_view),
                "source_id": "main",
            },
            files=files(),
        )

        incomplete_view = _source_view_payload()
        incomplete_view["projection_quality"] = {
            "status": "incomplete",
            "estimated": False,
        }
        incomplete = client.post(
            "/api/cameras/projection/propagate",
            data={
                "source_view_json": json.dumps(incomplete_view),
                "source_id": "main",
            },
            files=files(),
        )

        empty_scope_view = _source_view_payload()
        empty_scope_view["stream_scope"]["compatible_source_ids"] = []
        empty_scope = client.post(
            "/api/cameras/projection/propagate",
            data={
                "source_view_json": json.dumps(empty_scope_view),
                "source_id": "main",
            },
            files=files(),
        )

        incompatible_view = _source_view_payload()
        incompatible_view["stream_scope"]["compatible_source_ids"] = ["main"]
        incompatible = client.post(
            "/api/cameras/projection/propagate",
            data={
                "source_view_json": json.dumps(incompatible_view),
                "source_id": "sub",
            },
            files=files(),
        )

    assert missing_source.status_code == 400, missing_source.text
    missing_source_detail = str(missing_source.json()["detail"]).lower()
    assert "source" in missing_source_detail
    assert "required" in missing_source_detail
    assert estimated.status_code == 409, estimated.text
    assert estimated_flag.status_code == 409, estimated_flag.text
    assert incomplete.status_code == 409, incomplete.text
    assert empty_scope.status_code == 409, empty_scope.text
    assert incompatible.status_code == 409, incompatible.text


def test_visual_calibration_api_rejects_oversized_request_and_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes = _jpeg_bytes(_textured_scene())
    form = {
        "source_view_json": json.dumps(_source_view_payload()),
        "source_id": "main",
    }

    with _create_camera_client(tmp_path, monkeypatch) as client:
        oversized_content_length = client.post(
            "/api/cameras/projection/propagate",
            content=b"x",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(32 * 1024 * 1024),
            },
        )
        oversized_image = client.post(
            "/api/cameras/projection/propagate",
            data=form,
            files={
                "source_image": (
                    "reference.jpg",
                    b"x" * (12 * 1024 * 1024 + 1),
                    "image/jpeg",
                ),
                "target_image": ("current.jpg", source_bytes, "image/jpeg"),
            },
        )

    assert oversized_content_length.status_code == 413, oversized_content_length.text
    assert oversized_image.status_code == 413, oversized_image.text


def test_visual_calibration_api_accepts_streamed_multipart_without_content_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = "toposync-visual-calibration-boundary"
    source_bytes = _jpeg_bytes(_textured_scene())
    target_bytes = _jpeg_bytes(np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8))
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )

    def add_file(name: str, filename: str, content: bytes) -> None:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    "Content-Type: image/jpeg\r\n\r\n"
                ).encode(),
                content,
                b"\r\n",
            ]
        )

    add_field("source_view_json", json.dumps(_source_view_payload()))
    add_field("source_id", "main")
    add_file("source_image", "reference.jpg", source_bytes)
    add_file("target_image", "current.jpg", target_bytes)
    parts.append(f"--{boundary}--\r\n".encode())

    with _create_camera_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/cameras/projection/propagate",
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            content=iter(parts),
        )

    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "insufficient_visual_features"


def test_visual_calibration_upload_reader_times_out_without_waiting_for_body_end() -> None:
    class StalledRequest:
        async def stream(self) -> AsyncIterator[bytes]:
            yield b"partial-body"
            await asyncio.Event().wait()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            cameras_plugin._read_visual_calibration_request_body(  # noqa: SLF001
                StalledRequest(),  # type: ignore[arg-type]
                timeout_ms=10,
            )
        )

    assert raised.value.status_code == 408
    assert raised.value.detail == "Visual calibration upload timed out"


def test_visual_calibration_upload_admission_rejects_busy_and_releases_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOSYNC_CAMERA_VISUAL_CALIBRATION_UPLOAD_CONCURRENCY", "1")
    source_bytes = _jpeg_bytes(_textured_scene())
    target_bytes = _jpeg_bytes(np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8))
    upload_started = threading.Event()
    release_upload = threading.Event()
    original_reader = cameras_plugin._read_visual_calibration_request_body  # noqa: SLF001

    async def controlled_reader(request: Any, *, timeout_ms: int) -> bytes:
        if request.headers.get("x-test-hold-visual-upload") == "1":
            upload_started.set()
            released = await asyncio.to_thread(release_upload.wait, 1.0)
            assert released
            raise HTTPException(status_code=408, detail="Visual calibration upload timed out")
        return await original_reader(request, timeout_ms=timeout_ms)

    monkeypatch.setattr(
        cameras_plugin,
        "_read_visual_calibration_request_body",
        controlled_reader,
    )

    first_responses: list[Any] = []
    first_errors: list[BaseException] = []

    with _create_camera_client(tmp_path, monkeypatch) as client:

        def submit_blocked_upload() -> None:
            try:
                first_responses.append(
                    _post_visual_calibration(
                        client,
                        source_bytes=source_bytes,
                        target_bytes=target_bytes,
                        headers={"x-test-hold-visual-upload": "1"},
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                first_errors.append(exc)

        first_thread = threading.Thread(target=submit_blocked_upload, daemon=True)
        first_thread.start()
        assert upload_started.wait(1.0)

        busy = _post_visual_calibration(
            client,
            source_bytes=source_bytes,
            target_bytes=target_bytes,
        )
        assert busy.status_code == 429, busy.text
        assert busy.headers["retry-after"] == "1"
        assert busy.json()["detail"] == "Visual calibration upload capacity is busy"

        release_upload.set()
        first_thread.join(timeout=1.0)
        assert not first_thread.is_alive()
        assert first_errors == []
        assert len(first_responses) == 1
        assert first_responses[0].status_code == 408

        recovered = _post_visual_calibration(
            client,
            source_bytes=source_bytes,
            target_bytes=target_bytes,
        )

    assert recovered.status_code == 200, recovered.text


def test_visual_calibration_executor_rejects_backlog_and_releases_after_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOSYNC_CAMERA_VISUAL_CALIBRATION_CONCURRENCY", "1")
    monkeypatch.setenv("TOPOSYNC_CAMERA_VISUAL_CALIBRATION_UPLOAD_CONCURRENCY", "2")
    source_bytes = _jpeg_bytes(_textured_scene())
    target_bytes = _jpeg_bytes(np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8))
    worker_started = threading.Event()
    release_worker = threading.Event()
    original_propagate = cameras_plugin.propagate_visual_calibration

    def controlled_propagate(*args: Any):
        worker_started.set()
        assert release_worker.wait(1.0)
        return original_propagate(*args)

    monkeypatch.setattr(cameras_plugin, "propagate_visual_calibration", controlled_propagate)

    first_responses: list[Any] = []
    first_errors: list[BaseException] = []

    with _create_camera_client(tmp_path, monkeypatch) as client:

        def submit_blocked_worker() -> None:
            try:
                first_responses.append(
                    _post_visual_calibration(
                        client,
                        source_bytes=source_bytes,
                        target_bytes=target_bytes,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                first_errors.append(exc)

        first_thread = threading.Thread(target=submit_blocked_worker, daemon=True)
        first_thread.start()
        assert worker_started.wait(1.0)

        busy = _post_visual_calibration(
            client,
            source_bytes=source_bytes,
            target_bytes=target_bytes,
        )
        assert busy.status_code == 429, busy.text
        assert busy.headers["retry-after"] == "1"
        assert busy.json()["detail"] == "Visual calibration capacity is busy"

        release_worker.set()
        first_thread.join(timeout=1.0)
        assert not first_thread.is_alive()
        assert first_errors == []
        assert len(first_responses) == 1
        assert first_responses[0].status_code == 200, first_responses[0].text

        recovered = _post_visual_calibration(
            client,
            source_bytes=source_bytes,
            target_bytes=target_bytes,
        )

    assert recovered.status_code == 200, recovered.text


def test_visual_calibration_cancelled_request_keeps_executor_slot_until_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOSYNC_CAMERA_VISUAL_CALIBRATION_CONCURRENCY", "1")
    source_bytes = _jpeg_bytes(_textured_scene())
    target_bytes = _jpeg_bytes(np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 127, dtype=np.uint8))
    worker_started = threading.Event()
    worker_finished = threading.Event()
    release_worker = threading.Event()
    original_propagate = cameras_plugin.propagate_visual_calibration

    def controlled_propagate(*args: Any):
        worker_started.set()
        assert release_worker.wait(1.0)
        try:
            return original_propagate(*args)
        finally:
            worker_finished.set()

    monkeypatch.setattr(cameras_plugin, "propagate_visual_calibration", controlled_propagate)

    boundary = "toposync-cancelled-calibration"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="source_view_json"\r\n\r\n',
            json.dumps(_source_view_payload()).encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="source_id"\r\n\r\nmain\r\n',
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="source_image"; filename="reference.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            source_bytes,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="target_image"; filename="current.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            target_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )

    with _create_camera_client(tmp_path, monkeypatch) as client:
        endpoint = next(
            route.endpoint
            for route in client.app.routes
            if getattr(route, "path", "") == "/api/cameras/projection/propagate"
        )

        def make_request() -> Request:
            body_sent = False

            async def receive() -> dict[str, Any]:
                nonlocal body_sent
                if body_sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            return Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/cameras/projection/propagate",
                    "raw_path": b"/api/cameras/projection/propagate",
                    "query_string": b"",
                    "headers": [
                        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                    "app": client.app,
                },
                receive,
            )

        async def scenario() -> None:
            cancelled_request = asyncio.create_task(endpoint(make_request()))
            assert await asyncio.to_thread(worker_started.wait, 1.0)
            cancelled_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_request

            with pytest.raises(HTTPException) as busy:
                await endpoint(make_request())
            assert busy.value.status_code == 429
            assert busy.value.detail == "Visual calibration capacity is busy"

            release_worker.set()
            assert await asyncio.to_thread(worker_finished.wait, 1.0)
            await asyncio.sleep(0)

            recovered = await endpoint(make_request())
            assert recovered.accepted is False

        client.portal.call(scenario)
