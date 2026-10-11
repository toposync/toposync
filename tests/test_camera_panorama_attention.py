"""Software qualification gates and borrowed control, using synthetic evidence only."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from toposync_ext_cameras.panorama import PanoramaService
import toposync_ext_cameras.panorama as module
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
from test_camera_panorama_capture import environment


@asynccontextmanager
async def hold(*args):
    yield


def bound_job():
    return {"id": "job", "revision": 2, "camera_id": "camera", "source_id": "wide",
        "composition_id": "yard", "element_id": "element", "_source_digest": "identity",
        "_composition_digest": "composition", "source_panorama": {"id": "artifact", "revision": 3,
            "geometry": {"model_digest": "geometry"}}, "solution": {}, "stop_confirmed": True}


def qualify(job):
    job["_attention_qualification"] = {"job_id": job["id"], "revision": job["revision"],
        "source_identity": job["_source_digest"], "composition_digest": job["_composition_digest"],
        "artifact_id": "artifact", "artifact_revision": 3, "geometry_digest": "geometry",
        "arrival_verified": True, "return_verified": True}


@pytest.mark.parametrize("change", ["job_id", "revision", "source_identity", "composition_digest", "artifact_id", "artifact_revision", "geometry_digest", "arrival_verified", "return_verified", "integer_boolean"])
def test_physical_gate_requires_exact_current_qualification(change):
    job = bound_job()
    assert not PanoramaService._attention_qualified(job)
    qualify(job)
    assert PanoramaService._attention_qualified(job)
    job["_attention_qualification"]["arrival_verified" if change == "integer_boolean" else change] = 1 if change == "integer_boolean" else None
    assert not PanoramaService._attention_qualified(job)


@pytest.mark.anyio
@pytest.mark.parametrize("change", [None, "qualification", "revision", "owner", "fence", "ready", "final_owner", "stopped", "invalid_binding"])
async def test_attention_service_uses_borrowed_lease_and_strict_home_limit_without_presets(monkeypatch, change):
    job = bound_job()
    qualify(job)
    service = object.__new__(PanoramaService)
    service.jobs = {"job": job}
    service.reference_coordinator = SimpleNamespace(hold=hold)
    service.get_active = AsyncMock(return_value={"job": job.copy()})
    snapshot = {"active_lease": {"lease_id": "attention", "fence": 7, "owner_kind": "automation"},
        "automation_ready": True, "motion_epoch": 4}
    final = {**snapshot, "active_lease": snapshot["active_lease"].copy()}
    service.services = SimpleNamespace(call=AsyncMock(side_effect=[snapshot, final]))
    service._visual_operation_with_pinned_reference = AsyncMock(return_value=({}, {"verified": True}))
    monkeypatch.setattr(module, "map_ray_to_world", lambda *args: (0, 0))
    monkeypatch.setattr(module, "mapping_extrapolated", lambda *args: False)
    destination = {key: job[key] for key in ("camera_id", "source_id", "composition_id", "element_id", "revision")}
    destination.update(job_id="job", ray=[0, 0, 1])
    if change == "qualification":
        job.pop("_attention_qualification")
    if change == "revision":
        destination["revision"] = 3
    if change == "owner":
        snapshot["active_lease"]["owner_kind"] = "manual"
    if change == "fence":
        snapshot["active_lease"]["fence"] = 9
    if change == "ready":
        snapshot["automation_ready"] = False
    if change == "final_owner":
        final["active_lease"]["lease_id"] = "manual"
    if change == "stopped":
        job["stop_confirmed"] = False
    if change == "invalid_binding":
        destination["revision"] = True
    arguments = dict(destination=destination, lease_id="attention", fence=7, command_id="command", returning=True)
    if change:
        with pytest.raises(HTTPException):
            await service.navigate_attention(**arguments)
        if change not in {"final_owner", "stopped"}:
            service._visual_operation_with_pinned_reference.assert_not_awaited()
    else:
        result = await service.navigate_attention(**arguments)
        assert result["destination"] == destination and result["motion_epoch"] == 4
        options = service._visual_operation_with_pinned_reference.await_args.kwargs
        assert options == {"borrowed_control": {"lease_id": "attention", "fence": 7}, "maximum_error_pixels": 3}
        assert all(call.args[0] == "cameras.control.snapshot" and call.kwargs["refresh_physical"] is False for call in service.services.call.await_args_list)


@pytest.mark.anyio
@pytest.mark.parametrize("stolen", [False, True])
async def test_borrowed_camera_never_acquires_renews_or_releases_attention_lease(stolen):
    camera, _, _, services, _ = environment()
    services.active = {"lease_id": "attention", "fence": 7}
    camera.lease = services.active.copy()
    camera._borrowed_control = True
    if stolen:
        services.active["fence"] = 8
    if stolen:
        with pytest.raises(PanoramaCaptureError, match="control_lost"):
            await camera.acquire()
    else:
        assert await camera.acquire() == {"lease_id": "attention", "fence": 7}
        assert camera._renewal is None
    await camera.close()
    assert all(name not in {"cameras.control.acquire", "cameras.control.renew", "cameras.control.release"} for name, _ in services.calls)
