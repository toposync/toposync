"""Bounded concurrent pose observations do not authorize or command a camera."""
import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from toposync_ext_cameras import panorama_navigation as navigation


def frame(sequence=1, *, generation=1, received=10.):
    return {"image": np.zeros((54, 96, 3), np.uint8), "received_monotonic": received,
        "capture_evidence": {"capture_instance": "test", "generation": generation,
            "sequence": sequence, "published_at": 100. + sequence}}


def localizer(locate=None):
    result = {"status": "localized", "rotation_matrix": np.eye(3).tolist(),
        "analysis_width": 96, "validation_p95_pixels": 1., "validation_matches": 20,
        "inlier_fraction": .9, "reference_id": "reference"}
    return SimpleNamespace(lens={"width": 96, "height": 54, "fx": 60., "fy": 60.,
        "cx": 47.5, "cy": 26.5, "distortion": []}, locate=locate or (lambda _image, evidence, **_kwargs: {**result, "capture_evidence": dict(evidence)}))


def test_latest_only_worker_bounds_backlog_and_never_certifies_arrival(monkeypatch):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    started, release = threading.Event(), threading.Event()
    calls, records = [], []
    model = localizer()
    original = model.locate
    def locate(image, evidence, **options):
        calls.append(evidence["sequence"])
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        return original(image, evidence)
    model.locate = locate
    async def scenario():
        observer = navigation.MotionFrameObservations(model, records, 7)
        observer.offer(frame())
        assert await asyncio.to_thread(started.wait, 2)
        for sequence in (2, 3, 4):
            clock[0] += .11
            observer.offer(frame(sequence, received=clock[0]))
        release.set()
        await observer.worker
        await observer.close()
        observer.offer(frame(5, received=clock[0]))
    asyncio.run(scenario())
    assert calls == [1, 4]
    assert [r["capture_evidence"]["sequence"] for r in records] == [1, 4]
    assert all(r["kind"] == "visual_motion_observation" and r["arrival_verified"] is False for r in records)
    assert all(r["frame_geometry"]["capture_evidence"] == r["capture_evidence"] for r in records)
    assert all(r["command"] == 7 and "stop_confirmed" not in r and "verified" not in r for r in records)


@pytest.mark.parametrize("case", ["old", "future", "missing_identity", "invalid_time", "duplicate"])
def test_unusable_frames_do_not_schedule_more_localization(monkeypatch, case):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    calls, records = [], []
    model = localizer(lambda *args, **kwargs: calls.append(args) or {"status": "unlocalized", "reason": "no_support"})
    async def scenario():
        observer = navigation.MotionFrameObservations(model, records, 1)
        candidate = frame()
        if case == "old": candidate["received_monotonic"] = 8.
        if case == "future": candidate["received_monotonic"] = 11.
        if case == "missing_identity": candidate["capture_evidence"] = {}
        if case == "invalid_time": candidate["received_monotonic"] = float("nan")
        observer.offer(candidate)
        if observer.worker:
            await observer.worker
        if case == "duplicate": observer.offer(candidate)
        await observer.close()
    asyncio.run(scenario())
    assert len(calls) == (1 if case == "duplicate" else 0)


@pytest.mark.parametrize("failure", ["expired", "unlocalized", "exception", "geometry", "binding"])
def test_unqualified_localization_never_publishes_frame_geometry(monkeypatch, failure):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model, records = localizer(), []
    original = model.locate
    def locate(*args, **kwargs):
        if failure == "exception": raise RuntimeError("private failure detail")
        if failure == "expired": clock[0] += 1.1
        if failure == "unlocalized": return {"status": "unlocalized", "reason": "panorama_visual_support_insufficient"}
        result = original(*args, **kwargs)
        if failure == "geometry": result["validation_matches"] = 0
        if failure == "binding": result["capture_evidence"]["sequence"] += 1
        return result
    model.locate = locate
    async def scenario():
        observer = navigation.MotionFrameObservations(model, records, 1)
        observer.offer(frame())
        await observer.worker
        await observer.close()
    asyncio.run(scenario())
    assert len(records) == 1 and "frame_geometry" not in records[0]
    assert records[0]["status"] == "unlocalized" and not records[0]["arrival_verified"]
    assert "private failure detail" not in str(records)
    expected_reason = {"expired": "panorama_frame_not_recent", "geometry": "motion_frame_geometry_unqualified",
        "unlocalized": "panorama_visual_support_insufficient", "exception": "motion_observation_failed",
        "binding": "panorama_frame_binding_mismatch"}
    assert records[0]["reason"] == expected_reason[failure]


def test_close_and_epoch_change_discard_pending_and_drain_running_worker(monkeypatch):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    records = []
    def locate(*args):
        started.set()
        assert release.wait(2)
        finished.set()
        return localizer().locate(*args)
    async def scenario():
        observer = navigation.MotionFrameObservations(localizer(locate), records, 1)
        observer.offer(frame())
        assert await asyncio.to_thread(started.wait, 2)
        clock[0] += .11
        observer.offer(frame(2, generation=2, received=clock[0]))
        assert observer.closed and observer.pending is None
        closing = asyncio.create_task(observer.close())
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await closing
        assert finished.is_set() and observer.worker.done()
    asyncio.run(scenario())
    assert not records


def test_observation_history_is_bounded(monkeypatch):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    records = []
    async def scenario():
        observer = navigation.MotionFrameObservations(localizer(), records, 1)
        for sequence in range(1, 41):
            observer.offer(frame(sequence, received=clock[0]))
            await observer.worker
            clock[0] += .11
        await observer.close()
    asyncio.run(scenario())
    assert len(records) == 32 and records[0]["capture_evidence"]["sequence"] == 9


@pytest.mark.parametrize("failed", [False, True])
def test_following_pulse_uses_shared_observer_and_closes_it_on_failure(monkeypatch, failed):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    offered = []
    async def persist():
        pass
    async def pulse(axis, direction, duration, **options):
        observer = options["frame_observer"]
        offered.append(observer)
        observer(frame())
        await observer.__self__.worker
        if failed:
            raise navigation.PanoramaCaptureError("capture_cancelled")
        return {"frame": frame(), "stable": True, "match": {}}
    camera = SimpleNamespace(dispatch_guard=None)
    scanner = SimpleNamespace(last_frame=frame(), checkpoint={}, _check=lambda: None,
        _persist=persist, _pulse=pulse, capabilities={"velocity_supported": True},
        physical_state="stopped", camera=camera)
    navigator = navigation.VisualNavigator(scanner, localizer())
    navigator.dispatch_guard = lambda: None
    async def scenario():
        if failed:
            with pytest.raises(navigation.PanoramaCaptureError): await navigator._pulse("pan", .12)
        else:
            await navigator._pulse("pan", .12)
    asyncio.run(scenario())
    records = scanner.checkpoint["navigation_motion_observations"]
    assert len(records) == 1 and "frame_geometry" in records[0]
    assert records[0]["arrival_verified"] is False
    assert camera.dispatch_guard is None and offered[0].__self__.closed
    assert scanner.physical_state == "stopped"
