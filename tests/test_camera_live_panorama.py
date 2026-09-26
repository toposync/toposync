"""Deterministic orchestration evidence; no physical camera or transport."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from toposync_ext_cameras.live_panorama import LivePanoramaService, Session, Intention, Stop


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["retarget", "stop", "close", "failure", "cancel"])
async def test_early_recognition_is_bounded_and_never_publishes_or_holds_motion(action):
    import threading

    service, session = setup()
    session.can_control = True
    session.sequence = 1
    entered, release = threading.Event(), threading.Event()
    image = np.zeros((12, 20, 3), np.uint8)
    frame = {"image": image, "capture_evidence": {"capture_instance": "capture", "generation": 1, "sequence": 1},
             "received_monotonic": time.monotonic()}
    calls = []
    def locate(observed, evidence):
        calls.append(evidence)
        entered.set()
        assert release.wait(3), "Test did not release model worker"
        assert not observed.any(), "The worker must own an immutable snapshot"
        if action == "failure":
            raise RuntimeError("recognition failed")
        return {"status": "localized", "rotation_matrix": np.eye(3).tolist()}
    session.localizer.locate = locate
    diagnostics = {}
    cleanup = None
    try:
        service._prime_recognition(session, 1, frame, diagnostics)
        task = session.recognition_task
        assert task is not None
        assert await asyncio.to_thread(entered.wait, 1)
        image.fill(255)
        service._prime_recognition(session, 1, frame, {})
        assert session.recognition_task is task and len(calls) == 1
        from dataclasses import replace
        other = replace(session, id="other", recognition_task=None)
        service._prime_recognition(other, 1, frame, {})
        assert other.recognition_task is None
        if action == "retarget":
            reached = asyncio.Event()
            async def operation(current, intention, expires):
                reached.set()
            service._operate = operation
            service.intend(session, Intention(sequence=2, x=.5, y=.5))
            await asyncio.wait_for(reached.wait(), .5)
            await session.task
            # A result for the previous intention may not replace this newer state.
            session.result = {"sequence": 2, "marker": "current"}
        elif action == "stop":
            service.stop(session, Stop(sequence=2))
        elif action == "close":
            session.closed = True
        elif action == "cancel":
            task.cancel()
        expected = session.public().copy()
        cleanup = asyncio.create_task(service._drain_recognition(session))
        await asyncio.sleep(.02)
        assert not cleanup.done() and not task.done()
        release.set()
        await asyncio.wait_for(cleanup, 1)
        assert session.public() == expected
        assert session.recognition_task is None
        assert not service.recognition_tasks
        assert diagnostics["early_recognition"]["state"] == (
            "failed" if action == "failure" else "cancelled" if action == "cancel" else "finished")
    finally:
        release.set()
        await service._drain_recognition(session)
        if cleanup is not None:
            await cleanup


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["closed", "blocked", "permission", "sequence", "old", "future"])
async def test_early_recognition_rejects_ineligible_observations(change):
    from unittest.mock import Mock
    service, session = setup()
    session.can_control = change != "permission"
    session.sequence = 1
    session.closed, session.blocked = change == "closed", change == "blocked"
    session.localizer.locate = Mock()
    frame = {"image": np.zeros((12, 20, 3), np.uint8),
             "received_monotonic": time.monotonic() + (-2 if change == "old" else 2 if change == "future" else 0)}
    service._prime_recognition(session, 0 if change == "sequence" else 1, frame, {})
    assert session.recognition_task is None
    session.localizer.locate.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [None, "old_frame", "future_frame", "before_stop", "different_frame",
                                    "missing_identity", "unconfirmed", "bad_center", "nan_center",
                                    "new_command", "control_failure", "slow_lookup"])
async def test_arrival_reuse_requires_fresh_same_frame_and_current_stop(monkeypatch, change):
    import copy
    from unittest.mock import AsyncMock
    import toposync_ext_cameras.live_panorama as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    evidence = {"capture_instance": "capture", "generation": 1, "sequence": 12}
    scanner = SimpleNamespace(last_frame={"capture_evidence": evidence, "received_monotonic": 9.9},
                              physical_state="stopped", last_stop_receipt={"command_id": "stop"},
                              last_stop_accepted_monotonic=9.0)
    result = {"verified": True, "kind": "visual_aim_verified", "capture_evidence": copy.deepcopy(evidence),
              "measurement": {"center_error_pixels": 3.0}}
    camera = SimpleNamespace(stop_receipt_is_current=AsyncMock(return_value=True))
    if change == "old_frame":
        scanner.last_frame["received_monotonic"] = 8.0
    elif change == "future_frame":
        scanner.last_frame["received_monotonic"] = 11.0
    elif change == "before_stop":
        scanner.last_stop_accepted_monotonic = 9.95
    elif change == "different_frame":
        result["capture_evidence"]["sequence"] = 11
    elif change == "missing_identity":
        evidence.pop("capture_instance")
    elif change == "unconfirmed":
        scanner.physical_state = "unknown"
    elif change == "bad_center":
        result["measurement"]["center_error_pixels"] = 13
    elif change == "nan_center":
        result["measurement"]["center_error_pixels"] = float("nan")
    elif change == "new_command":
        camera.stop_receipt_is_current.return_value = False
    elif change == "control_failure":
        camera.stop_receipt_is_current.side_effect = RuntimeError("unavailable")
    elif change == "slow_lookup":
        async def delayed(_receipt):
            clock[0] = 11.1
            return True
        camera.stop_receipt_is_current.side_effect = delayed
    assert await module._arrival_stop_is_current(scanner, camera, result) is (change is None)


def setup():
    service = LivePanoramaService(None, None)
    localizer = SimpleNamespace(
        coverage_mask=np.full((32, 64), 255, np.uint8),
        target_reference=lambda ray: {"id": "reference"},
    )
    session = Session("session", "camera", "source", {}, localizer, None)
    session.last_registration = time.monotonic()
    return service, session


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['none', 'old', 'future', 'before_stop', 'moving', 'identity', 'generation', 'sequence', 'receipt', 'slow_guard', 'guard_failure'])
async def test_retarget_stop_requires_fresh_bound_observation_and_current_control(monkeypatch, change):
    from unittest.mock import AsyncMock
    import toposync_ext_cameras.live_panorama as module

    clock = [10.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    evidence = {'capture_instance': 'decoder', 'generation': 1, 'sequence': 12}
    frame = {'capture_evidence': evidence, 'received_monotonic': 9.8}
    scanner = SimpleNamespace(last_frame=frame, physical_state='stopped',
        last_stop_accepted_monotonic=9.5, last_stop_receipt={'command_id': 'stop'})
    camera = SimpleNamespace(stop_receipt_is_current=AsyncMock(return_value=True))
    if change in {'old', 'future', 'before_stop'}:
        frame['received_monotonic'] = {'old': 8, 'future': 11, 'before_stop': 9.4}[change]
    elif change == 'moving':
        scanner.physical_state = 'moving'
    elif change in {'identity', 'generation', 'sequence'}:
        evidence.pop({'identity': 'capture_instance', 'generation': 'generation', 'sequence': 'sequence'}[change])
    elif change == 'receipt':
        camera.stop_receipt_is_current.return_value = False
    elif change == 'guard_failure':
        camera.stop_receipt_is_current.side_effect = RuntimeError('Control unavailable')
    elif change == 'slow_guard':
        async def slow(_receipt):
            clock[0] = 11
            return True
        camera.stop_receipt_is_current.side_effect = slow
    assert await module._stopped_control_is_current(scanner, camera) == (change == 'none')


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["none", "receipt", "permission", "binding", "expired", "stop", "closed", "refresh_failure", "task_cancel", "renewal_lost", "metadata_expired", "metadata_failure", "metadata_automation", "metadata_during_stop", "overlap", "overlap_stale", "overlap_failure", "overlap_cancel", "overlap_stop", "continuous_prepare"])
async def test_serial_retarget_keeps_control_capture_and_reference_until_cleanup(tmp_path, monkeypatch, change):
    from unittest.mock import AsyncMock
    from contextlib import asynccontextmanager
    import toposync_ext_cameras.live_panorama as module
    from toposync_ext_cameras.panorama_reference import PanoramaReferenceCoordinator
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError

    service, session = setup()
    session.can_control = True
    events, records = [], []
    cameras = []
    offset = 0
    def now():
        return time.monotonic() + offset
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=now))
    renewal_started, stop_observed = asyncio.Event(), asyncio.Event()
    discovery_started, discovery_release = asyncio.Event(), asyncio.Event()
    endpoint_started, endpoint_release, endpoint_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    preparation_started, preparation_release = asyncio.Event(), asyncio.Event()

    class Coordinator(PanoramaReferenceCoordinator):
        @asynccontextmanager
        async def hold(self, *args, **kwargs):
            async with super().hold(*args, **kwargs):
                events.append('reference-held')
                try:
                    yield
                finally:
                    events.append('reference-released')

    coordinator = Coordinator()

    class Camera:
        lease = False

        async def discover(self):
            return {"motion_automation": {"auto_tracking": False, "automatic_return": False},
                    "velocity_supported": change == 'continuous_prepare'}

        async def prepare_continuous_move(self):
            assert events.count('stop') >= 1
            preparation_started.set()
            try:
                await preparation_release.wait()
            except asyncio.CancelledError:
                events.append('preparation-cancelled')
                # Model asynchronous transport cleanup that must still drain.
                await preparation_release.wait()
            events.append('continuous-prepared')
            return True

        async def refresh_discovery_observations(self):
            nonlocal offset
            if change.startswith('overlap') and session.sequence == 2:
                discovery_started.set()
                await discovery_release.wait()
                if change == 'overlap_stale':
                    offset += 2
                if change == 'overlap_failure':
                    raise PanoramaCaptureError('camera_configuration_changed')
            result = await self.discover()
            if change == 'metadata_automation' and session.sequence == 2:
                result['motion_automation']['auto_tracking'] = True
            return result

        async def refresh_discovery_metadata(self):
            events.append('metadata-renewal')
            renewal_started.set()
            await stop_observed.wait()
            if change == 'metadata_failure':
                raise PanoramaCaptureError('camera_configuration_changed')
            return await self.discover()

        async def acquire(self):
            if self.lease and change == 'renewal_lost':
                raise PanoramaCaptureError('control_lost')
            events.append('guard' if self.lease else 'acquire')
            self.lease = True

        async def close(self):
            assert coordinator._lock('camera', 'source').locked()
            if endpoint_started.is_set():
                assert endpoint_finished.is_set()
            self.lease = False
            events.append('close')

        async def stop_receipt_is_current(self, receipt):
            assert self.lease
            return change != 'receipt'

        async def motion_epoch(self):
            return 1

    class Scanner:
        def __init__(self, camera, directory, progress, cancelled, checkpoint):
            self.camera, self.cancelled = camera, cancelled
            self.acquired, self.physical_state = False, 'unknown'
            self.checkpoint, self.issues = {}, []
            self.capabilities = {}

        def _check(self):
            if self.cancelled():
                raise module._Stopped()

        async def _stop(self):
            assert self.camera.lease
            events.append('stop')
            self.last_stop_receipt = {'accepted': True}
            self.last_stop_accepted_monotonic = now()
            return True

        async def _reference_window(self):
            if change == 'continuous_prepare' and session.sequence == 1:
                await asyncio.wait_for(preparation_started.wait(), .5)
            events.append('window')
            self.last_frame = {'received_monotonic': now(),
                'capture_evidence': {'capture_instance': 'capture', 'generation': 1, 'sequence': len(events)}}
            return self.last_frame

        async def _confirm_stop(self, **kwargs):
            nonlocal offset
            if change == 'continuous_prepare':
                stop_observed.set()
            if session.sequence == 2 and change.startswith('metadata_'):
                if change == 'metadata_during_stop':
                    offset += 2
                else:
                    await asyncio.wait_for(renewal_started.wait(), .5)
                stop_observed.set()
            await self._stop()
            await self._reference_window()
            self.physical_state = 'stopped'

        async def refresh_stopped_frame(self):
            events.append('extend')
            if change == 'refresh_failure':
                raise PanoramaCaptureError('stop_observation_unconfirmed')
            if change.startswith('overlap'):
                endpoint_started.set()
                await endpoint_release.wait()
            result = await self._reference_window()
            endpoint_finished.set()
            return result

    class Navigator:
        commands, trace = 1, []

        def __init__(self, scanner, *args, **kwargs):
            self.scanner = scanner
            self.localization_replay = SimpleNamespace(preserve=lambda *args: None)

        async def prepare_stopped_view(self):
            pass

        async def aim(self, *args, **kwargs):
            nonlocal offset
            assert coordinator._lock('camera', 'source').locked()
            assert self.scanner.camera.lease
            events.append('aim')
            self.scanner.checkpoint['navigation_observation_timings'] = [
                {'stage': 'localization', 'commands': self.commands, 'sequence': session.sequence}
            ]
            if session.sequence == 1:
                if change.startswith('metadata_') and change != 'metadata_during_stop':
                    offset += 2
                session.sequence = 2
                session.pending = (Intention(sequence=2, x=.51, y=.5), now() + (-1 if change == 'expired' else 10))
                if change == 'stop':
                    service.stop(session, Stop(sequence=3))
                if change == 'closed':
                    session.closed = True
                if change == 'task_cancel':
                    raise asyncio.CancelledError()
                raise module._Stopped()
            return {'verified': True}

    def factory(**kwargs):
        camera = Camera()
        cameras.append(camera)
        return camera

    async def current(_session):
        if session.sequence == 2 and change == 'binding':
            raise PanoramaCaptureError('camera_configuration_changed')

    def authorize(*args, **kwargs):
        if session.sequence == 2 and change == 'permission':
            raise HTTPException(403)

    service.panorama = SimpleNamespace(root=tmp_path / 'panorama', services=None,
        reference_coordinator=coordinator, camera_factory=factory, native_references=lambda *args: [],
        _atomic=lambda path, record: records.append(record))
    service.sources = SimpleNamespace(_authorize=authorize)
    service.current = current
    monkeypatch.setattr(module, '_Scan', Scanner)
    monkeypatch.setattr(module, 'VisualNavigator', Navigator)
    monkeypatch.setattr(module, '_arrival_stop_is_current', AsyncMock(return_value=False))
    if change.startswith('metadata_'):
        session.prepared_camera = factory()
        session.preparation = asyncio.get_running_loop().create_future()
        session.preparation.set_result(await session.prepared_camera.discover())
        session.prepared_at = now() - 29
    service.intend(session, Intention(sequence=1, x=.5, y=.5))
    if change == 'continuous_prepare':
        await asyncio.wait_for(stop_observed.wait(), .5)
        assert preparation_started.is_set() and not session.task.done()
        assert events.count('aim') == 1 and events.count('stop') == 2 and not events.count('close')
        assert 'preparation-cancelled' in events
        preparation_release.set()
    if change.startswith('overlap'):
        await asyncio.wait_for(asyncio.gather(discovery_started.wait(), endpoint_started.wait()), .5)
        assert events.count('aim') == 1 and events.count('close') == 0
        if change == 'overlap_stale':
            endpoint_release.set()
            await endpoint_finished.wait()
        if change == 'overlap_cancel':
            session.task.cancel()
        elif change == 'overlap_stop':
            service.stop(session, Stop(sequence=3))
        discovery_release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if change != 'overlap_stale':
            assert events.count('aim') == 1 and events.count('close') == 0
        endpoint_release.set()
    if change in {'task_cancel', 'overlap_cancel'}:
        with pytest.raises(asyncio.CancelledError):
            await session.task
    else:
        await session.task
    assert len(cameras) == 1 and not cameras[0].lease
    assert events.count('acquire') == 1 and events.count('close') == 1
    assert events[-2:] == ['close', 'reference-released']
    assert events.count('reference-held') == events.count('reference-released') == 1
    assert session.control_chain is None and not service.camera_owners
    assert not coordinator._lock('camera', 'source').locked()
    assert events.count('aim') == (2 if change in {'none', 'receipt', 'metadata_expired', 'metadata_during_stop', 'overlap', 'overlap_stale', 'continuous_prepare'} else 1), (events, session.error, records)
    assert events.count('extend') == (1 if change.startswith('overlap') or change in {'none', 'refresh_failure', 'metadata_expired', 'metadata_during_stop', 'metadata_automation', 'continuous_prepare'} else 0)
    if change in {'none', 'receipt', 'metadata_expired', 'metadata_during_stop', 'overlap', 'overlap_stale', 'continuous_prepare'}:
        assert session.result['sequence'] == 2
        assert records[1]['observation_timings'] == [
            {'stage': 'localization', 'commands': 1, 'sequence': 2}
        ]
        assert records[0]['result'] is None and records[0]['superseded']
        assert records[0]['elapsed_seconds']['retarget_control_retained']
        assert records[1]['elapsed_seconds']['retarget_stop_continued'] == (change not in {'receipt', 'overlap_stale'})
        assert events.count('stop') == (4 if change in {'receipt', 'overlap_stale'} else 3)
        if change == 'continuous_prepare':
            assert records[0]['elapsed_seconds']['continuous_metadata_prepared']
    else:
        assert session.result is None and session.pending is None

    if change.startswith('metadata_'):
        assert events.count('metadata-renewal') == 1
        if change != 'metadata_failure':
            assert records[0]['elapsed_seconds']['retarget_metadata_renewed']


@pytest.mark.anyio
async def test_latest_intention_wins_and_old_network_requests_cannot_replace_it():
    service, session = setup()
    started = asyncio.Event()
    finish = asyncio.Event()
    entered = []
    active = 0
    maximum = 0

    async def operation(current, intention, expires):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.append(intention.sequence)
        if intention.sequence == 1:
            started.set()
            await finish.wait()
        if current.sequence == intention.sequence:
            current.result = {"sequence": intention.sequence}
        active -= 1

    service._operate = operation
    service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    await started.wait()
    service.intend(session, Intention(sequence=2, x=0.51, y=0.5))
    service.intend(session, Intention(sequence=3, x=0.52, y=0.5))
    service.intend(session, Intention(sequence=2, x=0.51, y=0.5))
    assert session.pending[0].sequence == 3
    finish.set()
    await session.task
    assert entered == [1, 3] and maximum == 1
    assert session.result == {"sequence": 3}
    assert not service.camera_owners


@pytest.mark.anyio
@pytest.mark.parametrize("close", [False, True])
async def test_stop_or_unmount_cancels_pending_and_fences_delayed_click(close):
    service, session = setup()
    started = asyncio.Event()
    finish = asyncio.Event()
    entered = []

    async def operation(current, intention, expires):
        entered.append(intention.sequence)
        started.set()
        await finish.wait()

    service._operate = operation
    service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    await started.wait()
    service.intend(session, Intention(sequence=2, x=0.5, y=0.5))
    service.stop(session, Stop(sequence=3))
    service.intend(session, Intention(sequence=2, x=0.5, y=0.5))
    if close:
        session.closed = True
    finish.set()
    await session.task
    assert entered == [1] and session.pending is None
    assert session.last_registration == 0


@pytest.mark.anyio
async def test_control_failure_drops_pending_without_retry():
    service, session = setup()
    started = asyncio.Event()
    finish = asyncio.Event()
    entered = []

    async def operation(current, intention, expires):
        entered.append(intention.sequence)
        started.set()
        await finish.wait()
        raise RuntimeError("simulated timeout")

    service._operate = operation
    service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    await started.wait()
    service.intend(session, Intention(sequence=2, x=0.5, y=0.5))
    finish.set()
    await session.task
    assert entered == [1] and session.pending is None and session.phase == "error"


def test_no_movement_without_current_registration_or_coverage_or_exclusivity():
    service, session = setup()
    session.last_registration = 0
    with pytest.raises(HTTPException):
        service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    session.last_registration = time.monotonic()
    session.localizer.coverage_mask[:] = 0
    with pytest.raises(HTTPException):
        service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    session.localizer.coverage_mask[:] = 255
    service.camera_owners["camera"] = "another-session"
    with pytest.raises(HTTPException):
        service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
    assert session.sequence == 0 and session.task is None


def test_uncertain_effect_requires_requalification():
    service, session = setup()
    session.blocked = True
    with pytest.raises(HTTPException):
        service.intend(session, Intention(sequence=1, x=0.5, y=0.5))


@pytest.mark.anyio
async def test_expired_target_waiting_for_reference_lock_never_opens_camera():
    from contextlib import asynccontextmanager

    service, session = setup()

    @asynccontextmanager
    async def hold(*args):
        yield

    service.panorama = SimpleNamespace(reference_coordinator=SimpleNamespace(hold=hold))
    service.sources = SimpleNamespace(_authorize=lambda *args, **kwargs: None)

    async def current(session):
        pass

    service.current = current
    session.sequence = 1
    with pytest.raises(HTTPException) as caught:
        await service._operate(session, Intention(sequence=1, x=0.5, y=0.5), time.monotonic() - 1)
    assert caught.value.detail["code"] == "intention_expired"


def test_permission_revocation_cancels_owned_operation_and_pending():
    service, session = setup()
    session.can_control = True
    session.sequence = 2
    session.pending = (Intention(sequence=3, x=0.5, y=0.5), time.monotonic() + 10)
    service.sessions[session.id] = session

    def authorize(request, camera, control=False):
        if control:
            raise HTTPException(403, detail={"code": "forbidden"})

    service.sources = SimpleNamespace(_authorize=authorize)
    service.session(None, session.id)
    assert not session.can_control and session.sequence == 3 and session.pending is None
    assert session.error == "camera_control_permission_required"


@pytest.mark.anyio
@pytest.mark.parametrize("superseded", [False, True])
async def test_observation_geometry_and_late_pose_are_bound_to_current_intention(superseded):
    import base64
    import cv2
    import threading
    from toposync_ext_cameras.live_panorama import Observation

    service, session = setup()
    started, finished = threading.Event(), threading.Event()
    seen = []

    def locate(image, evidence, geometry, *, reference_candidates=False):
        assert reference_candidates
        seen.append((image.shape, geometry))
        started.set()
        finished.wait(timeout=3)
        return {"status": "localized", "rotation_matrix": np.eye(3).tolist()}

    session.localizer = SimpleNamespace(lens={"width": 128, "height": 96}, locate=locate)

    async def current(session):
        pass

    service.current = current
    encoded = base64.b64encode(cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))[1]).decode()
    body = Observation(
        sequence=1, epoch="decoder", media_time=0.1, width=128, height=96, image=encoded
    )
    task = asyncio.create_task(service.observe(session, body))
    await asyncio.to_thread(started.wait, 3)
    if superseded:
        session.sequence = 1
    finished.set()
    result = await task
    assert seen[0][1]["to_source"] == [[2, 0, 0.5], [0, 2, 0.5], [0, 0, 1]]
    if superseded:
        assert result["reason"] == "superseded_observation" and session.last_registration == 0
    else:
        assert result["status"] == "localized" and result["observation_sequence"] == 1
        assert result["sequence"] == 0 and session.last_registration > 0
        with pytest.raises(HTTPException):
            await service.observe(session, body)


@pytest.mark.parametrize("limit", [0, -1, 17, True, 1.5, "14"])
def test_intention_cannot_expand_or_coerce_physical_command_budget(limit):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Intention(sequence=1, x=.5, y=.5, maximum_commands=limit)
    assert Intention(sequence=1, x=.5, y=.5).maximum_commands == 16


@pytest.mark.anyio
@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize("confirmed_stop", [False, True])
@pytest.mark.parametrize("command_limit", [14, 16])
@pytest.mark.parametrize("reusable_arrival", [False, True])
@pytest.mark.parametrize("prepared", ["none", "fresh", "expired", "inflight"])
async def test_final_stop_fences_pending_result_and_binds_its_own_controller_epoch(
    monkeypatch, tmp_path, superseded, confirmed_stop, command_limit, reusable_arrival, prepared, departure="none"
):
    from contextlib import asynccontextmanager
    import toposync_ext_cameras.live_panorama as module

    service, session = setup()
    session.sequence = 1
    session.can_control = True
    events = []
    reused = reusable_arrival and not superseded
    destination = {"kind": "preset", "preserve_zoom": True}
    reference = {"destination": destination, "ray": [1, 0, 0]}
    monkeypatch.setattr(module, "select_direct_native_reference", lambda *args: reference if departure == "direct" else None)
    if departure in {"fresh", "expired"}:
        session.departure_hint = {"rotation_matrix": np.eye(3).tolist(),
                                  "observed_at": time.monotonic() - (2 if departure == "expired" else 0)}
        session.last_registration = 0  # intend consumes pointing eligibility independently.
        monkeypatch.setattr(module, "select_native_reference", lambda *args: reference)

    async def reusable(*_args):
        return reusable_arrival

    monkeypatch.setattr(module, "_arrival_stop_is_current", reusable)

    @asynccontextmanager
    async def hold(*args):
        yield

    class Camera:
        async def bind_reference_destination(self, value):
            assert value == destination
            events.append("bind-reference")

        async def discover(self):
            events.append("discover")
            return {"motion_automation": {"auto_tracking": False, "automatic_return": False}}

        async def refresh_discovery_observations(self):
            events.append("refresh")
            return {"motion_automation": {"auto_tracking": False, "automatic_return": False}}

        async def acquire(self):
            events.append("acquire")

        async def position(self):
            return {}

        async def close(self):
            events.append("close")

        async def motion_epoch(self):
            assert events[-1] == ('aim' if reused else 'confirm-stop')
            events.append('final-epoch')
            return 17

    class Scanner:
        def __init__(self, *args):
            self.acquired = False
            self.physical_state = "unknown"
            self.checkpoint = {}

        def _check(self):
            pass

        async def _stop(self):
            events.append("initial-stop")
            return True

        async def _reference_window(self):
            events.append("fresh-observation")
            return {}

        async def _confirm_stop(self, *, observation_timeout, reuse_accepted=False):
            assert observation_timeout == 12.0
            assert reuse_accepted is superseded
            events.append("confirm-stop")
            self.physical_state = "stopped" if confirmed_stop else "unknown"

    class Navigator:
        commands = 1
        trace = []

        def __init__(self, *args, maximum_commands, stable_frame_observer):
            assert callable(stable_frame_observer)
            assert maximum_commands == command_limit
            assert maximum_commands <= module.MAXIMUM_LIVE_NAVIGATION_COMMANDS == 16
            from toposync_ext_cameras.processing.panorama_localization_replay import LocalizationReplay
            self.localization_replay = LocalizationReplay()

        async def prepare_stopped_view(self):
            events.append("prepare-view")

        async def aim(self, ray, **kwargs):
            if departure in {"fresh", "direct"}:
                assert kwargs == {"native_destination": destination, "native_ray": [1, 0, 0]}
            else:
                assert "native_destination" not in kwargs
            events.append("aim")
            if superseded:
                session.sequence = 2
            session.pending = (Intention(sequence=2, x=0.51, y=0.5), time.monotonic() + 10)
            return {"verified": True}

    service.panorama = SimpleNamespace(
        root=tmp_path / "panorama",
        services=None,
        reference_coordinator=SimpleNamespace(hold=hold),
        camera_factory=lambda **kwargs: Camera(),
        native_references=lambda *args: [reference] if departure != "none" else [],
        _atomic=lambda *args: None,
    )
    service.sources = SimpleNamespace(_authorize=lambda *args, **kwargs: None)

    async def current(session):
        pass

    service.current = current
    monkeypatch.setattr(module, "_Scan", Scanner)
    monkeypatch.setattr(module, "VisualNavigator", Navigator)
    if prepared != "none":
        session.prepared_camera = Camera()
        session.preparation = asyncio.create_task(asyncio.sleep(0, result={}))
        if prepared != "inflight":
            await session.preparation
            session.prepared_at = time.monotonic() - (31 if prepared == "expired" else 0)
        else:
            assert not session.preparation.done() and session.prepared_at == 0
    await service._operate(session, Intention(sequence=1, x=0.5, y=0.5, maximum_commands=command_limit), time.monotonic() + 10)
    assert events == [
        *(["close"] if prepared == "expired" else []),
        "refresh" if prepared in {"fresh", "inflight"} else "discover",
        "acquire",
        "initial-stop",
        "fresh-observation",
        "bind-reference" if departure in {"fresh", "direct"} else "prepare-view",
        "aim",
        *([] if reused else ["confirm-stop"]),
        *(['final-epoch'] if (confirmed_stop or reused) and not superseded else []),
        "close",
    ]
    if not confirmed_stop and not reused:
        assert session.blocked and session.pending is None and session.result is None
        assert session.error == "stop_unconfirmed"
    else:
        assert not session.blocked
        assert session.result == (None if superseded else {'sequence': 1, 'verified': True, 'motion_epoch': 17})
    assert session.commands == 1
    assert session.departure_hint is None
    if confirmed_stop or reused:
        assert session.preparation is not None
        assert 0 <= time.monotonic() - session.prepared_at < 30


@pytest.mark.anyio
@pytest.mark.parametrize("departure", ["fresh", "expired", "direct"])
@pytest.mark.parametrize("confirmed_stop", [True, False])
async def test_native_departure_hint_does_not_skip_stopped_observation_or_final_stop(
    monkeypatch, tmp_path, departure, confirmed_stop
):
    await test_final_stop_fences_pending_result_and_binds_its_own_controller_epoch(
        monkeypatch, tmp_path, False, confirmed_stop, 16, False, "fresh", departure
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "automation,reason",
    [
        ({"auto_tracking": None, "automatic_return": None}, "motion_automation_unqualified"),
        ({"auto_tracking": True, "automatic_return": False}, "external_automation_active"),
    ],
)
@pytest.mark.parametrize("prepared", [False, True, "retarget"])
async def test_unknown_or_active_native_automation_never_acquires_or_moves(
    monkeypatch, tmp_path, automation, reason, prepared
):
    from contextlib import asynccontextmanager
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError

    service, session = setup()
    session.sequence = 1
    events = []

    @asynccontextmanager
    async def hold(*args):
        yield

    class Camera:
        async def discover(self):
            return {"motion_automation": automation}

        async def refresh_discovery_observations(self):
            events.append("refresh")
            return {"motion_automation": automation}

        async def acquire(self):
            events.append("acquire")
            raise AssertionError("must not acquire")

        async def close(self):
            events.append("close")

    service.panorama = SimpleNamespace(
        root=tmp_path / "panorama",
        services=None,
        reference_coordinator=SimpleNamespace(hold=hold),
        camera_factory=lambda **kwargs: Camera(),
        _atomic=lambda *args: None,
    )
    service.sources = SimpleNamespace(_authorize=lambda *args, **kwargs: None)

    async def current(session):
        return {"control": {"automation_exclusive_control_confirmed": False}}

    service.current = current
    if prepared:
        camera = Camera()
        capabilities = {"motion_automation": {"auto_tracking": False, "automatic_return": False}}
        if prepared == "retarget":
            session.can_control = True
            session.pending = (Intention(sequence=2, x=.51, y=.5), time.monotonic() + 10)
            assert service._retain_retarget_metadata(session, camera, capabilities, time.monotonic(),
                                                     stopped=True, failed=False)
        else:
            session.prepared_camera = camera
            session.preparation = asyncio.create_task(asyncio.sleep(0, result=capabilities))
        await session.preparation
        session.prepared_at = time.monotonic()
    with pytest.raises(PanoramaCaptureError, match=reason):
        await service._operate(session, Intention(sequence=1, x=0.5, y=0.5), time.monotonic() + 10)
    assert events == (["refresh"] if prepared else []) + ["close"] and session.commands == 0


@pytest.mark.anyio
@pytest.mark.parametrize("condition", ["ready", "expired", "future", "closed", "blocked", "no_permission",
                                     "no_pending", "failed", "unconfirmed", "existing_preparation"])
async def test_retarget_metadata_reuse_is_bounded_and_never_carries_control(condition):
    from unittest.mock import AsyncMock
    service, session = setup()
    session.can_control = condition != "no_permission"
    session.closed = condition == "closed"
    session.blocked = condition == "blocked"
    session.pending = None if condition == "no_pending" else (Intention(sequence=2, x=.51, y=.5), time.monotonic() + 10)
    camera = SimpleNamespace(refresh_discovery_observations=AsyncMock(), acquire=AsyncMock(), close=AsyncMock())
    prepared_at = time.monotonic() - (31 if condition == "expired" else -1 if condition == "future" else 5)
    existing = None
    if condition == "existing_preparation":
        existing = session.preparation = asyncio.get_running_loop().create_future()
        existing.set_result({})
    accepted = service._retain_retarget_metadata(session, camera, {"protocol": "qualified"}, prepared_at,
                                                stopped=condition != "unconfirmed", failed=condition == "failed")
    assert accepted is (condition == "ready")
    camera.acquire.assert_not_awaited()
    camera.refresh_discovery_observations.assert_not_awaited()
    if accepted:
        assert session.prepared_at == prepared_at  # Chained retargets cannot renew metadata age.
        assert await session.preparation == {"protocol": "qualified"}
        assert session.prepared_camera is camera
        await service._discard_preparation(session)
        camera.close.assert_awaited_once()
    else:
        assert session.preparation is existing and session.prepared_camera is None


@pytest.mark.anyio
@pytest.mark.parametrize("condition", ["ready", "closed", "blocked", "no_permission", "moving"])
async def test_background_discovery_never_takes_control_and_closing_drains_it(tmp_path, condition):
    from unittest.mock import AsyncMock
    service, session = setup()
    session.can_control = condition != "no_permission"
    session.closed = condition == "closed"
    session.blocked = condition == "blocked"
    if condition == "moving":
        session.task = asyncio.get_running_loop().create_future()
    started, drained = asyncio.Event(), asyncio.Event()

    async def discover():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    camera = SimpleNamespace(discover=discover, acquire=AsyncMock(), close=AsyncMock())
    service.panorama = SimpleNamespace(root=tmp_path / "panorama", services=None, camera_factory=lambda **kwargs: camera)
    await service._start_preparation(session)
    task = session.preparation
    await service._start_preparation(session)
    assert session.preparation is task
    if condition == "ready":
        await asyncio.wait_for(started.wait(), 1)
        await service._discard_preparation(session)
        assert drained.is_set() and task.cancelled()
        camera.close.assert_awaited_once()
        assert session.prepared_camera is None and session.preparation is None
    else:
        assert task is None and not started.is_set()
        camera.close.assert_not_awaited()
    camera.acquire.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("condition", ["fresh", "expired", "closed_during_cleanup", "moving_during_cleanup"])
async def test_active_observation_renews_expired_discovery_without_competing_with_click(tmp_path, condition):
    from unittest.mock import AsyncMock
    service, session = setup()
    session.can_control = True
    old = SimpleNamespace(close=AsyncMock())
    new = SimpleNamespace(discover=AsyncMock(return_value={}), acquire=AsyncMock(), close=AsyncMock())
    factory = []

    def create(**kwargs):
        factory.append(kwargs)
        return new

    async def close():
        if condition == "closed_during_cleanup":
            session.closed = True
        if condition == "moving_during_cleanup":
            session.task = asyncio.get_running_loop().create_future()

    old.close.side_effect = close
    service.panorama = SimpleNamespace(root=tmp_path / "panorama", services=None, camera_factory=create)
    session.prepared_camera = old
    session.preparation = asyncio.create_task(asyncio.sleep(0, result={}))
    await session.preparation
    session.prepared_at = time.monotonic() - (0 if condition == "fresh" else 31)
    await service._start_preparation(session)
    if condition == "expired":
        assert len(factory) == 1 and session.prepared_camera is new
        await session.preparation
        new.discover.assert_awaited_once()
    elif condition == "fresh":
        assert not factory and session.prepared_camera is old
        old.close.assert_not_awaited()
    else:
        assert not factory and session.preparation is None
    new.acquire.assert_not_awaited()
    await service._discard_preparation(session)
    old.close.assert_awaited_once()


@pytest.mark.anyio
async def test_reopening_an_artifact_removed_from_selection_requires_new_catalog():
    from toposync_ext_cameras.live_panorama import OpenSession

    service, session = setup()

    async def context(*args):
        return {}, {"id": "camera"}, {}

    service.sources = SimpleNamespace(
        _authorize=lambda *args, **kwargs: None,
        _context=context,
        _pointer=lambda source: {"active": {"artifact_id": "b" * 32}},
    )
    with pytest.raises(HTTPException) as caught:
        await service.open(
            None,
            OpenSession(camera_id="camera", source_id="source", artifact_id="a" * 32, revision=1),
        )
    assert caught.value.detail["code"] == "source_panorama_changed" and not service.sessions


@pytest.mark.anyio
async def test_slow_recognition_cannot_grant_pointing_authority(monkeypatch):
    import base64
    import cv2
    import toposync_ext_cameras.live_panorama as module

    service, session = setup()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def current(_session):
        pass

    def locate(*args, **kwargs):
        clock[0] += 2
        return {"status": "localized", "rotation_matrix": np.eye(3).tolist()}

    service.current = current
    session.localizer.lens = {"width": 128, "height": 96}
    session.localizer.locate = locate
    encoded = base64.b64encode(cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))[1]).decode()
    result = await service.observe(session, module.Observation(
        sequence=1, epoch="decoder", media_time=0.1, width=128, height=96, image=encoded))
    assert result["status"] == "unlocalized" and result["reason"] == "panorama_frame_not_recent"
    assert "geometry" not in result and session.last_registration == 0
    with pytest.raises(HTTPException):
        service.intend(session, Intention(sequence=1, x=0.5, y=0.5))
