from __future__ import annotations

import asyncio
from dataclasses import replace
import json

from toposync.runtime.notifications.store import NotificationStore
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import NotifyRuntime
from toposync.runtime.pipelines.operators_distributed import _deserialize_packet, _serialize_packet
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.identity.contracts import IdentityEvidence
from toposync_ext_vision.identity.pipelines import (
    IdentityContextRuntime,
    IdentityEvidenceRuntime,
    RecognizeIdentityRuntime,
    occurrence_key,
    PRIVATE_EVIDENCE_ARTIFACT,
    PRIVATE_DECISION_ARTIFACT,
    read_identity_decision,
)
from toposync_ext_vision.identity.store import IdentityStore


def test_recognition_rejects_ambiguous_duplicate_calibrations():
    import pytest
    from pydantic import ValidationError
    from test_identity_store import policy
    from toposync_ext_vision.identity.pipelines import RecognizeIdentityConfig

    person = policy().model_dump()
    with pytest.raises(ValidationError, match="one recognition policy"):
        RecognizeIdentityConfig(policies=[person, {**person, "acceptance_similarity": .95}])
    assert len(RecognizeIdentityConfig(policies=[person, policy(species="cat").model_dump()]).policies) == 2


class Context:
    pipeline_name = "identity-test"
    node_id = "identity"

    async def run_blocking(self, function, *args, **kwargs):
        kwargs.pop("mode", None)
        kwargs.pop("concurrency_key", None)
        kwargs.pop("process_pool_key", None)
        kwargs.pop("process_pool_deadline_seconds", None)
        return function(*args, **kwargs)


def packet(lifecycle=Lifecycle.OPEN, *, correlation="visit-one"):
    return Packet.create(
        stream_id="camera:test",
        lifecycle=lifecycle,
        payload={
            "camera_id": "camera",
            "frame_ts": 1.0,
            "correlation_id": correlation,
            "subject": {
                "id": "event-1",
                "type": "event",
                "category": "cat",
                "bbox01": [0.1, 0.1, 0.8, 0.8],
            },
            "world_position": {"x": 1, "z": 2},
            "tracklet_id": "track-one",
            "identity_id": None,
        },
    )


def with_evidence(value):
    evidence = IdentityEvidence(
        species="cat",
        occurrence_id=occurrence_key(value),
        source_id="camera:test",
        camera_id="camera",
        capture_id=value.packet_id,
        observed_at=value.payload["frame_ts"],
        embedding_space="test:synthetic",
        vector=(1.0,) + (0.0,) * 15,
        quality=0.9,
        reference_eligible=True,
        region=(0.1, 0.1, 0.8, 0.8),
    )
    return value.with_artifact(
        Artifact(
            name=PRIVATE_EVIDENCE_ARTIFACT,
            data=json.dumps({"evidence": evidence.model_dump(), "crop": None}).encode(),
            private=True,
        )
    )


def test_disabled_identity_operators_do_not_infer_or_change_existing_packets():
    async def scenario():
        value = packet()
        dependencies = PipelineRuntimeDependencies()
        for runtime in (
            IdentityContextRuntime({}, dependencies),
            IdentityEvidenceRuntime({}, dependencies),
            RecognizeIdentityRuntime({}, dependencies),
        ):
            output = await runtime.process_packet(value, Context())
            assert output == [value] and output[0] is value

    asyncio.run(scenario())


def test_occurrence_identity_is_independent_of_reused_tracker_id():
    first, restarted = packet(), packet(correlation="another-camera-session")
    assert first.payload["subject"]["id"] == restarted.payload["subject"]["id"]
    assert occurrence_key(first) != occurrence_key(restarted)
    assert (
        occurrence_key(
            replace(first, payload={**first.payload, "subject": {"id": "group", "type": "group"}})
        )
        is None
    )


def test_identity_samples_only_the_original_subject_plane_and_capture(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from toposync.runtime.pipelines.image_geometry import image_geometry, transformed_geometry
    import toposync_ext_vision.identity.pipelines as pipelines

    async def scenario():
        capture = {"capture_instance": "camera-test", "generation": 1, "sequence": 2}
        value = packet()
        value = replace(value, payload={**value.payload, "capture_evidence": capture})
        pixels = np.zeros((100, 160, 3), dtype=np.uint8)
        original = Artifact(name="main", data=pixels, metadata={"image_geometry": image_geometry(160, 100, capture)})
        translated = transformed_geometry(original, [[1, 0, -32], [0, 1, 0], [0, 0, 1]], 160, 100)
        stale = image_geometry(160, 100, {**capture, "sequence": 1})
        calls = []
        def extract(*args, **kwargs):
            calls.append(kwargs["region"])
            return SimpleNamespace(evidence=None, status="pending", reason="probe")
        monkeypatch.setattr(pipelines, "extract_in_process", extract)
        runtime = IdentityEvidenceRuntime({"enabled": True}, PipelineRuntimeDependencies())
        runtime._extractor = lambda species: SimpleNamespace(process_specification=lambda: ())
        for artifact, reason in [
            (replace(original, metadata=translated), "original_frame_required"),
            (replace(original, metadata={"image_geometry": stale}), "original_frame_required"),
            (replace(original, metadata={"image_geometry": {}}), "original_frame_required"),
            (replace(original, metadata={"source_artifact_name": "main"}), "original_frame_required"),
            (replace(original, metadata={"attached_from_stream_id": "camera:other", "attached_frame_ts": 0.9}),
             "original_frame_required"),
            (original, "probe"),
        ]:
            selected = with_evidence(value).with_artifact(artifact)
            output, = await runtime.process_packet(selected, Context())
            assert output.payload["recognition"]["reason"] == reason
            assert output.payload["subject"] == selected.payload["subject"]
            assert PRIVATE_EVIDENCE_ARTIFACT not in output.artifacts
        # The invalid next image arrives inside the normal sampling interval.
        output, = await runtime.process_packet(with_evidence(value).with_artifact(replace(original, metadata=translated)), Context())
        assert output.payload["recognition"]["reason"] == "original_frame_required"
        assert PRIVATE_EVIDENCE_ARTIFACT not in output.artifacts
        stale_subject = {**value.payload["subject"], "source_anchor": {"capture_evidence": stale["capture_evidence"]}}
        selected = replace(value.with_artifact(original), payload={**value.payload, "subject": stale_subject})
        output, = await runtime.process_packet(selected, Context())
        assert output.payload["recognition"]["reason"] == "original_frame_required"
        assert calls == [tuple(value.payload["subject"]["bbox01"])]

    asyncio.run(scenario())


def test_identity_keyframes_skip_repetitions_preserve_quality_changes_and_bound_storage(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    import toposync_ext_vision.identity.pipelines as pipelines

    async def scenario():
        runtime = IdentityEvidenceRuntime({
            "enabled": True, "sample_interval_seconds": 0.25,
            "keyframe_min_region_change": 0.1,
            "keyframe_max_per_occurrence": 3, "keyframe_max_total": 4,
        }, PipelineRuntimeDependencies())
        runtime._extractor = lambda species: SimpleNamespace(process_specification=lambda: ())
        quality = {"eligible": False}

        def extract(*args, **kwargs):
            value = IdentityEvidence(
                species="cat", occurrence_id=kwargs["occurrence_id"],
                source_id="camera:test", camera_id="camera", capture_id="frame",
                observed_at=1, embedding_space="test:synthetic", quality=0.8,
                vector=(1.0,) + (0.0,) * 15,
                reference_eligible=quality["eligible"], region=kwargs["region"],
            )
            return SimpleNamespace(evidence=value, crop=None, status="ready", reason="")

        monkeypatch.setattr(pipelines, "extract_in_process", extract)
        original = packet().with_artifact(Artifact(name="main", data=np.zeros((20, 20, 3), dtype=np.uint8)))

        async def run(value):
            runtime._sampled.clear()  # Advance past the cadence without a wall-clock wait.
            return (await runtime.process_packet(value, Context()))[0]

        assert PRIVATE_EVIDENCE_ARTIFACT in (await run(original)).artifacts
        assert PRIVATE_EVIDENCE_ARTIFACT not in (await run(original)).artifacts
        quality["eligible"] = True
        assert PRIVATE_EVIDENCE_ARTIFACT in (await run(original)).artifacts
        moved = replace(original, payload={**original.payload, "subject": {
            **original.payload["subject"], "bbox01": [0.3, 0.1, 0.9, 0.8],
        }})
        assert PRIVATE_EVIDENCE_ARTIFACT in (await run(moved)).artifacts
        assert PRIVATE_EVIDENCE_ARTIFACT not in (await run(original)).artifacts
        closed = await run(replace(original, lifecycle=Lifecycle.CLOSE))
        assert closed.lifecycle == Lifecycle.CLOSE and not runtime._keyframes
        new_visit = replace(original, payload={**original.payload, "correlation_id": "visit-two"})
        assert PRIVATE_EVIDENCE_ARTIFACT in (await run(new_visit)).artifacts
        exhausted = await run(replace(new_visit, payload={**new_visit.payload, "correlation_id": "visit-three"}))
        assert exhausted.payload["recognition"]["reason"] == "evidence_budget"
        assert PRIVATE_EVIDENCE_ARTIFACT not in exhausted.artifacts
        assert (await run(replace(new_visit, lifecycle=Lifecycle.CLOSE))).lifecycle == Lifecycle.CLOSE

    asyncio.run(scenario())


def test_private_evidence_origin_resolution_and_notification_dedupe(tmp_path):
    async def scenario():
        store = IdentityStore(tmp_path / "gallery", scope="test")
        notifications = NotificationStore(tmp_path / "notifications.sqlite3")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        notifications_written = []

        async def upsert(**kwargs):
            notifications_written.append(notifications.upsert(**kwargs))

        dependencies = PipelineRuntimeDependencies(services=services, notifications_upsert=upsert)
        resolver = RecognizeIdentityRuntime({"enabled": True}, dependencies)
        sink = NotifyRuntime({"update_interval_seconds": 0}, dependencies)
        initial = packet()
        # Actual transport serializer, resolver, SQLite stores and sink. Synthetic vectors do not evaluate accuracy.
        received = _deserialize_packet(_serialize_packet(with_evidence(initial)))
        resolved = (await resolver.process_packet(received, Context()))[0]
        assert PRIVATE_EVIDENCE_ARTIFACT not in resolved.artifacts
        assert resolved.artifacts[PRIVATE_DECISION_ARTIFACT].private
        assert resolved.payload["recognition"]["status"] == "unavailable"
        assert resolved.payload["recognition"]["reason"] == "calibration_required"
        for key in ("subject", "tracklet_id", "world_position", "correlation_id"):
            assert resolved.payload[key] == initial.payload[key]
        await sink.process_packet(resolved, Context())
        selected = store.observations()[0]["id"]
        identity = store.curate(
            action="identify",
            values={
                "name": "Gato de teste",
                "species": "cat",
                "observation_ids": [selected],
                "use_as_reference": True,
            },
            actor="tester",
            request_key="one",
            expected_revision=store.revision,
        )["identity_id"]
        assert len(notifications_written) == 1  # Human correction never reruns sinks.
        for lifecycle in (Lifecycle.UPDATE, Lifecycle.CLOSE):
            follow = replace(
                initial,
                lifecycle=lifecycle,
                payload={
                    **initial.payload,
                    "frame_ts": 2.0,
                    "recognition": {"status": "pending", "reason": "sampling"},
                },
            )
            output = (await resolver.process_packet(follow, Context()))[0]
            assert output.lifecycle == lifecycle
            assert output.payload["subject"]["id"] == "event-1"
            assert output.payload["recognition"]["status"] == "recognized"
            assert identity not in json.dumps(output.payload["recognition"])
            internal = await read_identity_decision(output, services)
            assert internal.identity_id == identity
            mismatched = replace(
                output, payload={**output.payload, "correlation_id": "another-visit"}
            )
            assert await read_identity_decision(mismatched, services) is None
            await sink.process_packet(output, Context())
        assert len({record.id for record, _ in notifications_written}) == 1
        assert sum(created for _, created in notifications_written) == 1
        assert notifications_written[-1][0].payload["status"] == "closed"
        assert "vector" not in json.dumps(notifications_written[-1][0].payload)
        assert store.occurrence_details(occurrence_key(initial))["closed"]
        late = (await resolver.process_packet(with_evidence(initial), Context()))[0]
        assert len(store.observations()) == 1
        assert (
            late.payload["recognition"] == store.decision(occurrence_key(initial)).packet_summary()
        )
        store.close()

    asyncio.run(scenario())


def test_malformed_private_evidence_fails_closed_but_preserves_close(tmp_path):
    async def scenario():
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        runtime = RecognizeIdentityRuntime(
            {"enabled": True}, PipelineRuntimeDependencies(services=services)
        )
        value = packet().with_artifact(
            Artifact(name=PRIVATE_EVIDENCE_ARTIFACT, data=b"{invalid", private=True)
        )
        result = (await runtime.process_packet(value, Context()))[0]
        assert result.payload["recognition"]["status"] == "unavailable"
        assert not result.artifacts and result.lifecycle == Lifecycle.OPEN
        closed = (
            await runtime.process_packet(replace(value, lifecycle=Lifecycle.CLOSE), Context())
        )[0]
        assert closed.lifecycle == Lifecycle.CLOSE
        late = (await runtime.process_packet(with_evidence(packet()), Context()))[0]
        assert late.payload["recognition"]["reason"] == "occurrence_closed"
        assert not store.observations()
        store.close()

    asyncio.run(scenario())


def test_consumer_expires_automatic_evidence_even_on_a_fresh_packet(tmp_path):
    from test_identity_store import enroll, evidence, policy

    async def scenario():
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        profile = policy("cat").model_copy(update={"continuity_seconds": 2})
        store.register_policy(profile)
        identity, _, _ = enroll(store, species="cat")
        original = packet()
        key = occurrence_key(original)
        decision = store.observe(evidence(key, species="cat", observed_at=1), profile)
        assert decision.identity_id == identity
        resolved = original.with_artifact(
            Artifact(
                name=PRIVATE_DECISION_ARTIFACT,
                data=decision.model_dump_json().encode(),
                private=True,
            )
        )
        fresh = replace(resolved, payload={**resolved.payload, "frame_ts": 2.5})
        permitted = await read_identity_decision(fresh, services)
        assert permitted.identity_id == identity and permitted.candidate_ids == ()
        different_reason = fresh.with_artifact(Artifact(
            name=PRIVATE_DECISION_ARTIFACT, private=True,
            data=decision.model_copy(update={"reason": "different_projection"}).model_dump_json().encode(),
        ))
        assert await read_identity_decision(different_reason, services) is None
        # Packet creation time is fresh in all cases; the evidence age controls continuity.
        expired = replace(resolved, payload={**resolved.payload, "frame_ts": 3.01})
        assert await read_identity_decision(expired, services) is None
        future = replace(resolved, payload={**resolved.payload, "frame_ts": 0.5})
        assert await read_identity_decision(future, services) is None
        store.register_policy(profile.model_copy(update={"automatic_enabled": False}))
        assert await read_identity_decision(fresh, services) is None
        store.register_policy(profile)
        store.curate(
            action="rename",
            values={"identity_id": identity, "name": "Renamed"},
            actor="tester",
            request_key="renamed",
            expected_revision=store.revision,
        )
        assert await read_identity_decision(fresh, services) is None
        store.close()

    asyncio.run(scenario())


def test_consumer_database_read_does_not_block_the_event_loop(tmp_path):
    import threading

    async def scenario():
        main_thread = threading.get_ident()
        worker_threads = []
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        resolver = RecognizeIdentityRuntime(
            {"enabled": True}, PipelineRuntimeDependencies(services=services)
        )
        resolved = (await resolver.process_packet(with_evidence(packet()), Context()))[0]
        original_read = store.validated_decision

        def checked_read(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return original_read(*args, **kwargs)

        store.validated_decision = checked_read
        assert await read_identity_decision(resolved, services) is not None
        assert worker_threads and main_thread not in worker_threads
        store.close()

    asyncio.run(scenario())


def test_context_branch_link_preserves_lifecycle_and_original_frames():
    async def scenario():
        runtime = IdentityContextRuntime({"enabled": True}, PipelineRuntimeDependencies())
        for lifecycle in Lifecycle:
            original = packet(lifecycle).with_artifact(Artifact(name="main", data=b"fixture"))
            output = (await runtime.process_packet(original, Context()))[0]
            assert output.lifecycle == lifecycle
            assert output.packet_id == original.packet_id
            assert output.artifacts is original.artifacts
            assert output.payload["recognition"]["occurrence_id"] == occurrence_key(original)
            assert output.payload["recognition"]["status"] == "pending"
            assert {
                key: value for key, value in output.payload.items() if key != "recognition"
            } == original.payload

    asyncio.run(scenario())


def test_consumer_rechecks_expiry_after_waiting_for_database(tmp_path):
    import time
    from toposync_ext_vision.identity import pipelines as identity_pipelines

    async def scenario():
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        resolver = RecognizeIdentityRuntime(
            {"enabled": True}, PipelineRuntimeDependencies(services=services)
        )
        resolved = (await resolver.process_packet(with_evidence(packet()), Context()))[0]
        original_read = store.validated_decision

        def delayed_read(*args, **kwargs):
            time.sleep(0.10)
            return original_read(*args, **kwargs)

        store.validated_decision = delayed_read
        assert (
            await identity_pipelines.read_identity_decision(
                resolved, services, max_age_seconds=0.05
            )
            is None
        )
        store.close()

    asyncio.run(scenario())


def test_sampling_policy_selection_uses_species_and_space_independent_of_order(tmp_path):
    from test_identity_store import enroll, evidence, policy

    async def scenario():
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        identity, _, _ = enroll(store, species="dog")
        dog, cat = policy("dog"), policy("cat")
        for index, profiles in enumerate(([cat, dog], [dog, cat])):
            original = packet(correlation=f"order-{index}")
            original = replace(
                original,
                payload={
                    **original.payload,
                    "subject": {**original.payload["subject"], "category": "dog"},
                },
            )
            sample = evidence(occurrence_key(original), species="dog")
            incoming = original.with_artifact(
                Artifact(
                    name=PRIVATE_EVIDENCE_ARTIFACT,
                    data=json.dumps({"evidence": sample.model_dump(), "crop": None}).encode(),
                    private=True,
                )
            )
            resolver = RecognizeIdentityRuntime(
                {"enabled": True, "policies": [item.model_dump() for item in profiles]},
                PipelineRuntimeDependencies(services=services),
            )
            output = (await resolver.process_packet(incoming, Context()))[0]
            assert (await read_identity_decision(output, services)).identity_id == identity
            follow = replace(
                original,
                lifecycle=Lifecycle.UPDATE,
                payload={
                    **original.payload,
                    "frame_ts": 2,
                    "recognition": {"status": "pending", "reason": "sampling"},
                },
            )
            output = (await resolver.process_packet(follow, Context()))[0]
            assert output.payload["recognition"]["status"] == "recognized"
            assert (await read_identity_decision(output, services)).identity_id == identity
        store.close()

    asyncio.run(scenario())


def test_gallery_policy_lock_does_not_block_pipeline_event_loop(tmp_path):
    import threading
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler
    from test_identity_store import policy

    entered = threading.Event()
    release = threading.Event()
    policy_threads = []

    class BusyGallery(IdentityStore):
        def register_policy(self, value, *, owner=None):
            policy_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("Event loop blocked by gallery policy")
            super().register_policy(value, owner=owner)

    async def scenario():
        scheduler = ExecutionScheduler()
        store = BusyGallery(tmp_path / "gallery", scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        calibrated = policy("cat").model_copy(update={"embedding_space": "test:synthetic"})
        resolver = RecognizeIdentityRuntime(
            {"enabled": True, "policies": [calibrated.model_dump()]},
            PipelineRuntimeDependencies(services=services),
        )

        class ScheduledContext:
            async def run_blocking(self, function, *args, **kwargs):
                return await scheduler.run_sync(
                    function,
                    *args,
                    mode="thread_pool",
                    concurrency_key="test.identity",
                    max_concurrency=1,
                    **kwargs,
                )

        loop_thread = threading.get_ident()
        task = asyncio.create_task(
            resolver.process_packet(with_evidence(packet()), ScheduledContext())
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            release.set()
            output = (await asyncio.wait_for(task, 3))[0]
            assert len(policy_threads) == 1
            assert policy_threads[0] != loop_thread
            assert output.payload["recognition"]["reason"] != "gallery_operation_failed"
            assert len(store.observations()) == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await scheduler.shutdown()
            store.close()

    asyncio.run(scenario())


def test_canceled_resolution_keeps_its_executor_slot_until_persistence_finishes(tmp_path):
    import threading
    import pytest
    from toposync.runtime.pipelines.execution_scheduler import ExecutionScheduler

    entered = threading.Event()
    release = threading.Event()
    completed = []

    async def scenario():
        scheduler = ExecutionScheduler(thread_pool_max_workers=2)
        store = IdentityStore(tmp_path / "gallery", scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        resolver = RecognizeIdentityRuntime(
            {"enabled": True}, PipelineRuntimeDependencies(services=services)
        )
        resolve = resolver._resolve

        def delayed_resolve(value, *args):
            name = value.payload["correlation_id"]
            if name == "first":
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("test did not release first resolution")
            result = resolve(value, *args)
            completed.append(name)
            return result

        resolver._resolve = delayed_resolve
        second_queued = asyncio.Event()

        class ScheduledContext:
            async def run_blocking(self, function, *args, **kwargs):
                if args[0].payload["correlation_id"] == "second":
                    second_queued.set()
                return await scheduler.run_sync(
                    function,
                    *args,
                    mode="thread_pool",
                    concurrency_key="test.identity",
                    max_concurrency=1,
                    **kwargs,
                )

        context = ScheduledContext()
        first = asyncio.create_task(
            resolver.process_packet(with_evidence(packet(correlation="first")), context)
        )
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(
                resolver.process_packet(with_evidence(packet(correlation="second")), context)
            )
            await asyncio.wait_for(second_queued.wait(), 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), 0.05)
            assert completed == []
            release.set()
            output = (await asyncio.wait_for(second, 3))[0]
            assert completed == ["first", "second"]
            assert output.payload["recognition"]["reason"] == "calibration_required"
            assert len(store.observations()) == 2
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task), return_exceptions=True
            )
            await scheduler.shutdown()
            store.close()

    asyncio.run(scenario())


def test_uncalibrated_branch_cannot_reuse_automatic_name_on_replay_or_close(tmp_path):
    from test_identity_store import enroll, evidence, policy

    async def scenario():
        store = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: store)
        profile = policy("cat")
        try:
            identity, _, _ = enroll(store, species="cat")
            event = packet()
            sample = evidence(occurrence_key(event), species="cat", observed_at=1)
            incoming = event.with_artifact(Artifact(name=PRIVATE_EVIDENCE_ARTIFACT, private=True,
                data=json.dumps({"evidence": sample.model_dump(), "crop": None}).encode()))
            dependencies = PipelineRuntimeDependencies(services=services)
            calibrated = RecognizeIdentityRuntime({"enabled": True, "policies": [profile.model_dump()]}, dependencies)
            uncalibrated = RecognizeIdentityRuntime({"enabled": True}, dependencies)
            accepted, = await calibrated.process_packet(incoming, Context())
            assert (await read_identity_decision(accepted, services)).identity_id == identity
            followup = replace(event, lifecycle=Lifecycle.UPDATE, payload={
                **event.payload, "frame_ts": 2.0,
                "recognition": {"status": "pending", "reason": "sampling"},
            })
            for replay in (incoming, followup, replace(event, lifecycle=Lifecycle.CLOSE), incoming):
                result, = await uncalibrated.process_packet(replay, Context())
                assert result.payload["recognition"]["status"] == "unavailable"
                assert result.payload["recognition"]["reason"] == "calibration_required"
                assert await read_identity_decision(result, services) is None
            assert store.decision(occurrence_key(event)).identity_id == identity
        finally:
            store.close()

    asyncio.run(scenario())


def test_unverified_illumination_needs_explicit_calibration_even_on_replay(tmp_path):
    from test_identity_store import curate, enroll, evidence, policy

    async def scenario():
        gallery = IdentityStore(tmp_path, scope="test")
        services = ServiceRegistry()
        services.register("vision.identity.store", lambda: gallery)
        dependencies = PipelineRuntimeDependencies(services=services)
        qualified = policy("cat").model_dump()
        qualified["calibrated_without_illumination_metadata"] = True
        legacy = dict(qualified)
        del legacy["calibrated_without_illumination_metadata"]
        limited = RecognizeIdentityRuntime({"enabled": True, "policies": [legacy]}, dependencies)
        try:
            identity, _, _ = enroll(gallery, species="cat")
            event = packet()
            event = replace(event, payload={**event.payload, "illumination": "visible"})
            sample = evidence(occurrence_key(event), species="cat")
            incoming = event.with_artifact(Artifact(name=PRIVATE_EVIDENCE_ARTIFACT, private=True,
                data=json.dumps({"evidence": sample.model_dump(), "crop": None}).encode()))
            output, = await limited.process_packet(incoming, Context())
            assert output.payload["recognition"]["reason"] == "calibration_required"
            saved = gallery.occurrence_details(sample.occurrence_id)
            assert not saved["decision"]["candidate_ids"] and saved["decision"]["identity_id"] is None
            assert saved["observations"] and gallery.cluster_pending()["profiles"] == 0
            curate(gallery, "assign", {"observation_ids": [saved["observations"][0]["id"]], "identity_id": identity})
            closed, = await limited.process_packet(replace(event, lifecycle=Lifecycle.CLOSE), Context())
            assert (await read_identity_decision(closed, services)).provenance == "human"
            approved = RecognizeIdentityRuntime({"enabled": True, "policies": [qualified]}, dependencies)
            next_event = packet(correlation="other")
            next_sample = evidence(occurrence_key(next_event), species="cat")
            named, = await approved.process_packet(next_event.with_artifact(Artifact(
                name=PRIVATE_EVIDENCE_ARTIFACT, private=True,
                data=json.dumps({"evidence": next_sample.model_dump(), "crop": None}).encode())), Context())
            assert (await read_identity_decision(named, services)).identity_id == identity
            for message in (replace(next_event, lifecycle=Lifecycle.UPDATE), replace(next_event, lifecycle=Lifecycle.CLOSE)):
                projection, = await limited.process_packet(message, Context())
                assert projection.payload["recognition"]["reason"] == "calibration_required"
                assert await read_identity_decision(projection, services) is None
            await approved.shutdown()
            assert gallery.cluster_pending()["profiles"] == 0
            assert await read_identity_decision(named, services) is None
        finally:
            gallery.close()

    asyncio.run(scenario())


def test_shutdown_drains_admitted_observation_and_rejects_late_work(tmp_path):
    from threading import Event
    from test_identity_store import policy

    async def scenario():
        entered, finish = Event(), Event()
        gallery = IdentityStore(tmp_path, scope="shutdown")
        observe = gallery.observe

        def delayed(*args, **kwargs):
            entered.set()
            assert finish.wait(3)
            return observe(*args, **kwargs)

        gallery.observe = delayed
        profile = policy("cat").model_copy(update={"embedding_space": "test:synthetic"})
        runtime = RecognizeIdentityRuntime({"enabled": True, "policies": [profile.model_dump()]},
                                           PipelineRuntimeDependencies())
        event = with_evidence(packet())
        key = occurrence_key(event)
        pending = asyncio.create_task(asyncio.to_thread(runtime._resolve, event, gallery, key,
                                                         event.artifacts[PRIVATE_EVIDENCE_ARTIFACT]))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            shutdown = asyncio.create_task(runtime.shutdown())
            done, _ = await asyncio.wait({shutdown}, timeout=0.2)
            assert not done
            finish.set()
            await pending
            await shutdown
            assert gallery.cluster_pending()["profiles"] == 0
            assert runtime._resolve(replace(event, lifecycle=Lifecycle.CLOSE), gallery, key, None) is None
            assert gallery.occurrence_details(key)["closed"] is False
        finally:
            finish.set()
            await pending
            await runtime.shutdown()
            gallery.close()

    asyncio.run(scenario())
