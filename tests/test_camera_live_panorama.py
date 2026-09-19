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

    def locate(image, evidence, geometry):
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


@pytest.mark.anyio
@pytest.mark.parametrize("superseded", [False, True])
async def test_unknown_stop_discards_pending_and_never_reports_verified_current_target(
    monkeypatch, tmp_path, superseded
):
    from contextlib import asynccontextmanager
    import toposync_ext_cameras.live_panorama as module

    service, session = setup()
    session.sequence = 1
    events = []

    @asynccontextmanager
    async def hold(*args):
        yield

    class Camera:
        async def discover(self):
            events.append("discover")
            return {"motion_automation": {"auto_tracking": False, "automatic_return": False}}

        async def acquire(self):
            events.append("acquire")

        async def position(self):
            return {}

        async def close(self):
            events.append("close")

    class Scanner:
        def __init__(self, *args):
            self.acquired = False
            self.physical_state = "unknown"

        def _check(self):
            pass

        async def _stop(self):
            events.append("initial-stop")
            return True

        async def _reference_window(self):
            events.append("fresh-observation")
            return {}

        async def _confirm_stop(self):
            events.append("confirm-stop")
            self.physical_state = "unknown"

    class Navigator:
        commands = 1
        trace = []

        def __init__(self, *args, maximum_commands):
            assert maximum_commands == module.MAXIMUM_LIVE_NAVIGATION_COMMANDS == 12

        async def aim(self, ray):
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
        _atomic=lambda *args: None,
    )
    service.sources = SimpleNamespace(_authorize=lambda *args, **kwargs: None)

    async def current(session):
        pass

    service.current = current
    monkeypatch.setattr(module, "_Scan", Scanner)
    monkeypatch.setattr(module, "VisualNavigator", Navigator)
    await service._operate(session, Intention(sequence=1, x=0.5, y=0.5), time.monotonic() + 10)
    assert events == [
        "discover",
        "acquire",
        "initial-stop",
        "fresh-observation",
        "aim",
        "confirm-stop",
        "close",
    ]
    assert session.blocked and session.pending is None and session.result is None
    assert session.error == "stop_unconfirmed" and session.commands == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "automation,reason",
    [
        ({"auto_tracking": None, "automatic_return": None}, "motion_automation_unqualified"),
        ({"auto_tracking": True, "automatic_return": False}, "external_automation_active"),
    ],
)
async def test_unknown_or_active_native_automation_never_acquires_or_moves(
    monkeypatch, tmp_path, automation, reason
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
    with pytest.raises(PanoramaCaptureError, match=reason):
        await service._operate(session, Intention(sequence=1, x=0.5, y=0.5), time.monotonic() + 10)
    assert events == ["close"] and session.commands == 0


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
