"""Retired resource maintenance; simulated tests do not approve hardware."""
from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from toposync_ext_cameras.panorama import PanoramaService, PrepareNativeReference


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
    service._atomic(path.parents[2] / "artifact.json", {"id": binding["id"], "revision": 1,
                     "camera_id": "camera", "source_id": "main"})
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
@pytest.mark.parametrize("generated", [False, True])
async def test_retired_preparation_cannot_create_resources_even_for_generated_jobs(tmp_path, generated):
    service, _, body, path = environment(tmp_path)
    service.camera_factory = lambda **kwargs: pytest.fail("Retired preparation opened a camera")
    options = {"generated_job": {"id": "c" * 32}} if generated else {}
    with pytest.raises(HTTPException) as retired:
        await service.prepare_native_reference(None, "camera", body, **options)
    assert retired.value.status_code == 410
    assert retired.value.detail["code"] == "native_reference_retired"
    service.reference_localizer.assert_not_awaited()
    service.app.state.camera_source_panorama._context.assert_not_awaited()
    assert not path.exists() and not service._native_preparations and not service.cancelled


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
async def test_dismissing_confirmed_cleanup_never_contacts_camera(tmp_path):
    service, binding, body, path = environment(tmp_path)
    record = {"id": body.preparation_id, "camera_id": "camera", "source_id": "main",
              "artifact_id": binding["id"], "artifact_revision": 1, "status": "unverified", "revision": 1, "cleanup_confirmed": True}
    service._atomic(path, record)
    opened = []
    service.camera_factory = lambda **kwargs: opened.append(kwargs)
    assert (await service.remove_native_reference(None, "camera", body))["status"] == "retired"
    assert not opened and service._read(path) == {**record, "status": "retired", "revision": 2}
