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
    "metadata_geometry", "metadata_refresh_failure", "moving_during_cleanup", "lost_during_cleanup",
    "parallel_reference", "parallel_reference_failure", "metadata_cancelled",
    "metadata_expired", "metadata_control_failure", "metadata_stop_failure", "metadata_passive", "metadata_combined",
    "prepared_reference", "dynamic_reference_ignored", "cancel_initial_localization"])
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
        checkpoint={"navigation_target_observations": [{"commands": 0, "measurement": {"center_error_pixels": 4.2}}]}, _stop=AsyncMock(return_value=True), _reference_window=AsyncMock(return_value=frame),
        _confirm_stop=AsyncMock(), refresh_stopped_frame=AsyncMock(return_value=fresh))
    arrival_lens = {"width": 16, "height": 16, "fx": 10., "fy": 10., "cx": 7.5, "cy": 7.5}
    arrival_location = {"rotation_matrix": np.eye(3).tolist(), "analysis_width": 960,
        "validation_matches": 24, "validation_p95_pixels": 1., "inlier_fraction": .8}
    import time
    frame.update(received_monotonic=time.monotonic(), capture_evidence={"capture_instance": "decoder", "generation": 1, "sequence": 1})
    fresh.update(received_monotonic=frame["received_monotonic"], capture_evidence={"capture_instance": "decoder", "generation": 1, "sequence": 2})
    navigator = SimpleNamespace(aim=AsyncMock(return_value={"target_ray": [1, 0, 0],
        "frame_geometry": {"marker": "original-arrival"}}),
        locate=AsyncMock(return_value=arrival_location),
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
    if reuse == "cancel_initial_localization":
        service.localize_frame.side_effect = module.asyncio.CancelledError()
    service._localizers = {("artifact", "geometry"): SimpleNamespace(lens=arrival_lens)}
    service._attention_stopped_control = AsyncMock(return_value=True)
    if reuse in {"prepared_reference", "dynamic_reference_ignored"}:
        reference = {"destination": {"kind": "preset", "token": "prepared"}}
        service.native_references = Mock(return_value=[reference])
        monkeypatch.setattr(panorama_navigation, "select_direct_native_reference", Mock(return_value=reference))
        camera.bind_reference_destination = AsyncMock()
        navigator.approach_reference = AsyncMock()
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
    if reuse.startswith("parallel_reference"):
        import asyncio
        position_started, position_finished = asyncio.Event(), asyncio.Event()
        async def position():
            scanner._stop.assert_awaited()
            position_started.set()
            try:
                if reuse.endswith("failure"):
                    await asyncio.Event().wait()
                return {}
            finally:
                position_finished.set()
        async def reference():
            await asyncio.wait_for(position_started.wait(), 1)
            if reuse.endswith("failure"):
                raise PanoramaCaptureError("capture_timeout")
            return frame
        camera.position.side_effect = position
        scanner._reference_window.side_effect = reference
    job = bound_job()
    job["_attention_navigation_history"] = [{"trial": index} for index in range(8)]
    scanner.checkpoint["navigation_target_replans"] = 2
    scanner.checkpoint["navigation_target_rebases"] = 3
    scanner.checkpoint["navigation_target_predictions"] = 4
    current_direction = [[1., 0., 0.]]
    def current_target():
        if reuse == "lost_during_cleanup":
            raise HTTPException(409, detail={"code": "following_target_missing"})
        return current_direction[0]
    if reuse == "moving_during_cleanup":
        async def measured_after_update(*args):
            current_direction[0] = [float(np.cos(.005)), float(np.sin(.005)), 0.]
            return {"center_error_pixels": .3}
        navigator._target_measurement.side_effect = measured_after_update
    operation = service._visual_operation_with_pinned_reference(None, job,
        {"ray": [0, 0, 1]}, borrowed_control={"lease_id": "attention", "fence": 7}, maximum_error_pixels=tolerance,
        **({"target_provider": current_target} if reuse.endswith("during_cleanup") or reuse == "dynamic_reference_ignored" else {}))
    if reuse == "cancel_initial_localization":
        with pytest.raises(module.asyncio.CancelledError):
            await operation
        trace = job["_attention_navigation_trace"]
        timing = trace["navigation_observation_timings"][0]
        assert timing["stage"] == "initial_localization"
        assert timing["completed"] is False and timing["status"] is None
        assert timing["elapsed_seconds"] >= 0
        assert trace["error_code"] == "capture_cancelled"
        navigator.aim.assert_not_awaited()
        camera.close.assert_awaited_once()
        assert job["stop_confirmed"] is True
        return
    if reuse == "parallel_reference_failure":
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.detail["code"] == "capture_timeout"
        assert position_finished.is_set()
        navigator.aim.assert_not_awaited()
        camera.close.assert_awaited_once()
        assert not service._navigation_scanners and not service.busy
        return
    if reuse == "lost_during_cleanup":
        with pytest.raises(HTTPException) as failure:
            await operation
        assert failure.value.detail["code"] == "following_target_missing"
        assert job["_attention_navigation_trace"]["error_code"] == "following_target_missing"
        camera.close.assert_awaited_once()
        return
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
    if reuse == "prepared_reference":
        camera.bind_reference_destination.assert_awaited_once_with(reference["destination"])
        navigator.approach_reference.assert_awaited_once_with(reference["destination"], [0, 0, 1])
        navigator.aim.assert_awaited_once_with([0, 0, 1], maximum_error_pixels=12)
    elif reuse == "dynamic_reference_ignored":
        service.native_references.assert_not_called()
        camera.bind_reference_destination.assert_not_awaited()
        navigator.approach_reference.assert_not_awaited()
    if reuse == "moving_during_cleanup":
        assert evidence["target_ray"] == current_direction[0]
        assert evidence["measurement"]["center_error_pixels"] == pytest.approx(600 * np.tan(.005))
    assert evidence["capture_evidence"]["sequence"] == (1 if reuse_expected else 2)
    if reuse == "same":
        assert evidence["frame_geometry"]["capture_evidence"]["sequence"] == 2
        assert evidence["frame_geometry"]["rotation_matrix"] == arrival_location["rotation_matrix"]
        arrival_location["rotation_matrix"][0][0] = 99
        assert evidence["frame_geometry"]["rotation_matrix"][0][0] == 1
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
    assert trace["navigation_target_observations"] == [{"commands": 0, "measurement": {"center_error_pixels": 4.2}}]
    assert trace["target_replans"] == 2
    assert trace["target_rebases"] == 3
    assert trace["target_predictions"] == 4
    assert trace["error_code"] is None
    initial = trace["navigation_observation_timings"][0]
    assert initial["stage"] == "initial_localization"
    assert initial["completed"] is True and initial["status"] == "localized"
    if reuse == "clock_jump":
        assert initial["elapsed_seconds"] == pytest.approx(.06)
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
        if reuse == "metadata_passive":
            service._attention_metadata["camera"]["passive"] = True
            service._attention_metadata["camera"]["binding"]["control"] = None
        following = SimpleNamespace(discover=AsyncMock(return_value={}), acquire=AsyncMock(),
            position=AsyncMock(return_value={}), close=AsyncMock(),
            reuse_discovery_from=Mock(return_value=True), refresh_discovery_observations=AsyncMock(return_value={}))
        service.camera_factory = lambda **kwargs: following
        if reuse == "metadata_combined":
            following.acquire_with_capabilities = AsyncMock(return_value={"fresh": True})
        if reuse == "metadata_revision":
            job["revision"] += 1
        if reuse == "metadata_geometry":
            job["source_panorama"]["geometry"]["model_digest"] = "changed"
            service._localizers[("artifact", "changed")] = SimpleNamespace(lens={})
        if reuse == "metadata_refresh_failure":
            following.refresh_discovery_observations.side_effect = PanoramaCaptureError("camera_configuration_changed")
            scanner.acquired = False
        interrupted = reuse in {"metadata_cancelled", "metadata_expired", "metadata_control_failure", "metadata_stop_failure"}
        if interrupted:
            navigator.aim.side_effect = (module.asyncio.CancelledError() if reuse == "metadata_cancelled"
                else PanoramaCaptureError("control_lease_lost" if reuse == "metadata_control_failure"
                                          else "following_observation_expired"))
            if reuse == "metadata_stop_failure":
                service._attention_stopped_control.return_value = False
        operation = service._visual_operation_with_pinned_reference(None, job, {"ray": [0, 0, 1]},
            borrowed_control={"lease_id": "attention", "fence": 8 if reuse == "metadata_fence" else 7})
        if interrupted:
            with pytest.raises(module.asyncio.CancelledError if reuse == "metadata_cancelled" else HTTPException):
                await operation
            retained = reuse in {"metadata_cancelled", "metadata_expired"}
            assert ("camera" in service._attention_metadata) is retained
            following.close.assert_awaited_once()
            if retained:
                assert service._attention_metadata["camera"]["camera"] is following
                navigator.aim.side_effect = None
                await service._visual_operation_with_pinned_reference(None, job, {"ray": [0, 0, 1]},
                    borrowed_control={"lease_id": "attention", "fence": 7})
                assert job["_attention_navigation_trace"]["discovery_reused"] is True
                following.discover.assert_not_awaited()
                assert following.refresh_discovery_observations.await_count == 2
                assert following.acquire.await_count == 2
            assert not service._navigation_scanners and not service.busy
            return
        if reuse == "metadata_refresh_failure":
            with pytest.raises(HTTPException) as failure:
                await operation
            assert failure.value.detail["code"] == "camera_configuration_changed"
            following.acquire.assert_not_awaited()
            assert "camera" not in service._attention_metadata
        else:
            await operation
            expected = reuse in {"metadata_same", "metadata_passive", "metadata_combined"}
            assert job["_attention_navigation_trace"]["discovery_reused"] is expected
            assert following.discover.await_count == int(not expected)
            assert following.refresh_discovery_observations.await_count == int(expected and reuse != "metadata_combined")
            assert following.acquire.await_count == int(reuse != "metadata_combined")
            if reuse == "metadata_combined":
                following.acquire_with_capabilities.assert_awaited_once()
                assert scanner.capabilities == {"fresh": True}
                assert "discovered_and_acquired" in job["_attention_navigation_trace"]["preparation_elapsed_ms"]
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
async def test_attention_service_uses_borrowed_lease_and_live_precision_without_presets(monkeypatch, change):
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
        assert options == {"borrowed_control": {"lease_id": "attention", "fence": 7}, "maximum_error_pixels": 12}
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


def subject_destination(job, *, published_at=1000., sequence=1, anchor=None, center=None):
    anchor = anchor or [0, 0, 1]
    center = center or [0, 1, 0]
    return {**{key: job[key] for key in ("camera_id", "source_id", "composition_id", "element_id", "revision")},
        "job_id": job["id"], "ray": anchor,
        "framing": {"camera_id": job["camera_id"], "source_id": job["source_id"],
            "capture_instance": "decoder", "generation": 1, "sequence": sequence, "published_at": published_at,
            "source_width": 201, "source_height": 101, "bbox01": [.4, .1, .6, .5], "boundary_contact": False,
            "panorama": {"job_id": job["id"], "revision": job["revision"], "composition_id": job["composition_id"],
                "source_artifact_id": "artifact", "geometry_digest": "geometry",
                "center_ray": center, "corner_rays": [[0, .8, .6], [.6, .8, 0], [0, .8, -.6], [-.6, .8, 0]]}}}


def framing_service(monkeypatch):
    import numpy as np
    job = bound_job()
    qualify(job)
    service = object.__new__(PanoramaService)
    service.jobs = {"job": job}
    service._save = lambda _: None
    service.reference_coordinator = SimpleNamespace(hold=hold)
    service.get_active = AsyncMock(return_value={"job": {**job, "permissions": {"aim_enabled": True}}})
    service.reference_localizer = AsyncMock(return_value=SimpleNamespace(
        target_reference=Mock(return_value={"id": "reference"}), coverage_mask=np.ones((40, 80), dtype=np.uint8)))
    snapshot = {"active_lease": {"lease_id": "attention", "fence": 7, "owner_kind": "automation"},
        "automation_ready": True, "motion_epoch": 4}
    service.services = SimpleNamespace(call=AsyncMock(return_value=snapshot))
    service._visual_operation_with_pinned_reference = AsyncMock()
    ground = Mock(return_value=(0, 0))
    monkeypatch.setattr(module, "map_ray_to_world", ground)
    monkeypatch.setattr(module, "map_world_to_ray", lambda *args: [0, 0, 1])
    monkeypatch.setattr(module, "mapping_extrapolated", lambda *args: False)
    monkeypatch.setattr(module.time, "time", lambda: 1000.5)
    monkeypatch.setattr(module.time, "monotonic", lambda: 50.)
    return service, job, ground


def group_destination(job):
    from copy import deepcopy
    import numpy as np
    destination = subject_destination(job)
    first = destination.pop("framing")
    second = deepcopy(first)
    second.update(sequence=2, published_at=1000.2)
    angle = .2
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    second["panorama"]["corner_rays"] = [(rotation @ ray).tolist() for ray in second["panorama"]["corner_rays"]]
    second["panorama"]["center_ray"] = (rotation @ second["panorama"]["center_ray"]).tolist()
    destination["group_framing"] = {"members": {"person": first, "vehicle": second}}
    return destination


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "busy", "unqualified", "discovery", "cancel", "changed", "became_busy"])
async def test_passive_attention_preparation_never_acquires_or_captures(monkeypatch, tmp_path, failure):
    service, job, _ = framing_service(monkeypatch)
    camera = SimpleNamespace(discover=AsyncMock(), close=AsyncMock(), acquire=AsyncMock(), frame=AsyncMock())
    service.camera_factory = Mock(return_value=camera)
    service.root, service.busy, service._attention_metadata = tmp_path, {}, {}
    service._current = AsyncMock()
    if failure == "busy":
        service.busy["camera"] = "navigation"
    if failure == "unqualified":
        job.pop("_attention_qualification")
    async def discover():
        if failure == "cancel":
            raise module.asyncio.CancelledError()
        if failure == "discovery":
            raise PanoramaCaptureError("camera_discovery_failed")
        if failure == "changed":
            job["revision"] += 1
        if failure == "became_busy":
            service.busy["camera"] = "navigation"
    camera.discover.side_effect = discover
    operation = service.prepare_attention(camera_id="camera", source_id="wide",
        composition_id="yard", element_id="element")
    if failure in {"cancel", "discovery"}:
        with pytest.raises(module.asyncio.CancelledError if failure == "cancel" else PanoramaCaptureError):
            await operation
    else:
        assert (await operation)["prepared"] is (failure is None)
    camera.acquire.assert_not_awaited()
    camera.frame.assert_not_awaited()
    if failure in {"busy", "unqualified"}:
        service.camera_factory.assert_not_called()
    else:
        camera.close.assert_awaited_once()
    assert bool(service._attention_metadata) is (failure is None)
    if failure is None:
        assert service._attention_metadata["camera"]["binding"]["control"] is None
        assert service._attention_metadata["camera"]["passive"] is True


@pytest.mark.anyio
async def test_entry_timing_preserves_waits_when_target_expires_before_navigation(monkeypatch):
    from copy import deepcopy
    service, job, _ = framing_service(monkeypatch)
    clock = [50.]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    @asynccontextmanager
    async def delayed_hold(*args):
        clock[0] += .1
        yield
    async def mapping(**kwargs):
        clock[0] += .2
        return {"job": job}
    async def localizer(*args):
        clock[0] += .3
        return service.reference_localizer.return_value
    async def control(*args, **kwargs):
        clock[0] += .4
        return service.services.call.return_value
    service.reference_coordinator.hold = delayed_hold
    service.get_active.side_effect = mapping
    service.reference_localizer.side_effect = localizer
    service.services.call.side_effect = control
    saved = []
    service._save = lambda value: saved.append(deepcopy(value))
    job["_attention_entry_history"] = [{"old": index} for index in range(8)]
    with pytest.raises(PanoramaCaptureError, match="following_observation_expired"):
        await service.navigate_attention(destination=subject_destination(job, published_at=995.),
            lease_id="attention", fence=7, command_id="expired")
    service._visual_operation_with_pinned_reference.assert_not_awaited()
    history = saved[-1]["_attention_entry_history"]
    assert len(history) == 8 and history[0] == {"old": 1}
    assert history[-1]["command_id"] == "expired"
    assert history[-1]["last_stage"] == "control_checked"
    assert history[-1]["elapsed_ms"] == pytest.approx({
        "reference_acquired": 100, "mapping_resolved": 300,
        "localizer_ready": 600, "control_checked": 1000})


@pytest.mark.anyio
async def test_temporal_group_drives_angular_navigation_and_checks_oldest_member_between_pulses(monkeypatch):
    from copy import deepcopy
    from toposync_ext_cameras.panorama_navigation import group_framing_rays
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
    service, job, ground = framing_service(monkeypatch)
    destination = group_destination(job)
    estimate = destination["group_framing"]
    resolved = await service.resolve_target(camera_id="camera", source_id="wide", composition_id="yard", element_id="element",
        target={"world_envelope": {"center": {"x": 0, "z": 0}, "radius_meters": 1, "member_count": 2}, "group_framing": estimate})
    assert resolved["target"]["group_framing"] == estimate
    updated = deepcopy(destination)
    updated["group_framing"]["members"]["vehicle"].update(sequence=3, published_at=1000.4)
    elapsed = [0.]
    monkeypatch.setattr(module.time, "monotonic", lambda: 50.+elapsed[0])
    async def aim(request, current, target, **options):
        assert target["ray"] == pytest.approx(group_framing_rays(estimate)[0])
        assert options["target_provider"]() == pytest.approx(target["ray"])
        assert options["target_observation_provider"]() is None, "One changing member cannot imply group velocity"
        elapsed[0] = .6
        with pytest.raises(PanoramaCaptureError, match="following_observation_expired"):
            options["target_provider"]()
        elapsed[0] = 0.
        return {}, {"target_ray": target["ray"], "verified": True}
    service._visual_operation_with_pinned_reference.side_effect = aim
    result = await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
        command_id="command", target_provider=lambda: updated, observation_timeout_seconds=1.)
    assert result["destination"]["group_framing"] == updated["group_framing"]
    assert result["evidence"]["framing_projection"] is None
    projected = result["evidence"]["group_framing_projection"]
    assert projected["kind"] == "projected_member_history" and projected["subject_visibility_verified"] is False
    assert set(projected["members"]) == {"person", "vehicle"}
    assert all(list(call.args[1]) == [0, 0, 1] for call in ground.call_args_list)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["stale", "future", "epoch", "source", "empty", "hemisphere", "coverage", "return", "dual"])
async def test_temporal_group_rejects_unqualified_members_before_navigation(monkeypatch, failure):
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
    service, job, _ = framing_service(monkeypatch)
    destination = group_destination(job)
    members = destination["group_framing"]["members"]
    first = members["person"]
    if failure == "stale":
        first["published_at"] = 998.
    elif failure == "future":
        first["published_at"] = 1001.
    elif failure == "epoch":
        first["generation"] = 2
    elif failure == "source":
        first["source_id"] = "zoom"
    elif failure == "empty":
        members.clear()
    elif failure == "hemisphere":
        members["vehicle"]["panorama"]["corner_rays"] = [[-v for v in ray] for ray in first["panorama"]["corner_rays"]]
    elif failure == "coverage":
        service.reference_localizer.return_value.coverage_mask[:] = 0
    elif failure == "dual":
        destination["framing"] = first
    with pytest.raises((HTTPException, PanoramaCaptureError)):
        await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
            command_id="command", returning=failure == "return")
    service._visual_operation_with_pinned_reference.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["missing", "rewind", "mutation", "loss"])
async def test_temporal_group_updates_keep_every_member_and_its_capture_order(monkeypatch, change):
    from copy import deepcopy
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
    service, job, _ = framing_service(monkeypatch)
    destination = group_destination(job)
    updated = deepcopy(destination)
    member = updated["group_framing"]["members"]["vehicle"]
    if change == "missing":
        updated["group_framing"]["members"].pop("vehicle")
    elif change == "rewind":
        member["sequence"] = 1
    elif change == "mutation":
        member["panorama"]["center_ray"] = [1., 0., 0.]
    else:
        updated.pop("group_framing")
    async def aim(request, current, target, **options):
        options["target_provider"]()
    service._visual_operation_with_pinned_reference.side_effect = aim
    with pytest.raises(PanoramaCaptureError, match="following_observation_expired"):
        await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
            command_id="command", target_provider=lambda: updated)


@pytest.mark.anyio
@pytest.mark.parametrize("initial_expired", [False, True])
async def test_subject_center_drives_navigation_without_moving_world_anchor(monkeypatch, initial_expired):
    service, job, ground = framing_service(monkeypatch)
    destination = subject_destination(job, published_at=995. if initial_expired else 1000.)
    resolved = await service.resolve_target(camera_id="camera", source_id="wide", composition_id="yard",
        element_id="element", target={"world_anchor": {"x": 0, "z": 0}, "framing": destination["framing"]})
    assert resolved["target"] == {"ray": [0, 0, 1], "framing": destination["framing"]}
    updated = subject_destination(job, sequence=2, published_at=1000.2, anchor=[.6, 0, .8], center=[.6, .8, 0])

    async def aim(request, current, target, **options):
        assert target == {"ray": [.6, .8, 0]}, "Start from the current subject center after preparation"
        assert options["target_provider"]() == [.6, .8, 0]
        observation = options["target_observation_provider"]()
        assert observation["scope"] == ["camera", "wide", "job", 2, "artifact", "geometry"]
        assert observation["sequence"] == 2 and observation["observed_monotonic"] == pytest.approx(49.7)
        return {}, {"target_ray": [.6, .8, 0], "verified": True}

    service._visual_operation_with_pinned_reference.side_effect = aim
    result = await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
        command_id="command", target_provider=lambda: updated)
    assert result["destination"] == updated
    assert result["destination"]["ray"] == [.6, 0, .8], "Keep the actual world anchor in returned destination"
    assert result["evidence"]["framing_projection"] == {"kind": "projected_observed_corners",
        "subject_visibility_verified": False, "physical_capture_verified": False, "status": "geometry_unavailable"}
    assert destination["ray"] == [0, 0, 1]
    assert all(list(call.args[1]) in [[0, 0, 1], [.6, 0, .8]] for call in ground.call_args_list)
    assert len(service.reference_localizer.return_value.target_reference.call_args_list) >= 10


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["camera", "source", "job", "revision", "artifact", "geometry", "composition",
    "nan_ray", "non_unit", "missing_corner", "null", "frame", "timestamp", "stale", "future", "coverage", "hole", "missing_coverage", "ground", "return"])
async def test_invalid_subject_observation_never_reaches_motion(monkeypatch, failure):
    service, job, _ = framing_service(monkeypatch)
    destination = subject_destination(job)
    framing = destination["framing"]
    if failure in {"camera", "source"}:
        framing[failure + "_id"] = "other"
    elif failure in {"job", "revision", "artifact", "geometry", "composition"}:
        key = {"job": "job_id", "artifact": "source_artifact_id", "geometry": "geometry_digest",
            "composition": "composition_id"}.get(failure, failure)
        framing["panorama"][key] = True if failure == "revision" else "other"
    elif failure in {"nan_ray", "non_unit"}:
        framing["panorama"]["center_ray"] = [float("nan") if failure == "nan_ray" else 2, 0, 0]
    elif failure == "missing_corner":
        framing["panorama"]["corner_rays"].pop()
    elif failure == "null":
        framing["panorama"] = None
    elif failure == "frame":
        framing["sequence"] = True
    elif failure == "timestamp":
        framing["published_at"] = float("nan")
    elif failure in {"stale", "future"}:
        framing["published_at"] = 998 if failure == "stale" else 1001
    elif failure == "coverage":
        service.reference_localizer.return_value.target_reference.side_effect = [{}, {}, None]
    elif failure == "hole":
        service.reference_localizer.return_value.coverage_mask[:] = 0
    elif failure == "missing_coverage":
        service.reference_localizer.return_value.coverage_mask = None
    elif failure == "ground":
        monkeypatch.setattr(module, "mapping_extrapolated", lambda *args: True)
    with pytest.raises((HTTPException, PanoramaCaptureError)):
        await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
            command_id="command", returning=failure == "return")
    service._visual_operation_with_pinned_reference.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["expired", "epoch", "sequence", "timestamp", "same_frame_changed", "lost", "reference", "wrong_arrival"])
async def test_following_subject_is_rechecked_before_commands_and_arrival(monkeypatch, failure):
    import copy
    service, job, _ = framing_service(monkeypatch)
    destination = subject_destination(job)
    updated = copy.deepcopy(destination)
    if failure == "epoch":
        updated["framing"]["generation"] = 2
    elif failure == "sequence":
        destination["framing"]["sequence"] = 2
    elif failure == "timestamp":
        updated["framing"]["sequence"] = 2
    elif failure == "same_frame_changed":
        updated["framing"]["panorama"]["center_ray"] = [1, 0, 0]
    elif failure == "lost":
        updated.pop("framing")
    elif failure == "reference":
        updated["revision"] = 4

    async def aim(request, current, target, **options):
        if failure == "expired":
            monkeypatch.setattr(module.time, "monotonic", lambda: 52.)
            monkeypatch.setattr(module.time, "time", lambda: 900.)  # Wall clock rollback cannot extend validity.
        options["target_provider"]()
        return {}, {"target_ray": [0, 0, 1] if failure == "wrong_arrival" else [0, 1, 0]}

    service._visual_operation_with_pinned_reference.side_effect = aim
    with pytest.raises((HTTPException, PanoramaCaptureError)):
        await service.navigate_attention(destination=destination, lease_id="attention", fence=7,
            command_id="command", target_provider=lambda: updated)
    assert "_attention_navigation_result" not in job


@pytest.mark.anyio
@pytest.mark.parametrize('outcome', ['native', 'visual', 'arrival_failure', 'restore_failure', 'cleanup_failure', 'visual_mismatch', 'cancel',
    'prepared_reference', 'reference_binding_failed', 'reference_arrival_failed', 'reference_not_selected'])
async def test_qualification_preserves_starting_view_until_verified_recovery(monkeypatch, tmp_path, outcome):
    import asyncio
    import copy
    import numpy as np
    from toposync_ext_cameras import panorama_scan, panorama_navigation

    job = bound_job()
    frame = {'image': np.zeros((16, 16, 3), dtype=np.uint8), 'capture_evidence': {'sequence': 1}}
    camera = SimpleNamespace(discover=AsyncMock(return_value={}), acquire=AsyncMock(),
        position=AsyncMock(return_value={}), close=AsyncMock(), remove_return=AsyncMock())
    scanner = SimpleNamespace(acquired=False, physical_state='stopped', last_frame=frame,
        saved_return=None, checkpoint={}, _stop=AsyncMock(return_value=True),
        _reference_window=AsyncMock(return_value=frame), _private_image=lambda path: frame['image'])
    persisted = []

    async def persist():
        scanner.checkpoint['return'] = copy.deepcopy(scanner.saved_return)
        persisted.append(copy.deepcopy(scanner.checkpoint))

    async def save_original():
        assert scanner.checkpoint['initial_path']
        if outcome in {'visual', 'visual_mismatch'}:
            return False
        scanner.saved_return = {'kind': 'preset', 'token': 'owned'}
        await persist()
        return True

    async def restore(**kwargs):
        if outcome == 'restore_failure':
            raise PanoramaCaptureError('return_framing_unconfirmed')
        scanner.physical_state = 'restored'

    async def stop():
        scanner.physical_state = 'stopped'

    scanner._persist = AsyncMock(side_effect=persist)
    scanner._save_original_destination = AsyncMock(side_effect=save_original)
    scanner._restore = AsyncMock(side_effect=restore)
    scanner._confirm_stop = AsyncMock(side_effect=stop)
    navigator = SimpleNamespace(commands=0, response={})
    destinations = []

    async def aim(ray, **kwargs):
        # Recovery is durable before even the first qualification movement.
        assert any(item.get('initial_path') and item.get('visual_original_ray') for item in persisted)
        destinations.append(ray)
        navigator.commands += 1
        if len(destinations) == 2:
            if outcome == 'arrival_failure':
                raise PanoramaCaptureError('visual_response_unavailable')
            if outcome == 'cancel':
                raise asyncio.CancelledError()
        return {'verified': True, 'kind': 'visual_aim_verified', 'commands': navigator.commands,
            'measurement': {'center_error_pixels': 1}}

    navigator.aim = AsyncMock(side_effect=aim)
    reference = {'destination': {'kind': 'preset', 'token': 'panorama-reference'}}
    reference_outcomes = {'prepared_reference', 'reference_binding_failed', 'reference_arrival_failed', 'reference_not_selected'}
    navigator.locate = AsyncMock(return_value={'rotation_matrix': np.eye(3).tolist()})
    navigator.approach_reference = AsyncMock()
    camera.bind_reference_destination = AsyncMock()
    if outcome == 'reference_binding_failed':
        camera.bind_reference_destination.side_effect = PanoramaCaptureError('return_binding_changed')
    if outcome == 'reference_arrival_failed':
        navigator.approach_reference.side_effect = PanoramaCaptureError('visual_native_reference_unconfirmed')
    selector = Mock(return_value=None if outcome == 'reference_not_selected' else reference)
    monkeypatch.setattr(panorama_navigation, 'select_direct_native_reference', selector)
    if outcome == 'cleanup_failure':
        camera.remove_return.side_effect = PanoramaCaptureError('return_cleanup_unconfirmed')
    monkeypatch.setattr(panorama_scan, '_Scan', lambda *args: scanner)
    monkeypatch.setattr(panorama_scan, '_write_image', lambda *args: None)
    monkeypatch.setattr(panorama_scan, '_match', lambda *args: {
        'verified': True, 'overlap': .99, 'displacement': 13 if outcome == 'visual_mismatch' else 1})
    monkeypatch.setattr(panorama_navigation, 'VisualNavigator', lambda *args: navigator)
    service = object.__new__(PanoramaService)
    service.root, service.services = tmp_path, SimpleNamespace()
    service.camera_factory = lambda **kwargs: camera
    service._reserve = AsyncMock()
    service._navigation_scanners, service.busy, service.cancelled = {}, {}, set()
    service._attention_responses, service._attention_metadata = {}, {}
    service._save = lambda job: None
    service._current = AsyncMock()
    service.localize_frame = AsyncMock(return_value={'status': 'localized', 'rotation_matrix': np.eye(3).tolist()})
    service._localizers = {('artifact', 'geometry'): SimpleNamespace(lens={})}
    service.native_references = Mock(return_value=[reference] if outcome in reference_outcomes else [])
    service.invalidate_native_reference = Mock()
    service._image_write = lambda *args: None
    service._image_url = lambda job, name: name
    operation = service._visual_operation_with_pinned_reference(None, job,
        {'ray': [0, 0, 1]}, qualification_home={'ray': [0, 1, 0]})
    if outcome in {'native', 'visual', 'prepared_reference', 'reference_not_selected'}:
        _, evidence = await operation
        assert [stage['stage'] for stage in evidence['stages']] == ['home', 'arrival', 'return']
        assert job['navigation']['physical_state'] == 'restored'
        assert not job['navigation']['can_return']
        assert not scanner.saved_return and 'initial_path' not in scanner.checkpoint
        if outcome == 'visual':
            assert destinations[-1] == [1, 0, 0]
            camera.remove_return.assert_not_awaited()
        else:
            camera.remove_return.assert_awaited_once_with({'kind': 'preset', 'token': 'owned'})
    else:
        with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else HTTPException):
            await operation
        assert job['navigation']['can_return']
        assert scanner.checkpoint['initial_path']
        assert scanner.checkpoint['visual_original_ray'] == [1, 0, 0]
        if outcome != 'visual_mismatch':
            assert scanner.checkpoint['return'] == {'kind': 'preset', 'token': 'owned'}
    assert job['stop_confirmed'] is True
    camera.close.assert_awaited_once()
    if outcome == 'prepared_reference':
        assert navigator.approach_reference.await_count == 3
        assert camera.bind_reference_destination.await_count == 3
        assert [call.kwargs['maximum_error_pixels'] for call in navigator.aim.await_args_list] == [12, 12, 12]
        service.native_references.assert_called_with('camera', 'wide', job['source_panorama'])
    elif outcome in {'reference_binding_failed', 'reference_arrival_failed'}:
        navigator.aim.assert_not_awaited()
        camera.remove_return.assert_not_awaited()
        assert navigator.approach_reference.await_count == (outcome == 'reference_arrival_failed')
    else:
        navigator.approach_reference.assert_not_awaited()
    if outcome == 'reference_arrival_failed':
        service.invalidate_native_reference.assert_called_once_with(reference, 'visual_native_reference_unconfirmed')
    else:
        service.invalidate_native_reference.assert_not_called()
