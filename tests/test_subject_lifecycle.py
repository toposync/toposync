from toposync.runtime.pipelines.subject_lifecycle import ClosedSubjectLedger


def subject(sequence, epoch="first"):
    return {"lifecycle_epoch": epoch, "lifecycle_sequence": sequence}


def test_compaction_keeps_late_open_and_old_active_subjects_without_reviving_closed():
    ledger = ClosedSubjectLedger(capacity=4)
    def key(number):
        return ("stream", "camera", str(number))

    assert not ledger.observe(key(1), subject(1))
    for number in range(3, 5004):
        assert not ledger.observe(key(number), subject(number))
        ledger.add(key(number))
    assert len(ledger) == 1
    assert not ledger.capacity_reached
    # Um ator antigo e uma abertura atrasada ainda não encerrada continuam válidos.
    assert not ledger.observe(key(1), subject(1))
    assert not ledger.observe(key(2), subject(2))
    ledger.add(key(2))
    ledger.add(key(1))
    assert len(ledger) == 1
    for number in (1, 2, 3, 4097, 5003):
        assert ledger.observe(key(number), subject(number))
    assert not ledger._active
    assert not ledger.observe(key(5004), subject(5004))
    # Reconexão preserva época; reiniciar o produtor cria uma nova.
    assert ledger.observe(key(1), subject(1))
    assert not ledger.observe(key(1), subject(1, "second"))
    ledger.add(key(1))
    assert ledger.observe(key(1), subject(1, "first"))
    assert ledger.observe(key(1), subject(1, "second"))
    assert ledger.observe(key(1), {})  # não aceita perda de época após negociação
    ledger.clear()
    assert not ledger.observe(key(1), subject(1))


def test_fragmented_epochs_and_active_records_are_bounded_fail_closed():
    ledger = ClosedSubjectLedger(capacity=2)
    for epoch in ("one", "two", "three"):
        key = ("stream", "camera", epoch)
        assert not ledger.observe(key, subject(1, epoch))
        ledger.add(key)
    assert len(ledger) == 2
    assert ledger.capacity_reached
    ledger.clear()
    for number in range(3):
        ledger.observe(("stream", "camera", str(number)), subject(number + 1))
    assert len(ledger._active) == 2
    assert ledger.capacity_reached


def test_delayed_close_from_old_epoch_cannot_retire_reused_actor():
    import asyncio
    from toposync.runtime.pipelines.runtime import Lifecycle
    from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
    from test_vision_gestures import packet

    async def scenario():
        runtime = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.2})
        def sample(timestamp, epoch, lifecycle):
            value = packet(timestamp, "raised", actor="reused", lifecycle=lifecycle)
            value.payload["subject"].update(subject(1, epoch))
            return value
        try:
            await runtime.process_packet(sample(0, "old", Lifecycle.OPEN), None)
            await runtime.process_packet(sample(0.25, "old", Lifecycle.CLOSE), None)
            await runtime.process_packet(sample(1, "new", Lifecycle.OPEN), None)
            await runtime.process_packet(sample(1.25, "new", Lifecycle.UPDATE), None)
            await runtime.process_packet(sample(0.25, "old", Lifecycle.CLOSE), None)
            result = (await runtime.process_packet(sample(1.5, "new", Lifecycle.UPDATE), None))[-1]
            assert result.payload["vision"]["gestures"]["status"] == "active"
            assert len(runtime._actors) == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_five_thousand_unadmitted_packets_do_not_consume_lifecycle_capacity():
    import asyncio
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync_ext_cameras.pipelines.person_ground import PersonGroundRuntime
    from toposync_ext_cameras.pipelines.pointing import PointingTargetRuntime
    from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
    from test_vision_gestures import packet

    async def scenario():
        for mode in ("disabled", "stale", "missing_actor", "missing_source", "missing_evidence"):
            enabled = mode != "disabled"
            operators = (
                PersonGroundRuntime({"enabled": enabled}, PipelineRuntimeDependencies()),
                PointingTargetRuntime({"enabled": enabled}, PipelineRuntimeDependencies()),
                VisionGestureRecognizeRuntime({"enabled": enabled}),
            )
            try:
                for number in range(5000):
                    value = packet(number, "raised", actor=f"actor:{number}")
                    value.payload["subject"].update(subject(number + 1))
                    if mode == "stale":
                        value.payload["capture_evidence"]["published_at"] = 0
                    elif mode == "missing_actor":
                        value.payload["subject"]["id"] = ""
                    elif mode == "missing_source":
                        from dataclasses import replace
                        value = replace(value, stream_id="")
                        value.payload["source_stream_id"] = ""
                    elif mode == "missing_evidence":
                        value.payload.pop("capture_evidence")
                    for operator in operators:
                        await operator.process_packet(value, None)
                for operator in operators:
                    ledger = getattr(operator, "closed_subjects", None)
                    if ledger is None:
                        ledger = operator._closed
                    assert not ledger._active, mode
                    assert not ledger._sources, mode
                    assert not ledger.capacity_reached, mode
            finally:
                for operator in operators:
                    await operator.shutdown()

    asyncio.run(scenario())


def test_gesture_eviction_and_source_close_release_sequence_descriptors():
    import asyncio
    from toposync.runtime.pipelines.runtime import Lifecycle, Packet
    from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
    from test_vision_gestures import packet

    async def scenario():
        runtime = VisionGestureRecognizeRuntime({"maximum_subjects": 2})
        runtime._closed.capacity = 2
        try:
            for number in range(5000):
                value = packet(number, "raised", actor=f"actor:{number}")
                value.payload["subject"].update(subject(number + 1))
                await runtime.process_packet(value, None)
                assert len(runtime._closed._active) <= 2
            assert not runtime._closed.capacity_reached
            await runtime.process_packet(Packet.create(
                stream_id="camera:a", lifecycle=Lifecycle.CLOSE,
                payload={"camera_id": "camera:a", "source_stream_id": "camera:a"},
            ), None)
            assert not runtime._actors
            assert not runtime._closed._active
            for number in (4998, 4999):
                value = packet(6000, "raised", actor=f"actor:{number}")
                value.payload["subject"].update(subject(number + 1))
                result = (await runtime.process_packet(value, None))[-1]
                assert result.payload["vision"]["gestures"]["reason"] == "actor_closed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_spatial_eviction_and_source_close_release_sequence_descriptors():
    import asyncio
    from dataclasses import replace
    from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
    from toposync.runtime.pipelines.runtime import Lifecycle
    from toposync_ext_cameras.pipelines.person_ground import PersonGroundRuntime
    from toposync_ext_cameras.pipelines.pointing import PointingTargetRuntime
    from test_person_ground import packet, mapping_config

    async def scenario():
        ground = PersonGroundRuntime(
            {"mapping": mapping_config(), "maximum_subjects": 2}, PipelineRuntimeDependencies()
        )
        pointing = PointingTargetRuntime({}, PipelineRuntimeDependencies())
        try:
            for number in range(4):
                value = packet(number, actor=f"person:{number}")
                value.payload["subject"].update(subject(number + 1))
                value = (await ground.process_packet(value, None))[0]
                await pointing.process_packet(value, None)
                assert len(ground.closed_subjects._active) <= 2
            closed = replace(value, lifecycle=Lifecycle.CLOSE, payload={**value.payload, "subject": {}})
            await ground.process_packet(closed, None)
            await pointing.process_packet(closed, None)
            for runtime in (ground, pointing):
                assert not runtime.closed_subjects._active
                assert not runtime.closed_subjects.capacity_reached
                result = (await runtime.process_packet(value, None))[0].payload["spatial"]
                kind = "person_ground" if runtime is ground else "pointing"
                assert result[kind]["reason"] == "subject_closed"
        finally:
            await ground.shutdown()
            await pointing.shutdown()

    asyncio.run(scenario())


def test_file_producer_and_camera_optional_gestures_keep_source_scope():
    import asyncio
    from toposync_ext_vision.pipelines.schemas import VisionTrackConfig
    from toposync_ext_vision.processing.tasks.event_assembler import TrackEventAssembler
    from toposync_ext_vision.processing.tasks.gestures import VisionGestureRecognizeRuntime
    from test_vision_gestures import packet

    async def scenario():
        producer = TrackEventAssembler(VisionTrackConfig(default_interval_seconds=0))
        runtime = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.2})
        direct = VisionGestureRecognizeRuntime({"minimum_duration_seconds": 0.2})
        try:
            for timestamp in (0, 0.25):
                value = packet(timestamp, "raised", camera="file:clip", actor="tracked")
                value.payload.pop("camera_id")
                value.payload["vision"]["poses"][0]["tracking_id"] = "track:1"
                value.payload["vision"]["tracks"] = [{
                    "tracklet_id": "track:1", "label": "person", "score": 0.9,
                    "bbox01": [0.1, 0.1, 0.9, 0.9],
                }]
                from dataclasses import replace
                value = replace(value, stream_id="file:clip")
                emitted = (await producer.process_packet(value, None))[0]
                assert emitted.payload["subject"]["lifecycle_sequence"] == 1
                assert emitted.payload["camera_id"] == "file:clip"
                result = (await runtime.process_packet(emitted, None))[-1]
                emitted.payload.pop("camera_id")
                emitted.payload["vision"]["poses"][0].pop("camera_id")
                direct_result = (await direct.process_packet(emitted, None))[-1]
            assert result.payload["vision"]["gestures"]["status"] == "active"
            assert direct_result.payload["vision"]["gestures"]["status"] == "active"
            assert direct_result.payload["vision"]["gestures"]["source_stream_id"] == "file:clip"
        finally:
            await runtime.shutdown()
            await direct.shutdown()

    asyncio.run(scenario())
