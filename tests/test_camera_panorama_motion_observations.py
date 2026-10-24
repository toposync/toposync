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
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}), model)
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
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
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


@pytest.mark.parametrize("target_age", [0., .3, .5, .9])
def test_following_control_aligns_target_and_camera_observation_times(monkeypatch, target_age):
    now, pose_age = 10., .2
    monkeypatch.setattr(navigation.time, "monotonic", lambda: now)
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    model.coverage_mask = np.ones((20, 40), bool)
    model.target_reference = lambda _: {"id": "reference"}
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}), model)
    motion = navigation.TargetMotion()
    for index in range(3):
        timestamp = now - target_age - .8 + index * .4
        angle = .05 - .1 * (now - timestamp)
        ray = np.array([np.cos(angle), np.sin(angle), 0.])
        motion.observe({"scope": ["camera", "source", "job", 1, "artifact", "geometry"],
            "capture_instance": "source", "generation": 1, "sequence": index + 1,
            "observed_monotonic": timestamp}, ray, timestamp)
    navigator.motion_prediction = lambda geometry, seconds: navigator._predict_target(motion, geometry, 12., seconds)
    camera_angle = .04 - .08 * pose_age
    c, s = np.cos(camera_angle), np.sin(camera_angle)
    geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
        "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24, "inlier_fraction": .9}
    rate = {"rate": np.array([-.08, 0.]), "interval_seconds": .1, "observed_response_seconds": .4}
    control = navigator._following_control(ray, geometry, rate, pose_age)
    if target_age == .9:
        assert np.array_equal(control["target_rate"], np.zeros(2))
        assert control["target_observation_age_seconds"] is None
        assert control["control_error"][0] == pytest.approx(.01 - .1 * target_age)
        return
    assert control["target_rate"][0] == pytest.approx(.1, abs=.001)
    assert control["target_observation_age_seconds"] == pytest.approx(target_age)
    # Current target (.05) minus current camera (.04), despite different publication ages.
    assert control["control_error"][0] == pytest.approx(.01, abs=.001)


@pytest.mark.parametrize("case", ["prepared", "missing_reverse", "expired_capacity", "zero_response",
    "wrong_observed_direction", "missing_prediction", "expired_before_dispatch", "reverse_removed_before_dispatch", "asymmetric_reverse", "slower_initial"])
def test_finite_response_bootstraps_proposals_without_certifying_cruise_rate(monkeypatch, case):
    clock = [10.1]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    capabilities = {"defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
    navigator.following_seconds, navigator.trace = 6., [{}]
    navigator.response["pan"] = np.array([-.2, 0.])
    navigator.response_rotations["pan"] = np.eye(3)
    navigator.direction_responses = {("pan", 1): (.6, np.array([-.12, 0.])),
        ("pan", -1): (.6, np.array([.12, 0.]))}
    navigator.pulse_limits["pan"] = 1.2
    navigator.pulse_capacity["pan"] = {"limit_seconds": 1.2, "observed_monotonic": 9.,
        "rotation_matrix": np.eye(3), "response": navigator.response["pan"].copy(),
        "pixel_budget": navigator._response_pixel_delta(np.zeros(2), navigator.response["pan"] * 1.2)}
    if case == "missing_reverse": navigator.direction_responses.pop(("pan", -1))
    if case == "expired_capacity": navigator.pulse_capacity["pan"]["observed_monotonic"] = -300.
    if case == "zero_response": navigator.response["pan"][:] = 0.
    if case == "asymmetric_reverse": navigator.direction_responses[("pan", -1)] = (.6, np.array([.06, 0.]))
    target = lambda angle: np.array([np.cos(angle), np.sin(angle), 0.])
    target_angle = -.18 if case == "asymmetric_reverse" else .18
    navigator.motion_feedback_target = lambda: (target(target_angle), 12.)
    navigator.motion_prediction = lambda geometry, seconds: None if case == "missing_prediction" else (
        {"ray": target(target_angle + .005), "horizon_seconds": .1, "observation_age_seconds": 0.,
         "observed_displacement_pixels": 10.}, {})
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .04 if case == "slower_initial" else .1, "tilt": 0.}, "accepted_monotonic": 10.,
        "deadline_monotonic": 11.2, "window_deadline_monotonic": 16.,
        "maximum_duration_seconds": 1.2, "observed_response_seconds": .4}
    for index in range(2):
        clock[0] = 10.1 + .2 * index
        angle = .04 * index * (-1 if case == "wrong_observed_direction" else 1)
        c, s = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "decoder", "generation": 1, "sequence": index + 1}
        geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24,
            "inlier_fraction": .9, "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": clock[0],
            "capture_evidence": capture, "frame_geometry": geometry}
        plan = feedback(context)
    if case in {"missing_reverse", "expired_capacity", "zero_response", "wrong_observed_direction", "missing_prediction"}:
        assert plan is None or plan["measurement"]["kind"] != "finite_response_model"
        assert getattr(observer, "response_model", None) is None
        return
    assert plan is not None and plan["measurement"]["kind"] == "finite_response_model"
    assert plan["measurement"]["qualified_velocity"] is None
    assert plan["measurement"]["comparison_rate_radians_per_second"] is None
    assert plan["measurement"]["estimated_from_finite_response"] is True
    assert getattr(observer, "qualified_rate", None) is None
    assert len(plan["measurement"]["capture_evidence"]) == 2
    if case == "slower_initial":
        assert plan["measurement"]["rate_radians_per_second"] == pytest.approx([-.08, 0.])
        assert plan["measurement"]["model_velocity"]["pan"] == .04
        assert .04 < plan["velocity"]["pan"] <= .1
    if case == "asymmetric_reverse":
        assert plan["velocity"]["pan"] == pytest.approx(-.06)
        assert plan["measurement"]["reversal_model"]["delta_radians"] == [.06, 0.]
    if case == "expired_before_dispatch": navigator.pulse_capacity["pan"]["observed_monotonic"] = -300.
    if case == "reverse_removed_before_dispatch": navigator.direction_responses.pop(("pan", -1))
    if case in {"expired_before_dispatch", "reverse_removed_before_dispatch"}:
        with pytest.raises(navigation.PanoramaCaptureError, match="visual_response_unavailable"):
            plan["dispatch_guard"]()
    else:
        plan["dispatch_guard"]()


def test_following_progress_emits_once_per_frame_without_arrival_or_visibility_claim(monkeypatch):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    model = localizer()
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}), model)
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
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
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
        assert all(plan["velocity"] == context["velocity"] for plan in plans[3:])
        assert all(plan["measurement"]["kind"] == "visual_velocity_hold" for plan in plans[3:])
        assert all(plan["measurement"]["qualified_velocity"] == {"pan": .1, "tilt": 0.} for plan in plans[3:])
        assert observer.qualified_rate["observation"]["capture_evidence"]["sequence"] == 3, "Old video cannot qualify the new speed"
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


@pytest.mark.parametrize("case", ["continue", "static", "near", "wrong_direction", "unknown_direction",
    "unknown_response", "changed_speed", "stale", "view_changed", "too_early", "no_motion"])
def test_following_can_continue_original_velocity_before_acceleration_settles(monkeypatch, case):
    clock = [10.4]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    capabilities = {"velocity_supported": True, "defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
    navigator.following_seconds, navigator.trace = 6., [{}]
    navigator.response["pan"] = np.array([-.2, 0.])
    navigator.response_rotations["pan"] = np.eye(3)
    navigator.direction_responses[("pan", 1)] = (.6, np.array([-.12, 0.]))
    navigator.pulse_limits["pan"] = 1.2
    if case == "unknown_direction": navigator.direction_responses.clear()
    if case == "unknown_response": navigator.response.clear()
    if case == "view_changed": navigator.response_rotations["pan"] = np.diag([-1., 1., -1.])
    target = [.04 if case == "near" else .5]
    navigator.motion_feedback_target = lambda: ([np.cos(target[0]), np.sin(target[0]), 0.], 12.)
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .05 if case == "changed_speed" else .1, "tilt": 0.},
        "accepted_monotonic": 10., "deadline_monotonic": 12. if case == "too_early" else 11.03,
        "maximum_duration_seconds": 1.2, "window_deadline_monotonic": 16.}
    if case == "static": context.pop("window_deadline_monotonic")
    plans = []
    for i in range(2):
        received = 10.4 + .25 * i
        clock[0] = received + .12
        angle = i * .02 * (-1 if case == "wrong_direction" else 0 if case == "no_motion" else 1)
        c, s = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "test", "generation": 1, "sequence": i + 1}
        geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24,
            "inlier_fraction": .9, "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": received,
            "capture_evidence": capture, "frame_geometry": geometry}
        if case == "stale" and i == 1: clock[0] += .3
        plans.append(feedback(context))
    assert plans[0] is None
    assert observer.latest["feedback_first_frame_age_seconds"] == pytest.approx(.42 if case == "stale" else .12)
    if case != "continue":
        assert plans[1] is None
        return
    plan = plans[1]
    assert plan["velocity"] == context["velocity"]
    assert plan["duration_seconds"] == 1.2
    assert plan["measurement"]["kind"] == "visual_direction_continuation"
    assert observer.latest["feedback_planned_kind"] == "visual_direction_continuation"
    assert "rate_radians_per_second" not in plan["measurement"]
    assert getattr(observer, "qualified_rate", None) is None
    plan["dispatch_guard"]()
    target[0] = .04
    with pytest.raises(navigation.PanoramaCaptureError):
        plan["dispatch_guard"]()


@pytest.mark.parametrize("target_interval", [1 / 30, .1])
def test_following_rate_uses_target_history_independently_of_pose_cadence(monkeypatch, target_interval):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    model.coverage_mask = np.ones((20, 40), bool)
    model.target_reference = lambda _: {"id": "reference"}
    capabilities = {"defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
    navigator.following_seconds, navigator.trace = 6., [{}]
    navigator.response["pan"] = np.array([-.2, 0.])
    motion = navigation.TargetMotion()
    ray = [None]
    navigator.motion_feedback_target = lambda: (ray[0], 12.)
    navigator.motion_prediction = lambda geometry, seconds: navigator._predict_target(motion, geometry, 12., seconds)
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .1, "tilt": 0.}, "accepted_monotonic": 10.,
        "deadline_monotonic": 11.5, "window_deadline_monotonic": 16., "maximum_duration_seconds": 1.2}
    sequence = 0
    for pose_index in range(3):
        clock[0] = 10.3 + .3 * pose_index
        for timestamp in [clock[0] - 2 * target_interval, clock[0] - target_interval, clock[0]]:
            sequence += 1
            angle = .2 * (timestamp - 10.)
            ray[0] = np.array([np.cos(angle), np.sin(angle), 0.])
            motion.observe({"scope": ["camera", "source", "job", 1, "artifact", "geometry"],
                "capture_instance": "source", "generation": 1, "sequence": sequence,
                "observed_monotonic": timestamp}, ray[0], clock[0])
        angle = .2 * (clock[0] - 10.)
        c, s = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "decoder", "generation": 1, "sequence": pose_index + 1}
        geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "validation_matches": 24,
            "inlier_fraction": .9, "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": clock[0],
            "capture_evidence": capture, "frame_geometry": geometry}
        plan = feedback(context)
    assert plan is not None, "A slower pose cadence must not discard qualified target motion"
    assert plan["measurement"]["target_rate_radians_per_second"][0] == pytest.approx(.2, abs=.001)
    assert plan["velocity"]["pan"] == pytest.approx(.1, abs=.001)
    assert navigator._motion_stop_reason(observer, "pan", 1) is None


@pytest.mark.parametrize("change", [None, "capture", "generation", "expired", "future", "view", "static"])
def test_following_rate_reference_stays_bound_to_capture_time_and_local_view(monkeypatch, change):
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}), localizer())
    navigator.following_seconds = None if change == "static" else 6.
    capture = {"capture_instance": "capture", "generation": 1, "sequence": 1}
    origin = {"frame_received_monotonic": 3. if change == "expired" else 11. if change == "future" else 9.5,
        "capture_evidence": capture, "frame_geometry": {"rotation_matrix": np.eye(3).tolist()}}
    current_capture = {**capture, "sequence": 2}
    if change == "capture": current_capture["capture_instance"] = "replacement"
    if change == "generation": current_capture["generation"] = 2
    current = {"capture_evidence": current_capture, "frame_geometry": {
        "rotation_matrix": np.diag([-1., 1., -1.]).tolist() if change == "view" else np.eye(3).tolist()}}
    model = {"observation": origin}
    observer = SimpleNamespace(qualified_rate=model)
    assert navigator._following_rate_reference(observer, current) is (model if change is None else None)


def test_slow_velocity_requalification_accumulates_displacement_without_lowering_noise(monkeypatch):
    clock = [10.]
    monkeypatch.setattr(navigation.time, "monotonic", lambda: clock[0])
    model = localizer()
    model.lens = {"width": 960, "height": 540, "fx": 560., "fy": 560., "cx": 479.5, "cy": 269.5}
    capabilities = {"defaults": {"continuous": "normalized"}, "spaces": {"continuous": [
        {"uri": "normalized", "normalized": True, "x": {"min": -1., "max": 1.}, "y": {"min": -1., "max": 1.}}]}}
    navigator = navigation.VisualNavigator(SimpleNamespace(checkpoint={}, capabilities=capabilities), model)
    navigator.following_seconds, navigator.trace = 6., [{}]
    navigator.response["pan"] = np.array([-.2, 0.])
    navigator.motion_feedback_target = lambda: ([np.cos(.5), np.sin(.5), 0.], 12.)
    observer = SimpleNamespace(closed=False, latest=None, command=1)
    feedback = navigator._velocity_feedback(observer, "pan", 1, .1)
    context = {"velocity": {"pan": .1, "tilt": 0.}, "accepted_monotonic": 10.,
        "deadline_monotonic": 11.2, "window_deadline_monotonic": 16., "maximum_duration_seconds": 1.2}
    for index in range(10):
        clock[0] = 10.1 + index * .1
        if index == 3:
            context.update(velocity={"pan": .025, "tilt": 0.}, accepted_monotonic=10.31,
                deadline_monotonic=11.51, observed_response_seconds=.2)
        angle = .02 * index if index < 3 else .04 + .005 * (index - 2)
        c, s = np.cos(angle), np.sin(angle)
        capture = {"capture_instance": "capture", "generation": 1, "sequence": index + 1}
        geometry = {"rotation_matrix": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
            "analysis_width": 960, "validation_p95_pixels": 1., "capture_evidence": capture}
        observer.latest = {"status": "localized", "command": 1, "frame_received_monotonic": clock[0],
            "capture_evidence": capture, "frame_geometry": geometry}
        feedback(context)
    qualified = observer.qualified_rate
    assert qualified["velocity"] == {"pan": .025, "tilt": 0.}
    assert qualified["rate"][0] == pytest.approx(-.05)
    assert qualified["displacement_pixels"] >= 4.
    assert .08 <= qualified["interval_seconds"] <= .5
    assert len(qualified["capture_evidence"]) == 3


@pytest.mark.parametrize("failed", [False, True])
def test_velocity_preparation_observes_frames_without_feedback_or_extra_commands(monkeypatch, failed):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(navigation.time, "monotonic", lambda: 10.)
    observed = []
    async def pulse(axis, direction, duration, **options):
        assert (axis, direction, duration) == ("pan", 1, .6)
        assert set(options) == {"frame_observer"}
        observer = options["frame_observer"].__self__
        observed.append(observer)
        options["frame_observer"](frame())
        await observer.worker
        if failed:
            raise asyncio.CancelledError()
        return {"frame": frame(2), "stable": True}
    scanner = SimpleNamespace(last_frame=frame(), checkpoint={"navigation_step": {"kind": "velocity_qualification"}},
        _check=lambda: None, _persist=AsyncMock(), _pulse=pulse,
        capabilities={"velocity_supported": True}, physical_state="stopped")
    navigator = navigation.VisualNavigator(scanner, localizer())
    if failed:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(navigator._pulse("pan", .6))
    else:
        asyncio.run(navigator._pulse("pan", .6))
    assert len(observed) == 1 and observed[0].closed and observed[0].worker.done()
    record, = scanner.checkpoint["navigation_motion_observations"]
    assert record["command"] == 1 and record["status"] == "localized"
    assert record["arrival_verified"] is False and "stop_confirmed" not in record
    assert navigator.commands == 1 and navigator.following_seconds is None
