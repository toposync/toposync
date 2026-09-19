"""Deterministic simulated device evidence; these tests do not certify hardware."""

from __future__ import annotations

import asyncio
import copy
import math
import json
from pathlib import Path
import time
from typing import Any

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.testclient import TestClient
import numpy as np
import pytest

from toposync.runtime.config_store import (
    AppConfig,
    AppSettings,
    Composition,
    CompositionElement,
    ConfigStore,
    UserDataPaths,
)
from toposync.runtime.services import ServiceRegistry
import toposync_ext_cameras.panorama as panorama_module
from toposync_ext_cameras.panorama import register_panorama_routes


PROFILE = {
    "lens": {
        "width": 96,
        "height": 64,
        "fx": 48,
        "fy": 48,
        "cx": 47.5,
        "cy": 31.5,
        "distortion": [],
    },
    "pan_axis": {
        "position_min": -1,
        "position_max": 1,
        "angle_min_radians": -math.pi,
        "angle_max_radians": math.pi,
    },
    "tilt_axis": {
        "position_min": -1,
        "position_max": 1,
        "angle_min_radians": -math.pi / 2,
        "angle_max_radians": math.pi / 2,
    },
    "zoom": 0,
    "position_tolerance": 0.015,
    "settle_timeout_seconds": 1,
}
SCAN = {"pan_min": -0.4, "pan_max": 0.4, "tilt_min": -0.7, "tilt_max": -0.05, "overlap": 0.4}
BASE = "/api/cameras/cameras/simulated/panorama"


class FakePanoramaController:
    """Small fenced controller double with deliberate failure injection."""

    def __init__(self) -> None:
        self.lease: dict[str, Any] | None = None
        self.pose = {"pan": 0.0, "tilt": 0.0, "zoom": 0.0}
        self.commands: list[dict[str, Any]] = []
        self.fence = 0
        self.epoch = 0
        self.snapshot_calls = 0
        self.delay = 0.0
        self.stale = False
        self.geometry_safe = True
        self.frame_verified = True
        self.frame_drift = False
        self.steal_on_frame = False

    async def acquire(self, **kwargs: Any) -> dict[str, Any]:
        if self.lease:
            raise RuntimeError("Already owned")
        self.fence += 1
        self.lease = {
            "lease_id": f"simulated-{self.fence}",
            "fence": self.fence,
            "camera_id": kwargs["camera_id"],
        }
        return self.lease.copy()

    def _owns(self, lease_id: str, fence: int) -> None:
        if not self.lease or self.lease["lease_id"] != lease_id or self.lease["fence"] != fence:
            raise RuntimeError("Ownership changed")

    async def renew(self, *, lease_id: str, fence: int, **kwargs: Any) -> dict[str, Any]:
        self._owns(lease_id, fence)
        return self.lease.copy()  # type: ignore[union-attr]

    async def submit(
        self, *, lease_id: str, fence: int, command: dict[str, Any], command_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._owns(lease_id, fence)
        self.commands.append(command.copy())
        if command["kind"] == "absolute_move":
            self.pose.update({key: command[key] for key in ("pan", "tilt", "zoom")})
            self.epoch += 1
            await asyncio.sleep(self.delay)
        return {
            "accepted": True,
            "stale_after_execution": self.stale,
            "lease_id": lease_id,
            "fence": fence,
            "command_id": command_id,
        }

    async def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.snapshot_calls += 1
        return {
            "active_lease": self.lease.copy() if self.lease else None,
            "pose": self.pose.copy(),
            "geometry_safe": self.geometry_safe,
            "motion_epoch": self.epoch,
            "transport_binding_current": True,
        }

    async def release(self, *, lease_id: str, fence: int) -> dict[str, Any]:
        self._owns(lease_id, fence)
        self.lease = None
        return {"released": True}

    async def emergency_stop(
        self, *, expected_lease_id: str, expected_fence: int, **kwargs: Any
    ) -> dict[str, Any]:
        self._owns(expected_lease_id, expected_fence)
        self.commands.append({"kind": "emergency_stop"})
        self.lease = None
        return {"ok": True, "state_published": True}

    async def capture(self, request: Any, camera_id: str, **kwargs: Any) -> Response:
        assert kwargs == {"source_id": "main", "fresh": True, "freshness": "physical"}
        if self.frame_drift:
            self.pose["pan"] += 0.1
            self.epoch += 1
        if self.steal_on_frame:
            self.lease = {"lease_id": "replacement-owner", "fence": 100, "camera_id": camera_id}
        image = np.zeros((64, 96, 3), np.uint8)
        image[:, :, 0] = np.arange(96, dtype=np.uint8)
        image[:, :, 1] = 170
        image[:, :, 2] = np.arange(64, dtype=np.uint8)[:, None]
        _, encoded = cv2.imencode(".jpg", image)
        evidence = "verified" if self.frame_verified else "unverified"
        return Response(
            encoded.tobytes(),
            media_type="image/jpeg",
            headers={
                "X-Toposync-Snapshot-Capture-Evidence": evidence,
                "X-Toposync-Snapshot-Freshness": evidence,
                "X-Toposync-Snapshot-Captured-Timestamp": str(time.time()),
            },
        )


def make_panorama_app(tmp_path: Path) -> tuple[FastAPI, FakePanoramaController]:
    """Standalone authenticated routes, real ConfigStore and local fake hardware."""
    directory = tmp_path / "data"
    store = ConfigStore(
        paths=UserDataPaths(
            data_dir=directory, config_path=directory / "config.json", files_dir=directory / "files"
        )
    )
    settings = {
        "schema_version": 4,
        "devices": [
            {
                "id": "simulated",
                "name": "Simulated camera",
                "kind": "camera",
                "control": {"type": "onvif"},
                "sources": [
                    {
                        "id": "main",
                        "name": "Simulated image",
                        "kind": "video",
                        "enabled": True,
                        "is_default": True,
                        "origin": {
                            "type": "rtsp",
                            "rtsp_url": "rtsp://simulation.invalid/never-opened",
                        },
                        "metadata": {"panorama_profile": PROFILE},
                    }
                ],
            }
        ],
    }
    config = AppConfig(
        compositions=[
            Composition(id="ground", name="Ground", elements=[]),
            Composition(
                id="yard",
                name="Simulated yard",
                elements=[
                    CompositionElement(
                        id="camera-element",
                        type="com.toposync.cameras.camera",
                        props={"camera_id": "simulated"},
                    )
                ],
            ),
        ],
        active_composition_id="ground",
        settings=AppSettings(extensions={"com.toposync.cameras": settings}),
    )
    if not directory.joinpath("config.json").exists():
        asyncio.run(store.save_config(config))
    app = FastAPI()
    app.state.config_store = store
    services = ServiceRegistry()
    controller = FakePanoramaController()
    for name in ("acquire", "renew", "submit", "snapshot", "release", "emergency_stop"):
        services.register(f"cameras.control.{name}", getattr(controller, name))

    def authorize(request: Any, *, action: str, **kwargs: Any) -> None:
        if request.headers.get("x-deny") == "all" or request.headers.get("x-deny") == action:
            raise HTTPException(status_code=403, detail="Denied")

    async def read_settings(request: Any) -> dict[str, Any]:
        return (await store.get_settings()).extensions["com.toposync.cameras"]

    register_panorama_routes(
        app,
        services=services,
        authorize=authorize,
        read_settings=read_settings,
        capture_snapshot=controller.capture,
    )
    return app, controller


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = panorama_module.render_panorama
    monkeypatch.setattr(
        panorama_module,
        "render_panorama",
        lambda captures: original(captures, width=512, height=256),
    )
    app, controller = make_panorama_app(tmp_path)
    with TestClient(app) as client:
        yield client, controller, app.state.camera_panorama
        client.portal.call(app.state.camera_panorama.shutdown)


def create_job(client: TestClient, **changes: Any) -> dict[str, Any]:
    body = {
        "element_id": "camera-element",
        "source_id": "main",
        "profile": copy.deepcopy(PROFILE),
        "scan": copy.deepcopy(SCAN),
        **changes,
    }
    response = client.post(BASE, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def wait_capture(client: TestClient, job: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        latest = client.get(f"{BASE}/{job['id']}").json()
        if latest["state"] not in {"capturing", "processing"}:
            return latest
        time.sleep(0.02)
    pytest.fail("Simulated capture did not complete")


def ready_job(client: TestClient) -> dict[str, Any]:
    job = create_job(client)
    response = client.post(f"{BASE}/{job['id']}/capture", json={"revision": 1})
    assert response.status_code == 200, response.text
    job = wait_capture(client, job)
    assert job["state"] == "ready", job
    return job


def synthetic_points() -> list[dict[str, Any]]:
    # Independent oracle: ground (x,z), camera (0,0,3), optical world [x,z,-3].
    coordinates = [(2, -3), (8, -3), (8, 3), (2, 3), (4, -2), (6, 2), (4, 0), (6, 0)]
    points = []
    for index, (x, z) in enumerate(coordinates):
        pan = math.atan2(z, x)
        tilt = math.atan2(-3, math.hypot(x, z))
        points.append(
            {
                "id": str(index),
                "role": "fit" if index < 6 else "check",
                "panorama": {"x": (pan + math.pi) / (2 * math.pi), "y": 0.5 - tilt / math.pi},
                "world": {"x": x, "z": z},
            }
        )
    return points


def saved_source_panorama(client: TestClient, service: Any) -> tuple[dict[str, Any], Path]:
    """Preserved canonical fixture, independent of capture or camera capabilities."""
    from toposync_ext_cameras.source_panorama import _identity
    from toposync_ext_cameras.settings import get_camera_device, get_camera_source

    config = client.portal.call(service.store.get_config)
    camera = config.settings.extensions["com.toposync.cameras"]["devices"][0]
    source = camera["sources"][0]
    source["metadata"].pop("panorama_profile", None)
    camera["control"] = {"type": "none"}
    identifier = "a" * 32
    source["metadata"]["panorama"] = {
        "revision": 1,
        "active": {
            "artifact_id": identifier,
            "crop_revision": 2,
            "crop": {"u_start": 0.4, "u_width": 0.3, "v_start": 0.5, "v_height": 0.4},
        },
    }
    client.portal.call(service.store.save_config, config)
    camera = get_camera_device(
        config.settings.extensions["com.toposync.cameras"], camera_id="simulated"
    )
    source = get_camera_source(camera, source_id="main", enabled_only=True)
    directory = service.root.parent / "source-panorama" / "artifacts" / identifier
    directory.mkdir(parents=True)
    cv2.imwrite(str(directory / "panorama.png"), np.full((128, 256, 3), 130, np.uint8))
    coverage = np.full((128, 256), 255, np.uint8)
    coverage[:40] = 0
    cv2.imwrite(str(directory / "coverage.png"), coverage)
    model = {
        "algorithm_version": "automatic_rotation_brown_v1",
        "status": "estimated_for_visual_panorama_only",
        "reference_frame": "estimated_presentation_right_down_forward_SO3_then_canonical_panorama_basis",
        "presentation": {"rotation_matrix": np.eye(3).tolist(), "gravity_verified": False},
        "captures": [{"id": "saved-1", "rotation_matrix": np.eye(3).tolist()}],
    }
    (directory / "model.json").write_text(json.dumps(model))
    artifact = {
        "id": identifier,
        "revision": 1,
        "camera_id": camera["id"],
        "source_id": source["id"],
        "status": "partial",
        "quality": {"status": "ready", "reasons": []},
        "width": 256,
        "height": 128,
        "algorithm_version": model["algorithm_version"],
        "crop": {"u_start": 0, "u_width": 1, "v_start": 0, "v_height": 1},
        "image_url": f"/api/cameras/panorama-artifacts/{identifier}/files/panorama",
        "coverage_url": f"/api/cameras/panorama-artifacts/{identifier}/files/coverage",
        "_identity": _identity(camera, source),
        "_files": {"model": "model.json", "panorama": "panorama.png", "coverage": "coverage.png"},
    }
    (directory / "artifact.json").write_text(json.dumps(artifact))
    return {
        "element_id": "camera-element",
        "source_id": "main",
        "source_artifact_id": identifier,
        "source_artifact_revision": 1,
    }, directory


def update_source_panorama_pointer(
    client: TestClient, service: Any, pointer: dict[str, Any]
) -> None:
    config = client.portal.call(service.store.get_config)
    source = config.settings.extensions["com.toposync.cameras"]["devices"][0]["sources"][0]
    source["metadata"]["panorama"] = copy.deepcopy(pointer)
    client.portal.call(service.store.save_config, config)


def clone_source_panorama_artifact(directory: Path, identifier: str) -> Path:
    clone = directory.parent / identifier
    clone.mkdir()
    for filename in ("panorama.png", "coverage.png", "model.json"):
        (clone / filename).write_bytes((directory / filename).read_bytes())
    artifact = json.loads((directory / "artifact.json").read_text())
    artifact.update(id=identifier, quality_approved=True)
    (clone / "artifact.json").write_text(json.dumps(artifact))
    return clone


def test_legacy_active_source_panorama_with_explicit_clean_quality_is_compatible(
    environment,
) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    assert "quality_approved" not in artifact
    assert artifact["quality"] == {"status": "ready", "reasons": []}

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["sources"][0]["panorama"]["compatible"] is True
    response = client.post(BASE, json=body)

    assert response.status_code == 200, response.text
    assert response.json()["source_panorama"]["id"] == body["source_artifact_id"]
    assert controller.commands == [] and controller.snapshot_calls == 0


def test_mechanical_pan_axis_panorama_geometry_is_compatible(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    model = json.loads((directory / "model.json").read_text())
    artifact = json.loads((directory / "artifact.json").read_text())
    model["algorithm_version"] = artifact["algorithm_version"] = "automatic_rotation_brown_v2"
    model["presentation"].update(
        status="verified", method="verified_mechanical_pan_axis"
    )
    (directory / "model.json").write_text(json.dumps(model))
    (directory / "artifact.json").write_text(json.dumps(artifact))

    response = client.post(BASE, json=body)

    assert response.status_code == 200, response.text
    assert controller.commands == [] and controller.snapshot_calls == 0


@pytest.mark.parametrize(
    "quality",
    [
        {"status": "review", "reasons": []},
        {"status": "ready", "reasons": ["independent_alignment_error"]},
    ],
)
def test_legacy_active_source_panorama_still_requires_a_clean_quality_verdict(
    environment, quality: dict[str, Any]
) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    assert "quality_approved" not in artifact
    artifact["quality"] = quality
    (directory / "artifact.json").write_text(json.dumps(artifact))

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["sources"][0]["panorama"]["compatible"] is False
    response = client.post(BASE, json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_panorama_geometry_incompatible"
    assert not service.jobs and controller.commands == [] and controller.snapshot_calls == 0


def test_candidate_with_failed_quality_cannot_enter_mapping(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    artifact["quality"] = {
        "status": "ready",
        "reasons": ["independent_alignment_error"],
    }
    artifact["quality_approved"] = False
    (directory / "artifact.json").write_text(json.dumps(artifact))
    update_source_panorama_pointer(
        client,
        service,
        {
            "revision": 2,
            "candidate": {
                "artifact_id": body["source_artifact_id"],
                "crop_revision": 1,
            },
        },
    )

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["sources"][0]["panorama"] is None
    response = client.post(BASE, json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_panorama_unavailable"
    assert not service.jobs and controller.commands == [] and controller.snapshot_calls == 0


def test_clean_candidate_cannot_enter_mapping_before_promotion(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    artifact["quality_approved"] = True
    (directory / "artifact.json").write_text(json.dumps(artifact))
    update_source_panorama_pointer(
        client,
        service,
        {
            "revision": 2,
            "candidate": {
                "artifact_id": body["source_artifact_id"],
                "crop_revision": 1,
            },
        },
    )

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["sources"][0]["panorama"] is None
    response = client.post(BASE, json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_panorama_unavailable"
    assert not service.jobs and controller.commands == [] and controller.snapshot_calls == 0


def test_existing_mapping_keeps_its_bound_panorama_after_active_pointer_changes(environment) -> None:
    client, controller, service = environment
    body, _ = saved_source_panorama(client, service)
    job = client.post(BASE, json=body).json()
    response = client.put(
        f"{BASE}/{job['id']}/points",
        json={"revision": job["revision"], "points": synthetic_points()},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    response = client.post(f"{BASE}/{job['id']}/activate", json={"revision": job["revision"]})
    assert response.status_code == 200, response.text
    update_source_panorama_pointer(
        client,
        service,
        {
            "revision": 2,
            "candidate": {
                "artifact_id": body["source_artifact_id"],
                "crop_revision": 1,
            },
        },
    )

    before = len(controller.commands)
    response = client.post(
        BASE + "/aim",
        json={"element_id": "camera-element", "world": {"x": 4, "z": 0}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "physical_checks_required"
    assert len(controller.commands) == before


def test_current_v3_source_panorama_is_accepted_for_mapping(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    model = json.loads((directory / "model.json").read_text())
    artifact["algorithm_version"] = "automatic_rotation_brown_v3"
    model["algorithm_version"] = "automatic_rotation_brown_v3"
    (directory / "artifact.json").write_text(json.dumps(artifact))
    (directory / "model.json").write_text(json.dumps(model))

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["sources"][0]["panorama"]["compatible"] is True
    response = client.post(BASE, json=body)

    assert response.status_code == 200, response.text
    assert response.json()["source_panorama"]["id"] == body["source_artifact_id"]
    assert not controller.commands and controller.snapshot_calls == 0


def test_unapproved_active_never_falls_back_to_clean_candidate(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    active_artifact = json.loads((directory / "artifact.json").read_text())
    active_artifact["quality_approved"] = False
    (directory / "artifact.json").write_text(json.dumps(active_artifact))
    candidate_id = "b" * 32
    clone_source_panorama_artifact(directory, candidate_id)
    update_source_panorama_pointer(
        client,
        service,
        {
            "revision": 2,
            "active": {
                "artifact_id": body["source_artifact_id"],
                "crop_revision": 2,
            },
            "candidate": {"artifact_id": candidate_id, "crop_revision": 1},
        },
    )

    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    resource = preflight["sources"][0]["panorama"]
    assert resource == {
        "id": body["source_artifact_id"],
        "compatible": False,
        "blockers": ["source_panorama_geometry_incompatible"],
    }
    active_response = client.post(BASE, json=body)
    candidate_response = client.post(
        BASE,
        json={
            **body,
            "source_artifact_id": candidate_id,
        },
    )

    assert active_response.status_code == 409
    assert active_response.json()["detail"]["code"] == "source_panorama_geometry_incompatible"
    assert candidate_response.status_code == 409
    assert candidate_response.json()["detail"]["code"] == "source_panorama_unavailable"
    assert not service.jobs and controller.commands == [] and controller.snapshot_calls == 0


def test_assisted_draft_resume_and_review_preserve_active_mapping(environment) -> None:
    client, controller, service = environment
    body, _ = saved_source_panorama(client, service)
    body["reuse_draft"] = True
    first = client.post(BASE, json=body).json()
    response = client.put(f"{BASE}/{first['id']}/points", json={"revision": 1, "points": synthetic_points()})
    assert response.status_code == 200, response.text
    fitted = response.json()
    resumed = client.post(BASE, json=body).json()
    assert resumed["id"] == first["id"] and resumed["revision"] == fitted["revision"]
    assert resumed["points"] == fitted["points"] and len(service.jobs) == 1
    active = client.post(f"{BASE}/{first['id']}/activate", json={"revision": fitted["revision"]}).json()
    review_body = {**body, "review_job_id": active["id"], "review_revision": active["revision"]}
    response = client.post(BASE, json=review_body)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["id"] != active["id"] and not draft["active"]
    assert draft["points"] == active["points"] and draft["checks"] == []
    assert client.post(BASE, json=review_body).json()["id"] == draft["id"]
    assert client.get(f"{BASE}/{active['id']}").json()["active"]
    assert len(service.jobs) == 2
    stale = client.post(BASE, json={**review_body, "review_revision": active["revision"] + 1})
    assert stale.status_code == 409
    assert client.post(BASE, json={**review_body, "review_revision": None}).status_code == 422
    replacement = client.post(f"{BASE}/{draft['id']}/activate", json={"revision": draft["revision"]})
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["active"]
    assert not client.get(f"{BASE}/{active['id']}").json()["active"]
    # A late timestamp on history cannot hide the active result or become its draft.
    service.jobs[active["id"]]["updated_at"] = time.time() + 100
    context = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert context["job"]["id"] == draft["id"] and context["job"]["active"]
    assert context["previous"]["job_id"] == active["id"]
    assert client.post(BASE, json=review_body).status_code == 409
    restored = client.post(BASE + "/restore", json={"element_id": "camera-element", "expected_job_id": draft["id"], "expected_revision": draft["revision"]})
    assert restored.status_code == 200, restored.text
    context = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert context["job"]["id"] == active["id"] and context["job"]["active"]
    new_review = client.post(BASE, json=review_body).json()
    assert new_review["id"] not in {active["id"], draft["id"]}
    assert not new_review["active"]
    assert controller.commands == [] and controller.snapshot_calls == 0


def test_assisted_draft_does_not_reuse_changed_geometry(environment) -> None:
    client, _, service = environment
    body, _ = saved_source_panorama(client, service)
    body["reuse_draft"] = True
    first = client.post(BASE, json=body).json()
    service.jobs[first["id"]]["_composition_digest"] = "old-composition"
    second = client.post(BASE, json=body).json()
    assert second["id"] != first["id"]
    assert len(service.jobs) == 2


def test_saved_source_panorama_reuses_canonical_geometry_without_profile_or_capture(
    environment,
) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["profile"] is None and not preflight["blockers"]
    resource = preflight["sources"][0]["panorama"]
    assert resource["compatible"] and resource["crop_revision"] == 2
    response = client.post(BASE, json=body)
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["state"] == "ready" and job["profile"] is None and job["scan"] is None
    assert job["source_panorama"] == resource
    assert client.get(job["panorama_url"]).content == (directory / "panorama.png").read_bytes()
    assert client.get(job["panorama_url"], headers={"x-deny": "all"}).status_code == 403
    response = client.put(
        f"{BASE}/{job['id']}/points", json={"revision": 1, "points": synthetic_points()[:6]}
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["solution"]["preview"]["status"] == "provisional"
    assert job["solution"]["preview"]["context"]["source_artifact_id"] == body["source_artifact_id"]
    response = client.put(
        f"{BASE}/{job['id']}/points",
        json={"revision": job["revision"], "points": synthetic_points()},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["solution"]["quality"]["status"] == "ready"
    response = client.post(f"{BASE}/{job['id']}/check", json={"revision": job["revision"], "point_id": "6"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "visual_control_unavailable"
    assert not controller.commands and controller.snapshot_calls == 0
    assert not controller.lease and not job["active"]
    assert not service._validated(service.jobs[job["id"]])
    response = client.post(f"{BASE}/{job['id']}/activate", json={"revision": job["revision"]})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["active"] and job["permissions"]["map_validated"]
    assert not job["permissions"]["can_verify_aim"]
    assert not job["permissions"]["aim_enabled"]
    active = client.portal.call(lambda: service.get_active(camera_id=job["camera_id"], source_id=job["source_id"], composition_id=job["composition_id"]))
    assert active["applies"] and active.get("job", {}).get("id") == job["id"]
    response = client.post(f"{BASE}/{job['id']}/capture", json={"revision": job["revision"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_panorama_immutable"

    # A presentation edit must not rebase already observed canonical coordinates.
    config = client.portal.call(service.store.get_config)
    reference = config.settings.extensions["com.toposync.cameras"]["devices"][0]["sources"][0][
        "metadata"
    ]["panorama"]["active"]
    reference["crop_revision"] = 3
    reference["crop"]["u_start"] = 0.6
    client.portal.call(service.store.save_config, config)
    resumed = client.get(f"{BASE}/{job['id']}").json()
    assert resumed["solution"]["preview"]["eligible"]
    assert resumed["source_panorama"]["crop_revision"] == 2
    assert resumed["points"] == synthetic_points()

    artifact = json.loads((directory / "artifact.json").read_text())
    artifact["revision"] = 2
    (directory / "artifact.json").write_text(json.dumps(artifact))
    stale = client.get(f"{BASE}/{job['id']}").json()
    assert not stale["solution"]["preview"]["eligible"]
    assert "source_panorama_revision_conflict" in stale["solution"]["preview"]["reasons"]


@pytest.mark.parametrize(
    "problem", ["revision", "identity", "projection", "rotation", "quality", "mask", "path"]
)
def test_saved_source_panorama_rejects_incompatible_or_missing_evidence(
    environment, problem
) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    artifact = json.loads((directory / "artifact.json").read_text())
    model = json.loads((directory / "model.json").read_text())
    if problem == "revision":
        body["source_artifact_revision"] = 2
    elif problem == "identity":
        artifact["_identity"] = "old-source"
    elif problem == "projection":
        model["reference_frame"] = "arbitrary_visual_montage"
    elif problem == "rotation":
        model["presentation"]["rotation_matrix"][0][0] = 2
    elif problem == "quality":
        artifact["quality"]["status"] = "review"
    elif problem == "mask":
        cv2.imwrite(str(directory / "coverage.png"), np.zeros((128, 256), np.uint8))
    else:
        artifact["_files"]["model"] = "../model.json"
    (directory / "artifact.json").write_text(json.dumps(artifact))
    (directory / "model.json").write_text(json.dumps(model))
    response = client.post(BASE, json=body)
    assert response.status_code == 409, response.text
    assert not service.jobs and not controller.commands and controller.snapshot_calls == 0


def test_saved_source_panorama_binds_revision_coverage_and_source_identity(environment) -> None:
    client, controller, service = environment
    body, directory = saved_source_panorama(client, service)
    job = client.post(BASE, json=body).json()
    outside = synthetic_points()[:1]
    outside[0]["panorama"]["y"] = 0.1
    response = client.put(f"{BASE}/{job['id']}/points", json={"revision": 1, "points": outside})
    assert (
        response.status_code == 422 and response.json()["detail"]["code"] == "point_outside_capture"
    )
    response = client.put(
        f"{BASE}/{job['id']}/points", json={"revision": 1, "points": synthetic_points()[:6]}
    )
    job = response.json()
    config = client.portal.call(service.store.get_config)
    config.settings.extensions["com.toposync.cameras"]["devices"][0]["sources"][0]["video"] = {
        "width": 640,
        "height": 480,
    }
    client.portal.call(service.store.save_config, config)
    stale = client.get(f"{BASE}/{job['id']}").json()
    assert stale["solution"]["preview"]["eligible"] is False
    assert "camera_configuration_changed" in stale["solution"]["preview"]["reasons"]
    response = client.put(
        f"{BASE}/{job['id']}/points",
        json={"revision": job["revision"], "points": synthetic_points()},
    )
    assert response.status_code == 409
    assert not controller.commands and controller.snapshot_calls == 0


def fit_job(client: TestClient) -> dict[str, Any]:
    job = ready_job(client)
    response = client.put(
        f"{BASE}/{job['id']}/points", json={"revision": 1, "points": synthetic_points()}
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["solution"]["quality"]["status"] == "ready", job["solution"]
    return job


def confirm_checks(client: TestClient, job: dict[str, Any]) -> dict[str, Any]:
    for point_id in ("6", "7"):
        response = client.post(
            f"{BASE}/{job['id']}/check", json={"revision": job["revision"], "point_id": point_id}
        )
        assert response.status_code == 200, response.text
        job = response.json()
        check = job["checks"][-1]
        response = client.post(
            f"{BASE}/{job['id']}/check-result",
            json={
                "revision": job["revision"],
                "point_id": point_id,
                "check_id": check["id"],
                "result": "correct",
            },
        )
        assert response.status_code == 200, response.text
        job = response.json()
    return job


def test_capture_calibrate_validate_activate_and_aim_real_routes(environment) -> None:
    client, controller, service = environment
    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["profile"] == PROFILE and not preflight["blockers"]
    assert controller.snapshot_calls == 0 and controller.commands == []
    job = fit_job(client)
    assert job["revision"] == 2
    before = len(controller.commands)
    assert client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2}).status_code == 409
    assert len(controller.commands) == before
    job = confirm_checks(client, job)
    response = client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2})
    assert response.status_code == 200 and response.json()["active"] is True
    configuration = client.portal.call(service.store.get_config)
    assert configuration.active_composition_id == "ground"
    marker = configuration.compositions[1].elements[0].props["panorama_mapping"]
    assert marker == {"job_id": job["id"], "revision": 2, "source_id": "main", "status": "ready"}
    assert not (service.root / "active.json").exists()
    active = client.portal.call(
        lambda: service.get_active(camera_id="simulated", source_id="main", composition_id="yard")
    )
    assert active["applies"] is True and active["job"]["id"] == job["id"]
    checked_target = next(
        command for command in reversed(controller.commands) if command["kind"] == "absolute_move"
    )
    aim = client.post(
        BASE + "/aim", json={"element_id": "camera-element", "world": {"x": 6, "z": 0}}
    )
    assert aim.status_code == 200, aim.text
    aimed_target = next(
        command for command in reversed(controller.commands) if command["kind"] == "absolute_move"
    )
    assert aimed_target == checked_target
    assert client.get(aim.json()["aim_image_url"]).status_code == 200
    assert (
        client.put(f"{BASE}/{job['id']}/points", json={"revision": 2, "points": []}).status_code
        == 409
    )


def test_points_are_persisted_revisioned_and_do_not_move_camera(environment) -> None:
    client, controller, _ = environment
    job = ready_job(client)
    before = len(controller.commands)
    url = f"{BASE}/{job['id']}/points"
    response = client.put(url, json={"revision": 1, "points": synthetic_points()[:1]})
    assert response.status_code == 200
    assert response.json()["solution"]["quality"]["status"] == "incomplete"
    assert response.json()["revision"] == 2
    assert (
        client.put(url, json={"revision": 1, "points": []}).json()["detail"]["code"]
        == "revision_conflict"
    )
    assert len(controller.commands) == before
    assert client.get(f"{BASE}/{job['id']}").json()["points"] == synthetic_points()[:1]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("stale", "movement_unconfirmed"),
        ("frame_verified", "freshness_unverified"),
        ("frame_drift", "camera_moved_during_capture"),
        ("steal_on_frame", "control_lost"),
    ],
)
def test_capture_rejects_unverified_or_moving_frames_and_preserves_new_owner(
    environment, failure: str, code: str
) -> None:
    client, controller, _ = environment
    setattr(controller, failure, failure != "frame_verified")
    job = create_job(client)
    assert client.post(f"{BASE}/{job['id']}/capture", json={"revision": 1}).status_code == 200
    failed = wait_capture(client, job)
    assert failed["state"] == "failed" and failed["error"]["code"] == code
    assert failed["panorama_url"] is None
    if failure == "steal_on_frame":
        assert controller.lease["lease_id"] == "replacement-owner"
        assert all(command["kind"] == "absolute_move" for command in controller.commands)


def test_cancel_stops_only_owned_camera_and_prevents_remaining_scan(environment) -> None:
    client, controller, _ = environment
    controller.delay = 0.1
    job = create_job(client)
    client.post(f"{BASE}/{job['id']}/capture", json={"revision": 1})
    deadline = time.monotonic() + 2
    while not controller.commands and time.monotonic() < deadline:
        time.sleep(0.01)
    response = client.post(f"{BASE}/{job['id']}/cancel", json={"revision": 1})
    assert response.status_code == 200
    assert wait_capture(client, job)["state"] == "cancelled"
    assert (
        len([command for command in controller.commands if command["kind"] == "absolute_move"]) == 1
    )
    assert controller.commands[-1]["kind"] == "stop" and controller.lease is None


def test_scope_auth_limits_and_empty_panorama_are_enforced(environment) -> None:
    client, controller, _ = environment
    assert (
        client.get(
            BASE, params={"element_id": "camera-element"}, headers={"x-deny": "all"}
        ).status_code
        == 403
    )
    assert client.get(BASE, params={"element_id": "missing"}).status_code == 422
    body = {
        "element_id": "camera-element",
        "source_id": "main",
        "profile": copy.deepcopy(PROFILE),
        "scan": SCAN,
    }
    body["profile"]["lens"]["fx"] = 100000
    assert client.post(BASE, json=body).status_code == 422
    assert controller.commands == []
    job = ready_job(client)
    assert client.get(job["panorama_url"], headers={"x-deny": "all"}).status_code == 403
    assert client.get(f"/api/cameras/cameras/other/panorama/{job['id']}").status_code == 404
    assert client.get(f"{BASE}/{job['id']}/images/job.json").status_code == 404
    point = synthetic_points()[0]
    point["panorama"] = {"x": 0.5, "y": 0.0}
    response = client.put(f"{BASE}/{job['id']}/points", json={"revision": 1, "points": [point]})
    assert (
        response.status_code == 422 and response.json()["detail"]["code"] == "point_outside_capture"
    )


def test_check_receipts_reject_old_attempts_and_unverifiable_is_not_success(environment) -> None:
    client, _, _ = environment
    job = fit_job(client)
    path = f"{BASE}/{job['id']}"
    old = client.post(path + "/check", json={"revision": 2, "point_id": "6"}).json()["checks"][0]
    latest = client.post(path + "/check", json={"revision": 2, "point_id": "6"}).json()["checks"][0]
    assert old["id"] != latest["id"]
    assert (
        client.post(
            path + "/check-result",
            json={"revision": 2, "point_id": "6", "check_id": old["id"], "result": "correct"},
        ).status_code
        == 409
    )
    response = client.post(
        path + "/check-result",
        json={"revision": 2, "point_id": "6", "check_id": latest["id"], "result": "unverifiable"},
    )
    assert response.status_code == 200 and response.json()["checks"][0]["result"] == "unverifiable"
    assert client.post(path + "/activate", json={"revision": 2}).status_code == 409


def test_restart_interrupts_without_motion_and_recovers_saved_draft(
    environment, tmp_path: Path
) -> None:
    client, _, service = environment
    job = create_job(client)
    service.jobs[job["id"]]["state"] = "capturing"
    service._save(service.jobs[job["id"]])
    restarted, controller = make_panorama_app(tmp_path)
    with TestClient(restarted) as restarted_client:
        state = restarted_client.get(f"{BASE}/{job['id']}").json()
        assert state["state"] == "interrupted" and state["error"]["code"] == "process_interrupted"
        assert state["revision"] == 1 and controller.commands == []


def test_active_mapping_invalidates_geometry_but_preserves_cosmetic_edits(environment) -> None:
    client, controller, service = environment
    job = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2}).status_code == 200

    async def edit(*, geometry: bool) -> None:
        config = await service.store.get_config()
        config = config.model_copy(deep=True)
        config.compositions[1].name = "Renamed yard"
        config.compositions[1].elements[0].props["color"] = "red"
        if geometry:
            config.compositions[1].elements[0].position.x = 10
        await service.store.save_config(config)

    client.portal.call(lambda: edit(geometry=False))
    assert (
        client.portal.call(
            lambda: service.get_active(
                camera_id="simulated", source_id="main", composition_id="yard"
            )
        )["job"]["id"]
        == job["id"]
    )
    client.portal.call(lambda: edit(geometry=True))
    active = client.portal.call(
        lambda: service.get_active(camera_id="simulated", source_id="main", composition_id="yard")
    )
    assert active == {"applies": True, "reason": "composition_changed"}
    before = len(controller.commands)
    response = client.post(
        BASE + "/aim", json={"element_id": "camera-element", "world": {"x": 4, "z": 0}}
    )
    assert response.status_code == 409 and len(controller.commands) == before


def test_active_reference_conflict_and_restore_are_atomic_without_movement(environment) -> None:
    client, controller, service = environment
    first = confirm_checks(client, fit_job(client))
    stale = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{first['id']}/activate", json={"revision": 2}).status_code == 200
    conflict = client.post(f"{BASE}/{stale['id']}/activate", json={"revision": 2})
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "activation_conflict"
    )
    second = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{second['id']}/activate", json={"revision": 2}).status_code == 200
    preflight = client.get(BASE, params={"element_id": "camera-element"}).json()
    assert preflight["previous"]["job_id"] == first["id"]
    before = len(controller.commands)
    restored = client.post(
        BASE + "/restore",
        json={
            "element_id": "camera-element",
            "expected_job_id": second["id"],
            "expected_revision": 2,
        },
    )
    assert (
        restored.status_code == 200
        and restored.json()["id"] == first["id"]
        and restored.json()["active"]
    )
    assert len(controller.commands) == before
    assert (
        client.post(
            BASE + "/restore",
            json={
                "element_id": "camera-element",
                "expected_job_id": second["id"],
                "expected_revision": 2,
            },
        ).status_code
        == 409
    )
    config = client.portal.call(service.store.get_config)
    assert config.active_composition_id == "ground"


def test_additional_failed_or_pending_check_prevents_activation(environment) -> None:
    client, _, _ = environment
    job = ready_job(client)
    points = synthetic_points()
    extra = {
        "id": "8",
        "role": "check",
        "world": {"x": 5, "z": 1},
        "panorama": {
            "x": (math.atan2(1, 5) + math.pi) / (2 * math.pi),
            "y": 0.5 - math.atan2(-3, math.hypot(5, 1)) / math.pi,
        },
    }
    response = client.put(
        f"{BASE}/{job['id']}/points", json={"revision": 1, "points": [*points, extra]}
    )
    assert response.status_code == 200
    job = confirm_checks(client, response.json())
    assert client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2}).status_code == 409
    check = client.post(f"{BASE}/{job['id']}/check", json={"revision": 2, "point_id": "8"}).json()[
        "checks"
    ][-1]
    assert (
        client.post(
            f"{BASE}/{job['id']}/check-result",
            json={"revision": 2, "point_id": "8", "check_id": check["id"], "result": "offset"},
        ).status_code
        == 200
    )
    assert client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2}).status_code == 409


def test_capture_memory_budget_receipt_identity_and_mask_values(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, controller, service = environment
    profile = copy.deepcopy(PROFILE)
    profile["lens"].update(width=4096, height=2160, fx=2048, fy=2048, cx=2047.5, cy=1079.5)
    response = client.post(
        BASE,
        json={
            "element_id": "camera-element",
            "source_id": "main",
            "profile": profile,
            "scan": SCAN,
        },
    )
    assert response.status_code == 422 and response.json()["detail"]["code"] == "scan_memory_limit"
    job = ready_job(client)
    mask = cv2.imread(str(service.root / job["id"] / "coverage.png"), cv2.IMREAD_GRAYSCALE)
    assert set(np.unique(mask)) == {0, 255}
    original = controller.submit

    async def wrong_receipt(**kwargs: Any) -> dict[str, Any]:
        receipt = await original(**kwargs)
        receipt["command_id"] = "another-command"
        return receipt

    service.services.register("cameras.control.submit", wrong_receipt)
    job = create_job(client)
    client.post(f"{BASE}/{job['id']}/capture", json={"revision": 1})
    result = wait_capture(client, job)
    assert result["error"]["code"] == "movement_unconfirmed"
    assert result["stop_confirmed"] is True


def test_target_wrap_selects_equivalent_inside_scan_and_grid_respects_off_center_lens(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, service = environment
    job = {
        "state": "ready",
        "solution": {"quality": {"status": "ready"}},
        "profile": PROFILE,
        "scan": {"pan_min": 0.8, "pan_max": 1.0, "tilt_min": -0.7, "tilt_max": -0.05},
    }
    monkeypatch.setattr(panorama_module, "map_world_to_ray", lambda *args: (-1, 0, -1))
    target = service._target(job, {"x": 0, "z": 0})
    assert target["pan"] == pytest.approx(1.0)
    centered = panorama_module._grid(PROFILE, SCAN)
    off_center = copy.deepcopy(PROFILE)
    off_center["lens"]["cx"] = 0
    assert len(panorama_module._grid(off_center, SCAN)) > len(centered)


def test_discard_only_unreferenced_draft_preserves_active_and_previous(environment) -> None:
    client, controller, service = environment
    draft = ready_job(client)
    before = len(controller.commands)
    assert client.delete(f"{BASE}/{draft['id']}?revision=2").status_code == 409
    response = client.delete(f"{BASE}/{draft['id']}?revision=1")
    assert response.status_code == 200 and response.json()["deleted"]
    assert not (service.root / draft["id"]).exists()
    assert client.get(f"{BASE}/{draft['id']}").status_code == 404
    assert len(controller.commands) == before
    first = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{first['id']}/activate", json={"revision": 2}).status_code == 200
    second = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{second['id']}/activate", json={"revision": 2}).status_code == 200
    for job in (first, second):
        protected = client.delete(f"{BASE}/{job['id']}?revision=2")
        assert (
            protected.status_code == 409 and protected.json()["detail"]["code"] == "mapping_in_use"
        )


def test_stop_without_confirmation_never_returns_success(environment) -> None:
    client, controller, service = environment
    original = controller.submit

    async def stop_failure(**kwargs: Any) -> dict[str, Any]:
        if kwargs["command"]["kind"] == "stop":
            raise RuntimeError("Simulation: stop unavailable")
        return await original(**kwargs)

    async def emergency_failure(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Simulation: emergency stop unavailable")

    service.services.register("cameras.control.submit", stop_failure)
    service.services.register("cameras.control.emergency_stop", emergency_failure)
    job = create_job(client)
    client.post(f"{BASE}/{job['id']}/capture", json={"revision": 1})
    result = wait_capture(client, job)
    assert result["state"] == "failed" and result["error"]["code"] == "stop_unconfirmed"
    assert result["stop_confirmed"] is False


def test_active_lookup_loads_config_once_and_rechecks_mutable_geometry(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, service = environment
    job = confirm_checks(client, fit_job(client))
    assert client.post(f"{BASE}/{job['id']}/activate", json={"revision": 2}).status_code == 200
    original = service.store.get_config
    loads = 0

    async def counted_config() -> AppConfig:
        nonlocal loads
        loads += 1
        return await original()

    monkeypatch.setattr(service.store, "get_config", counted_config)
    for source_id in ("main", ""):
        loads = 0
        active = client.portal.call(
            lambda: service.get_active(
                camera_id="simulated", source_id=source_id, composition_id="yard"
            )
        )
        assert active["job"]["id"] == job["id"] and active["job"]["active"]
        assert loads == 1

    # ConfigStore returns a mutable object; identity cannot be an invalidation key.
    config = client.portal.call(original)
    config.compositions[1].elements[0].position.x += 1
    loads = 0
    active = client.portal.call(
        lambda: service.get_active(camera_id="simulated", source_id="main", composition_id="yard")
    )
    assert active == {"applies": True, "reason": "composition_changed"}
    assert loads == 1


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('save_fails', [False, True])
def test_visual_operation_discovery_failure_cleans_ownership_and_never_reports_complete(environment, cancelled, save_fails):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    client, controller, service = environment
    body, _ = saved_source_panorama(client, service)
    public = client.post(BASE, json=body).json()
    job = service.jobs[public['id']]
    camera = SimpleNamespace(
        discover=AsyncMock(side_effect=asyncio.CancelledError() if cancelled else RuntimeError('private transport details')),
        close=AsyncMock(),
    )
    service.camera_factory = lambda **_: camera
    if save_fails:
        from unittest.mock import Mock
        service._save = Mock(side_effect=OSError('disk unavailable'))

    async def operation():
        expected = OSError if save_fails else asyncio.CancelledError if cancelled else HTTPException
        with pytest.raises(expected):
            await service._visual_operation(None, job, {'ray': [0, 0, 1]})

    client.portal.call(operation)
    assert job['navigation']['phase'] == 'failed'
    assert job['navigation']['error_code'] == ('capture_cancelled' if cancelled else 'capture_failed')
    assert not service.busy and not service._navigation_scanners
    camera.close.assert_awaited_once()
    assert controller.commands == []
