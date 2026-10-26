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
@pytest.mark.parametrize("window", [False, True])
def test_following_pulse_uses_shared_observer_and_closes_it_on_failure(monkeypatch, failed, window):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    offered = []
    async def persist():
        pass
    async def pulse(axis, direction, duration, **options):
        assert options.get("following_seconds") == (6. if window else None)
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
    if window:
        navigator.following_seconds = 6.
        navigator.motion_feedback_target = lambda: ([1., 0., 0.], 12.)
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


@pytest.mark.parametrize("case, expected", [
    ("toward", None), ("reached", "visual_feedback_target_reached"),
    ("passed", "visual_feedback_direction_changed"), ("old", "visual_feedback_expired"),
    ("failed", "visual_feedback_unavailable"), ("binding", "visual_feedback_binding_changed"),
    ("epoch", "visual_feedback_capture_changed"), ("previous_command", None),
])
def test_motion_feedback_stops_only_from_current_operation_evidence(monkeypatch, case, expected):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    model = localizer()
    navigator = navigation.VisualNavigator(SimpleNamespace(), model)
    navigator.commands = 1
    navigator.response["pan"] = np.array([-.2, 0.])
    angle = .5 if case != "passed" else -.5
    if case == "reached": angle = 0.
    navigator.motion_feedback_target = lambda: ([np.cos(angle), np.sin(angle), 0.], 12.)
    observed = frame()
    geometry = navigation.arrival_frame_geometry(observed, model.locate(observed['image'], observed['capture_evidence']), model.lens, [1, 0, 0])
    record = {"command": 1, "status": "localized", "frame_received_monotonic": 10.,
        "capture_evidence": observed['capture_evidence'], "frame_geometry": geometry}
    if case == "old": record["frame_received_monotonic"] = 8.
    if case == "failed": record["status"] = "unlocalized"
    if case == "binding": record["capture_evidence"] = {}
    if case == "previous_command": record["command"] = 0
    observer = SimpleNamespace(closed=case == "epoch", latest=record, command=1)
    assert navigator._motion_stop_reason(observer, "pan", 1) == expected
    # An old entry in persistent history cannot stop a new operation.
    observer.latest = None
    observer.closed = False
    assert navigator._motion_stop_reason(observer, "pan", 1) is None


def test_motion_feedback_checks_target_validity_before_first_pose():
    from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}), localizer())
    def expired():
        raise PanoramaCaptureError("following_observation_expired")
    navigator.motion_feedback_target = expired
    with pytest.raises(PanoramaCaptureError, match="following_observation_expired"):
        navigator._motion_stop_reason(SimpleNamespace(closed=False, latest=None), "pan", 1)


@pytest.mark.parametrize("case", ["valid", "one_interval", "stale", "new_segment", "wrong_direction", "target_changed", "budget", "unsupported_space"])
def test_velocity_feedback_uses_measured_in_motion_rate_and_fresh_dispatch(monkeypatch, case):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    capabilities = {"defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    if case == "unsupported_space": capabilities["spaces"] = {}
    navigator = navigation.VisualNavigator(SimpleNamespace(capabilities=capabilities), model)
    navigator.commands = 1
    navigator.trace = [{}]
    navigator.response["pan"] = np.array([-999., 0.])  # A pulse gain must not drive this rate calculation.
    target = [.28]
    navigator.motion_feedback_target = lambda: ([np.cos(target[0]), np.sin(target[0]), 0.], 12.)
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .1, "tilt": 0.}, "accepted_monotonic": 10., "deadline_monotonic": 11.}
    plans = []
    for i in range(2 if case == "one_interval" else 3):
        clock[0] = 10.1 + i * .1
        angle = i * .1 * (-1 if case == "wrong_direction" else 1)
        c, t = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "decoder", "generation": 1, "sequence": i+1}
        geometry = {"rotation_matrix": [[c, 0, t], [0, 1, 0], [-t, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24, "inlier_fraction": .9,
            "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": clock[0],
            "capture_evidence": capture, "frame_geometry": geometry}
        if case == "new_segment" and i == 2: context["accepted_monotonic"] = clock[0]
        if case == "stale" and i == 2: clock[0] += 1.
        plans.append(feedback(context))
    if case in {"one_interval", "stale", "new_segment", "wrong_direction", "unsupported_space"}:
        assert all(plan is None for plan in plans)
        return
    assert plans[:-1] == [None, None]
    plan = plans[-1]
    assert plan["velocity"] == {"pan": .025, "tilt": 0.}
    assert plan["measurement"]["timing_basis"] == "local_frame_receipt"
    assert [capture["sequence"] for capture in plan["measurement"]["capture_evidence"]] == [1, 2, 3]
    if case == "target_changed":
        target[0] += .1
        with pytest.raises(navigation.PanoramaCaptureError, match="visual_feedback_target_changed"):
            plan["dispatch_guard"]()
    else:
        plan["dispatch_guard"]()
    if case == "budget":
        navigator.commands = navigator.maximum_commands
        with pytest.raises(navigation.PanoramaCaptureError, match="visual_navigation_budget_exhausted"):
            plan["reserve"]({"state": "pending"})
        assert navigator.commands == navigator.maximum_commands
        assert navigator.trace == [{}]
    else:
        entry = {"state": "pending"}
        plan["reserve"](entry)
        assert navigator.commands == 2
        assert navigator.trace[-1]["velocity_updates"] == [{"state": "pending", "navigation_command": 2}]
        assert navigator.trace[-1]["velocity_updates"][0] is entry


def test_following_progress_emits_once_per_frame_without_arrival_or_visibility_claim(monkeypatch):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    model = localizer()
    navigator = navigation.VisualNavigator(SimpleNamespace(), model)
    navigator.commands = 1
    navigator.motion_feedback_target = lambda: ([1., 0., 0.], 12.)
    events = []
    navigator.following_observer = events.append
    observed = frame()
    geometry = navigation.arrival_frame_geometry(observed, model.locate(observed["image"], observed["capture_evidence"]), model.lens, [1, 0, 0])
    observer = SimpleNamespace(closed=False, command=1, latest={"command": 1, "status": "localized", "frame_received_monotonic": 10.,
        "capture_evidence": observed["capture_evidence"], "frame_geometry": geometry})
    for _ in range(2):
        assert navigator._motion_stop_reason(observer, "pan", 1) == "visual_feedback_target_reached"
    assert len(events) == 1
    assert events[0]["arrival_verified"] is events[0]["stop_confirmed"] is events[0]["subject_visibility_verified"] is False
    assert events[0]["frame_geometry"]["capture_evidence"] == events[0]["capture_evidence"]


@pytest.mark.parametrize("case", ["moving", "stopped", "reversed", "coverage_gap", "expired", "capture_changed",
    "same_velocity_renewal", "new_velocity_waits_for_effect"])
def test_following_matches_world_target_rate_instead_of_stopping_at_instantaneous_center(monkeypatch, case):
    clock = [10.1]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    model.coverage_mask = np.zeros((20, 40), bool) if case == "coverage_gap" else np.ones((20, 40), bool)
    model.target_reference = lambda _: {"id": "reference"}
    capabilities = {"defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    navigator = navigation.VisualNavigator(SimpleNamespace(capabilities=capabilities), model)
    navigator.response["pan"] = np.array([-999., 0.])  # Never convert this pulse gain into a motor rate.
    navigator.following_seconds = 6.
    navigator.trace = [{}]
    motion = navigation.TargetMotion()
    ray = [np.array([1., 0., 0.])]
    navigator.motion_feedback_target = lambda: (ray[0], 12.)
    navigator.motion_prediction = lambda geometry, seconds: navigator._predict_target(motion, geometry, 12., seconds)
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .1, "tilt": 0.}, "accepted_monotonic": 10.,
        "deadline_monotonic": 10.7, "window_deadline_monotonic": 16., "maximum_duration_seconds": 1.2}
    plans = []
    for index in range(6 if case in {"same_velocity_renewal", "new_velocity_waits_for_effect"} else 3):
        clock[0] = 10.1 + index * .11
        if index == 3:
            context.update(accepted_monotonic=clock[0] - .1, deadline_monotonic=11.1, observed_response_seconds=.5)
            if case == "new_velocity_waits_for_effect": context["velocity"] = {"pan": .05, "tilt": 0.}
        angle = index * .022
        target_angle = .022 if case == "stopped" and index == 2 else 0. if case == "reversed" and index == 2 else angle
        ray[0] = np.array([np.cos(target_angle), np.sin(target_angle), 0.])
        motion.observe({"scope": ["camera", "source", "job", 1, "artifact", "geometry"],
            "capture_instance": "source", "generation": 2 if case == "capture_changed" and index == 2 else 1,
            "sequence": index + 1, "observed_monotonic": clock[0] - (2 if case == "expired" else 0)}, ray[0], clock[0])
        c, s = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "decoder", "generation": 1, "sequence": index + 1}
        geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24,
            "inlier_fraction": .9, "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": clock[0],
            "capture_evidence": capture, "frame_geometry": geometry}
        plans.append(feedback(context))
    reason = navigator._motion_stop_reason(observer, "pan", 1)
    if case == "new_velocity_waits_for_effect":
        assert plans[2] is not None
        assert plans[3:] == [None, None, None], "Old video cannot qualify the new speed"
        return
    if case == "same_velocity_renewal":
        assert plans[2] is not None and plans[3] is not None, "Same-speed renewal must not relearn the unchanged rate"
        return
    if case != "moving":
        assert reason in {"visual_feedback_target_reached", "visual_feedback_direction_changed"}
        assert plans[-1] is None
        return
    assert reason is None
    assert plans[:2] == [None, None]
    plan = plans[-1]
    assert plan["duration_seconds"] == 1.2
    assert plan["velocity"] == {"pan": .1, "tilt": 0.}
    assert plan["measurement"]["target_rate_radians_per_second"][0] == pytest.approx(.2, abs=.001)
    assert plan["measurement"]["rate_radians_per_second"][0] == pytest.approx(-.2, abs=.001)
    plan["dispatch_guard"]()
    observer.latest["status"] = "unlocalized"
    with pytest.raises(navigation.PanoramaCaptureError, match="visual_feedback_unavailable"):
        plan["dispatch_guard"]()
