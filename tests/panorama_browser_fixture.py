"""Loopback-only browser fixture: real panorama endpoints, synthetic camera inputs.

Run: .venv/bin/python tests/panorama_browser_fixture.py --port 8127
No real devices, streams, calibration results, or production data are loaded.
The image oracle is an independent pinhole projection of a floor at height zero.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import HTTPException
from fastapi.responses import Response

CAMERA_ID = "panorama-simulated-camera"
ELEMENT_ID = "panorama-simulated-element"
COMPOSITION_ID = "panorama-simulated-composition"
SOURCE_ARTIFACT_ID = "b" * 32
PROFILE = {
    "lens": {"width": 640, "height": 400, "fx": 380, "fy": 380, "cx": 319.5, "cy": 199.5, "distortion": []},
    "pan_axis": {"position_min": -0.95, "position_max": 0.95, "angle_min_radians": -0.95, "angle_max_radians": 0.95},
    "tilt_axis": {"position_min": -1.05, "position_max": -0.25, "angle_min_radians": -1.05, "angle_max_radians": -0.25},
    "zoom": 0,
    "position_tolerance": 0.005,
    "settle_timeout_seconds": 2,
}
TARGETS = [(3, -3), (8, -4), (9, 3), (3, 3), (5, -4), (7, 4), (4.5, -1), (7, 1.5)]


def known_points() -> list[dict]:
    points = []
    for index, (x, z) in enumerate(TARGETS):
        pan = math.atan2(z, x)
        tilt = math.atan2(-3, math.hypot(x, z))
        points.append({"id": f"known-{index + 1}", "role": "fit" if index < 6 else "check", "world": {"x": x, "z": z}, "panorama": {"x": (pan / math.pi + 1) / 2, "y": 0.5 - tilt / math.pi}})
    return points


def fixture_config() -> dict:
    return {
        "schema_version": 1,
        "active_composition_id": COMPOSITION_ID,
        "compositions": [{"id": COMPOSITION_ID, "name": "Validação simulada", "elements": [
            {"id": "simulated-floor", "type": "com.toposync.structural.area", "name": "Chão sintético", "props": {"vertices": [{"x": 0, "z": -5}, {"x": 10, "z": -5}, {"x": 10, "z": 5}, {"x": 0, "z": 5}], "fill": "#67887b", "opacity": 1}},
            {"id": ELEMENT_ID, "type": "com.toposync.cameras.camera", "name": "Câmera simulada", "position": {"x": 0, "y": 3, "z": 0}, "props": {"camera_id": CAMERA_ID, "camera_name": "Câmera simulada", "view_mode": "ceiling"}},
            *[{"id": f"target-{index + 1}", "type": "com.toposync.structural.area", "name": f"Lugar {index + 1}", "props": {"vertices": [{"x": x - .13, "z": z - .13}, {"x": x + .13, "z": z - .13}, {"x": x + .13, "z": z + .13}, {"x": x - .13, "z": z + .13}], "fill": "#f4b54a" if index < 6 else "#5edce5", "opacity": 1}} for index, (x, z) in enumerate(TARGETS)],
        ]}],
        "settings": {"core": {}, "extensions": {"com.toposync.cameras": {"schema_version": 4, "devices": [{
            "id": CAMERA_ID, "name": "Câmera simulada", "kind": "camera", "enabled": True,
            "control": {"type": "onvif", "automation_exclusive_control_confirmed": True},
            "onvif": {"xaddr": "http://127.0.0.1:1/synthetic-only"},
            "sources": [{"id": "synthetic-wide", "name": "Imagem sintética", "enabled": True, "is_default": True, "kind": "video", "role": "main", "view_id": "synthetic-wide", "origin": {"type": "rtsp", "rtsp_url": "rtsp://127.0.0.1:1/synthetic-only", "has_ptz": True}, "ingest": {"mode": "direct"}, "video": {"width": 640, "height": 400}, "metadata": {"panorama_profile": copy.deepcopy(PROFILE), "panorama": {"revision": 1, "active": {"artifact_id": SOURCE_ARTIFACT_ID, "crop_revision": 1, "crop": {"u_start": .32, "u_width": .36, "v_start": .55, "v_height": .30}}}}}],
        }]}}},
        "pipelines": [],
    }


def write_saved_source_panorama(data_dir: Path, config: dict) -> None:
    """Independent canonical sphere fixture; browser navigation never needs a scan."""
    from toposync_ext_cameras.settings import get_camera_device, get_camera_source
    from toposync_ext_cameras.source_panorama import _identity

    width, height = 1024, 512
    rows, columns = np.indices((height, width))
    pan = ((columns + .5) / width * 2 - 1) * math.pi
    tilt = (.5 - (rows + .5) / height) * math.pi
    distance = np.divide(-3, np.sin(tilt), out=np.zeros_like(tilt), where=tilt < -.03)
    x, z = np.cos(tilt) * np.cos(pan) * distance, np.cos(tilt) * np.sin(pan) * distance
    covered = (tilt < -.03) & (x > 0) & (x < 14) & (np.abs(z) < 8)
    checker = (np.floor(x) + np.floor(z)).astype(int) % 2 == 0
    image = np.full((height, width, 3), (48, 42, 38), np.uint8)
    image[covered & checker] = (95, 126, 112)
    image[covered & ~checker] = (112, 145, 132)
    for index, point in enumerate(known_points()):
        pixel = point["panorama"]
        u, v = round(pixel["x"] * width), round(pixel["y"] * height)
        cv2.circle(image, (u, v), 5, (60, 184, 255) if index < 6 else (230, 220, 70), -1)
        cv2.putText(image, str(index + 1), (u + 7, v - 4), cv2.FONT_HERSHEY_SIMPLEX, .38, (255, 255, 255), 1)
    directory = data_dir / "runtime" / "cameras" / "source-panorama" / "artifacts" / SOURCE_ARTIFACT_ID
    directory.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(directory / "panorama.png"), image)
    cv2.imwrite(str(directory / "coverage.png"), covered.astype(np.uint8) * 255)
    cv2.imwrite(str(directory / "thumbnail.jpg"), cv2.resize(image, (512, 256)))
    model = {
        "algorithm_version": "automatic_rotation_brown_v1", "status": "estimated_for_visual_panorama_only",
        "reference_frame": "estimated_presentation_right_down_forward_SO3_then_canonical_panorama_basis",
        "presentation": {"rotation_matrix": np.eye(3).tolist(), "gravity_verified": False},
        "captures": [{"id": "synthetic-saved-panorama", "rotation_matrix": np.eye(3).tolist()}],
    }
    (directory / "model.json").write_text(json.dumps(model))
    camera = get_camera_device(config["settings"]["extensions"]["com.toposync.cameras"], camera_id=CAMERA_ID)
    source = get_camera_source(camera, source_id="synthetic-wide", enabled_only=True)
    artifact = {
        "id": SOURCE_ARTIFACT_ID, "revision": 1, "camera_id": CAMERA_ID, "source_id": "synthetic-wide",
        "status": "partial", "quality": {"status": "ready", "reasons": []}, "width": width, "height": height,
        "algorithm_version": model["algorithm_version"], "positioning_status": "not_validated", "created_at": time.time(),
        "crop": source["metadata"]["panorama"]["active"]["crop"], "crop_revision": 1,
        "coverage_ratio": float(covered.mean()), "coverage": {"pixel_ratio": float(covered.mean())},
        **{f"{name}_url": f"/api/cameras/panorama-artifacts/{SOURCE_ARTIFACT_ID}/files/{file_id}" for name, file_id in (("image", "panorama"), ("coverage", "coverage"), ("thumbnail", "thumbnail"))},
        "_identity": _identity(camera, source), "_files": {"panorama": "panorama.png", "coverage": "coverage.png", "thumbnail": "thumbnail.jpg", "model": "model.json"},
    }
    (directory / "artifact.json").write_text(json.dumps(artifact))


class SyntheticCamera:
    def __init__(self) -> None:
        self.pose = {"pan": 0.0, "tilt": -0.6, "zoom": 0.0}
        self.lease = None
        self.epoch = 0
        self.commands: list[dict] = []

    def acquire(self, **_kwargs):
        if self.lease:
            raise HTTPException(409, "Synthetic camera already leased")
        self.lease = {"lease_id": "synthetic-lease", "fence": self.epoch + 1, "camera_id": CAMERA_ID}
        return dict(self.lease)

    def renew(self, **kwargs):
        assert self.lease and kwargs["lease_id"] == self.lease["lease_id"] and kwargs["fence"] == self.lease["fence"]
        return dict(self.lease)

    def submit(self, **kwargs):
        self.renew(**kwargs)
        command = kwargs["command"]
        assert command["kind"] == "absolute_move"
        self.pose = {axis: float(command[axis]) for axis in ("pan", "tilt", "zoom")}
        self.commands.append(dict(self.pose))
        self.epoch += 1
        return {"accepted": True, "stale_after_execution": False, "lease_id": kwargs["lease_id"], "fence": kwargs["fence"], "command_id": kwargs["command_id"]}

    def snapshot(self, **_kwargs):
        return {"active_lease": self.lease, "pose": dict(self.pose), "geometry_safe": True, "motion_epoch": self.epoch, "transport_binding_current": True}

    def release(self, **kwargs):
        self.renew(**kwargs)
        self.lease = None
        return {"released": True}

    def emergency_stop(self, **kwargs):
        assert self.lease and kwargs["expected_lease_id"] == self.lease["lease_id"] and kwargs["expected_fence"] == self.lease["fence"]
        return {"state_published": True, "ok": True}

    async def capture(self, _request, camera_id, **_kwargs):
        assert camera_id == CAMERA_ID
        pan, tilt = self.pose["pan"], self.pose["tilt"]
        forward = np.array([math.cos(tilt) * math.cos(pan), math.cos(tilt) * math.sin(pan), math.sin(tilt)])
        right = np.array([-math.sin(pan), math.cos(pan), 0])
        up = np.cross(forward, right)
        rows, columns = np.indices((400, 640))
        rays = forward + ((columns - 319.5) / 380)[..., None] * right - ((rows - 199.5) / 380)[..., None] * up
        downward = rays[..., 2] < -0.001
        distance = np.divide(-3, rays[..., 2], out=np.zeros((400, 640)), where=downward)
        floor_x, floor_z = rays[..., 0] * distance, rays[..., 1] * distance
        checker = ((np.floor(floor_x) + np.floor(floor_z)).astype(int) % 2).astype(bool)
        image = np.full((400, 640, 3), (48, 42, 38), np.uint8)
        image[downward & checker] = (95, 126, 112)
        image[downward & ~checker] = (112, 145, 132)
        for index, (x, z) in enumerate(TARGETS):
            direction = np.array([x, z, -3])
            depth = float(direction @ forward)
            if depth <= 0:
                continue
            u, v = round(319.5 + 380 * float(direction @ right) / depth), round(199.5 - 380 * float(direction @ up) / depth)
            if -20 < u < 660 and -20 < v < 420:
                cv2.circle(image, (u, v), 9, (60, 184, 255) if index < 6 else (230, 220, 70), -1)
                cv2.putText(image, str(index + 1), (u + 11, v - 7), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(image, "SYNTHETIC CAMERA - NO REAL DEVICE", (12, 26), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
        encoded, payload = cv2.imencode(".jpg", image)
        assert encoded
        return Response(payload.tobytes(), media_type="image/jpeg", headers={"X-Toposync-Snapshot-Capture-Evidence": "verified", "X-Toposync-Snapshot-Freshness": "verified", "X-Toposync-Snapshot-Captured-Timestamp": str(time.time())})


def make_app(data_dir: Path):
    # A sentinel prevents accidental reuse or deletion of an existing user directory.
    sentinel = data_dir / ".panorama-browser-fixture"
    if data_dir.exists() and any(data_dir.iterdir()) and not sentinel.is_file():
        raise RuntimeError("Choose an empty, dedicated fixture directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("Synthetic browser test data only.\n")
    config = fixture_config()
    (data_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False))
    write_saved_source_panorama(data_dir, config)
    os.environ["TOPOSYNC_DATA_DIR"] = str(data_dir.resolve())
    os.environ["TOPOSYNC_AUTH_MODE"] = "bypass"
    from toposync.app import create_app
    app = create_app()
    original_lifespan = app.router.lifespan_context
    camera = SyntheticCamera()

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            for name in ("acquire", "renew", "submit", "snapshot", "release", "emergency_stop"):
                application.state.services.register(f"cameras.control.{name}", getattr(camera, name))
            application.state.camera_panorama.capture_snapshot = camera.capture
            yield

    app.router.lifespan_context = lifespan

    @app.get("/api/__panorama_fixture")
    async def evidence():
        return {"synthetic": True, "camera_id": CAMERA_ID, "element_id": ELEMENT_ID, "source_id": "synthetic-wide", "source_artifact_id": SOURCE_ARTIFACT_ID, "source_artifact_revision": 1, "profile": PROFILE, "points": known_points(), "commands": camera.commands, "pose": camera.pose}

    @app.post("/api/__panorama_fixture/reset")
    async def reset():
        service = app.state.camera_panorama
        if service.tasks or service.busy:
            raise HTTPException(409, "Wait for synthetic operations to finish")
        for identifier in list(service.jobs):
            shutil.rmtree(service.root / identifier)
        service.jobs.clear()
        camera.__init__()
        from toposync.runtime.config_store import Composition
        await app.state.config_store.set_active_composition(Composition.model_validate(fixture_config()["compositions"][0]))
        return {"synthetic": True, "reset": True}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8127)
    parser.add_argument("--data-dir", type=Path, default=Path(".toposync-data/panorama-browser-fixture"))
    arguments = parser.parse_args()
    uvicorn.run(make_app(arguments.data_dir), host="127.0.0.1", port=arguments.port, log_level="warning")
