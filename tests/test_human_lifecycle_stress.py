"""Synthetic lifecycle regression, not model accuracy or sustained-load qualification.

Real person/feet, gesture, pointing and notification operators run in one process.
Only the notification publishing boundary is replaced by an in-memory collector.
"""

import asyncio
from collections import Counter
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.config_store import (
    Composition,
    CompositionElement,
    ConfigStore,
    UserDataPaths,
)
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync_ext_cameras.pipelines.person_ground import PersonGroundRuntime
from toposync_ext_cameras.pipelines.pointing import PointingTargetRuntime
from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime

from test_person_ground import mapping_config
from test_person_pointing import scene, volume
from test_vision_gestures import packet as gesture_packet


SUBJECT_COUNT = 1000


def test_thousand_subjects_release_real_operator_state_and_reject_late_updates(
    tmp_path, monkeypatch
):
    # Source publication is frozen, not refreshed by an operator. Media timestamps
    # advance independently. This is not a real-time throughput/latency benchmark.
    monkeypatch.setattr("time.time", lambda: 1000.0)

    async def scenario():
        started = perf_counter()
        store = ConfigStore(
            paths=UserDataPaths(tmp_path, tmp_path / "config.json", tmp_path / "files")
        )
        await store.set_active_composition(
            Composition(
                id="map",
                name="Map",
                elements=[
                    volume(),
                    CompositionElement(
                        id="placement",
                        type="camera",
                        props={
                            "camera_id": "camera",
                            "calibrated_views": mapping_config()["calibrated_views"],
                        },
                    ),
                ],
            )
        )
        notifications = Counter()
        subject_notifications = Counter()
        projection_statuses = Counter()

        async def collect_notification(**arguments):
            payload = arguments["payload"]
            notifications[payload["lifecycle"]] += 1
            subject_notifications[(payload["subject"]["id"], payload["lifecycle"])] += 1
            assert arguments["image_path"] is None
            assert "ephemeral_image" not in arguments
            projection_statuses[payload["data_projection"]["status"]] += 1

        dependencies = PipelineRuntimeDependencies(
            config_store=store,
            notifications_upsert=collect_notification,
        )
        ground = PersonGroundRuntime({}, dependencies)
        gestures = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.2})
        pointing = PointingTargetRuntime({}, dependencies)
        notify = NotifyRuntime(
            {
                "update_interval_seconds": 0,
                "include_payload_paths": [
                    "vision.gestures",
                    "spatial.person_ground",
                    "spatial.pointing",
                ],
            },
            dependencies,
        )
        context = SimpleNamespace(pipeline_name="synthetic-lifecycle", node_id="notify")
        pixels = np.zeros((720, 1280, 3), dtype=np.uint8)
        pose_template = scene()[1]

        def sample(index, timestamp, lifecycle):
            actor = f"person:{index}"
            evidence = {
                "capture_instance": "synthetic-decoder",
                "generation": 1,
                "sequence": index * 10 + int(timestamp * 10) + 1,
                "published_at": 1000.0,
                "physical_timestamp_verified": False,
            }
            pose = deepcopy(pose_template)
            pose.update(actor_subject_id=actor, camera_id="camera", source_stream_id="camera")
            pose["metadata"]["actor_subject_id"] = actor
            value = Packet.create(
                stream_id="camera",
                lifecycle=lifecycle,
                payload={
                    "camera_id": "camera",
                    "source_stream_id": "camera",
                    "capture_evidence": evidence,
                    "source": {"source_id": "recording", "view_id": "fixed"},
                    "subject": {
                        "id": actor,
                        "type": "event",
                        "category": "person",
                        "bbox01": [0.35, 0.15, 0.65, 0.6],
                    },
                    "media": {"ts": timestamp, "width": 1280, "height": 720},
                    "vision": {
                        "poses": [pose],
                        "pose_media_ts": timestamp,
                        "pose_source_size": [1280, 720],
                    },
                },
                artifacts={
                    "frame": Artifact(
                        "frame",
                        pixels,
                        metadata={
                            "image_geometry": image_geometry(1280, 720, evidence),
                        },
                    )
                },
            )
            value.payload["vision"]["pose_frame_packet_id"] = value.packet_id
            return value

        try:
            for index in range(SUBJECT_COUNT):
                for timestamp, lifecycle in (
                    (0, Lifecycle.OPEN),
                    (0.25, Lifecycle.UPDATE),
                    (0.5, Lifecycle.CLOSE),
                ):
                    value = sample(index, timestamp, lifecycle)
                    for operator in (ground, gestures, pointing):
                        outputs = await operator.process_packet(value, None)
                        assert len(outputs) == 1
                        value = outputs[0]
                    spatial = value.payload["spatial"]
                    if lifecycle == Lifecycle.CLOSE:
                        assert spatial["person_ground"]["reason"] == "subject_closed"
                        assert spatial["person_ground"]["feet"] == {"left": None, "right": None}
                        assert spatial["pointing"]["reason"] == "subject_closed"
                        assert spatial["pointing"]["selected_entity_id"] is None
                        assert not value.payload["vision"]["gestures"]["active"]
                    else:
                        assert spatial["person_ground"]["status"] == "estimated", spatial[
                            "person_ground"
                        ]
                        assert (
                            len(ground.history)
                            == len(gestures._actors)
                            == len(pointing.timestamps)
                            == 1
                        )
                        if lifecycle == Lifecycle.UPDATE:
                            assert all(
                                foot["contact"] == "stationary_support_hypothesis"
                                for foot in spatial["person_ground"]["feet"].values()
                            )
                            assert value.payload["vision"]["gestures"]["status"] == "active"
                            assert spatial["pointing"]["selected_entity_id"] == "target"
                            assert spatial["pointing"]["actions_authorized"] is False
                    await notify.process_packet(value, context)
                assert not ground.history and not gestures._actors and not pointing.timestamps
                assert not notify._state

            assert notifications == {
                "open": SUBJECT_COUNT,
                "update": SUBJECT_COUNT,
                "close": SUBJECT_COUNT,
            }
            assert len(subject_notifications) == SUBJECT_COUNT * 3
            assert set(subject_notifications.values()) == {1}
            assert len(ground.closed_subjects) == len(pointing.closed_subjects) == SUBJECT_COUNT
            assert len(ground.closed_subjects) <= 4096
            assert len(gestures._closed) <= 4096
            # Person/pointing tombstones must survive the complete turnover, not
            # just the immediately preceding CLOSE. Never notify on these probes:
            # the generic sink has no terminal-identity rejection contract.
            for index in range(SUBJECT_COUNT):
                for lifecycle in (Lifecycle.UPDATE, Lifecycle.OPEN):
                    value = sample(index, 0.75, lifecycle)
                    value = (await ground.process_packet(value, None))[0]
                    value = (await pointing.process_packet(value, None))[0]
                    assert value.payload["spatial"]["person_ground"]["reason"] == "subject_closed"
                    assert value.payload["spatial"]["pointing"]["reason"] == "subject_closed"
            assert not ground.history and not pointing.timestamps
            print(
                {
                    "subjects": SUBJECT_COUNT,
                    "pipeline_packets": SUBJECT_COUNT * 3,
                    "late_person_pointing_probes": SUBJECT_COUNT * 2,
                    "notifications": dict(notifications),
                    "active_states": 0,
                    "person_terminal": len(ground.closed_subjects),
                    "pointing_terminal": len(pointing.closed_subjects),
                    "gesture_terminal": len(gestures._closed),
                    "notification_projection": dict(projection_statuses),
                    "elapsed_seconds": round(perf_counter() - started, 3),
                }
            )
            assert projection_statuses == {"ready": SUBJECT_COUNT * 3}
        finally:
            for operator in (notify, pointing, gestures, ground):
                await operator.shutdown()
        assert not ground.history and not ground.closed_subjects
        assert not pointing.timestamps and not pointing.closed_subjects
        assert not gestures._actors and not gestures._closed
        assert not notify._state

    asyncio.run(scenario())


@pytest.mark.parametrize("late_lifecycle", [Lifecycle.UPDATE, Lifecycle.OPEN])
def test_thousand_closed_gesture_subjects_never_resurrect(late_lifecycle):
    async def scenario():
        operator = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.2})
        try:
            for index in range(SUBJECT_COUNT):
                for timestamp, lifecycle in (
                    (0, Lifecycle.OPEN),
                    (0.25, Lifecycle.UPDATE),
                    (0.5, Lifecycle.CLOSE),
                ):
                    value = gesture_packet(
                        timestamp, "raised", actor=f"person:{index}", lifecycle=lifecycle
                    )
                    result = (await operator.process_packet(value, None))[-1]
                    if lifecycle == Lifecycle.UPDATE:
                        assert result.payload["vision"]["gestures"]["status"] == "active"
                assert not operator._actors
            assert len(operator._closed) <= 4096
            for index in range(SUBJECT_COUNT):
                for timestamp in (0.75, 1.0):
                    result = (
                        await operator.process_packet(
                            gesture_packet(
                                timestamp,
                                "raised",
                                actor=f"person:{index}",
                                lifecycle=late_lifecycle,
                            ),
                            None,
                        )
                    )[-1].payload["vision"]["gestures"]
                    assert result["reason"] == "actor_closed", (index, late_lifecycle, result)
                    assert result["active"] == []
                    assert not operator._actors
        finally:
            await operator.shutdown()
        assert not operator._actors and not operator._closed

    asyncio.run(scenario())


@pytest.mark.parametrize("output_mode", ["annotate", "events"])
def test_gesture_terminal_capacity_closes_active_events_and_abstains_until_shutdown(output_mode):
    async def scenario():
        operator = VisionGestureRecognizeRuntime(
            {
                "minimum_duration_seconds": 0.2,
                "output_mode": output_mode,
            }
        )

        def close(actor):
            return Packet.create(
                stream_id="camera:a",
                lifecycle=Lifecycle.CLOSE,
                payload={
                    "camera_id": "camera:a",
                    "source_stream_id": "camera:a",
                    "subject": {"id": actor},
                },
            )

        try:
            for index in range(4096):
                await operator.process_packet(close(f"closed:{index}"), None)
            terminal_identities = set(operator._closed)
            assert len(terminal_identities) == 4096
            assert ("camera:a", "camera:a", "closed:0") in terminal_identities
            assert not operator._closed_capacity_reached

            active_event_ids = set()
            for actor in ("active:one", "active:two"):
                for timestamp, lifecycle in ((0, Lifecycle.OPEN), (0.25, Lifecycle.UPDATE)):
                    outputs = await operator.process_packet(
                        gesture_packet(
                            timestamp,
                            "raised",
                            actor=actor,
                            lifecycle=lifecycle,
                        ),
                        None,
                    )
                    if timestamp == 0.25:
                        if output_mode == "events":
                            assert len(outputs) == 1 and outputs[0].lifecycle == Lifecycle.OPEN
                            active_event_ids.add(outputs[0].payload["subject"]["id"])
                        else:
                            assert outputs[-1].payload["vision"]["gestures"]["status"] == "active"
            assert len(operator._actors) == 2
            # A repeated CLOSE at capacity must not consume another slot or
            # interrupt other identities. Only a new terminal identity saturates.
            await operator.process_packet(close("closed:0"), None)
            assert not operator._closed_capacity_reached
            assert len(operator._actors) == 2

            outputs = await operator.process_packet(close("overflow:4097"), None)
            assert operator._closed_capacity_reached
            assert set(operator._closed) == terminal_identities
            assert not operator._actors
            if output_mode == "events":
                assert len(outputs) == 2
                assert {value.payload["subject"]["id"] for value in outputs} == active_event_ids
                assert all(value.lifecycle == Lifecycle.CLOSE for value in outputs)
                assert all(
                    value.payload["gesture_event"]["reason"] == "closed_subject_capacity_reached"
                    for value in outputs
                )
                assert all(not value.artifacts for value in outputs)
            else:
                retired = [
                    value.payload["vision"]["gestures"]
                    for value in outputs
                    if value.payload["vision"]["gestures"]["reason"]
                    == "closed_subject_capacity_reached"
                ]
                assert {value["actor_subject_id"] for value in retired} == {
                    "active:one",
                    "active:two",
                }
                assert all(
                    value["status"] == "unknown" and value["active"] == [] for value in retired
                )

            for actor in ("closed:0", "closed:4095", "overflow:4097", "active:one", "new:subject"):
                for lifecycle in (Lifecycle.UPDATE, Lifecycle.OPEN):
                    outputs = await operator.process_packet(
                        gesture_packet(
                            0.75,
                            "raised",
                            actor=actor,
                            lifecycle=lifecycle,
                        ),
                        None,
                    )
                    if output_mode == "events":
                        assert outputs == []
                    else:
                        result = outputs[-1].payload["vision"]["gestures"]
                        assert result["reason"] == "closed_subject_capacity_reached"
                        assert result["status"] == "unknown" and result["active"] == []
                    assert not operator._actors
                    assert set(operator._closed) == terminal_identities
        finally:
            await operator.shutdown()
        assert not operator._actors and not operator._closed
        assert not operator._closed_capacity_reached
        # Cleanup itself is idempotent and never emits/recreates an actor.
        await operator.shutdown()
        assert not operator._actors and not operator._closed
        assert not operator._closed_capacity_reached

    asyncio.run(scenario())
