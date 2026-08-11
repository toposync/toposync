from __future__ import annotations

import json
import math
import os
import time
from typing import Any

import httpx
import pytest


pytestmark = pytest.mark.integration


def _require_live_configuration() -> tuple[str, str, str]:
    if os.getenv("TOPOSYNC_CAMERA_CALIBRATION_LIVE", "").strip() != "1":
        pytest.skip("Set TOPOSYNC_CAMERA_CALIBRATION_LIVE=1 to move a configured PTZ camera")
    camera_id = os.getenv("TOPOSYNC_CAMERA_CALIBRATION_CAMERA_ID", "").strip()
    source_id = os.getenv("TOPOSYNC_CAMERA_CALIBRATION_SOURCE_ID", "").strip()
    if not camera_id or not source_id:
        pytest.fail("Live camera and source identifiers are required")
    base_url = os.getenv("TOPOSYNC_CAMERA_CALIBRATION_BASE_URL", "http://127.0.0.1:8100").rstrip(
        "/"
    )
    return base_url, camera_id, source_id


def _request_json(response: httpx.Response) -> dict[str, Any]:
    assert response.is_success, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _status(client: httpx.Client, camera_id: str, source_id: str) -> dict[str, Any]:
    body = _request_json(
        client.get(
            f"/api/cameras/cameras/{camera_id}/ptz/status",
            params={"source_id": source_id},
        )
    )
    status = body.get("status")
    assert isinstance(status, dict)
    return status


def _finite_pose(status: dict[str, Any]) -> tuple[float, float, float] | None:
    try:
        pose = tuple(float(status.get(axis)) for axis in ("pan", "tilt", "zoom"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in pose):
        return None
    return pose  # type: ignore[return-value]


def _wait_until_idle(
    client: httpx.Client,
    camera_id: str,
    source_id: str,
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    consecutive_idle_reads = 0
    while time.monotonic() < deadline:
        latest = _status(client, camera_id, source_id)
        if str(latest.get("move_status") or "").strip().lower() == "idle":
            consecutive_idle_reads += 1
            if consecutive_idle_reads >= 2:
                return latest
        else:
            consecutive_idle_reads = 0
        time.sleep(0.5)
    pytest.fail(f"Camera did not become idle: {latest.get('move_status')}")


def _preset_record(
    client: httpx.Client,
    camera_id: str,
    source_id: str,
    preset_token: str,
) -> dict[str, Any]:
    body = _request_json(
        client.get(
            f"/api/cameras/cameras/{camera_id}/ptz/presets",
            params={"source_id": source_id},
        )
    )
    presets = body.get("presets")
    assert isinstance(presets, list)
    for preset in presets:
        if isinstance(preset, dict) and str(preset.get("token") or "").strip() == preset_token:
            return preset
    pytest.fail(f"PTZ preset is not available: {preset_token}")


def _position_camera(
    client: httpx.Client,
    camera_id: str,
    source_id: str,
    *,
    pose: tuple[float, float, float] | None,
    preset_token: str,
) -> dict[str, Any]:
    if preset_token:
        _request_json(
            client.post(
                f"/api/cameras/cameras/{camera_id}/ptz/goto-preset",
                json={"source_id": source_id, "preset_token": preset_token},
            )
        )
    else:
        assert pose is not None
        _request_json(
            client.post(
                f"/api/cameras/cameras/{camera_id}/ptz/absolute-move",
                json={
                    "source_id": source_id,
                    "pan": pose[0],
                    "tilt": pose[1],
                    "zoom": pose[2],
                },
            )
        )

    positioned_status = _wait_until_idle(client, camera_id, source_id)
    assert str(positioned_status.get("move_status") or "").strip().lower() == "idle"
    if pose is not None:
        positioned_pose = _finite_pose(positioned_status)
        assert positioned_pose is not None
        assert all(
            abs(actual - expected) <= 0.01
            for actual, expected in zip(positioned_pose, pose, strict=True)
        )
    return positioned_status


def _stop_camera(client: httpx.Client, camera_id: str, source_id: str) -> None:
    _request_json(
        client.post(
            f"/api/cameras/cameras/{camera_id}/ptz/stop",
            json={"source_id": source_id, "pan_tilt": True, "zoom": True},
        )
    )


def _snapshot(client: httpx.Client, camera_id: str, source_id: str) -> bytes:
    response = client.get(
        f"/api/cameras/cameras/{camera_id}/snapshot",
        params={"source_id": source_id, "fresh": "true"},
    )
    assert response.is_success, response.text
    assert response.headers.get("content-type", "").startswith("image/")
    assert len(response.content) > 1_000
    return response.content


def _ready_view_for_camera(
    composition: dict[str, Any],
    camera_id: str,
    source_id: str,
) -> dict[str, Any]:
    requested_view_id = os.getenv("TOPOSYNC_CAMERA_CALIBRATION_VIEW_ID", "").strip()
    for element in composition.get("elements", []):
        if not isinstance(element, dict):
            continue
        props = element.get("props") if isinstance(element.get("props"), dict) else {}
        if str(props.get("camera_id") or "").strip() != camera_id:
            continue
        for view in props.get("calibrated_views", []):
            if not isinstance(view, dict):
                continue
            quality = view.get("projection_quality")
            if not isinstance(quality, dict) or quality.get("status") != "ready":
                continue
            if requested_view_id and str(view.get("id") or "").strip() != requested_view_id:
                continue
            stream_scope = (
                view.get("stream_scope") if isinstance(view.get("stream_scope"), dict) else {}
            )
            compatible_source_ids = [
                str(item or "").strip()
                for item in stream_scope.get("compatible_source_ids", [])
                if str(item or "").strip()
            ]
            if compatible_source_ids and source_id not in compatible_source_ids:
                continue
            return view
    pytest.fail("The camera has no approved calibration view in the active composition")


def _propagate(
    client: httpx.Client,
    source_view: dict[str, Any],
    source_id: str,
    source_image: bytes,
    target_image: bytes,
) -> dict[str, Any]:
    response = client.post(
        "/api/cameras/projection/propagate",
        data={"source_view_json": json.dumps(source_view), "source_id": source_id},
        files={
            "source_image": ("reference.jpg", source_image, "image/jpeg"),
            "target_image": ("current.jpg", target_image, "image/jpeg"),
        },
    )
    return _request_json(response)


def _world_quad_error_ratio(source_view: dict[str, Any], propagated: dict[str, Any]) -> float:
    source_quad = source_view["projection_model"]["world_quad"]
    target_quad = propagated["projection_model"]["world_quad"]
    corners = ("top_left", "top_right", "bottom_right", "bottom_left")
    errors = [
        math.hypot(
            float(target_quad[corner]["x"]) - float(source_quad[corner]["x"]),
            float(target_quad[corner]["z"]) - float(source_quad[corner]["z"]),
        )
        for corner in corners
    ]
    diagonal = math.hypot(
        float(source_quad["bottom_right"]["x"]) - float(source_quad["top_left"]["x"]),
        float(source_quad["bottom_right"]["z"]) - float(source_quad["top_left"]["z"]),
    )
    return max(errors) / max(diagonal, 1e-9)


def test_live_camera_propagates_a_small_pan_and_restores_pose() -> None:
    base_url, camera_id, source_id = _require_live_configuration()
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        source_view = _ready_view_for_camera(
            _request_json(client.get("/api/composition")),
            camera_id,
            source_id,
        )
        source_pose_value = source_view.get("pose_reference")
        source_pose = source_pose_value if isinstance(source_pose_value, dict) else {}
        source_reference_pose = _finite_pose(source_pose)
        source_preset_token = str(source_pose.get("preset_token") or "").strip()
        if source_reference_pose is None and not source_preset_token:
            pytest.skip(
                "The approved view has no complete PTZ pose or preset; camera was not moved"
            )

        if source_preset_token:
            source_preset = _preset_record(
                client,
                camera_id,
                source_id,
                source_preset_token,
            )
            source_reference_pose = source_reference_pose or _finite_pose(source_preset)

        initial_status = _wait_until_idle(client, camera_id, source_id)
        initial_pose = _finite_pose(initial_status)
        restore_preset_token = os.getenv(
            "TOPOSYNC_CAMERA_CALIBRATION_RESTORE_PRESET_TOKEN", ""
        ).strip()
        if initial_pose is None and not restore_preset_token:
            pytest.skip("Camera position is unavailable; configure a return preset before moving")
        if initial_pose is None and not source_preset_token:
            pytest.skip(
                "Camera does not report PTZ position and the approved view has no preset; "
                "camera was not moved"
            )
        source_position_pose = source_reference_pose if initial_pose is not None else None

        restore_preset_pose: tuple[float, float, float] | None = None
        if restore_preset_token:
            restore_preset = _preset_record(
                client,
                camera_id,
                source_id,
                restore_preset_token,
            )
            restore_preset_pose = _finite_pose(restore_preset) if initial_pose is not None else None

        movement_attempted = False
        target_image: bytes | None = None
        forward: dict[str, Any] | None = None
        cycle: dict[str, Any] | None = None

        try:
            movement_attempted = True
            reference_status = _position_camera(
                client,
                camera_id,
                source_id,
                pose=source_position_pose,
                preset_token=source_preset_token,
            )
            reference_pose = _finite_pose(reference_status)
            reference_image = _snapshot(client, camera_id, source_id)

            if reference_pose is not None:
                reference_pan, reference_tilt, reference_zoom = reference_pose
                pan_delta = 0.025 if reference_pan <= 0.95 else -0.025
                _request_json(
                    client.post(
                        f"/api/cameras/cameras/{camera_id}/ptz/absolute-move",
                        json={
                            "source_id": source_id,
                            "pan": reference_pan + pan_delta,
                            "tilt": reference_tilt,
                            "zoom": reference_zoom,
                        },
                    )
                )
            else:
                _request_json(
                    client.post(
                        f"/api/cameras/cameras/{camera_id}/ptz/move",
                        json={
                            "source_id": source_id,
                            "pan": 0.08,
                            "tilt": 0.0,
                            "zoom": 0.0,
                            "timeout_s": 1.0,
                        },
                    )
                )
                time.sleep(0.2)
                _stop_camera(client, camera_id, source_id)

            target_status = _wait_until_idle(client, camera_id, source_id)
            target_pose = _finite_pose(target_status)
            if reference_pose is not None:
                assert target_pose is not None
                assert abs(target_pose[0] - reference_pose[0]) >= 0.005
            target_image = _snapshot(client, camera_id, source_id)
            forward = _propagate(client, source_view, source_id, reference_image, target_image)
            assert forward["accepted"] is True, forward
            assert forward["projection_model"] is not None
            assert forward["quality"]["median_displacement_ratio"] >= 0.01

            _stop_camera(client, camera_id, source_id)
            _position_camera(
                client,
                camera_id,
                source_id,
                pose=source_position_pose,
                preset_token=source_preset_token,
            )
            restored_reference_image = _snapshot(client, camera_id, source_id)
            target_view = {
                **source_view,
                "id": f"{source_view.get('id', 'reference')}-live-target",
                "stream_scope": {
                    "compatible_roles": source_view.get("stream_scope", {}).get(
                        "compatible_roles", []
                    ),
                    "compatible_source_ids": [source_id],
                },
                "projection_model": forward["projection_model"],
                "projection_quality": {"status": "ready", "estimated": False},
            }
            cycle = _propagate(
                client,
                target_view,
                source_id,
                target_image,
                restored_reference_image,
            )
            assert cycle["accepted"] is True, cycle
            assert cycle["quality"]["median_displacement_ratio"] >= 0.01
            assert _world_quad_error_ratio(source_view, cycle) <= 0.01
        finally:
            try:
                _stop_camera(client, camera_id, source_id)
            finally:
                try:
                    if movement_attempted and restore_preset_token:
                        _position_camera(
                            client,
                            camera_id,
                            source_id,
                            pose=restore_preset_pose,
                            preset_token=restore_preset_token,
                        )
                    elif movement_attempted and initial_pose is not None:
                        _position_camera(
                            client,
                            camera_id,
                            source_id,
                            pose=initial_pose,
                            preset_token="",
                        )
                finally:
                    _stop_camera(client, camera_id, source_id)

        assert target_image is not None
        assert forward is not None
        assert cycle is not None
