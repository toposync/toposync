"""Software qualification gates and borrowed control, using synthetic evidence only."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from toposync_ext_cameras.panorama import PanoramaService, QualifyAttention
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


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["settles", "unknown", "other_owner"])
async def test_attention_stop_refreshes_native_status_without_promoting_unknown_or_other_owner(monkeypatch, outcome):
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    service = object.__new__(PanoramaService)
    observations = 0

    async def snapshot(name, **kwargs):
        nonlocal observations
        observations += 1
        assert name == "cameras.control.snapshot" and kwargs["refresh_physical"] is True
        stable = observations >= 3 and outcome == "settles"
        return {"active_lease": {"lease_id": "other" if outcome == "other_owner" else "attention", "fence": 7},
            "automation_ready": True, "geometry_safe": stable,
            "motion_state": "stable" if stable else "unknown"}

    service.services = SimpleNamespace(call=snapshot)
    result = await service._attention_stopped_control(bound_job(), {"lease_id": "attention", "fence": 7})
    assert result is (outcome == "settles")
    assert observations == {"settles": 3, "unknown": 6, "other_owner": 1}[outcome]


@pytest.mark.anyio
@pytest.mark.parametrize("reuse", ["same", "revision_changed", "optics_changed", "expired", "stop_unknown",
    "current_arrival", "current_return", "expired_during_status", "frame_aged_during_status",
    "stop_failed_after_expiry", "return_needs_correction", "cancel_receipt", "cancel_status",
    "clock_jump", "clock_jump_failure", "metadata_same", "metadata_revision", "metadata_fence",
    "metadata_geometry", "metadata_refresh_failure"])
async def test_attention_arrival_uses_refreshed_endpoint_and_saves_only_one_image(monkeypatch, tmp_path, reuse):
    import numpy as np
    from toposync_ext_cameras import panorama_scan, panorama_navigation

    frame = {"image": np.zeros((16, 16, 3), dtype=np.uint8), "capture_evidence": {"sequence": 1}}
    fresh = {**frame, "capture_evidence": {"sequence": 2}}
    camera = SimpleNamespace(discover=AsyncMock(return_value={}), acquire=AsyncMock(),
        position=AsyncMock(return_value={}), close=AsyncMock())
    if reuse.startswith("metadata_"):
        camera.reuse_discovery_from = Mock(return_value=True)
    scanner = SimpleNamespace(acquired=False, physical_state="stopped", last_frame=frame,
        checkpoint={}, _stop=AsyncMock(return_value=True), _reference_window=AsyncMock(return_value=frame),
        _confirm_stop=AsyncMock(), refresh_stopped_frame=AsyncMock(return_value=fresh))
    navigator = SimpleNamespace(aim=AsyncMock(return_value={"target_ray": [0, 0, 1]}),
        locate=AsyncMock(return_value={}),
        _target_measurement=AsyncMock(return_value={"center_error_pixels": .3}),
        response={"pan": np.array([.2, 0])}, response_rotations={"pan": np.eye(3)},
        pulse_limits={"pan": .3}, direction_responses={}, stationary_durations={}, commands=1)
    reuse_expected = reuse in {"current_arrival", "current_return"}
    tolerance = 3 if reuse in {"current_return", "return_needs_correction"} else 12
    if reuse_expected or reuse in {"expired_during_status", "frame_aged_during_status", "stop_failed_after_expiry", "return_needs_correction", "cancel_receipt", "cancel_status"}:
        import time
        frame.update(received_monotonic=time.monotonic(), capture_evidence={
            "capture_instance": "decoder", "generation": 1, "sequence": 1})
        scanner.last_stop_accepted_monotonic = frame["received_monotonic"] - .01
        scanner.last_stop_receipt = {"command_id": "stop"}
        camera.stop_receipt_is_current = AsyncMock(return_value=True)
        navigator.aim.return_value.update(verified=True, kind="visual_aim_verified",
            capture_evidence=frame["capture_evidence"],
            measurement={"center_error_pixels": 4 if reuse == "return_needs_correction" else .3})
        if reuse in {"expired_during_status", "stop_failed_after_expiry"}:
            camera.stop_receipt_is_current.side_effect = [True, False]
        if reuse == "stop_failed_after_expiry":
            scanner._stop.side_effect = [True, False]
        if reuse == "cancel_receipt":
            camera.stop_receipt_is_current.side_effect = module.asyncio.CancelledError()
    monkeypatch.setattr(panorama_scan, "_Scan", lambda *args: scanner)
    monkeypatch.setattr(panorama_scan, "_write_image", lambda *args: pytest.fail("Duplicate endpoint encoding"))
    monkeypatch.setattr(panorama_navigation, "VisualNavigator", lambda *args: navigator)
    service = object.__new__(PanoramaService)
    service.root, service.services = tmp_path, SimpleNamespace()
    service.camera_factory = lambda **kwargs: camera
    service._reserve = AsyncMock()
    service._navigation_scanners, service.busy, service.cancelled = {}, {}, set()
    service._attention_responses = {}
    service._attention_metadata = {}
    service._save = lambda job: None
    service._current = AsyncMock()
    service.localize_frame = AsyncMock(return_value={"status": "localized"})
    service._localizers = {("artifact", "geometry"): SimpleNamespace(lens={})}
    service._attention_stopped_control = AsyncMock(return_value=True)
    if reuse == "cancel_status":
        service._attention_stopped_control.side_effect = [module.asyncio.CancelledError(), True]
    if reuse == "frame_aged_during_status":
        clock = [frame["received_monotonic"]]
        monkeypatch.setattr(panorama_navigation.time, "monotonic", lambda: clock[0])
        async def slow_status(*args):
            clock[0] += 1.1
            return True
        service._attention_stopped_control.side_effect = slow_status
    images = []
    service._image_write = lambda job, name, data: images.append(name)
    service._image_url = lambda job, name: name
    if reuse in {"clock_jump", "clock_jump_failure"}:
        clock, wall = [100.0], [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(module.time, "time", lambda: wall[0])
        async def reserve(*args):
            clock[0] += .01
        async def discover():
            clock[0] += .02
            return {}
        async def acquire():
            clock[0] += .03
            wall[0] -= 1000
        async def reference():
            clock[0] += .04
            if reuse == "clock_jump_failure":
                raise PanoramaCaptureError("capture_timeout")
            return frame
        async def position():
            clock[0] += .05
            return {}
        async def localize(**kwargs):
            clock[0] += .06
            return {"status": "localized"}
        service._reserve.side_effect = reserve
        camera.discover.side_effect, camera.acquire.side_effect = discover, acquire
        scanner._reference_window.side_effect, camera.position.side_effect = reference, position
        service.localize_frame.side_effect = localize
    job = bound_job()
    job["_attention_navigation_history"] = [{"trial": index} for index in range(8)]
    operation = service._visual_operation_with_pinned_reference(None, job,
        {"ray": [0, 0, 1]}, borrowed_control={"lease_id": "attention", "fence": 7}, maximum_error_pixels=tolerance)
    if reuse == "clock_jump_failure":
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.detail["code"] == "capture_timeout"
        trace = job["_attention_navigation_trace"]
        assert trace["preparation_elapsed_ms"] == pytest.approx({
            "reserved": 10, "discovered": 30, "acquired": 60, "stopped": 60})
        assert trace["error_code"] == "capture_timeout"
        assert job["stop_confirmed"] is True
        camera.close.assert_awaited_once()
        assert not images
        return
    if reuse in {"cancel_receipt", "cancel_status"}:
        with pytest.raises(module.asyncio.CancelledError):
            await operation
        assert scanner._stop.await_count == 2, "Cancellation must still attempt a final Stop"
        scanner._confirm_stop.assert_awaited_once_with(reuse_accepted=True)
        assert job["stop_confirmed"] is True
        assert job["_attention_navigation_trace"]["error_code"] == "capture_cancelled"
        assert job["_attention_navigation_trace"]["final_stop_reused"] is False
        scanner.refresh_stopped_frame.assert_not_awaited()
        camera.close.assert_awaited_once()
        assert not service._navigation_scanners and not images
        return
    if reuse == "stop_failed_after_expiry":
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.detail["code"] == "stop_unconfirmed"
        assert job["stop_confirmed"] is False
        assert job["_attention_navigation_trace"]["final_stop_reused"] is False
        scanner.refresh_stopped_frame.assert_not_awaited()
        camera.close.assert_awaited_once()
        assert not images
        return
    _, evidence = await operation
    assert evidence["capture_evidence"]["sequence"] == (1 if reuse_expected else 2)
    if reuse_expected:
        scanner.refresh_stopped_frame.assert_not_awaited()
        scanner._confirm_stop.assert_not_awaited()
        assert scanner._stop.await_count == 1, "Only initial Stop is needed with a current qualified arrival"
    else:
        scanner.refresh_stopped_frame.assert_awaited_once()
    assert len(images) == 1 and images[0].endswith(".jpg")
    camera.close.assert_awaited_once()
    history = job["_attention_navigation_history"]
    assert len(history) == 8 and history[0] == {"trial": 1}
    trace = history[-1]
    assert trace == job["_attention_navigation_trace"]
    assert trace["error_code"] is None
    assert trace["final_stop_reused"] is reuse_expected
    if reuse == "clock_jump":
        assert trace["preparation"]["acquired_at"] < trace["preparation"]["discovered_at"]
        assert trace["preparation_elapsed_ms"] == pytest.approx({
            "reserved": 10, "discovered": 30, "acquired": 60, "stopped": 60,
            "reference": 100, "position": 150, "localized": 210})
        assert trace["timing_clock"] == "process_monotonic"
        return
    assert list(trace["preparation"].values()) == sorted(trace["preparation"].values())
    assert trace["finished_at"] >= trace["preparation"]["localized_at"]
    cached_at = service._attention_responses["camera"]["observed_monotonic"]
    if reuse.startswith("metadata_"):
        assert service._attention_metadata["camera"]["camera"] is camera
        assert trace["discovery_reused"] is False
        following = SimpleNamespace(discover=AsyncMock(return_value={}), acquire=AsyncMock(),
            position=AsyncMock(return_value={}), close=AsyncMock(),
            reuse_discovery_from=Mock(return_value=True), refresh_discovery_observations=AsyncMock(return_value={}))
        service.camera_factory = lambda **kwargs: following
        if reuse == "metadata_revision":
            job["revision"] += 1
        if reuse == "metadata_geometry":
            job["source_panorama"]["geometry"]["model_digest"] = "changed"
            service._localizers[("artifact", "changed")] = SimpleNamespace(lens={})
        if reuse == "metadata_refresh_failure":
            following.refresh_discovery_observations.side_effect = PanoramaCaptureError("camera_configuration_changed")
            scanner.acquired = False
        operation = service._visual_operation_with_pinned_reference(None, job, {"ray": [0, 0, 1]},
            borrowed_control={"lease_id": "attention", "fence": 8 if reuse == "metadata_fence" else 7})
        if reuse == "metadata_refresh_failure":
            with pytest.raises(HTTPException) as failure:
                await operation
            assert failure.value.detail["code"] == "camera_configuration_changed"
            following.acquire.assert_not_awaited()
            assert "camera" not in service._attention_metadata
        else:
            await operation
            expected = reuse == "metadata_same"
            assert job["_attention_navigation_trace"]["discovery_reused"] is expected
            assert following.discover.await_count == int(not expected)
            assert following.refresh_discovery_observations.await_count == int(expected)
            assert following.acquire.await_count == 1
            assert scanner._reference_window.await_count == 2
            assert service._attention_metadata["camera"]["camera"] is following
        following.close.assert_awaited_once()
        assert not service._navigation_scanners and not service.busy
        return
    if reuse == "revision_changed":
        job["revision"] += 1
    elif reuse == "optics_changed":
        camera.discover.return_value = {"source_identity": {"zoom": .8}}
    elif reuse == "expired":
        service._attention_responses["camera"]["observed_monotonic"] -= 301
    elif reuse == "stop_unknown":
        service._attention_stopped_control.return_value = False
    navigator.response = {}
    navigator.commands = 0
    operation = service._visual_operation_with_pinned_reference(None, job, {"ray": [0, 0, 1]},
        borrowed_control={"lease_id": "attention", "fence": 7})
    if reuse == "stop_unknown":
        with pytest.raises(HTTPException):
            await operation
        assert "camera" not in service._attention_responses
    else:
        await operation
        assert bool(navigator.response) is (reuse not in {"revision_changed", "optics_changed", "expired"})
        if reuse == "same" or reuse_expected:
            assert service._attention_responses["camera"]["observed_monotonic"] == cached_at
            navigator.response["pan"][0] = 99
            assert service._attention_responses["camera"]["response"]["response"]["pan"][0] == .2


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
    service._save = lambda _: None
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
        assert job["_attention_navigation_result"]["command_id"] == "command"
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


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "arrival", "return", "stop", "motion", "reference", "cancel",
    "inactive", "same_target", "pending_return", "pending_visual_return"])
async def test_supervised_round_trip_is_the_only_qualification_writer(failure):
    job = bound_job()
    job["solution"] = {"type": "panorama_ray_plane_v1"}
    service = object.__new__(PanoramaService)
    service.jobs = {"job": job}
    service.busy = {}
    service.reference_coordinator = SimpleNamespace(hold=hold)
    service._authorize = lambda *args, **kwargs: None
    service._job = lambda *args: job
    service._current = AsyncMock()
    service._save = lambda job: None
    service.get_active = AsyncMock(return_value={"job": None if failure == "inactive" else job})
    service._target = lambda job, point: {"ray": [point["x"], point["z"], 1]}
    stages = [{"stage": name, "evidence": {"verified": True, "kind": "visual_aim_verified",
        "commands": index, "measurement": {"center_error_pixels": 1}}}
        for index, name in enumerate(("home", "arrival", "return"))]
    if failure in {"arrival", "return"}:
        stages[1 if failure == "arrival" else 2]["evidence"]["measurement"]["center_error_pixels"] = 13
    if failure == "stop":
        job["stop_confirmed"] = False
    if failure == "motion":
        stages[2]["evidence"]["commands"] = 1
    if failure in {"pending_return", "pending_visual_return"}:
        job["_navigation_checkpoint"] = {
            "return" if failure == "pending_return" else "visual_original_ray": "preserve"}
    if failure == "reference":
        service._current.side_effect = [None, HTTPException(409, "reference changed")]
    service._visual_operation_with_pinned_reference = AsyncMock(return_value=(
        "image", {"verified": True, "stages": stages}))
    if failure == "cancel":
        import asyncio
        service._visual_operation_with_pinned_reference.side_effect = asyncio.CancelledError()
    body = QualifyAttention(revision=2, home={"x": 0, "z": 0},
        target={"x": 0 if failure == "same_target" else 1, "z": 0})
    if failure:
        import asyncio
        with pytest.raises((HTTPException, asyncio.CancelledError)):
            await service.qualify_attention(None, "camera", "job", body)
        assert not service._attention_qualified(job)
        if failure in {"inactive", "same_target", "pending_return", "pending_visual_return"}:
            service._visual_operation_with_pinned_reference.assert_not_awaited()
        if failure in {"pending_return", "pending_visual_return"}:
            assert "preserve" in job["_navigation_checkpoint"].values()
    else:
        result = await service.qualify_attention(None, "camera", "job", body)
        assert result["qualified"] and service._attention_qualified(job)
        assert result["trial"]["stages"] == stages
        assert service._visual_operation_with_pinned_reference.await_args.kwargs == {
            "qualification_home": {"ray": [0., 0., 1]}}
