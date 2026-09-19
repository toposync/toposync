"""Source panorama contracts using a fake scanner: never access physical cameras."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from toposync.runtime.config_store import AppConfig, AppSettings, ConfigStore, UserDataPaths
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.source_panorama import (
    _identity,
    _replacement_candidate_reason,
    register_source_panorama_routes,
)

BASE = "/api/cameras/cameras/simulated/sources/main/panorama"
JOBS = "/api/cameras/panorama-jobs"


def test_operational_automation_qualification_does_not_stale_visual_artifact():
    camera = {
        "id": "camera",
        "enabled": True,
        "control": {"type": "onvif", "automation_exclusive_control_confirmed": False},
        "onvif": {"xaddr": "http://camera/onvif/device_service"},
        "sources": [],
        "metadata": {"panorama_mount_revision": 3},
    }
    source = {
        "id": "main",
        "enabled": True,
        "origin": {"type": "onvif", "profile_token": "profile"},
        "video": {"width": 1920, "height": 1080},
    }
    before = _identity(camera, source)
    camera["control"]["automation_exclusive_control_confirmed"] = True
    assert _identity(camera, source) == before
    source["origin"]["profile_token"] = "replacement"
    assert _identity(camera, source) != before


class FakeReturnReconciler:
    def __init__(self, scanner: FakeScanner) -> None:
        self.scanner = scanner

    async def remove_managed_returns(self) -> int:
        from toposync_ext_cameras.panorama_capture import _MANAGED_RETURN_PRESET_NAME

        self.scanner.managed_cleanup_calls += 1
        managed = [
            item
            for item in self.scanner.managed_presets
            if _MANAGED_RETURN_PRESET_NAME.fullmatch(str(item.get("name") or ""))
        ]
        self.scanner.managed_presets = [
            item for item in self.scanner.managed_presets if item not in managed
        ]
        return len(managed)

    async def close(self) -> None:
        return None


class FakeScanner:
    def __init__(self) -> None:
        self.calls = 0
        self.moves = 0
        self.stops = 0
        self.returns = 0
        self.closed = 0
        self.delay = 0.0
        self.complete = True
        self.block = False
        self.fail = False
        self.stop_fail = False
        self.processing_block = False
        self.before_publish: Any = None
        self.settings: dict[str, Any] | None = None
        self.capture_count = 2
        self.reconstruction_quality: Any = {
            "status": "ready",
            "reasons": [],
            "validation": "synthetic",
        }
        self.reconstruction_coverage: dict[str, Any] = {
            "pixel_ratio": 0.8,
            "solid_angle_ratio": 0.8,
            "provenance": "synthetic_coverage_v1",
        }
        self.acquisition_coverage: dict[str, Any] = {
            "pan_limits": "confirmed",
            "tilt_limits": "confirmed",
            "progress": {"primary_complete": True, "bands_completed": 1},
        }
        self.algorithm_version = "fake-1"
        self.width: Any = 64
        self.height: Any = 32
        self.include_reconstruction_quality = True
        self.cleanup_events: list[Any] = []
        self.cleanup_failures: set[str] = set()
        self.managed_cleanup_calls = 0
        self.managed_presets: list[dict[str, str]] = []

    def camera_factory(self, **kwargs: Any) -> FakeScanner | FakeReturnReconciler:
        self.settings = kwargs["settings"]
        if not (Path(kwargs["output_dir"]) / "job.json").is_file():
            return FakeReturnReconciler(self)
        return self

    async def close(self) -> None:
        self.closed += 1

    async def discover(self) -> dict[str, Any]:
        self.cleanup_events.append("discover")
        return {}

    async def acquire(self) -> None:
        raise AssertionError("Preset cleanup must never acquire movement control")

    async def remove_return(self, destination: dict[str, Any]) -> None:
        from toposync_ext_cameras.panorama_capture import PanoramaCaptureError

        token = destination.get("preset_token", destination.get("preset_name"))
        self.cleanup_events.append(("remove", token))
        if token in self.cleanup_failures:
            raise PanoramaCaptureError("return_cleanup_unconfirmed")

    async def stop(self) -> dict[str, Any]:
        self.stops += 1
        if self.stop_fail:
            raise RuntimeError("rtsp://private-user:private-password@private-host")
        return {"stopped": True}

    async def scan(
        self,
        camera: Any,
        output: Path,
        *,
        checkpoint: Any,
        progress: Any,
        cancelled: Any,
        return_only: bool,
        acquisition_policy: Any = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if return_only:
            self.returns += 1
            return {
                "captures": checkpoint["captures"],
                "checkpoint": checkpoint,
                "physical_state": "restored",
                "issues": [],
            }
        self.moves += 1
        captures = []
        for index in range(self.capture_count):
            image = output / f"capture-{index:03}.jpg"
            Image.new("RGB", (32, 16), (30, 120, 200)).save(image)
            captures.append(
                {
                    "id": f"capture-{index:03}",
                    "path": str(image),
                    "pose": {"pan": None, "tilt": None, "zoom": None},
                }
            )
        checkpoint = {
            "captures": captures,
            "return": {"pan": None, "native_pan": 1200},
            "initial_path": captures[0]["path"],
        }
        await progress(
            {
                "status": "capturing",
                "phase": "waiting_for_stability",
                "captures": captures,
                "checkpoint": checkpoint,
                "preview_path": str(image),
                "password": "never-published",
                "native_pan": 1200,
            }
        )
        while self.block and not cancelled():
            await asyncio.sleep(0.01)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("rtsp://private-user:private-password@private-host")
        if not cancelled():
            self.returns += 1
        return {
            "captures": captures,
            "complete": self.complete,
            "coverage": copy.deepcopy(self.acquisition_coverage),
            "checkpoint": checkpoint,
            "issues": [],
            "physical_state": "stop_unconfirmed"
            if self.stop_fail
            else "stopped"
            if cancelled()
            else "restored",
        }

    async def reconstruct(
        self, captures: list[dict[str, Any]], output: Path, *, progress: Any, cancelled: Any
    ) -> dict[str, Any]:
        while self.processing_block and not cancelled():
            await asyncio.sleep(0.01)
        files = {
            "panorama": "panorama.png",
            "coverage": "coverage.png",
            "thumbnail": "thumbnail.jpg",
            "source_indices": "source-indices.npy",
            "model": "model.json",
            "report": "report.json",
        }
        for name in ("panorama.png", "coverage.png", "thumbnail.jpg"):
            Image.new("RGB", (64, 32), (200, 150, 20)).save(output / name)
        for name in ("source-indices.npy", "model.json", "report.json"):
            (output / name).write_text("{}")
        if self.before_publish:
            await self.before_publish()
        result = {
            "status": "ready",
            "algorithm_version": self.algorithm_version,
            "width": self.width,
            "height": self.height,
            "coverage": copy.deepcopy(self.reconstruction_coverage),
            "model": {
                "presentation": {
                    "status": "verified",
                    "method": "synthetic_horizontal_axis",
                }
            },
            "files": files,
            "positioning_status": "not_validated",
        }
        if self.include_reconstruction_quality:
            result["quality"] = copy.deepcopy(self.reconstruction_quality)
        return result


def make_app(tmp_path: Path, fake: FakeScanner | None = None, **options: Any):
    fake = fake or FakeScanner()
    directory = tmp_path / "data"
    store = ConfigStore(
        paths=UserDataPaths(
            data_dir=directory, config_path=directory / "config.json", files_dir=directory / "files"
        )
    )
    original = {
        "schema_version": 4,
        "devices": [
            {
                "id": "simulated",
                "name": "Simulated",
                "control": {"type": "onvif"},
                "sources": [
                    {
                        "id": "main",
                        "name": "Main",
                        "kind": "video",
                        "enabled": True,
                        "is_default": True,
                        "origin": {
                            "type": "rtsp",
                            "rtsp_url": "rtsp://private:secret@fake.invalid/no-network",
                        },
                        "metadata": {
                            "panorama_profile": {"legacy": "preserved"},
                            "other": "preserved",
                        },
                    }
                ],
            }
        ],
    }
    if not store.paths.config_path.exists():
        asyncio.run(
            store.save_config(
                AppConfig(settings=AppSettings(extensions={"com.toposync.cameras": original}))
            )
        )
    app = FastAPI()
    app.state.config_store = store
    requirements = []

    def authorize(request: Any, *, action: str, **kwargs: Any) -> None:
        requirements.append((action, kwargs))
        if request.headers.get("x-deny") in {"all", action}:
            raise HTTPException(status_code=403, detail="Denied")

    async def read_settings(request: Any) -> dict[str, Any]:
        return (await store.get_settings()).extensions["com.toposync.cameras"]

    service = register_source_panorama_routes(
        app,
        services=ServiceRegistry(),
        authorize=authorize,
        read_settings=read_settings,
        camera_factory=fake.camera_factory,
        scan_runner=fake.scan,
        reconstruct_runner=fake.reconstruct,
        # These fixtures describe legacy, full-reach jobs. Region contracts
        # opt into the new policy explicitly in their dedicated cases below.
        acquisition_policy=options.pop("acquisition_policy", None),
        max_job_bytes=1024 * 1024,
        max_global_bytes=10 * 1024 * 1024,
        minimum_free_bytes=0,
        **options,
    )
    return app, fake, service, requirements


@pytest.fixture
def environment(tmp_path: Path):
    app, fake, service, requirements = make_app(tmp_path)
    with TestClient(app) as client:
        yield client, fake, service, requirements
        client.portal.call(service.shutdown)


def create(client: TestClient, key: str = "repeatable-one", base: str = BASE) -> dict[str, Any]:
    response = client.post(base + "/jobs", json={"idempotency_key": key})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def test_default_capture_uses_reachable_domain_and_reports_presentation(environment):
    client, _, _, _ = environment

    job = wait_done(client, create(client, "reachable-domain-default"))
    artifact = client.get(BASE).json()["active"]

    assert job["capture_goal"] == "reachable_domain"
    assert artifact["capture_goal"] == "reachable_domain"
    assert artifact["presentation"] == {
        "status": "verified", "method": "synthetic_horizontal_axis"
    }


def wait_done(client: TestClient, job: dict[str, Any], timeout: float = 3) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = client.get(f"{JOBS}/{job['id']}").json()["job"]
        if latest["status"] not in {
            "queued",
            "preparing",
            "exploring",
            "capturing",
            "returning",
            "processing",
            "stopping",
        }:
            return latest
        time.sleep(0.01)
    pytest.fail("Fake panorama did not finish within the test budget")


def wait_phase(client: TestClient, job: dict[str, Any], phase: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        latest = client.get(f"{JOBS}/{job['id']}").json()["job"]
        if latest["phase"] == phase:
            return latest
        time.sleep(0.01)
    pytest.fail(f"Fake panorama did not reach {phase}")


def set_promotion_evidence(
    fake: FakeScanner,
    *,
    pixel_ratio: float,
    solid_angle_ratio: float | None,
    primary_complete: bool = False,
    bands_completed: int = 0,
    provenance: str | None = "synthetic_coverage_v1",
) -> None:
    fake.reconstruction_coverage = {"pixel_ratio": pixel_ratio}
    if solid_angle_ratio is not None:
        fake.reconstruction_coverage["solid_angle_ratio"] = solid_angle_ratio
    if provenance is not None:
        fake.reconstruction_coverage["provenance"] = provenance
    fake.acquisition_coverage = {
        "progress": {
            "primary_complete": primary_complete,
            "bands_completed": bands_completed,
        }
    }


@pytest.mark.parametrize("omit_last,quality_reason", [(False, None), (True, None), (False, "independent_alignment_error")])
def test_initial_region_requires_all_six_views_and_persists_crop_without_moving(tmp_path, omit_last, quality_reason):
    import hashlib
    from toposync_ext_cameras.panorama_region import LEGACY_REGION_POLICY as REGION_POLICY, REGION_VIEWS, region_progress

    class RegionScanner(FakeScanner):
        async def scan(self, camera, output, **arguments):
            assert arguments["acquisition_policy"] == REGION_POLICY
            result = await super().scan(camera, output, **arguments)
            captures = result["captures"]
            for index, photo in enumerate(captures):
                photo.update(capture_schema_version=2, capture_instance="synthetic-stream",
                             generation=1, sequence=index, quality={"stable": True},
                             sha256=hashlib.sha256(Path(photo["path"]).read_bytes()).hexdigest())
            checkpoint = result["checkpoint"]
            region = {"version": 1, "status": "captured", "views": [
                {"row": row, "column": column, "capture_id": photo["id"]}
                for (row, column), photo in zip(REGION_VIEWS, captures)
            ]}
            checkpoint.update(acquisition_policy=dict(REGION_POLICY), continuous_cursor={
                "version": 4, "stage": "done", "row": -1, "region": region,
            })
            progress = region_progress(checkpoint)
            result.update(complete=False, coverage={"region": region, "progress": progress})
            await arguments["progress"]({"captures": captures, "checkpoint": checkpoint, "coverage_progress": progress})
            return result

        async def reconstruct(self, captures, output, **arguments):
            result = await super().reconstruct(captures, output, **arguments)
            result["source_ids"] = [photo["id"] for photo in (captures[:-1] if omit_last else captures)]
            return result

    fake = RegionScanner()
    fake.capture_count = 6
    if quality_reason:
        fake.reconstruction_quality = {"status": "review", "reasons": [quality_reason]}
    app, _, service, _ = make_app(tmp_path, fake, acquisition_policy=REGION_POLICY)
    with TestClient(app) as client:
        try:
            job = wait_done(client, create(client, "initial-region"))
            assert job["capture_goal"] == "initial_region" and job["captures_accepted"] == 6
            assert job["coverage_progress"]["qualified_views"] == 6
            assert job["status"] == "partial" and not job["can_resume"]
            state = client.get(BASE).json()
            artifact = state["active"] or state["candidate"]
            assert artifact["capture_goal"] == "initial_region"
            assert artifact["region_status"] == ("incomplete" if omit_last else "review" if quality_reason else "ready")
            assert artifact["coverage"]["acquisition_complete"] is False
            assert artifact["quality_approved"] is (not omit_last and not quality_reason)
            report = client.get(f"/api/cameras/panorama-artifacts/{artifact['id']}/files/report").json()
            assert report["region_status"] == artifact["region_status"]
            if omit_last:
                assert "required_region_missing" in report["quality"]["reasons"]
            if quality_reason:
                assert quality_reason in report["quality"]["reasons"]
            image_before = client.get(artifact["image_url"]).content
            moves_before = fake.moves
            crop = {"u_start": 0.1, "u_width": 0.6, "v_start": 0.2, "v_height": 0.5}
            saved = client.patch(BASE + "/crop", json={"artifact_id": artifact["id"], "expected_revision": 1, "crop": crop})
            assert saved.status_code == 200
            reloaded = client.get(BASE).json()
            assert (reloaded["active"] or reloaded["candidate"])["crop"] == crop
            assert fake.moves == moves_before and client.get(artifact["image_url"]).content == image_before
        finally:
            client.portal.call(service.shutdown)


def test_zero_geometry_complete_source_artifact_and_read_only_status(environment):
    client, fake, service, requirements = environment
    initial = client.get(BASE)
    assert initial.status_code == 200
    assert initial.json()["active"] is None and fake.moves == 0
    job = wait_done(client, create(client))
    assert job["status"] == "ready" and job["physical_state"] == "restored"
    assert fake.returns == 1 and fake.closed == 1
    artifact = client.get(BASE).json()["active"]
    assert artifact["positioning_status"] == "not_validated"
    assert artifact["quality_approved"] is True
    assert artifact["crop_revision"] == 1 and artifact["coverage_ratio"] == 0.8
    image = client.get(artifact["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert fake.moves == 1
    assert not any(action.startswith("core:compositions") for action, _ in requirements)
    settings = client.portal.call(service.store.get_settings).extensions["com.toposync.cameras"]
    metadata = settings["devices"][0]["sources"][0]["metadata"]
    assert metadata["panorama_profile"] == {"legacy": "preserved"}
    assert metadata["other"] == "preserved"


def test_idempotency_and_exclusive_camera_head(environment):
    client, fake, service, _ = environment
    fake.block = True
    job = create(client)
    same = create(client)
    assert same["id"] == job["id"]
    blocked = client.post(BASE + "/jobs", json={"idempotency_key": "another-key"})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "camera_busy"
    fake.block = False
    assert wait_done(client, job)["status"] == "ready"
    assert fake.calls == 1


@pytest.mark.parametrize(("verified", "restored"), [(True, True), (False, False), (True, False)])
def test_control_verification_does_not_reconstruct_or_publish(environment, verified, restored):
    client, fake, service, _ = environment

    async def verify(camera, output, **kwargs):
        assert kwargs["verify_control"] is True
        return {"captures": [], "physical_state": "restored" if restored else "stopped",
                "issues": [], "checkpoint": {"control_verification": {
                    "status": "verified" if verified else "return_unconfirmed",
                    "checks": [{"status": "verified"}] if verified else [],
                }}}

    service.scan_runner = verify
    response = client.post(BASE + "/jobs", json={
        "idempotency_key": "verify-camera", "operation": "verify_control",
    })
    assert response.status_code == 200
    identifier = response.json()["job"]["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(f"{JOBS}/{identifier}").json()["job"]
        if job["status"] in {"verified", "failed"}:
            break
        time.sleep(0.01)
    assert job["status"] == ("verified" if verified and restored else "failed")
    assert job["control_checks_passed"] == int(verified)
    assert job["operation"] == "verify_control"
    assert not job["can_resume"] and not job["can_reconstruct"]
    assert "artifact_id" not in job
    assert client.get(BASE).json()["active"] is None
    conflict = client.post(BASE + "/jobs", json={"idempotency_key": "verify-camera"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "action", ["core:camera:read", "core:camera:control", "core:settings:write"]
)
def test_create_authorization_precedes_any_motion(environment, action: str):
    client, fake, _, _ = environment
    response = client.post(
        BASE + "/jobs", json={"idempotency_key": "denied-key"}, headers={"x-deny": action}
    )
    assert response.status_code == 403 and fake.moves == 0


def test_public_job_and_files_never_expose_checkpoint_or_credentials(environment):
    client, fake, _, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    response = client.get(f"{JOBS}/{job['id']}")
    assert "private" not in response.text and "password" not in response.text
    assert "checkpoint" not in response.text and "native_pan" not in response.text
    preview = client.get(response.json()["job"]["preview_url"])
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert (
        client.get(
            response.json()["job"]["preview_url"], headers={"x-deny": "core:camera:read"}
        ).status_code
        == 403
    )
    fake.block = False
    finished = wait_done(client, job)
    artifact = client.get(BASE).json()["active"]
    assert (
        client.get(artifact["image_url"], headers={"x-deny": "core:camera:read"}).status_code == 403
    )
    assert (
        client.get(
            f"/api/cameras/panorama-artifacts/{finished['artifact_id']}/files/job.json"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("return_verified,omit_required", [(True, False), (False, False), (False, True)])
def test_serpentine_service_keeps_full_result_and_separates_return(tmp_path, monkeypatch, return_verified, omit_required):
    import io
    from test_camera_panorama_region import serpentine_double
    from toposync_ext_cameras.panorama_region import REGION_POLICY, acquire_region

    class RegionalScanner(FakeScanner):
        async def scan(self, camera, output, **arguments):
            if arguments["return_only"]:
                self.returns += 1
                await arguments["progress"]({"phase": "returning", "physical_state": "returning"})
                raise RuntimeError("local return failure after publishing")
            assert arguments["acquisition_policy"] == REGION_POLICY
            scanner = serpentine_double(output, monkeypatch, progress=arguments["progress"])
            scanner.saved_return = {"kind": "preset", "preset_token": "original", "role": "original"}
            await acquire_region(scanner)
            self.moves += len(scanner.moves)
            await arguments["progress"]({"phase": "returning", "physical_state": "returning"})
            scanner.physical_state = "restored" if return_verified else "stopped"
            if not return_verified:
                scanner.issues.append({"code": "return_framing_unconfirmed"})
            await scanner._persist()
            return scanner.result()

        async def reconstruct(self, captures, output, **arguments):
            self.reconstruction_inputs = copy.deepcopy(captures)
            result = await super().reconstruct(captures, output, **arguments)
            result["source_ids"] = [photo["id"] for photo in (captures[:-1] if omit_required else captures)]
            return result

    fake = RegionalScanner()
    app, _, service, _ = make_app(tmp_path, fake, acquisition_policy=REGION_POLICY)
    with TestClient(app) as client:
        try:
            job = wait_done(client, create(client, "serpentine"))
            assert job["outcomes"] == {"acquisition": "sufficient", "reconstruction": "ready",
                                      "return": "verified" if return_verified else "unverified"}
            assert job["coverage_progress"]["policy_version"] == REGION_POLICY["version"]
            assert "required_views" not in job["coverage_progress"]
            assert job["coverage_progress"]["decision"]["sufficient"] is True
            assert job["status"] == ("partial" if omit_required else "ready")
            state = client.get(BASE).json()
            artifact = state["active"] or state["candidate"]
            assert artifact["outcomes"] == job["outcomes"]
            assert artifact["quality_approved"] is True  # Acquisition/return cannot rewrite this verdict.
            assert artifact["region_status"] == ("incomplete" if omit_required else "ready")
            assert artifact["coverage"]["acquisition_complete"] is False  # Mechanical domain unknown.
            assert artifact["crop"] == {"u_start": 0, "u_width": 1, "v_start": 0, "v_height": 1}
            original = client.get(artifact["image_url"]).content
            coverage = client.get(artifact["coverage_url"]).content
            assert Image.open(io.BytesIO(original)).size == (64, 32)
            assert Image.open(io.BytesIO(coverage)).size == (64, 32)
            assert len(fake.reconstruction_inputs) == job["captures_accepted"] > 6
            assert all(Image.open(photo["path"]).size == (160, 100) for photo in fake.reconstruction_inputs)
            report = client.get(f"/api/cameras/panorama-artifacts/{artifact['id']}/files/report").json()
            assert report["quality"] == fake.reconstruction_quality
            assert report["acquisition_decision"] == job["coverage_progress"]["decision"]
            if not return_verified:
                moves_before = fake.moves
                response = client.post(f"{JOBS}/{job['id']}/return")
                assert response.status_code == 200
                returned = wait_done(client, job)
                assert returned["artifact_id"] == artifact["id"] and returned["status"] == job["status"]
                assert returned["outcomes"]["reconstruction"] == "ready"
                assert returned["outcomes"]["return"] == "unverified"
                assert client.get(artifact["image_url"]).content == original
                assert client.get(artifact["coverage_url"]).content == coverage
                assert fake.moves == moves_before
        finally:
            client.portal.call(service.shutdown)


def test_crop_wrap_cas_and_immutable_reconstruction(environment):
    client, fake, service, _ = environment
    wait_done(client, create(client))
    artifact = client.get(BASE).json()["active"]
    image_before = client.get(artifact["image_url"]).content
    body = {
        "artifact_id": artifact["id"],
        "expected_revision": 1,
        "crop": {"u_start": 0.9, "u_width": 0.3, "v_start": 0.2, "v_height": 0.5},
    }
    saved = client.patch(BASE + "/crop", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["artifact"]["crop_revision"] == 2
    assert saved.json()["artifact"]["revision"] == 1
    assert client.patch(BASE + "/crop", json=body).status_code == 409
    assert client.get(BASE).json()["active"]["crop"] == body["crop"]
    assert client.get(artifact["image_url"]).content == image_before and fake.moves == 1
    disk_artifact = service._artifact(artifact["id"])
    assert disk_artifact["crop"]["u_width"] == 1  # Optical revision stays immutable.


@pytest.mark.parametrize(
    "crop",
    [
        {"u_start": -0.1, "u_width": 0.2, "v_start": 0, "v_height": 1},
        {"u_start": 0.9, "u_width": 0, "v_start": 0, "v_height": 1},
        {"u_start": 0, "u_width": 1, "v_start": 0.8, "v_height": 0.4},
    ],
)
def test_invalid_crop_never_moves_camera(environment, crop):
    client, fake, _, _ = environment
    response = client.patch(
        BASE + "/crop", json={"artifact_id": "a" * 32, "expected_revision": 1, "crop": crop}
    )
    assert response.status_code == 422 and fake.moves == 0


def test_partial_does_not_replace_complete_and_keeps_independent_crop(environment):
    client, fake, _, _ = environment
    first = wait_done(client, create(client))
    fake.complete = False
    partial = wait_done(client, create(client, "second-generation"))
    assert partial["status"] == "partial"
    source = client.get(BASE).json()
    assert source["active"]["id"] == first["artifact_id"]
    candidate = source["candidate"]
    assert candidate["id"] == partial["artifact_id"]
    body = {
        "artifact_id": candidate["id"],
        "expected_revision": 1,
        "crop": {"u_start": 0.1, "u_width": 0.8, "v_start": 0, "v_height": 1},
    }
    assert client.patch(BASE + "/crop", json=body).status_code == 200
    source = client.get(BASE).json()
    assert source["candidate"]["crop_revision"] == 2
    assert source["active"]["crop_revision"] == 1
    assert (
        client.get(f"/api/cameras/panorama-artifacts/{candidate['id']}").json()["artifact"]["crop"]
        == body["crop"]
    )


def test_ready_quality_partial_acquisition_becomes_active_without_complete_active(environment):
    client, fake, _, _ = environment
    fake.complete = False

    partial = wait_done(client, create(client))

    source = client.get(BASE).json()
    assert partial["status"] == "partial"
    assert source["active"]["id"] == partial["artifact_id"]
    assert source["candidate"] is None


def test_lower_coverage_partial_becomes_candidate_and_preserves_active_state(environment):
    client, fake, service, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.1, solid_angle_ratio=0.15)
    previous = wait_done(client, create(client))
    set_promotion_evidence(
        fake,
        pixel_ratio=0.16401946544647217,
        solid_angle_ratio=0.22224435075220947,
    )
    active_capture = wait_done(client, create(client, "capture-eight"))
    active = client.get(BASE).json()["active"]
    saved_crop = {
        "artifact_id": active["id"],
        "expected_revision": active["crop_revision"],
        "crop": {"u_start": 0.2, "u_width": 0.6, "v_start": 0.1, "v_height": 0.7},
    }
    assert client.patch(BASE + "/crop", json=saved_crop).status_code == 200

    set_promotion_evidence(
        fake,
        pixel_ratio=0.13577866554260254,
        solid_angle_ratio=0.1858963254108331,
    )
    candidate_capture = wait_done(client, create(client, "capture-nine"))
    source = client.get(BASE).json()

    assert active_capture["candidate_reason"] == "coverage_alignment_unverified"
    assert source["active"]["id"] == previous["artifact_id"]
    assert source["active"]["crop"] == saved_crop["crop"]
    assert source["previous"] is None
    assert source["candidate"]["id"] == candidate_capture["artifact_id"]
    assert candidate_capture["candidate_reason"] == "coverage_alignment_unverified"
    assert "active_panorama_preserved" in candidate_capture["issue_codes"]
    assert all("0.164" not in message for message in candidate_capture["issues"])
    service.jobs[candidate_capture["id"]]["candidate_reason"] = (
        "rtsp://private-user:private-password@private-host"
    )
    visible = client.get(f"{JOBS}/{candidate_capture['id']}").json()["job"]
    assert "candidate_reason" not in visible


@pytest.mark.parametrize(
    ("new_pixel", "new_solid"),
    [(0.21, 0.19), (0.19, 0.21)],
)
def test_mixed_partial_coverage_regression_stays_candidate(
    environment, new_pixel: float, new_solid: float
) -> None:
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.2, solid_angle_ratio=0.2)
    first = wait_done(client, create(client))
    set_promotion_evidence(
        fake,
        pixel_ratio=new_pixel,
        solid_angle_ratio=new_solid,
    )

    second = wait_done(client, create(client, f"mixed-{new_pixel}-{new_solid}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "coverage_regressed"


def test_partial_with_improved_scalar_coverage_stays_candidate_without_alignment(environment):
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.13, solid_angle_ratio=0.18)
    fake.acquisition_coverage.update(
        {
            "boundaries": {"pan_min": {"confirmed": True}},
            "bands": {"0": {"complete": False, "edges": {"-1": "unconfirmed"}}},
        }
    )
    first = wait_done(client, create(client))
    set_promotion_evidence(
        fake,
        pixel_ratio=0.16,
        solid_angle_ratio=0.22,
        primary_complete=True,
        bands_completed=1,
    )
    fake.acquisition_coverage.update(
        {
            "boundaries": {
                "pan_min": {"confirmed": True},
                "pan_max": {"confirmed": True},
            },
            "bands": {
                "0": {
                    "complete": True,
                    "edges": {"-1": "limit", "1": "limit"},
                }
            },
        }
    )

    second = wait_done(client, create(client, "better-coverage"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "coverage_alignment_unverified"


def test_partial_cannot_replace_complete_even_with_more_reconstructed_coverage(environment):
    client, fake, _, _ = environment
    set_promotion_evidence(
        fake,
        pixel_ratio=0.2,
        solid_angle_ratio=0.2,
        primary_complete=True,
        bands_completed=1,
    )
    first = wait_done(client, create(client))
    fake.complete = False
    set_promotion_evidence(
        fake,
        pixel_ratio=0.3,
        solid_angle_ratio=0.3,
        primary_complete=True,
        bands_completed=1,
    )

    second = wait_done(client, create(client, "partial-after-complete"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "acquisition_completeness_regressed"


@pytest.mark.parametrize(
    ("new_primary", "new_bands"),
    [(False, 1), (True, 0)],
)
def test_partial_cannot_regress_confirmed_acquisition_topology(
    environment, new_primary: bool, new_bands: int
) -> None:
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(
        fake,
        pixel_ratio=0.2,
        solid_angle_ratio=0.2,
        primary_complete=True,
        bands_completed=1,
    )
    first = wait_done(client, create(client))
    set_promotion_evidence(
        fake,
        pixel_ratio=0.2,
        solid_angle_ratio=0.2,
        primary_complete=new_primary,
        bands_completed=new_bands,
    )

    second = wait_done(client, create(client, f"topology-{new_primary}-{new_bands}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "acquisition_progress_regressed"


@pytest.mark.parametrize("lost_evidence", ["boundary", "edge", "edge-status"])
def test_partial_cannot_drop_confirmed_acquisition_structure(
    environment, lost_evidence: str
) -> None:
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(
        fake,
        pixel_ratio=0.2,
        solid_angle_ratio=0.2,
        primary_complete=True,
        bands_completed=1,
    )
    complete_structure = {
        "boundaries": {
            "pan_min": {"confirmed": True},
            "pan_max": {"confirmed": True},
        },
        "bands": {
            "0": {"complete": True, "edges": {"-1": "limit", "1": "limit"}}
        },
    }
    fake.acquisition_coverage.update(copy.deepcopy(complete_structure))
    first = wait_done(client, create(client))

    set_promotion_evidence(
        fake,
        pixel_ratio=0.3,
        solid_angle_ratio=0.3,
        primary_complete=True,
        bands_completed=1,
    )
    candidate_structure = copy.deepcopy(complete_structure)
    if lost_evidence == "boundary":
        candidate_structure["boundaries"].pop("pan_min")
    elif lost_evidence == "edge":
        candidate_structure["bands"]["0"]["edges"].pop("-1")
    else:
        candidate_structure["bands"]["0"]["edges"]["-1"] = "loop"
    fake.acquisition_coverage.update(candidate_structure)

    second = wait_done(client, create(client, f"lost-{lost_evidence}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "acquisition_topology_regressed"


def test_missing_candidate_metric_cannot_displace_active_with_that_metric(environment):
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.2, solid_angle_ratio=0.2)
    first = wait_done(client, create(client))
    set_promotion_evidence(fake, pixel_ratio=0.3, solid_angle_ratio=None)

    second = wait_done(client, create(client, "missing-solid-angle"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "coverage_evidence_missing"


@pytest.mark.parametrize("incompatibility", ["algorithm", "provenance"])
def test_incompatible_coverage_semantics_stay_candidate(environment, incompatibility: str) -> None:
    client, fake, _, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.2, solid_angle_ratio=0.2)
    first = wait_done(client, create(client))
    set_promotion_evidence(fake, pixel_ratio=0.3, solid_angle_ratio=0.3)
    if incompatibility == "algorithm":
        fake.algorithm_version = "fake-2"
    else:
        fake.reconstruction_coverage["provenance"] = "synthetic_coverage_v2"

    second = wait_done(client, create(client, f"different-{incompatibility}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == first["artifact_id"]
    assert source["candidate"]["id"] == second["artifact_id"]
    assert second["candidate_reason"] == "comparison_incompatible"


@pytest.mark.parametrize(
    "active_problem",
    [
        "stale",
        "unapproved",
        "missing",
        "missing-file",
        "malformed-completeness",
        "malformed-ratio",
    ],
)
def test_invalid_active_does_not_block_current_approved_artifact(
    environment, active_problem: str
) -> None:
    client, fake, service, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.2, solid_angle_ratio=0.2)
    previous = wait_done(client, create(client, "previous-valid"))
    previous_active = client.get(BASE).json()["active"]
    previous_crop = {
        "artifact_id": previous_active["id"],
        "expected_revision": previous_active["crop_revision"],
        "crop": {"u_start": 0.1, "u_width": 0.8, "v_start": 0.1, "v_height": 0.8},
    }
    assert client.patch(BASE + "/crop", json=previous_crop).status_code == 200
    path = service.root / "artifacts" / previous["artifact_id"] / "artifact.json"
    if active_problem == "missing":
        path.unlink()
    else:
        artifact = json.loads(path.read_text())
        if active_problem == "stale":
            artifact["_identity"] = "stale-source-identity"
        elif active_problem == "unapproved":
            artifact["quality_approved"] = False
        elif active_problem == "missing-file":
            (path.parent / artifact["_files"]["panorama"]).unlink()
        elif active_problem == "malformed-completeness":
            artifact["coverage"]["acquisition_complete"] = "unknown"
        else:
            artifact["coverage"]["pixel_ratio"] = True
        path.write_text(json.dumps(artifact))
    set_promotion_evidence(fake, pixel_ratio=0.1, solid_angle_ratio=0.1)

    second = wait_done(client, create(client, f"replace-{active_problem}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == second["artifact_id"]
    assert source["candidate"] is None
    assert source["previous"] is None
    assert "candidate_reason" not in second


@pytest.mark.parametrize(
    ("problem", "reason"),
    [
        ("unknown-algorithm", "comparison_incompatible"),
        ("missing-provenance", "comparison_incompatible"),
        ("missing-pixel", "coverage_evidence_missing"),
        ("missing-solid", "coverage_evidence_missing"),
        ("boolean-pixel", "coverage_evidence_missing"),
        ("boolean-solid", "coverage_evidence_missing"),
        ("nan-pixel", "coverage_evidence_missing"),
        ("nan-solid", "coverage_evidence_missing"),
        ("missing-progress", "coverage_evidence_missing"),
        ("missing-primary", "coverage_evidence_missing"),
        ("missing-bands", "coverage_evidence_missing"),
        ("boolean-primary", "coverage_evidence_missing"),
        ("boolean-bands", "coverage_evidence_missing"),
        ("nan-bands", "coverage_evidence_missing"),
    ],
)
def test_ineligible_evidence_is_candidate_even_without_active(
    environment, problem: str, reason: str
) -> None:
    client, fake, _, _ = environment
    if problem == "unknown-algorithm":
        fake.algorithm_version = "unknown"
    elif problem == "missing-provenance":
        fake.reconstruction_coverage.pop("provenance")
    elif problem == "missing-pixel":
        fake.reconstruction_coverage.pop("pixel_ratio")
    elif problem == "missing-solid":
        fake.reconstruction_coverage.pop("solid_angle_ratio")
    elif problem == "boolean-pixel":
        fake.reconstruction_coverage["pixel_ratio"] = True
    elif problem == "boolean-solid":
        fake.reconstruction_coverage["solid_angle_ratio"] = False
    elif problem == "nan-pixel":
        fake.reconstruction_coverage["pixel_ratio"] = float("nan")
    elif problem == "nan-solid":
        fake.reconstruction_coverage["solid_angle_ratio"] = float("nan")
    elif problem == "missing-progress":
        fake.acquisition_coverage.pop("progress")
    elif problem == "missing-primary":
        fake.acquisition_coverage["progress"].pop("primary_complete")
    elif problem == "missing-bands":
        fake.acquisition_coverage["progress"].pop("bands_completed")
    elif problem == "boolean-primary":
        fake.acquisition_coverage["progress"]["primary_complete"] = 1
    elif problem == "boolean-bands":
        fake.acquisition_coverage["progress"]["bands_completed"] = True
    else:
        fake.acquisition_coverage["progress"]["bands_completed"] = float("nan")

    capture = wait_done(client, create(client, f"ineligible-{problem}"))
    source = client.get(BASE).json()

    assert source["active"] is None
    assert source["candidate"]["id"] == capture["artifact_id"]
    assert capture["candidate_reason"] == reason
    assert "active_panorama_preserved" not in capture["issue_codes"]


def test_malformed_scan_completeness_cannot_be_certified(environment) -> None:
    client, fake, _, _ = environment
    fake.complete = "false"

    capture = wait_done(client, create(client, "malformed-completeness"))
    source = client.get(BASE).json()

    assert capture["status"] == "partial"
    assert capture["candidate_reason"] == "coverage_evidence_missing"
    assert source["active"] is None
    assert source["candidate"]["id"] == capture["artifact_id"]


@pytest.mark.parametrize(
    "missing_evidence", ["algorithm", "provenance", "pixel", "solid", "progress"]
)
def test_active_without_comparison_evidence_does_not_block_recovery(
    environment, missing_evidence: str
) -> None:
    client, fake, service, _ = environment
    fake.complete = False
    set_promotion_evidence(fake, pixel_ratio=0.2, solid_angle_ratio=0.2)
    first = wait_done(client, create(client))
    path = service.root / "artifacts" / first["artifact_id"] / "artifact.json"
    artifact = json.loads(path.read_text())
    if missing_evidence == "algorithm":
        artifact["algorithm_version"] = "unknown"
    elif missing_evidence == "provenance":
        artifact["coverage"].pop("provenance")
    elif missing_evidence == "progress":
        artifact["coverage"]["acquisition"].pop("progress")
    else:
        artifact["coverage"].pop(
            "pixel_ratio" if missing_evidence == "pixel" else "solid_angle_ratio"
        )
    path.write_text(json.dumps(artifact))
    set_promotion_evidence(fake, pixel_ratio=0.3, solid_angle_ratio=0.3)

    second = wait_done(client, create(client, f"active-missing-{missing_evidence}"))
    source = client.get(BASE).json()

    assert source["active"]["id"] == second["artifact_id"]
    assert source["candidate"] is None
    assert "candidate_reason" not in second


@pytest.mark.parametrize("contradiction", ["complete-primary", "complete-count", "band-count"])
def test_contradictory_acquisition_evidence_is_candidate(
    environment, contradiction: str
) -> None:
    client, fake, _, _ = environment
    if contradiction == "complete-primary":
        fake.acquisition_coverage["progress"]["primary_complete"] = False
    elif contradiction == "complete-count":
        fake.acquisition_coverage["progress"]["bands_completed"] = 0
    else:
        fake.acquisition_coverage["bands"] = {
            "0": {"complete": True, "edges": {"-1": "limit", "1": "limit"}}
        }
        fake.acquisition_coverage["progress"]["bands_completed"] = 2

    capture = wait_done(client, create(client, f"contradictory-{contradiction}"))
    source = client.get(BASE).json()

    assert source["active"] is None
    assert source["candidate"]["id"] == capture["artifact_id"]
    assert capture["candidate_reason"] == "coverage_evidence_missing"


def test_empty_required_reconstruction_file_is_not_published(environment) -> None:
    client, fake, service, _ = environment

    async def truncate_output() -> None:
        # Fake reconstruction writes into its job directory before invoking this hook.
        for path in service.root.glob("jobs/*/reconstruction-*/panorama.png"):
            path.write_bytes(b"")

    fake.before_publish = truncate_output
    capture = wait_done(client, create(client, "empty-panorama"))

    assert capture["status"] == "failed"
    assert capture["error"]["code"] == "invalid_reconstruction"
    source = client.get(BASE).json()
    assert source["active"] is None and source["candidate"] is None
    assert list((service.root / "artifacts").glob("*")) == []


@pytest.mark.parametrize("dimension", ["width", "height"])
def test_boolean_panorama_dimensions_are_rejected(environment, dimension: str) -> None:
    client, fake, _, _ = environment
    setattr(fake, dimension, True)

    capture = wait_done(client, create(client, f"boolean-{dimension}"))

    assert capture["status"] == "failed"
    assert capture["error"]["code"] == "invalid_reconstruction"
    source = client.get(BASE).json()
    assert source["active"] is None and source["candidate"] is None


@pytest.mark.parametrize(
    ("quality", "included"),
    [
        ({"status": "review", "reasons": []}, True),
        ({"status": "ready"}, True),
        ({"status": "ready", "reasons": ["synthetic_warning"]}, True),
        ({"status": "ready", "reasons": None}, True),
        ({"status": "ready", "reasons": "not-a-list"}, True),
        ({"status": "ready", "reasons": ()}, True),
        ([], True),
        (None, True),
        (None, False),
    ],
)
def test_unapproved_quality_is_candidate_even_without_active(environment, quality, included):
    client, fake, _, _ = environment
    fake.reconstruction_quality = quality
    fake.include_reconstruction_quality = included

    job = wait_done(client, create(client))

    source = client.get(BASE).json()
    assert source["active"] is None
    assert source["candidate"]["id"] == job["artifact_id"]
    assert source["candidate"]["quality_approved"] is False


def test_unapproved_quality_preserves_even_a_partial_active_artifact(environment):
    client, fake, _, _ = environment
    fake.complete = False
    first = wait_done(client, create(client))
    fake.complete = True
    fake.reconstruction_quality = {"status": "review", "reasons": []}

    review = wait_done(client, create(client, "review-generation"))

    source = client.get(BASE).json()
    assert source["active"]["id"] == first["artifact_id"]
    assert source["active"]["status"] == "partial"
    assert source["candidate"]["id"] == review["artifact_id"]


def test_approved_complete_artifact_replaces_candidate_without_inventing_previous(environment):
    client, fake, _, _ = environment
    fake.reconstruction_quality = {"status": "review", "reasons": []}
    candidate = wait_done(client, create(client))
    fake.reconstruction_quality = {"status": "ready", "reasons": []}

    ready = wait_done(client, create(client, "approved-generation"))

    source = client.get(BASE).json()
    assert source["active"]["id"] == ready["artifact_id"]
    assert source["candidate"] is None
    assert source["previous"] is None
    assert candidate["artifact_id"] != ready["artifact_id"]


def test_reconstruction_process_records_quality_verdict_before_sanitizing_tuple(
    tmp_path, monkeypatch
):
    import toposync_ext_cameras.source_panorama as module
    from toposync_ext_cameras.processing import panorama_reconstruction

    class Connection:
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

        def close(self):
            pass

    class Cancellation:
        @staticmethod
        def is_set():
            return False

    monkeypatch.setattr(
        panorama_reconstruction,
        "reconstruct_panorama",
        lambda *args, **kwargs: {"quality": {"status": "ready", "reasons": ()}},
    )
    connection = Connection()

    module._reconstruction_process([], str(tmp_path), connection, Cancellation())

    assert connection.messages == [
        {
            "result": {"quality": {"status": "ready", "reasons": []}},
            "quality_approved": False,
        }
    ]


def test_new_complete_preserves_previous_and_resets_crop(environment):
    client, fake, _, _ = environment
    first = wait_done(client, create(client))
    artifact = client.get(BASE).json()["active"]
    assert (
        client.patch(
            BASE + "/crop",
            json={
                "artifact_id": artifact["id"],
                "expected_revision": 1,
                "crop": {"u_start": 0.2, "u_width": 0.4, "v_start": 0.1, "v_height": 0.4},
            },
        ).status_code
        == 200
    )
    second = wait_done(client, create(client, "second-generation"))
    source = client.get(BASE).json()
    assert source["active"]["id"] == first["artifact_id"]
    assert source["active"]["crop"]["u_width"] == 0.4
    assert source["candidate"]["id"] == second["artifact_id"]
    assert source["candidate"]["crop"]["u_width"] == 1
    assert source["previous"] is None
    assert second["candidate_reason"] == "coverage_alignment_unverified"


def test_policy_v4_candidate_is_promoted_once_without_new_camera_work(environment):
    client, fake, service, _ = environment
    first = wait_done(client, create(client))
    second = wait_done(client, create(client, "replacement-before-contract-change"))
    before = (fake.moves, fake.calls, fake.returns, fake.stops)
    state = client.get(BASE).json()
    assert state["active"]["id"] == first["artifact_id"]
    assert state["candidate"]["id"] == second["artifact_id"]

    service.jobs[second["id"]]["_acquisition_policy"] = {"version": 4}
    pending = client.get(BASE).json()
    assert pending["replacement_pending"] is True
    assert pending["active"]["id"] == first["artifact_id"]
    migrated = client.post(BASE + "/finalize").json()

    assert migrated["active"]["id"] == second["artifact_id"]
    assert migrated["previous"]["id"] == first["artifact_id"]
    assert migrated["candidate"] is None
    assert (fake.moves, fake.calls, fake.returns, fake.stops) == before


def test_policy_v4_approved_result_has_no_candidate_state() -> None:
    artifact = {
        "status": "partial",
        "quality": {"status": "ready", "reasons": []},
        "quality_approved": True,
        "coverage": {},
    }

    assert (
        _replacement_candidate_reason(
            artifact,
            None,
            active_is_current=False,
            policy_version=4,
        )
        is None
    )
    artifact["quality_approved"] = False
    assert _replacement_candidate_reason(
        artifact,
        None,
        active_is_current=False,
        policy_version=4,
    ) == "quality_not_approved"


def test_stop_does_not_return_and_resume_is_deliberate(environment):
    client, fake, _, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    assert client.post(f"{JOBS}/{job['id']}/stop").status_code == 200
    stopped = wait_done(client, job)
    assert stopped["status"] == "interrupted" and stopped["physical_state"] == "stopped"
    assert stopped["can_resume"] and fake.returns == 0 and fake.stops == 1
    assert client.get(BASE).status_code == 200 and fake.moves == 1
    fake.block = False
    assert client.post(f"{JOBS}/{job['id']}/resume").status_code == 200
    assert wait_done(client, job)["status"] == "ready" and fake.moves == 2


def test_explicit_return_is_separate_from_stop(environment):
    client, fake, _, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    wait_done(client, job)
    assert client.post(f"{JOBS}/{job['id']}/return").status_code == 200
    returned = wait_done(client, job)
    assert returned["status"] == "interrupted" and returned["physical_state"] == "restored"
    assert fake.moves == 1 and fake.returns == 1


def test_stop_failure_and_exception_are_sanitized(environment):
    client, fake, _, _ = environment
    fake.block = True
    fake.stop_fail = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    stopped = wait_done(client, job)
    assert stopped["physical_state"] == "stop_unconfirmed"
    assert "private-password" not in json.dumps(stopped)
    fake.block = False
    fake.fail = True
    failed = wait_done(client, create(client, "error-generation"))
    assert failed["status"] == "failed" and failed["error"]["code"] == "panorama_failed"
    assert "private-host" not in json.dumps(failed)


def test_restart_interrupts_without_moving(tmp_path: Path):
    app, fake, service, _ = make_app(tmp_path)
    job_id = "a" * 32
    job = {
        "id": job_id,
        "camera_id": "simulated",
        "source_id": "main",
        "status": "capturing",
        "phase": "capturing",
        "created_at": time.time(),
        "physical_state": "returning",
        "_checkpoint": {"captures": []},
        "captures_accepted": 1,
    }
    service._save(job)
    restarted_app, restarted_fake, restarted, _ = make_app(tmp_path)
    with TestClient(restarted_app) as client:
        visible = client.get(f"{JOBS}/{job_id}").json()["job"]
        assert visible["status"] == "interrupted" and visible["physical_state"] == "unknown"
        assert not visible["can_resume"] and restarted_fake.moves == 0
        client.portal.call(restarted.shutdown)


def test_storage_reservation_blocks_before_motion(environment):
    client, fake, service, _ = environment
    service.max_global_bytes = 1
    response = client.post(BASE + "/jobs", json={"idempotency_key": "storage-failure"})
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "panorama_storage_full"
    )
    assert fake.moves == 0 and not service.jobs


def test_new_capture_reconciles_presets_without_deleting_historical_evidence(environment):
    client, fake, service, _ = environment
    first = wait_done(client, create(client, "reconciliation-first"))
    second = wait_done(client, create(client, "reconciliation-second"))
    first_directory = service._job_directory(service.jobs[first["id"]])
    (first_directory / "stale-captures.bin").write_bytes(b"x" * (256 * 1024))
    fake.managed_presets = [
        {"token": f"managed-{index}", "name": f"Pano O {index:016x}"}
        for index in range(62)
    ] + [
        {"token": "user-home", "name": "Casa"},
        {"token": "user-gate", "name": "Portão"},
    ]
    third = create(client, "reconciliation-third")

    assert third["status"] == "queued"
    assert fake.managed_presets == [
        {"token": "user-home", "name": "Casa"},
        {"token": "user-gate", "name": "Portão"},
    ]
    assert fake.managed_cleanup_calls == 3
    assert first["id"] in service.jobs and first_directory.exists()
    assert (first_directory / "stale-captures.bin").stat().st_size == 256 * 1024
    assert second["id"] in service.jobs
    assert service.jobs[second["id"]]["can_resume"] is False
    assert service.jobs[second["id"]]["_checkpoint"]["return"] is None


def test_identity_change_blocks_resume_without_motion(environment):
    client, fake, service, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    wait_done(client, job)

    async def change() -> None:
        def update(settings):
            settings["devices"][0]["sources"][0]["origin"]["rtsp_url"] = (
                "rtsp://different.invalid/image"
            )
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(change)
    response = client.post(f"{JOBS}/{job['id']}/resume")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "source_changed"
    assert fake.moves == 1


def test_publish_conflict_preserves_intervening_crop_without_exposing_orphan_candidate(
    environment,
):
    client, fake, service, _ = environment
    first = wait_done(client, create(client))
    original = client.get(BASE).json()["active"]

    async def intervene() -> None:
        def update(settings):
            pointer = settings["devices"][0]["sources"][0]["metadata"]["panorama"]
            pointer["active"]["crop_revision"] += 1
            pointer["active"]["crop"]["u_width"] = 0.4
            pointer["revision"] += 1
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    fake.before_publish = intervene
    second = wait_done(client, create(client, "publish-conflict"))
    assert second["status"] == "failed" and second["error"]["code"] == "panorama_revision_conflict"
    source = client.get(BASE).json()
    active = source["active"]
    assert active["id"] == first["artifact_id"] and active["crop"]["u_width"] == 0.4
    assert source["candidate"] is None
    assert original["id"] != second["artifact_id"]
    saved = client.patch(
        BASE + "/crop",
        json={
            "artifact_id": active["id"],
            "expected_revision": active["crop_revision"],
            "crop": {"u_start": 0.1, "u_width": 0.3, "v_start": 0, "v_height": 1},
        },
    )
    assert saved.status_code == 200
    assert client.get(BASE).json()["active"]["crop"]["u_width"] == 0.3


def test_processing_stop_preserves_camera_return_and_captures(environment):
    client, fake, _, _ = environment
    fake.processing_block = True
    job = create(client)
    wait_phase(client, job, "reconstructing")
    assert fake.closed == 1 and fake.returns == 1
    client.post(f"{JOBS}/{job['id']}/stop")
    done = wait_done(client, job)
    assert done["status"] == "interrupted" and done["physical_state"] == "restored"
    assert done["captures_accepted"] == 2 and fake.stops == 0


def test_file_manifest_cannot_escape_artifact_directory(environment, tmp_path: Path):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    artifact = service._artifact(job["artifact_id"])
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    artifact["_files"]["model"] = str(outside)
    service._atomic(service.root / "artifacts" / artifact["id"] / "artifact.json", artifact)
    response = client.get(f"/api/cameras/panorama-artifacts/{artifact['id']}/files/model")
    assert response.status_code == 404 and "private.txt" not in response.text


def _slow_reconstruction(captures, directory, connection, cancellation):
    """Real separate process for the cancellation contract; no camera or network."""
    import os

    (Path(directory) / "worker.pid").write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)  # Deliberately noncooperative: parent must terminate it.


def test_default_worker_is_terminated_on_cancel(environment, monkeypatch):
    import os
    import toposync_ext_cameras.source_panorama as module

    client, fake, service, _ = environment
    monkeypatch.setattr(module, "_reconstruction_process", _slow_reconstruction)
    service.reconstruct_runner = None
    job = create(client)
    wait_phase(client, job, "reconstructing")
    worker_path = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        worker_path = next(service._job_directory(job).glob("reconstruction-*/worker.pid"), None)
        if worker_path:
            break
        time.sleep(0.01)
    assert worker_path is not None
    process_id = int(worker_path.read_text())
    os.kill(process_id, 0)
    client.post(f"{JOBS}/{job['id']}/stop")
    stopped = wait_done(client, job, timeout=5)
    assert stopped["status"] == "interrupted" and fake.returns == 1
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


def test_queue_is_bounded_and_acquisition_is_serial(environment):
    import copy

    client, fake, service, _ = environment

    async def add_sources():
        def update(settings):
            for index in range(2, 5):
                camera = copy.deepcopy(settings["devices"][0])
                camera["id"] = f"simulated-{index}"
                settings["devices"].append(camera)
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(add_sources)
    fake.block = True
    first = create(client)
    wait_phase(client, first, "waiting_for_stability")
    second = create(client, base=BASE.replace("/simulated/", "/simulated-2/"))
    third = create(client, base=BASE.replace("/simulated/", "/simulated-3/"))
    full = client.post(
        BASE.replace("/simulated/", "/simulated-4/") + "/jobs",
        json={"idempotency_key": "queue-is-full"},
    )
    assert full.status_code == 409 and full.json()["detail"]["code"] == "panorama_queue_full"
    assert fake.calls == 1
    fake.block = False
    for job in (first, second, third):
        assert wait_done(client, job)["status"] == "ready"
    assert fake.calls == 3


def test_crop_denied_does_not_modify_state(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    body = {
        "artifact_id": job["artifact_id"],
        "expected_revision": 1,
        "crop": {"u_start": 0, "u_width": 0.5, "v_start": 0, "v_height": 1},
    }
    denied = client.patch(BASE + "/crop", json=body, headers={"x-deny": "core:settings:write"})
    assert denied.status_code == 403
    assert client.get(BASE).json()["active"]["crop_revision"] == 1
    assert fake.moves == 1


def test_return_does_not_reserve_a_new_capture_budget(environment):
    client, fake, service, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    wait_done(client, job)
    service.max_global_bytes = 1
    service.max_job_bytes = 1
    response = client.post(f"{JOBS}/{job['id']}/return")
    assert response.status_code == 200
    returned = wait_done(client, job)
    assert returned["physical_state"] == "restored" and fake.returns == 1
    assert fake.moves == 1


def test_mount_revision_change_blocks_resume_and_does_not_move(environment):
    client, fake, service, _ = environment
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    wait_done(client, job)

    async def remount():
        def update(settings):
            settings["devices"][0].setdefault("metadata", {})["panorama_mount_revision"] = 2
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(remount)
    response = client.post(f"{JOBS}/{job['id']}/resume")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "source_changed"
    assert fake.moves == 1


def test_stop_queued_job_finishes_without_waiting_for_another_camera(environment):
    import copy

    client, fake, service, _ = environment

    async def add_camera():
        def update(settings):
            camera = copy.deepcopy(settings["devices"][0])
            camera["id"] = "another-camera"
            settings["devices"].append(camera)
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(add_camera)
    fake.block = True
    first = create(client)
    wait_phase(client, first, "waiting_for_stability")
    second = create(client, base=BASE.replace("/simulated/", "/another-camera/"))
    response = client.post(f"{JOBS}/{second['id']}/stop")
    assert response.status_code == 200
    stopped = wait_done(client, second, timeout=0.5)
    assert stopped["status"] == "interrupted"
    assert client.get(f"{JOBS}/{first['id']}").json()["job"]["status"] == "capturing"
    assert fake.calls == 1 and fake.stops == 0
    fake.block = False
    assert wait_done(client, first)["status"] == "ready"


def test_storage_exception_does_not_masquerade_as_user_stop(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    service.max_job_bytes = 1
    with pytest.raises(HTTPException) as failure:
        client.portal.call(service._progress, service.jobs[job["id"]], {"phase": "capturing"})
    assert failure.value.detail["code"] == "job_storage_exceeded"
    assert job["id"] not in service.cancelled


def test_scanner_issue_records_expose_translation_codes_without_internal_details(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    service.jobs[job["id"]]["issues"] = [
        {"code": "return_framing_unconfirmed", "reason": "rtsp://private:secret@example.invalid"},
        {"code": "return_framing_unconfirmed"},
        {"code": "new_diagnostic_code", "internal": {"password": "private"}},
        {"code": "rtsp://private:secret@example.invalid"},
    ]
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["issues"] == [
        "Não conseguimos confirmar o enquadramento inicial da câmera.",
        "Não foi possível confirmar uma etapa da captura.",
    ]
    assert visible["issue_codes"] == ["return_framing_unconfirmed", "new_diagnostic_code"]
    assert "secret" not in json.dumps(visible) and "password" not in json.dumps(visible)


def test_current_scanner_recovery_diagnostics_have_public_messages(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    codes = [
        "absolute_grid_checkpoint_incompatible",
        "control_verification_resume_unavailable",
        "original_reference_changed",
        "pilot_cycle_unconfirmed",
        "return_coarse_observation_unconfirmed",
        "return_correction_budget_exhausted",
        "visual_control_resolution_unverified",
        "visual_response_unavailable",
        "working_reference_changed",
        "working_reference_unavailable",
    ]
    service.jobs[job["id"]]["issues"] = [{"code": code} for code in codes]

    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]

    assert visible["issue_codes"] == codes
    assert len(visible["issues"]) == len(codes)
    assert "Não foi possível confirmar uma etapa da captura." not in visible["issues"]


def test_progress_storage_failure_is_typed_and_does_not_prevent_return_cleanup(environment):
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError

    client, fake, service, _ = environment
    caught = []

    async def boundary(camera, output, *, checkpoint, progress, cancelled, return_only):
        service.max_job_bytes = 1
        try:
            await progress({"phase": "capturing"})
        except PanoramaCaptureError as exc:
            caught.append(exc.code)
            await camera.stop()
            assert not cancelled()
            await progress({"phase": "returning", "physical_state": "restored"})
        return {
            "captures": [],
            "checkpoint": {"reference": "preserved"},
            "physical_state": "restored",
            "issues": [{"code": "job_storage_exceeded"}],
        }

    service.scan_runner = boundary
    job = wait_done(client, create(client))
    assert caught == ["job_storage_exceeded"]
    assert job["status"] == "failed" and job["error"]["code"] == "job_storage_exceeded"
    assert job["physical_state"] == "restored" and fake.stops == 1 and fake.closed == 1
    assert not job["can_resume"]


def test_one_capture_can_return_but_cannot_resume(environment):
    client, fake, _, _ = environment
    fake.capture_count = 1
    fake.block = True
    job = create(client)
    wait_phase(client, job, "waiting_for_stability")
    client.post(f"{JOBS}/{job['id']}/stop")
    stopped = wait_done(client, job)
    assert stopped["can_return"] and not stopped["can_resume"]
    assert client.post(f"{JOBS}/{job['id']}/resume").status_code == 409
    assert client.post(f"{JOBS}/{job['id']}/return").status_code == 200
    returned = wait_done(client, job)
    assert returned["physical_state"] == "restored"
    assert fake.moves == 1 and fake.returns == 1


def test_stale_settings_draft_cannot_overwrite_crop_but_other_changes_survive(environment):
    import copy

    client, _, service, _ = environment
    wait_done(client, create(client))
    initial_settings = client.portal.call(service.store.get_settings)
    stale_devices = copy.deepcopy(initial_settings.extensions["com.toposync.cameras"]["devices"])
    artifact = client.get(BASE).json()["active"]
    crop = {"u_start": 0.8, "u_width": 0.4, "v_start": 0.1, "v_height": 0.6}
    assert (
        client.patch(
            BASE + "/crop",
            json={"artifact_id": artifact["id"], "expected_revision": 1, "crop": crop},
        ).status_code
        == 200
    )
    stale_source = stale_devices[0]["sources"][0]
    stale_source["name"] = "Edited while the panorama changed"
    stale_source["origin"]["rtsp_url"] = "rtsp://replacement.invalid/main"
    stale_source["metadata"]["other"] = "new unrelated metadata"
    stale_source["metadata"]["panorama_profile"] = {"manual": "intentional legacy change"}
    client.portal.call(
        service.store.patch_extension_settings, "com.toposync.cameras", {"devices": stale_devices}
    )
    current = client.portal.call(service.store.get_settings).extensions["com.toposync.cameras"][
        "devices"
    ][0]["sources"][0]
    assert current["name"] == stale_source["name"]
    assert current["origin"] == stale_source["origin"]
    assert current["metadata"]["other"] == "new unrelated metadata"
    assert current["metadata"]["panorama_profile"] == stale_source["metadata"]["panorama_profile"]
    pointer = current["metadata"]["panorama"]
    assert pointer["active"]["crop_revision"] == 2 and pointer["active"]["crop"] == crop


def test_stale_settings_without_panorama_keeps_new_artifact_and_strips_forged_references(
    environment,
):
    import copy

    client, _, service, _ = environment
    draft = copy.deepcopy(client.portal.call(service.store.get_settings))
    job = wait_done(client, create(client))
    devices = draft.extensions["com.toposync.cameras"]["devices"]
    new_source = copy.deepcopy(devices[0]["sources"][0])
    new_source["id"] = "new-source"
    new_source["metadata"]["panorama"] = {"active": {"artifact_id": "a" * 32}}
    new_source["metadata"]["other"] = "new source value"
    devices[0]["sources"].append(new_source)
    new_camera = copy.deepcopy(devices[0])
    new_camera["id"] = "new-camera"
    new_camera["sources"][0]["metadata"]["panorama"] = {
        "active": {"artifact_id": job["artifact_id"]}
    }
    devices.append(new_camera)
    client.portal.call(service.store.replace_settings, draft)
    current = client.portal.call(service.store.get_settings).extensions["com.toposync.cameras"][
        "devices"
    ]
    assert (
        current[0]["sources"][0]["metadata"]["panorama"]["active"]["artifact_id"]
        == job["artifact_id"]
    )
    assert "panorama" not in current[0]["sources"][1]["metadata"]
    assert current[0]["sources"][1]["metadata"]["other"] == "new source value"
    assert "panorama" not in current[1]["sources"][0]["metadata"]
    assert "panorama" not in current[1]["sources"][1]["metadata"]


def test_settings_deletion_is_not_undone_by_panorama_reference_filter(environment):
    client, _, service, _ = environment
    wait_done(client, create(client))
    client.portal.call(
        service.store.patch_extension_settings, "com.toposync.cameras", {"devices": []}
    )
    current = client.portal.call(service.store.get_settings).extensions["com.toposync.cameras"]
    assert current["devices"] == []
    assert client.get(BASE).status_code == 404


@pytest.mark.parametrize("changed", ["origin", "video", "mount"])
def test_optical_configuration_change_marks_preserved_artifact_stale(environment, changed):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    previous = client.get(BASE).json()["active"]
    assert previous["stale"] is False and previous["stale_reason"] is None
    image = client.get(previous["image_url"]).content

    async def change():
        def update(settings):
            camera = settings["devices"][0]
            source = camera["sources"][0]
            if changed == "origin":
                source["origin"]["rtsp_url"] = "rtsp://changed.invalid/image"
            elif changed == "video":
                source["video"] = {"width": 1920, "height": 1080}
            else:
                camera.setdefault("metadata", {})["panorama_mount_revision"] = 2
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(change)
    source_artifact = client.get(BASE).json()["active"]
    direct_artifact = client.get(f"/api/cameras/panorama-artifacts/{job['artifact_id']}").json()[
        "artifact"
    ]
    for artifact in (source_artifact, direct_artifact):
        assert artifact["id"] == previous["id"] and artifact["stale"] is True
        assert artifact["stale_reason"] == "source_changed"
    assert client.get(previous["image_url"]).content == image and fake.moves == 1


def test_label_and_crop_edits_do_not_mark_artifact_stale(environment):
    client, _, service, _ = environment
    wait_done(client, create(client))
    artifact = client.get(BASE).json()["active"]

    async def rename():
        def update(settings):
            settings["devices"][0]["name"] = "A new camera label"
            settings["devices"][0]["sources"][0]["name"] = "A new source label"
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(rename)
    response = client.patch(
        BASE + "/crop",
        json={
            "artifact_id": artifact["id"],
            "expected_revision": 1,
            "crop": {"u_start": 0.9, "u_width": 0.2, "v_start": 0, "v_height": 1},
        },
    )
    assert response.status_code == 200 and response.json()["artifact"]["stale"] is False
    assert client.get(BASE).json()["active"]["stale"] is False


def test_removed_source_keeps_historical_artifact_readable_but_stale(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    artifact = client.get(BASE).json()["active"]
    client.portal.call(
        service.store.patch_extension_settings, "com.toposync.cameras", {"devices": []}
    )
    historical = client.get(f"/api/cameras/panorama-artifacts/{job['artifact_id']}")
    assert historical.status_code == 200 and historical.json()["artifact"]["stale"] is True
    assert historical.json()["artifact"]["stale_reason"] == "source_unavailable"
    assert client.get(artifact["image_url"]).status_code == 200


def test_default_worker_timeout_terminates_process_and_preserves_captures(environment, monkeypatch):
    import os
    import toposync_ext_cameras.source_panorama as module

    client, fake, service, _ = environment
    monkeypatch.setattr(module, "_reconstruction_process", _slow_reconstruction)
    service.reconstruct_runner = None
    service.reconstruction_timeout_seconds = 1.5
    job = wait_done(client, create(client), timeout=5)
    assert job["status"] == "failed" and job["error"]["code"] == "reconstruction_timeout"
    assert job["captures_accepted"] == 2 and job["physical_state"] == "restored"
    directory = service._job_directory(job)
    assert (directory / "capture-000.jpg").is_file() and (directory / "capture-001.jpg").is_file()
    process_id = int(next(directory.glob("reconstruction-*/worker.pid")).read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert fake.moves == 1 and fake.returns == 1


def test_capture_timeouts_describe_missing_evidence_without_guessing_a_cause(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    records = [
        {"code": "frame_acquisition_timeout", "received_frames": 138},
        {"code": "stability_timeout"},
        {"code": "stop_observation_unconfirmed"},
        {"code": "unclassified_capture_failure", "reason": "private diagnosis"},
    ]
    service.jobs[job["id"]]["issues"] = records
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["issues"] == [
        "Uma imagem não pôde ser confirmada dentro do tempo disponível.",
        "Não foi possível confirmar a estabilidade de algumas imagens a tempo.",
        "As imagens não permitiram confirmar a parada da câmera.",
        "Não foi possível confirmar uma etapa da captura.",
    ]
    assert all(record["code"] not in " ".join(visible["issues"]) for record in records)
    assert service.jobs[job["id"]]["issues"] == records
    assert "não recebemos" not in " ".join(visible["issues"]).lower()
    assert "vento" not in " ".join(visible["issues"]).lower()


def test_last_motion_telemetry_is_bounded_and_excludes_private_diagnostics(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    telemetry = {
        "kind": "movement",
        "outcome": "timeout",
        "timing_basis": "media",
        "analysis_width": 960,
        "command_accepted_seconds": 0.2,
        "first_motion_transition_seconds": 0.3,
        "stop_requested_seconds": 0.8,
        "stop_accepted_seconds": 0.9,
        "path": "/private/capture.jpg",
        "code": "internal_failure_code",
        "url": "rtsp://private:secret@private-host",
        "samples": [
            {
                "elapsed_seconds": index / 20,
                "media_time": 10 + index / 20,
                "motion_pixels": 10 / (index + 1),
                "speed_px_s": 20 / (index + 1),
                "drift_pixels": 0.2,
                "confidence": 0.7,
                "state": "observing",
                "pose": {"pan": 0.2, "tilt": None, "native_pan": 1500, "password": "hidden"},
                "code": "private_frame_code",
                "image_path": "/private/frame.jpg",
            }
            for index in range(200)
        ],
    }
    client.portal.call(service._progress, service.jobs[job["id"]], {"telemetry": telemetry})
    response = client.get(f"{JOBS}/{job['id']}")
    visible = response.json()["job"]["telemetry"]
    assert len(visible["samples"]) == 128
    assert visible["samples"][0]["elapsed_seconds"] == 0
    assert visible["samples"][-1]["elapsed_seconds"] == 199 / 20
    assert visible["samples"][0]["speed_px_s"] == 20
    assert visible["samples"][0]["pose"]["tilt"] is None
    assert visible["stop_accepted_seconds"] == 0.9 and visible["timing_basis"] == "media"
    assert (
        "secret" not in response.text
        and "private" not in response.text
        and "internal_failure" not in response.text
    )
    assert fake.moves == 1
    replacement = {**telemetry, "outcome": "accepted", "samples": telemetry["samples"][-2:]}
    client.portal.call(service._progress, service.jobs[job["id"]], {"telemetry": replacement})
    latest = client.get(BASE).json()["job"]["telemetry"]
    assert latest["outcome"] == "accepted" and len(latest["samples"]) == 2
    persisted = service._read(service._job_directory(job) / "job.json")
    assert persisted["_telemetry"] == latest


def test_local_observation_telemetry_does_not_claim_media_speed_or_fill_unknown_pose(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    telemetry = {
        "kind": "movement",
        "timing_basis": "local_observation",
        "outcome": "inconclusive",
        "analysis_width": 960,
        "stop_requested_seconds": float("nan"),
        "samples": [
            {
                "elapsed_seconds": 1.0,
                "motion_pixels": 0.1,
                "speed_px_s": 99.0,
                "media_time": None,
                "drift_pixels": float("inf"),
                "confidence": 2,
                "state": "private_diagnostic_code",
                "pose": {"pan": None, "tilt": True, "native_pan": None},
            },
            {"elapsed_seconds": 0.5, "motion_pixels": 1},
            {"elapsed_seconds": float("inf"), "motion_pixels": 0},
        ],
    }
    client.portal.call(service._progress, service.jobs[job["id"]], {"telemetry": telemetry})
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]["telemetry"]
    assert len(visible["samples"]) == 1
    sample = visible["samples"][0]
    assert sample["speed_px_s"] is None and sample["media_time"] is None
    assert sample["confidence"] is None and sample["drift_pixels"] is None
    assert sample["pose"]["pan"] is None and sample["pose"]["tilt"] is None
    assert sample["state"] == "unknown" and visible["stop_requested_seconds"] is None


@pytest.mark.parametrize(("count", "can_resume"), [(255, True), (256, False)])
def test_resume_requires_remaining_capture_budget_without_disabling_return(
    environment, count, can_resume
):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    captures = [
        {"id": f"capture-{index:03}", "path": stored["_captures"][0]["path"]}
        for index in range(count)
    ]
    stored.update(
        status="partial",
        phase="complete",
        physical_state="stopped",
        can_resume=True,
        captures_accepted=count,
        _captures=captures,
    )
    stored["_checkpoint"]["captures"] = captures
    service._save(stored)
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["can_resume"] is can_resume
    assert visible["can_reconstruct"] is True
    assert visible["can_return"] is True
    assert client.get(BASE).json()["job"]["can_resume"] is can_resume
    assert service._has_return_reference(stored)
    # Reading an older persisted True recomputes eligibility without a migration
    # that rewrites a live job or changes its independent return reference.
    assert stored["can_resume"] is True
    assert service._read(service._job_directory(stored) / "job.json")["can_resume"] is True
    if not can_resume:
        response = client.post(f"{JOBS}/{job['id']}/resume")
        assert (
            response.status_code == 409
            and response.json()["detail"]["code"] == "resume_unavailable"
        )
    assert fake.moves == 1


@pytest.mark.parametrize("cursor", [None, [], {"version": 1}, {"version": 2}, {"version": 3}])
def test_legacy_continuous_job_remains_readable_reconstructable_and_returnable(environment, cursor):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    stored.update(status="partial", physical_state="stopped", can_resume=True)
    stored["_checkpoint"].update(mode="continuous", continuous_cursor=cursor)
    service._save(stored)
    manifest_path = service._job_directory(stored) / "job.json"
    manifest = manifest_path.read_bytes()
    checkpoint = copy.deepcopy(stored["_checkpoint"])
    motion_before = (fake.calls, fake.moves, fake.returns, fake.stops)

    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert not visible["can_resume"]
    assert visible["resume_unavailable_code"] == "continuous_resume_unavailable"
    assert visible["can_reconstruct"] and visible["can_return"]
    assert any("percurso anterior" in issue for issue in visible["issues"])
    assert "continuous_resume_unavailable" in visible["issue_codes"]
    source = client.get(BASE).json()
    assert source["active"]["id"] == job["artifact_id"]
    assert client.get(source["active"]["image_url"]).status_code == 200
    response = client.post(f"{JOBS}/{job['id']}/resume")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "continuous_resume_unavailable"
    assert (fake.calls, fake.moves, fake.returns, fake.stops) == motion_before
    assert manifest_path.read_bytes() == manifest

    assert client.post(f"{JOBS}/{job['id']}/reconstruct").status_code == 200
    rebuilt = wait_done(client, job)
    assert rebuilt["status"] == "ready" and not rebuilt["can_resume"]
    assert (fake.calls, fake.moves, fake.returns, fake.stops) == motion_before
    assert stored["_checkpoint"] == checkpoint
    assert client.post(f"{JOBS}/{job['id']}/return").status_code == 200
    returned = wait_done(client, job)
    assert returned["physical_state"] == "restored" and not returned["can_resume"]
    assert fake.moves == motion_before[1] and fake.returns == motion_before[2] + 1
    assert stored["_checkpoint"] == checkpoint


def continuous_checkpoint() -> dict[str, Any]:
    return {
        "mode": "continuous",
        "active_seconds": 10.5,
        "captures": [
            {
                "id": "one",
                "row_index": 0,
                "path": "/private/one.jpg",
                "quality": {"stable": True},
            },
            {
                "id": "two",
                "row_index": -1,
                "path": "/private/two.jpg",
                "quality": {"stable": True},
            },
        ],
        "continuous_cursor": {
            "version": 4,
            "stage": "pan",
            "anchor": {"capture_id": "two", "row": -1, "path": "/private/two.jpg"},
            "recovery_attempts": {"reference:0": 1},
            "reference_path": "/private/one.jpg",
            "reference_destination": {
                "kind": "absolute",
                "role": "work",
                "pan": 0.2,
                "tilt": -0.4,
                "binding": {"source_identity": "private-camera-profile"},
                "capture_id": "one",
                "path": "/private/one.jpg",
            },
        },
    }


def continuous_pending_seek_checkpoint() -> dict[str, Any]:
    checkpoint = continuous_checkpoint()
    cursor = checkpoint["continuous_cursor"]
    cursor.update(
        row=-1,
        direction=1,
        branch=-1,
        bands={"-1": {"complete": False, "edges": {}, "origin": "center"}},
    )
    cursor["seek"] = {
        "axis": "pan",
        "direction": 1,
        "row": -1,
        "origin_path": "/private/two.jpg",
        "progress": True,
        "steps": 2,
        "stationary_count": 0,
        "origin_excursion": False,
        "duration": 0.6,
        "command_failures": 0,
    }
    cursor["transition"] = {
        "state": "pending",
        "stage": "pan",
        "row": -1,
        "direction": 1,
        "branch": -1,
        "intent": {
            "type": "seek_pulse",
            "axis": "pan",
            "direction": 1,
            "row": -1,
            "step": 2,
            "duration": 0.6,
            "anchor": dict(cursor["anchor"]),
        },
        "baseline": {
            "path": "/private/pending.jpg",
            "sha256": "a" * 64,
            "capture_instance": "source-camera",
            "sequence": 30,
            "generation": 4,
        },
    }
    return checkpoint


def test_continuous_pending_seek_requires_exact_durable_identity():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_pending_seek_checkpoint()
    assert SourcePanoramaService._can_resume({"_checkpoint": checkpoint})

    invalid = copy.deepcopy(checkpoint)
    invalid["continuous_cursor"]["transition"].pop("baseline")
    assert not SourcePanoramaService._can_resume({"_checkpoint": invalid})

    invalid = copy.deepcopy(checkpoint)
    invalid["continuous_cursor"]["transition"]["intent"]["direction"] = -1
    assert not SourcePanoramaService._can_resume({"_checkpoint": invalid})

    invalid = copy.deepcopy(checkpoint)
    invalid["continuous_cursor"]["seek"]["steps"] = -1
    assert not SourcePanoramaService._can_resume({"_checkpoint": invalid})


@pytest.mark.parametrize("correction_state", ["pending", "observed", "failed"])
def test_visual_band_return_is_resumable_without_a_device_return_destination(correction_state):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    captures = checkpoint["captures"]
    captures[0].update(row_index=0)
    captures[1].update(
        row_index=0,
        movement={
            "type": "seek_pulse",
            "axis": "pan",
            "direction": 1,
            "duration": 0.6,
            "anchor_capture_id": "one",
        },
    )
    cursor = checkpoint["continuous_cursor"]
    cursor.update(
        row=0,
        direction=-1,
        branch=-1,
        anchor={"capture_id": "two", "row": 0, "path": "/private/two.jpg"},
        bands={"0": {"complete": True, "edges": {"-1": "limit", "1": "limit"}}},
        visual_band_return={
            "version": 1,
            "state": "active",
            "row": 0,
            "capture_ids": ["two", "one"],
            "current_index": 0,
            "origin_capture_id": "one",
            "origin_path": "/private/one.jpg",
            "after_stage": "step",
            "after_direction": -1,
            "after_branch": -1,
            "commands": 0,
            "corrections": 0,
        },
    )
    cursor.pop("reference_destination")

    assert SourcePanoramaService._resume_unavailable_code(
        {"_checkpoint": checkpoint, "physical_state": "unknown"}
    ) is None

    invalid = copy.deepcopy(checkpoint)
    invalid["continuous_cursor"]["visual_band_return"]["capture_ids"].reverse()
    assert SourcePanoramaService._resume_unavailable_code(
        {"_checkpoint": invalid, "physical_state": "unknown"}
    ) == "continuous_resume_unavailable"

    interrupted = copy.deepcopy(checkpoint)
    interrupted_cursor = interrupted["continuous_cursor"]
    interrupted_cursor["resume_anchor_verified"] = True
    interrupted_cursor["visual_band_return"].update(
        state="correcting", commands=2, corrections=1,
        correction={"target_capture_id": "one", "state": correction_state,
                    "direction": -1, "duration": 0.3},
    )
    assert SourcePanoramaService._resume_unavailable_code(
        {"_checkpoint": interrupted, "physical_state": "stopped"}
    ) == "relocalization_required"


def _connection_recovery_record(cursor: dict[str, Any], *, state: str) -> dict[str, Any]:
    original_step = 1 if state == "complete" else cursor["seek"]["steps"]
    record = {
        "version": 1,
        "state": state,
        "attempt": 1,
        "identity": {
            "axis": "pan",
            "direction": 1,
            "row": -1,
            "stage": "pan",
            "branch": -1,
            "step": original_step,
            "anchor": dict(cursor["anchor"]),
        },
        "target": {"pan": 0.2, "tilt": -0.4},
        "failed_duration": 1.2,
        "retry_duration": 0.6,
        "failure_code": "correspondences_not_distributed",
    }
    if state == "complete":
        record.update(
            retry_step=2,
            stationary=False,
            capture_id="three",
            capture_path="/private/three.jpg",
            recovered_connection={"verified": True, "overlap": 0.75},
        )
    return record


def test_connection_recovery_resume_gate_blocks_incomplete_or_tampered_work():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    incomplete = continuous_pending_seek_checkpoint()
    incomplete_cursor = incomplete["continuous_cursor"]
    incomplete_cursor["seek"]["subdivided_steps"] = [2]
    incomplete_cursor["connection_recovery"] = _connection_recovery_record(
        incomplete_cursor, state="return_pending"
    )
    incomplete_cursor["transition"]["intent"]["type"] = "connection_anchor_return"
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": incomplete})
        == "continuous_resume_unavailable"
    )

    tampered = continuous_pending_seek_checkpoint()
    tampered["continuous_cursor"]["seek"]["subdivided_steps"] = [3]
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": tampered})
        == "continuous_resume_unavailable"
    )

    complete = continuous_pending_seek_checkpoint()
    complete_cursor = complete["continuous_cursor"]
    complete["captures"][1]["pose"] = {"pan": 0.2, "tilt": -0.4}
    complete["captures"].append(
        {
            "id": "three",
            "row_index": -1,
            "path": "/private/three.jpg",
            "quality": {"stable": True},
        }
    )
    complete_cursor["seek"]["subdivided_steps"] = [1]
    complete_cursor["transition"]["state"] = "confirmed"
    complete_cursor["connection_recovery"] = _connection_recovery_record(
        complete_cursor, state="complete"
    )
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": complete}) is None

    wrong_target = copy.deepcopy(complete)
    wrong_target["continuous_cursor"]["connection_recovery"]["target"]["pan"] = 0.3
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": wrong_target})
        == "continuous_resume_unavailable"
    )

    wrong_capture = copy.deepcopy(complete)
    wrong_capture["continuous_cursor"]["connection_recovery"]["capture_path"] = (
        "/private/missing.jpg"
    )
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": wrong_capture})
        == "continuous_resume_unavailable"
    )

    impossible_stationary = copy.deepcopy(complete)
    impossible_recovery = impossible_stationary["continuous_cursor"][
        "connection_recovery"
    ]
    impossible_recovery["stationary"] = True
    impossible_recovery.pop("capture_id")
    impossible_recovery.pop("capture_path")
    assert (
        SourcePanoramaService._resume_unavailable_code(
            {"_checkpoint": impossible_stationary}
        )
        == "continuous_resume_unavailable"
    )

    malformed_movement = copy.deepcopy(complete)
    malformed_movement["continuous_cursor"]["transition"]["movement_id"] = (
        "not-a-movement-id"
    )
    assert (
        SourcePanoramaService._resume_unavailable_code(
            {"_checkpoint": malformed_movement}
        )
        == "continuous_resume_unavailable"
    )


def test_continuous_pending_limit_probe_is_never_offered_for_resume():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_pending_seek_checkpoint()
    cursor = checkpoint["continuous_cursor"]
    cursor["transition"]["state"] = "accepted"
    cursor["limit_probe_intent"] = {
        "type": "limit_probe",
        "state": "pending",
        "stage": "pan",
        "axis": "pan",
        "direction": -1,
        "row": -1,
        "step": 2,
        "duration": 0.3,
        "anchor": dict(cursor["anchor"]),
    }

    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "continuous_resume_unavailable"
    )


@pytest.mark.parametrize(
    ("transition_state", "command_failures", "recovery_level", "duration"),
    [
        ("effect_or_state_ambiguous", 0, 0, 0.6),
        ("recovery_exhausted", 0, 0, 0.6),
        ("pending", 1, 0, 0.6),
        ("pending", 0, 2, 0.6),
        ("pending", 0, 0, 0.12),
    ],
)
def test_continuous_pending_seek_retry_budget_cannot_be_renewed(
    transition_state, command_failures, recovery_level, duration
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_pending_seek_checkpoint()
    cursor = checkpoint["continuous_cursor"]
    cursor["transition"]["state"] = transition_state
    cursor["seek"].update(command_failures=command_failures, duration=duration)
    cursor["transition"]["intent"]["duration"] = duration
    checkpoint["seek_recovery_level"] = {"pan": recovery_level}
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


def test_continuous_pending_return_requires_atomic_transition():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    cursor = checkpoint["continuous_cursor"]
    cursor.update(
        stage="return_reference",
        row=0,
        direction=1,
        branch=-1,
        bands={"0": {"complete": False, "edges": {}, "origin": "center"}},
        after_reference_stage="step",
        after_reference_direction=-1,
    )
    cursor["recovery"] = {
        "destination": "step:0:-1",
        "state": "pending",
        "row": 0,
        "branch": -1,
        "direction": 1,
        "action": "step",
    }
    cursor["transition"] = {
        "state": "pending",
        "stage": "return_reference",
        "row": 0,
        "direction": 1,
        "branch": -1,
    }
    assert SourcePanoramaService._can_resume({"_checkpoint": checkpoint})

    cursor.pop("transition")
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


def test_continuous_fallback_transition_is_resumable_before_cursor_creation():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = {
        "mode": "continuous_fallback_pending",
        "active_seconds": 10.5,
        "captures": [
            {
                "id": "one",
                "row_index": -1,
                "path": "/private/one.jpg",
                "quality": {"stable": True},
            }
        ],
        "absolute_grid_fallback": {
            "state": "active",
            "reason": "pilot_cycle_unconfirmed",
            "from": "absolute",
            "to": "continuous",
            "continuous_mode": "velocity",
            "absolute_plan_published": False,
        },
    }
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None

    checkpoint["absolute_grid_fallback"]["continuous_mode"] = "relative"
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None

    checkpoint["absolute_grid_fallback"]["continuous_mode"] = "unknown"
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "continuous_resume_unavailable"
    )


def _preplan_v3_sample(
    axis: str, direction: int, device_amplitude: float, pixel_amplitude: float
) -> dict:
    device_delta = direction * device_amplitude
    shift_x = direction * pixel_amplitude if axis == "pan" else 0.0
    shift_y = direction * pixel_amplitude if axis == "tilt" else 0.0
    gain_vector = [shift_x / device_delta, shift_y / device_delta]
    return {
        "verified": True,
        "cycle_closed": True,
        "device_delta": device_delta,
        "overlap": (
            (960 - abs(shift_x)) * (540 - abs(shift_y)) / (960 * 540)
        ),
        "homography": [
            [1.0, 0.0, shift_x],
            [0.0, 1.0, shift_y],
            [0.0, 0.0, 1.0],
        ],
        "analysis_size": [960, 540],
        "kind": "pilot_outward" if direction > 0 else "pilot_return",
        "pilot_cycle_id": f"{axis}:1",
        "image_delta": [shift_x, shift_y],
        "gain_vector": gain_vector,
        "motion_gain": abs(pixel_amplitude / device_amplitude),
    }


def _nested_preplan_observation(sample: dict) -> dict:
    return {
        key: copy.deepcopy(value)
        for key, value in sample.items()
        if key not in {"cycle_closed", "pilot_cycle_id"}
    }


@pytest.mark.parametrize(
    "state",
    ["planned", "outward_observed", "stationary_no_op", "cycle_unconfirmed"],
)
def test_absolute_preplan_does_not_offer_resume_for_nonreusable_pilot_state(state):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [
            {"id": "one", "path": "/private/one.jpg", "quality": {"stable": True}},
            {"id": "two", "path": "/private/two.jpg", "quality": {"stable": True}},
        ],
        "pilot_attempts": [{"axis": "pan", "state": state}],
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


def test_absolute_preplan_offers_resume_for_structurally_reusable_closed_pilot():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    samples = [
        _preplan_v3_sample("pan", direction, 0.05, 10.0)
        for direction in (-1, 1)
    ]
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [
            {"id": "one", "path": "/private/one.jpg", "quality": {"stable": True}},
            {"id": "two", "path": "/private/two.jpg", "quality": {"stable": True}},
        ],
        "pilot_attempts": [
            {
                "axis": "pan",
                "state": "cycle_closed",
                "cycle_id": "pan:1",
                "outward_observation": _nested_preplan_observation(samples[1]),
                "return_observation": _nested_preplan_observation(samples[0]),
            }
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None

    original_samples = copy.deepcopy(samples)
    checkpoint["pilot_attempts"][0].pop("outward_observation")
    checkpoint["pilot_attempts"][0].pop("return_observation")
    for sample in checkpoint["pilot_observations"]["pan"]:
        sample["device_delta"] = 1e-5 if sample["device_delta"] > 0 else -1e-5
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )
    checkpoint["pilot_observations"]["pan"] = original_samples
    checkpoint.pop("absolute_pilot_limits")
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


@pytest.mark.parametrize(
    "missing_field",
    ["kind", "pilot_cycle_id", "image_delta", "gain_vector", "motion_gain"],
)
def test_absolute_preplan_rejects_sample_missing_grid_v3_field(missing_field: str):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    samples = [
        _preplan_v3_sample("pan", direction, 0.05, 10.0)
        for direction in (-1, 1)
    ]
    samples[0].pop(missing_field)
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": "one"}, {"id": "two"}],
        "pilot_attempts": [
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:1"}
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


@pytest.mark.parametrize("nested_mutation", ["wrong_kind", "missing_motion_gain"])
def test_absolute_preplan_rejects_invalid_nested_cycle_observation(nested_mutation: str):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    samples = [
        _preplan_v3_sample("pan", direction, 0.05, 10.0)
        for direction in (-1, 1)
    ]
    nested = _nested_preplan_observation(samples[1])
    if nested_mutation == "wrong_kind":
        nested["kind"] = "junk"
    else:
        nested.pop("motion_gain")
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": "one"}, {"id": "two"}],
        "pilot_attempts": [
            {
                "axis": "pan",
                "state": "cycle_closed",
                "cycle_id": "pan:1",
                "outward_observation": nested,
            }
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


def test_absolute_preplan_requires_two_slots_for_unpiloted_moving_axis():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    samples = [
        _preplan_v3_sample("pan", direction, 0.05, 10.0)
        for direction in (-1, 1)
    ]
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": f"capture-{index}"} for index in range(255)],
        "pilot_attempts": [
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:1"}
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


def test_absolute_preplan_with_captures_but_no_durable_intent_is_not_resumable():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [{"id": f"legacy-{index}"} for index in range(255)],
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


def test_finished_coverage_budget_failure_is_not_offered_for_resume_but_keeps_rebuild():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    job = {
        "status": "partial",
        "issues": [{"code": "coverage_exceeds_capture_budget"}],
        "_checkpoint": checkpoint,
        "_captures": checkpoint["captures"],
    }
    assert (
        SourcePanoramaService._resume_unavailable_code(job)
        == "coverage_exceeds_capture_budget"
    )
    assert SourcePanoramaService._can_reconstruct(job)


def test_absolute_preplan_rejects_combined_grid_beyond_persisted_remaining_budget():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    def samples(axis: str) -> list[dict]:
        return [
            _preplan_v3_sample(axis, direction, 0.01, 2.0)
            for direction in (-1, 1)
        ]

    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [
            {"id": f"capture-{index}", "quality": {"stable": True}}
            for index in range(4)
        ],
        "pilot_attempts": [
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:1"},
            {"axis": "tilt", "state": "cycle_closed", "cycle_id": "tilt:1"},
        ],
        "pilot_observations": {"pan": samples("pan"), "tilt": samples("tilt")},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 2,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )

    checkpoint["pilot_attempts"] = checkpoint["pilot_attempts"][:1]
    checkpoint["pilot_observations"].pop("tilt")
    checkpoint["grid_pilot_budget"]["cycles_planned"] = 1
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None


def test_absolute_preplan_counts_fixed_axis_against_remaining_capture_budget():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    samples = [
        _preplan_v3_sample("pan", direction, 0.001575, 1.0)
        for direction in (-1, 1)
    ]
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [
            {"id": f"capture-{index}", "quality": {"stable": True}}
            for index in range(2)
        ],
        "pilot_attempts": [
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:1"}
        ],
        "pilot_observations": {"pan": samples},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": 0.0, "max": 0.0, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 1,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


@pytest.mark.parametrize("invalid_evidence", ["singular_homography", "inconsistent_overlap"])
def test_absolute_preplan_rejects_any_invalid_closed_sample_among_valid_evidence(
    invalid_evidence: str,
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    valid = [
        _preplan_v3_sample("pan", direction, 0.05, 10.0)
        for direction in (-1, 1)
    ]
    invalid = copy.deepcopy(valid[0])
    invalid["pilot_cycle_id"] = "pan:2"
    if invalid_evidence == "singular_homography":
        invalid["homography"][2][2] = 0.0
    else:
        invalid["overlap"] = 0.8
    checkpoint = {
        "mode": "absolute",
        "active_seconds": 10.5,
        "captures": [
            {"id": "one", "path": "/private/one.jpg", "quality": {"stable": True}},
            {"id": "two", "path": "/private/two.jpg", "quality": {"stable": True}},
        ],
        "pilot_attempts": [
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:1"},
            {"axis": "pan", "state": "cycle_closed", "cycle_id": "pan:2"},
        ],
        "pilot_observations": {"pan": [*valid, invalid]},
        "absolute_pilot_limits": {
            "pan": {"min": -0.2, "max": 0.2, "space": None},
            "tilt": {"min": -0.1, "max": 0.1, "space": None},
        },
        "grid_pilot_budget": {
            "maximum_cycles_per_axis": 3,
            "captures_per_cycle": 2,
            "cycles_planned": 2,
        },
    }
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "pilot_resume_unavailable"
    )


def test_new_continuous_reference_cursor_is_resumable_before_first_movement():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = {
        "mode": "continuous",
        "active_seconds": 10.5,
        "captures": [
            {
                "id": "one",
                "row_index": -1,
                "path": "/private/one.jpg",
                "quality": {"stable": True},
            }
        ],
        "continuous_cursor": {
            "version": 4,
            "stage": "reference",
            "row": 0,
            "direction": -1,
            "branch": -1,
            "bands": {"0": {"complete": False, "edges": {}, "origin": "center"}},
            "finished_branches": [],
            "horizontal_duration": 0.3,
            "recovery_attempts": {},
        },
    }
    assert SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint}) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", 3),
        ("version", 4.0),
        ("active_seconds", None),
        ("active_seconds", -1),
        ("active_seconds", True),
        ("active_seconds", float("nan")),
        ("active_seconds", float("inf")),
        ("active_seconds", 1200),
        ("recovery_attempts", None),
        ("recovery_attempts", {"reference:0": True}),
        ("recovery_attempts", {"reference:0": -1}),
        ("recovery_attempts", {"reference:0": 2}),
        ("anchor", None),
        ("anchor", {"capture_id": "two"}),
        ("anchor", {"capture_id": "missing", "row": -1}),
        ("anchor", {"capture_id": "two", "row": 0}),
        ("anchor", {"capture_id": "two", "row": -1.0}),
        ("anchor", {"capture_id": "two", "row": -1, "path": "/private/other.jpg"}),
        ("stage", "done"),
        ("stage", "reference"),
        ("stage", []),
    ],
)
def test_continuous_resume_requires_current_anchor_and_consumed_budget(field, value):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    target = checkpoint if field == "active_seconds" else checkpoint["continuous_cursor"]
    target[field] = value
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


@pytest.mark.parametrize("active_seconds", [0, 10.5, 1199.5])
def test_current_continuous_resume_preserves_valid_budget_and_anchor(active_seconds):
    from toposync_ext_cameras.panorama_scan import MAX_JOB_SECONDS
    from toposync_ext_cameras.source_panorama import SourcePanoramaService, _MAX_JOB_SECONDS

    assert _MAX_JOB_SECONDS == MAX_JOB_SECONDS
    checkpoint = continuous_checkpoint()
    checkpoint["active_seconds"] = active_seconds
    assert SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    checkpoint["captures"][1].pop("row_index")
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


@pytest.mark.parametrize(("finished", "expected"), [([-1, 1], False), ([-1], True), ([], True)])
def test_continuous_resume_has_an_unfinished_vertical_branch(finished, expected):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    checkpoint["continuous_cursor"].update(stage="step", finished_branches=finished)
    assert SourcePanoramaService._can_resume({"_checkpoint": checkpoint}) is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "original"),
        ("kind", "continuous"),
        ("kind", []),
        ("binding", None),
        ("binding", {}),
        ("capture_id", "two"),
        ("path", "/private/original.jpg"),
        ("pan", True),
        ("tilt", float("nan")),
        ("pan", None),
    ],
)
def test_continuous_resume_rejects_unbound_working_destination(field, value):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    checkpoint["continuous_cursor"]["reference_destination"][field] = value
    assert (
        SourcePanoramaService._resume_unavailable_code({"_checkpoint": checkpoint})
        == "relocalization_required"
    )


def test_continuous_resume_preset_needs_owned_token_and_qualified_reference():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    destination = checkpoint["continuous_cursor"]["reference_destination"]
    destination.update(
        kind="preset",
        preset_token="private-token",
        preset_name="temporary-work",
        owner_id="private-owner",
    )
    destination.pop("pan")
    destination.pop("tilt")
    assert SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    destination.pop("preset_token")
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})
    destination["preset_token"] = "private-token"
    checkpoint["captures"][0]["quality"]["stable"] = False
    assert not SourcePanoramaService._can_resume({"_checkpoint": checkpoint})


@pytest.mark.parametrize(
    ("verified", "physical_state", "expected"),
    [
        (True, "stopped", True),
        (None, "stopped", False),
        (False, "stopped", False),
        (True, "restored", False),
        (True, "unknown", False),
        (True, "stop_unconfirmed", False),
        (1, "stopped", False),
    ],
)
def test_continuous_resume_without_destination_needs_intentional_verified_stop(
    verified, physical_state, expected
):
    from toposync_ext_cameras.source_panorama import SourcePanoramaService

    checkpoint = continuous_checkpoint()
    cursor = checkpoint["continuous_cursor"]
    cursor.pop("reference_destination")
    cursor["resume_anchor_verified"] = verified
    assert (
        SourcePanoramaService._can_resume(
            {"_checkpoint": checkpoint, "physical_state": physical_state}
        )
        is expected
    )


def test_failed_relocalization_disables_repeat_candidate_without_camera_call(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    checkpoint = continuous_checkpoint()
    checkpoint.update({key: stored["_checkpoint"][key] for key in ("return", "initial_path")})
    checkpoint["continuous_cursor"].update(
        recovery={"destination": "reference:0", "state": "pending"},
        relocalization_failed=True,
    )
    stored.update(
        status="partial",
        physical_state="stopped",
        can_resume=True,
        _checkpoint=checkpoint,
    )
    service._save(stored)
    original = service._read(service._job_directory(stored) / "job.json")
    motion_before = (fake.calls, fake.moves, fake.returns, fake.stops)
    for _ in range(2):
        visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
        assert not visible["can_resume"]
        assert visible["resume_unavailable_code"] == "relocalization_required"
        assert visible["can_reconstruct"] and visible["can_return"]
        response = client.post(f"{JOBS}/{job['id']}/resume")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "relocalization_required"
    assert (fake.calls, fake.moves, fake.returns, fake.stops) == motion_before
    assert service._read(service._job_directory(stored) / "job.json") == original
    # A later explicit Stop with a newly verified anchor is new evidence.
    checkpoint["continuous_cursor"]["resume_anchor_verified"] = True
    assert service._can_resume(stored)


def test_current_continuous_cursor_is_persisted_without_public_private_paths(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    stored.update(status="partial", physical_state="stopped")
    checkpoint = continuous_checkpoint()
    checkpoint["continuous_cursor"]["recovery"] = {
        "destination": "reference:0",
        "state": "pending",
    }
    client.portal.call(service._progress, stored, {"checkpoint": checkpoint})
    persisted = service._read(service._job_directory(stored) / "job.json")
    assert persisted["_checkpoint"] == checkpoint
    response = client.get(f"{JOBS}/{job['id']}")
    assert response.json()["job"]["can_resume"]
    assert response.json()["job"]["resume_unavailable_code"] is None
    assert "/private/" not in response.text and "continuous_cursor" not in response.text
    assert "private-camera-profile" not in response.text


def test_checkpoint_persists_coordinate_identifiers_without_leaking_transport(environment):
    from toposync_ext_cameras.onvif.client import NORMALIZED_PAN_TILT_SPACES, NORMALIZED_ZOOM_POSITION_SPACE

    client, _, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    identifiers = [*sorted(NORMALIZED_PAN_TILT_SPACES), NORMALIZED_ZOOM_POSITION_SPACE]
    checkpoint = {"return": {"kind": "absolute", "space": identifiers[0], "zoom_space": identifiers[-1]},
                  "coordinate_identifiers": identifiers,
                  "diagnostic_endpoint": "https://private:secret@camera.test/control",
                  "spoofed_identifier": identifiers[0] + "?secret=private"}
    client.portal.call(service._progress, stored, {"checkpoint": checkpoint})
    persisted = service._read(service._job_directory(stored) / "job.json")["_checkpoint"]
    assert persisted["coordinate_identifiers"] == identifiers
    assert persisted["return"] == checkpoint["return"]
    assert persisted["diagnostic_endpoint"] == "[private transport]"
    assert persisted["spoofed_identifier"] == "[private transport]"
    assert "secret" not in json.dumps(persisted)
    assert "coordinate_identifiers" not in client.get(f"{JOBS}/{job['id']}").text


def test_new_continuous_reference_progress_does_not_claim_a_legacy_checkpoint(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    stored.update(
        status="capturing",
        _checkpoint={
            "mode": "continuous",
            "continuous_cursor": {"version": 4, "stage": "reference"},
        },
    )
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert not visible["can_resume"] and visible["issues"] == []


def pending_cleanup(job: dict[str, Any], token: str, *, role: str = "work") -> dict[str, Any]:
    return {
        "kind": "preset",
        "role": role,
        "owner_id": job["id"],
        "preset_token": token,
        "preset_name": f"private-temporary-{token}",
        "binding": {"camera_id": job["camera_id"], "source_id": job["source_id"]},
    }


def test_cleanup_without_original_removes_owned_pending_presets_without_movement(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    owned = pending_cleanup(stored, "private-work")
    foreign = {**pending_cleanup(stored, "user-preset"), "owner_id": "another-job"}
    checkpoint = stored["_checkpoint"]
    checkpoint.update(
        pending_returns=[owned, foreign],
        **{"return": None},
        continuous_cursor={"reference_destination": dict(owned)},
    )
    service._save(stored)
    before = (fake.calls, fake.moves, fake.stops, fake.returns)
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["can_cleanup"] and not visible["can_return"]
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 200
    assert response.json()["cleanup_complete"] and response.json()["cleaned_count"] == 1
    assert not response.json()["job"]["can_cleanup"]
    assert fake.cleanup_events == ["discover", ("remove", "private-work")]
    assert checkpoint["pending_returns"] == [foreign]
    assert "reference_destination" not in checkpoint["continuous_cursor"]
    assert (fake.calls, fake.moves, fake.stops, fake.returns) == before
    assert service._read(service._job_directory(stored) / "job.json")["_checkpoint"] == checkpoint
    assert "private-work" not in response.text and "user-preset" not in response.text
    assert client.post(f"{JOBS}/{job['id']}/cleanup").json()["cleaned_count"] == 0
    assert fake.cleanup_events == ["discover", ("remove", "private-work")]


def test_cleanup_retains_failed_ownership_and_retry_only_reconciles_remaining(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    first = pending_cleanup(stored, "work")
    second = pending_cleanup(stored, "original", role="original")
    stored["_checkpoint"]["pending_returns"] = [first, second]
    fake.cleanup_failures = {"work"}
    service._save(stored)
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 200
    assert not response.json()["cleanup_complete"] and response.json()["cleaned_count"] == 1
    assert response.json()["job"]["can_cleanup"]
    persisted = service._read(service._job_directory(stored) / "job.json")
    assert persisted["_checkpoint"]["pending_returns"] == [first]
    assert "return_cleanup_unconfirmed" in response.json()["job"]["issue_codes"]
    fake.cleanup_failures.clear()
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.json()["cleanup_complete"] and response.json()["cleaned_count"] == 1
    assert "return_cleanup_unconfirmed" not in response.json()["job"]["issue_codes"]
    assert fake.cleanup_events == [
        "discover",
        ("remove", "work"),
        ("remove", "original"),
        "discover",
        ("remove", "work"),
    ]
    assert not stored["_checkpoint"]["pending_returns"]


@pytest.mark.parametrize("physical_state", ["restored", "stopped"])
@pytest.mark.parametrize("duplicate_pending", [False, True])
def test_cleanup_collects_terminal_canonical_presets_without_pending_list(
    environment, physical_state, duplicate_pending
):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    checkpoint = continuous_checkpoint()
    original = pending_cleanup(stored, "original", role="original")
    work = pending_cleanup(stored, "work")
    work.update(capture_id="one", path="/private/one.jpg")
    checkpoint.update(
        **{"return": original},
        initial_path="/private/original.jpg",
        pending_returns=[
            dict(original),
            {key: value for key, value in work.items() if key not in {"capture_id", "path"}},
        ]
        if duplicate_pending
        else [],
    )
    checkpoint["continuous_cursor"].update(
        stage="step",
        finished_branches=[-1, 1],
        reference_destination=work,
    )
    stored.update(
        status="partial",
        physical_state=physical_state,
        can_resume=True,
        _checkpoint=checkpoint,
    )
    service._save(stored)
    response = client.get(f"{JOBS}/{job['id']}")
    assert not response.json()["job"]["can_resume"]
    assert response.json()["job"]["can_cleanup"]
    before = (fake.calls, fake.moves, fake.stops, fake.returns)
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 200 and response.json()["cleanup_complete"]
    expected_tokens = ["original", "work"] if physical_state == "restored" else ["work"]
    assert response.json()["cleaned_count"] == len(expected_tokens)
    assert fake.cleanup_events == ["discover", *[("remove", token) for token in expected_tokens]]
    assert checkpoint["return"] == (None if physical_state == "restored" else original)
    assert "reference_destination" not in checkpoint["continuous_cursor"]
    assert checkpoint["pending_returns"] == (
        [original] if duplicate_pending and physical_state == "stopped" else []
    )
    assert (fake.calls, fake.moves, fake.stops, fake.returns) == before
    assert not response.json()["job"]["can_cleanup"]
    assert service._read(service._job_directory(stored) / "job.json")["_checkpoint"] == checkpoint


def test_cleanup_failed_canonical_removal_preserves_destination_for_explicit_retry(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    work = pending_cleanup(stored, "work")
    stored["_checkpoint"].update(
        pending_returns=[],
        continuous_cursor={"reference_destination": work},
    )
    service._save(stored)
    fake.cleanup_failures.add("work")
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 200 and not response.json()["cleanup_complete"]
    assert response.json()["job"]["can_cleanup"]
    checkpoint = service._read(service._job_directory(stored) / "job.json")["_checkpoint"]
    assert checkpoint["continuous_cursor"]["reference_destination"] == work
    assert checkpoint["pending_returns"] == []
    fake.cleanup_failures.clear()
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.json()["cleanup_complete"] and response.json()["cleaned_count"] == 1
    assert "reference_destination" not in stored["_checkpoint"]["continuous_cursor"]


@pytest.mark.parametrize(
    "denied", ["core:camera:read", "core:camera:control", "core:settings:write"]
)
def test_cleanup_requires_control_and_settings_permissions(environment, denied):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    stored["_checkpoint"]["pending_returns"] = [pending_cleanup(stored, "work")]
    response = client.post(f"{JOBS}/{job['id']}/cleanup", headers={"x-deny": denied})
    assert response.status_code == 403 and not fake.cleanup_events


def test_cleanup_protects_original_still_needed_for_explicit_return(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    original = pending_cleanup(stored, "original", role="original")
    stored.update(status="failed", physical_state="stopped", can_resume=False)
    stored["_checkpoint"].update(**{"return": original}, pending_returns=[original])
    service._save(stored)
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 200 and response.json()["cleaned_count"] == 0
    assert response.json()["job"]["can_return"] and not response.json()["job"]["can_cleanup"]
    assert stored["_checkpoint"]["pending_returns"] == [original] and not fake.cleanup_events


def test_deliberate_return_closure_releases_only_owned_reference_and_preserves_history(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    original = pending_cleanup(stored, "original", role="original")
    stored.update(status="failed", physical_state="stopped", can_resume=False)
    stored["_checkpoint"].update(**{"return": original}, pending_returns=[original])
    stored.setdefault("issues", []).append({"code": "return_framing_unconfirmed"})
    service._save(stored)
    before_moves = fake.moves
    before_captures = copy.deepcopy(stored["_checkpoint"]["captures"])
    response = client.post(f"{JOBS}/{job['id']}/cleanup?abandon_return=true")
    assert response.status_code == 200 and response.json()["cleaned_count"] == 1
    public = response.json()["job"]
    assert public["return_closure"]["decision"] == "deliberately_abandoned"
    assert public["return_closure"]["return_verified"] is False
    assert public["return_closure"]["resources_released"] is True
    assert public["physical_state"] == "stopped" and not public["can_return"]
    assert stored["return_closure"]["references"]["return"] == original
    assert stored["_checkpoint"]["return"] is None
    assert stored["_checkpoint"]["captures"] == before_captures and fake.moves == before_moves
    assert {"code": "return_framing_unconfirmed"} in stored["issues"]
    assert client.post(f"{JOBS}/{job['id']}/cleanup?abandon_return=true").json()["cleaned_count"] == 0


def test_cleanup_discovery_failure_retains_private_ownership_and_closes_without_motion(environment):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    owned = pending_cleanup(stored, "work")
    stored["_checkpoint"]["pending_returns"] = [owned]
    service._save(stored)
    closed_before = fake.closed

    async def unavailable():
        raise RuntimeError("rtsp://private-user:private-password@private-host")

    fake.discover = unavailable
    response = client.post(f"{JOBS}/{job['id']}/cleanup")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "camera_discovery_failed"
    assert "private-" not in response.text
    assert not fake.cleanup_events and fake.closed == closed_before + 1
    assert service._read(service._job_directory(stored) / "job.json")["_checkpoint"][
        "pending_returns"
    ] == [owned]


@pytest.mark.parametrize("busy", ["same_job", "other_source", "resume"])
def test_cleanup_rejects_active_or_resumable_capture_before_discovery(environment, busy):
    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    stored["_checkpoint"]["pending_returns"] = [pending_cleanup(stored, "work")]
    if busy == "same_job":
        stored["status"] = "capturing"
    elif busy == "other_source":
        service.jobs["another-job"] = {
            "id": "another-job",
            "status": "capturing",
            "phase": "capturing",
            "camera_id": stored["camera_id"],
            "source_id": "other-source",
        }
    else:
        stored.update(status="partial", can_resume=True)
    try:
        response = client.post(f"{JOBS}/{job['id']}/cleanup")
        assert response.status_code == 409
        assert (
            response.json()["detail"]["code"]
            == {
                "same_job": "job_busy",
                "other_source": "camera_busy",
                "resume": "resume_available",
            }[busy]
        )
        assert not fake.cleanup_events
    finally:
        service.jobs.pop("another-job", None)
        stored["status"] = "ready"


def test_reconstructs_45_legacy_photographs_without_opening_camera_or_certifying_coverage(
    environment,
):
    import copy
    import hashlib

    client, fake, service, _ = environment
    fake.capture_count = 45

    async def fail_first_assembly():
        raise RuntimeError("simulated first assembly failure")

    fake.before_publish = fail_first_assembly
    job = wait_done(client, create(client))
    assert job["status"] == "failed"
    stored = service.jobs[job["id"]]
    assert stored["_scan_result"]["complete"] is True  # Saved before the failed CPU phase.
    stored.pop("_scan_result")  # A pre-upgrade job only has its checkpoint and originals.
    stored["physical_state"] = "stop_unconfirmed"
    service._save(stored)
    checkpoint = copy.deepcopy(stored["_checkpoint"])
    hashes = {
        item["path"]: hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
        for item in stored["_captures"]
    }
    fake.before_publish = None
    original_counts = (fake.moves, fake.returns, fake.stops, fake.closed)

    def forbidden_camera(**kwargs):
        pytest.fail("Reconstruction must not instantiate a camera")

    async def forbidden_scan(*args, **kwargs):
        pytest.fail("Reconstruction must not run acquisition")

    service.camera_factory = forbidden_camera
    service.scan_runner = forbidden_scan
    service.max_global_bytes = service._bytes(service.root) + service.max_job_bytes // 4
    assert client.get(f"{JOBS}/{job['id']}").json()["job"]["can_reconstruct"]
    response = client.post(
        f"{JOBS}/{job['id']}/reconstruct", headers={"x-deny": "core:camera:control"}
    )
    assert response.status_code == 200 and response.json()["job"]["id"] == job["id"]
    rebuilt = wait_done(client, job)
    assert rebuilt["status"] == "partial" and rebuilt["captures_accepted"] == 45
    assert rebuilt["physical_state"] == "stop_unconfirmed"
    assert (fake.moves, fake.returns, fake.stops, fake.closed) == original_counts
    assert stored["_checkpoint"] == checkpoint
    assert stored["_scan_result"]["complete"] is False
    assert {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in hashes} == hashes
    artifact = client.get(f"/api/cameras/panorama-artifacts/{rebuilt['artifact_id']}").json()[
        "artifact"
    ]
    assert artifact["coverage"]["acquisition_complete"] is False
    assert artifact["coverage"]["acquisition"]["kind"] == "unconfirmed_reachable_domain"


@pytest.mark.parametrize("action", ["core:camera:read", "core:settings:write"])
def test_reconstruction_authorization_precedes_any_work(environment, action):
    client, fake, _, _ = environment
    job = wait_done(client, create(client))
    before = (fake.moves, fake.calls, fake.returns, fake.stops, fake.closed)
    response = client.post(f"{JOBS}/{job['id']}/reconstruct", headers={"x-deny": action})
    assert response.status_code == 403
    assert (fake.moves, fake.calls, fake.returns, fake.stops, fake.closed) == before
    assert client.get(f"{JOBS}/{job['id']}").json()["job"]["status"] == "ready"


def test_reconstruction_with_saved_scan_receipt_preserves_complete_status_and_previous_image(
    environment,
):
    import copy

    client, fake, service, _ = environment
    original = wait_done(client, create(client))
    stored = service.jobs[original["id"]]
    receipt = copy.deepcopy(stored["_scan_result"])
    checkpoint = copy.deepcopy(stored["_checkpoint"])
    first = client.get(BASE).json()["active"]
    image = client.get(first["image_url"]).content
    assert client.post(f"{JOBS}/{original['id']}/reconstruct").status_code == 200
    rebuilt = wait_done(client, original)
    assert rebuilt["status"] == "ready" and rebuilt["artifact_id"] != original["artifact_id"]
    assert stored["_scan_result"] == receipt and stored["_checkpoint"] == checkpoint
    current = client.get(BASE).json()
    assert current["active"]["id"] == original["artifact_id"]
    assert current["candidate"]["id"] == rebuilt["artifact_id"]
    assert current["previous"] is None
    assert rebuilt["candidate_reason"] == "coverage_alignment_unverified"
    assert client.get(first["image_url"]).content == image
    assert fake.moves == 1 and fake.calls == 1 and fake.returns == 1 and fake.stops == 0


def test_reconstruction_cancel_kills_only_worker_and_preserves_physical_state(
    environment, monkeypatch
):
    import copy
    import os
    import toposync_ext_cameras.source_panorama as module

    client, fake, service, _ = environment
    original = wait_done(client, create(client))
    stored = service.jobs[original["id"]]
    checkpoint = copy.deepcopy(stored["_checkpoint"])
    monkeypatch.setattr(module, "_reconstruction_process", _slow_reconstruction)
    service.reconstruct_runner = None
    response = client.post(f"{JOBS}/{original['id']}/reconstruct")
    assert response.status_code == 200
    wait_phase(client, original, "reconstructing")
    deadline = time.monotonic() + 5
    worker_path = None
    while time.monotonic() < deadline:
        worker_path = next(service._job_directory(stored).glob("reconstruction-*/worker.pid"), None)
        if worker_path:
            break
        time.sleep(0.01)
    assert worker_path is not None
    process_id = int(worker_path.read_text())
    stopped_response = client.post(
        f"{JOBS}/{original['id']}/stop", headers={"x-deny": "core:camera:control"}
    )
    assert stopped_response.status_code == 200
    assert stopped_response.json()["job"]["phase"] == "stopping_processing"
    stopped = wait_done(client, original)
    assert stopped["phase"] == "interrupted_processing" and stopped["can_reconstruct"]
    assert stopped["physical_state"] == "restored"
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert stored["_checkpoint"] == checkpoint
    assert fake.moves == 1 and fake.calls == 1 and fake.returns == 1 and fake.stops == 0
    assert client.get(BASE).json()["active"]["id"] == original["artifact_id"]


def test_reconstruction_queue_cancel_does_not_acquire_camera_or_wait_for_cpu(environment):
    client, fake, service, _ = environment
    original = wait_done(client, create(client))
    client.portal.call(service.processing.acquire)
    try:
        response = client.post(f"{JOBS}/{original['id']}/reconstruct")
        assert response.status_code == 200
        assert response.json()["job"]["phase"] == "queued_processing"
        stopped = client.post(
            f"{JOBS}/{original['id']}/stop", headers={"x-deny": "core:camera:control"}
        )
        assert stopped.status_code == 200
        assert wait_done(client, original)["phase"] == "interrupted_processing"
        assert fake.moves == 1 and fake.stops == 0
    finally:
        client.portal.call(service.processing.release)


def test_reconstruction_rejects_missing_originals_and_changed_source_before_processing(
    environment, tmp_path
):
    client, fake, service, _ = environment
    original = wait_done(client, create(client))
    stored = service.jobs[original["id"]]
    original_path = stored["_captures"][0]["path"]
    outside = tmp_path / "unrelated.jpg"
    Image.new("RGB", (32, 32)).save(outside)
    stored["_captures"][0]["path"] = str(outside)
    response = client.post(f"{JOBS}/{original['id']}/reconstruct")
    assert (
        response.status_code == 404 and response.json()["detail"]["code"] == "invalid_panorama_file"
    )
    stored["_captures"][0]["path"] = original_path

    async def change_source():
        def update(settings):
            settings["devices"][0]["sources"][0]["video"] = {"width": 1280, "height": 720}
            return settings

        await service.store.update_extension_settings("com.toposync.cameras", update)

    client.portal.call(change_source)
    response = client.post(f"{JOBS}/{original['id']}/reconstruct")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "source_changed"
    assert fake.moves == 1 and fake.calls == 1


def test_restart_during_cpu_work_preserves_last_physical_evidence(tmp_path):
    _, _, service, _ = make_app(tmp_path)
    job = {
        "id": "d" * 32,
        "camera_id": "simulated",
        "source_id": "main",
        "status": "processing",
        "phase": "reconstructing",
        "_processing_only": True,
        "created_at": time.time(),
        "physical_state": "stop_unconfirmed",
        "captures_accepted": 2,
        "_captures": [{"id": "one"}, {"id": "two"}],
        "_checkpoint": {"captures": [{"id": "one"}, {"id": "two"}]},
    }
    service._save(job)
    app, fake, restarted, _ = make_app(tmp_path)
    with TestClient(app) as client:
        visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
        assert visible["status"] == "interrupted" and visible["phase"] == "interrupted_processing"
        assert visible["physical_state"] == "stop_unconfirmed" and visible["can_reconstruct"]
        assert fake.moves == 0 and fake.stops == 0
        client.portal.call(restarted.shutdown)


def test_cancelled_queued_task_cannot_overwrite_or_unregister_its_replacement(environment):
    from starlette.requests import Request

    client, _, service, _ = environment
    job = wait_done(client, create(client))

    async def scenario():
        request = Request({"type": "http", "headers": []})
        await service.processing.acquire()
        replacement = None
        try:
            await service.reconstruct_job(request, job["id"])
            old_task = service.tasks[job["id"]]
            await asyncio.sleep(0.05)
            assert service.jobs[job["id"]]["phase"] == "queued_processing"
            await service.stop(request, job["id"])
            replacement = asyncio.create_task(asyncio.sleep(60))
            service.tasks[job["id"]] = replacement
            service.jobs[job["id"]].update(status="processing", phase="queued_processing")
            await asyncio.gather(old_task, return_exceptions=True)
            assert service.tasks[job["id"]] is replacement
            assert service.jobs[job["id"]]["status"] == "processing"
            assert service.jobs[job["id"]]["phase"] == "queued_processing"
        finally:
            service.processing.release()
            if replacement is not None:
                replacement.cancel()
                await asyncio.gather(replacement, return_exceptions=True)
            service.tasks.pop(job["id"], None)
            service.jobs[job["id"]].update(status="interrupted", phase="interrupted_processing")

    client.portal.call(scenario)


def test_reconstruction_rejects_a_changed_original_with_a_saved_hash(environment):
    import hashlib

    client, fake, service, _ = environment
    job = wait_done(client, create(client))
    capture = service.jobs[job["id"]]["_captures"][0]
    path = Path(capture["path"])
    capture["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    Image.new("RGB", (32, 16), (200, 0, 0)).save(path)
    response = client.post(f"{JOBS}/{job['id']}/reconstruct")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "capture_integrity_changed"
    assert fake.moves == 1 and fake.calls == 1
    assert client.get(BASE).json()["active"]["id"] == job["artifact_id"]


def test_reconstruction_preserves_return_diagnostics_recorded_after_acquisition(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    stored = service.jobs[job["id"]]
    assert stored["_scan_result"]["issues"] == []
    stored["issues"] = [{"code": "return_framing_unconfirmed", "reason": "movement_unconfirmed"}]
    stored["physical_state"] = "stop_unconfirmed"
    response = client.post(f"{JOBS}/{job['id']}/reconstruct")
    assert response.status_code == 200
    rebuilt = wait_done(client, job)
    assert rebuilt["physical_state"] == "stop_unconfirmed"
    assert "Não conseguimos confirmar o enquadramento inicial da câmera." in rebuilt["issues"]
    assert stored["_scan_result"]["issues"] == []


def test_coverage_progress_exposes_only_confirmed_bands(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    expected = {"primary_complete": True, "bands_completed": 1, "current_band": -1}
    client.portal.call(
        service._progress,
        service.jobs[job["id"]],
        {
            "coverage_progress": {**expected, "path": "/private/frame.jpg"},
        },
    )
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["coverage_progress"] == expected
    client.portal.call(
        service._progress,
        service.jobs[job["id"]],
        {
            "coverage_progress": {**expected, "bands_completed": "100"},
        },
    )
    assert client.get(f"{JOBS}/{job['id']}").json()["job"]["coverage_progress"] == expected


def test_coverage_progress_exposes_bounded_recovery_state(environment):
    client, _, service, _ = environment
    job = wait_done(client, create(client))
    base = {"primary_complete": True, "bands_completed": 2, "current_band": -1}
    expected = {
        **base,
        "stage": "return_reference",
        "regions_pending": 2,
        "continued_after_recovery": True,
    }
    client.portal.call(
        service._progress,
        service.jobs[job["id"]],
        {"coverage_progress": {**expected, "anchor": {"path": "/private/frame.jpg"}}},
    )
    visible = client.get(f"{JOBS}/{job['id']}").json()["job"]
    assert visible["coverage_progress"] == expected
    client.portal.call(
        service._progress,
        service.jobs[job["id"]],
        {
            "coverage_progress": {
                **base,
                "stage": [],
                "regions_pending": True,
                "continued_after_recovery": 1,
            }
        },
    )
    assert client.get(f"{JOBS}/{job['id']}").json()["job"]["coverage_progress"] == base
