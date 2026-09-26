"""Persistent preparation contracts; simulated tests do not approve hardware."""
from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException

from toposync_ext_cameras.panorama import PanoramaService, PrepareNativeReference
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError


@asynccontextmanager
async def hold(*args, **kwargs):
    yield


def environment(tmp_path):
    service = object.__new__(PanoramaService)
    service.root = tmp_path / "panorama"
    binding = {"id": "a" * 32, "revision": 1, "geometry": {"model_digest": "model-1"},
               "source_identity": {"source": "main"}}
    body = PrepareNativeReference(preparation_id="b" * 32, source_id="main", artifact_id=binding["id"], revision=1)
    sources = SimpleNamespace(_authorize=lambda *args, **kwargs: None,
                              _context=AsyncMock(return_value=({}, {}, {})))
    service.app = SimpleNamespace(state=SimpleNamespace(camera_source_panorama=sources))
    service._source_artifact = lambda *args: (binding, {})
    service.reference_coordinator = SimpleNamespace(hold=hold)
    service.reference_localizer = AsyncMock(return_value=None)
    service.services = None
    service._native_preparations = {}
    service.cancelled = set()
    path = tmp_path / "source-panorama" / "artifacts" / binding["id"] / "native-references" / body.preparation_id / "reference.json"
    return service, binding, body, path


@pytest.mark.parametrize("fault", [None, "geometry", "source", "moving", "unobserved", "owner", "zoom", "invalid_measurement", "bad_ray"])
def test_persisted_reference_is_bound_to_geometry_and_observed_arrival(tmp_path, fault):
    service, binding, body, path = environment(tmp_path)
    record = {"schema_version": 1, "revision": 1, "id": body.preparation_id, "camera_id": "camera", "source_id": "main",
              "artifact_id": binding["id"], "artifact_revision": 1, "geometry": binding["geometry"],
              "source_identity": binding["source_identity"], "status": "ready", "physical_state": "stopped",
              "validation": {"state": "observed", "measurement": {"center_error_pixels": 1.2}},
              "ray": [0, 0, 1], "destination": {"role": "reference", "owner_id": f"native-{body.preparation_id}",
                                               "preserve_zoom": True}}
    if fault == "geometry":
        record["geometry"] = {"model_digest": "old"}
    if fault == "source":
        record["source_id"] = "other"
    if fault == "moving":
        record["physical_state"] = "moving"
    if fault == "unobserved":
        record["validation"]["state"] = "pending"
    if fault == "invalid_measurement":
        record["validation"]["measurement"] = []
    if fault == "owner":
        record["destination"]["owner_id"] = "acquisition"
    if fault == "zoom":
        record["destination"]["preserve_zoom"] = False
    if fault == "bad_ray":
        record["ray"] = [0, 0, 0]
    service._atomic(path, record)
    assert bool(service.native_references("camera", "main", binding)) == (fault is None)
    if fault is None:
        service.invalidate_native_reference(record, "return_preset_changed")
        assert not service.native_references("camera", "main", binding)
        assert service._read(path)["revision"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("fault", [None, "recall", "stop", "journal", "cancel_before_acquire", "cancel_after_create"])
async def test_preparation_publishes_only_observed_reference_and_does_not_retry_creation(tmp_path, monkeypatch, fault):
    import toposync_ext_cameras.panorama_scan as scan_module
    import toposync_ext_cameras.panorama_navigation as navigation_module
    service, binding, body, path = environment(tmp_path)
    events = []

    class Camera:
        async def discover(self):
            if fault == "cancel_before_acquire":
                assert (await service.stop_native_reference(None, "camera", body))["status"] == "stopping"
            return {"motion_automation": {"auto_tracking": False, "automatic_return": False}}

        async def acquire(self):
            events.append("acquire")

        async def save_return(self, role, *, preserve_zoom, before_create):
            pending = {"kind": "pending_preset", "role": role, "owner_id": f"native-{body.preparation_id}",
                       "preserve_zoom": preserve_zoom}
            await before_create(pending)
            assert service._read(path)["destination"] == pending
            if fault == "journal":
                raise OSError("Device call never dispatched")
            events.append("create")
            if fault == "cancel_after_create":
                await service.stop_native_reference(None, "camera", body)
            return {**pending, "kind": "preset", "preset_token": "owned"}

        def pending_return_destinations(self):
            return []

        async def remove_return(self, destination):
            events.append("remove")

        async def close(self):
            events.append("close")

    class Scanner:
        def __init__(self, *args):
            self.acquired = False
            self.physical_state = "unknown"
            self.cancelled = args[3]

        def _check(self):
            if self.cancelled():
                raise scan_module._Stopped

        async def _stop(self):
            return True

        async def _reference_window(self):
            self._check()
            return {}

        async def _confirm_stop(self, **kwargs):
            self.physical_state = "stop_unconfirmed" if fault == "stop" else "stopped"

    class Navigator:
        def __init__(self, *args, **kwargs):
            self.commands = 0
            self.trace = []

        async def locate(self):
            return {"rotation_matrix": np.eye(3).tolist()}

        async def _pulse(self, axis, amount):
            assert axis == "pan" and amount == .12
            self.commands += 1

        async def approach_reference(self, destination, ray):
            self.commands += 1
            if fault == "recall":
                raise PanoramaCaptureError("visual_native_reference_unconfirmed")
            self.trace.append({"state": "observed", "measurement": {"center_error_pixels": 1.2}})

    monkeypatch.setattr(scan_module, "_Scan", Scanner)
    monkeypatch.setattr(navigation_module, "VisualNavigator", Navigator)
    service.camera_factory = lambda **kwargs: Camera()
    if fault in {"cancel_before_acquire", "cancel_after_create"}:
        assert (await service.prepare_native_reference(None, "camera", body))["status"] == "interrupted"
        record = service._read(path)
        assert record["status"] == "unverified" and record["commands"] == 0
        assert not service._native_preparations and not service.cancelled
        if fault == "cancel_before_acquire":
            assert events == ["close"]
        else:
            assert events == ["acquire", "create", "remove", "close"]
            assert record["physical_state"] == "stopped" and record["cleanup_confirmed"]
    elif fault:
        with pytest.raises((PanoramaCaptureError, OSError)):
            await service.prepare_native_reference(None, "camera", body)
        assert service._read(path)["status"] == "unverified"
        assert not service.native_references("camera", "main", binding)
        with pytest.raises(HTTPException):
            await service.prepare_native_reference(None, "camera", body)
    else:
        assert (await service.prepare_native_reference(None, "camera", body))["status"] == "ready"
        assert service.native_references("camera", "main", binding)
        assert (await service.prepare_native_reference(None, "camera", body))["reused"] is True
        assert events.count("create") == 1
        assert (await service.remove_native_reference(None, "camera", body))["status"] == "retired"
        assert not service.native_references("camera", "main", binding)
    assert events[-1] == "close"


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["queued", "preparing", "stopping", "abandoned", "foreign"])
async def test_preparation_catalog_tracks_live_ownership_and_hides_device_details(tmp_path, state):
    service, binding, body, path = environment(tmp_path)
    if state not in {"queued", "foreign"}:
        service._atomic(path, {"id": body.preparation_id, "camera_id": "camera", "source_id": "main",
                               "artifact_id": binding["id"], "status": "preparing",
                               "destination": {"preset_token": "private-device-token"}})
    if state != "abandoned":
        service._native_preparations[body.preparation_id] = (
            "other" if state == "foreign" else "camera", body, asyncio.current_task())
    if state == "stopping":
        service.cancelled.add(f"native-{body.preparation_id}")
    result = await service.list_native_references(None, "camera", "main", binding["id"], 1)
    if state == "foreign":
        assert result == {"references": []}
        with pytest.raises(HTTPException):
            await service.stop_native_reference(None, "camera", body)
        assert not service.cancelled
    else:
        row, = result["references"]
        assert row["status"] == ("unverified" if state == "abandoned" else state)
        assert "destination" not in row and "private-device-token" not in str(result)


@pytest.mark.anyio
async def test_stop_while_waiting_for_shared_reference_lock_never_opens_camera(tmp_path):
    from toposync_ext_cameras.panorama_reference import PanoramaReferenceCoordinator
    service, _, body, _ = environment(tmp_path)
    service.reference_coordinator = PanoramaReferenceCoordinator()
    opened = []
    service.camera_factory = lambda **kwargs: opened.append(kwargs)
    async with service.reference_coordinator.hold("camera", body.source_id):
        task = asyncio.create_task(service.prepare_native_reference(None, "camera", body))
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as duplicate:
            await service.prepare_native_reference(None, "camera", body)
        assert duplicate.value.detail["code"] == "native_reference_busy"
        assert (await service.stop_native_reference(None, "camera", body))["status"] == "stopping"
        assert (await asyncio.wait_for(task, 1))["status"] == "interrupted"
        assert service.reference_coordinator._lock("camera", body.source_id).locked()
    assert not opened and not service.cancelled and not service._native_preparations


@pytest.mark.anyio
async def test_dismissing_confirmed_cleanup_never_contacts_camera(tmp_path):
    service, binding, body, path = environment(tmp_path)
    record = {"id": body.preparation_id, "camera_id": "camera", "source_id": "main",
              "artifact_id": binding["id"], "artifact_revision": 1, "status": "unverified", "revision": 1, "cleanup_confirmed": True}
    service._atomic(path, record)
    opened = []
    service.camera_factory = lambda **kwargs: opened.append(kwargs)
    assert (await service.remove_native_reference(None, "camera", body))["status"] == "retired"
    assert not opened and service._read(path) == {**record, "status": "retired", "revision": 2}
