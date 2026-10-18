import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from toposync.runtime.pipelines import PipelineRuntimeDependencies, Packet
from toposync.runtime.services import ServiceRegistry
from toposync_ext_vision.association_api import AssociationRepository
from toposync_ext_vision.association_runtime import AssociationRuntime, observation_from_packet
from test_vision_association import config


def packet(camera="a", stamp=0, track=1, x=0, **payload):
    return Packet.create(
        stream_id=camera,
        payload={
            "source": {"device_id": camera, "source_id": camera, "clock_domain": "clock"},
            "camera_id": camera,
            "media": {"ts": stamp},
            "tracklet_id": str(track),
            "correlation_id": "episode",
            "capture_evidence": {
                "capture_instance": camera,
                "generation": 1,
                "sequence": int(stamp) + 1,
            },
            "subject": {
                "type": "event",
                "id": str(track),
                "category": "person",
                "world_anchor": {"x": x, "z": stamp},
            },
            "mapping": {
                "status": "mapped",
                "composition_id": "ground",
                "calibrated_view_id": camera,
                "panorama_revision": 1,
            },
            **payload,
        },
    )


def test_adapter_uses_original_provenance_and_rejects_stale_anchor():
    original = packet()
    item = observation_from_packet(original, config())
    assert item.position == (0, 0) and item.observed_at == 0 and item.uncertainty == 0
    stale = packet(
        subject={
            **original.payload["subject"],
            "source_anchor": {"capture_evidence": {"sequence": 99}},
        }
    )
    assert observation_from_packet(stale, config()).position is None
    with pytest.raises(ValueError):
        observation_from_packet(packet(media={}), config())
    unknown = packet(source={"source_id": "a", "clock_domain": "unknown"})
    assert observation_from_packet(unknown, config()).uncertainty is None
    moving = replace(original, metadata={"ptz_geometry_safe": False})
    assert observation_from_packet(moving, config()).position is None


def test_real_operator_batches_and_deduplicates_with_bounded_shutdown(tmp_path):
    async def scenario():
        services = ServiceRegistry()
        repository = AssociationRepository(tmp_path)
        services.register("vision.association.store", repository.get)
        runtime = AssociationRuntime(
            config().model_dump(), PipelineRuntimeDependencies(services=services)
        )
        queue = asyncio.Queue()
        emitted = []
        inspection = []
        cancel = asyncio.Event()

        async def emit(value):
            emitted.append(value)

        context = SimpleNamespace(
            read=queue.get,
            emit=emit,
            cancel_event=cancel,
            inspect=lambda *a, **kw: inspection.append((a, kw)),
        )
        await runtime.prepare(context)
        task = asyncio.create_task(runtime.run(context))
        try:
            inputs = [
                packet(camera, stamp, track, (track - 1) * 0.7)
                for camera, stamp in [("a", 0), ("a", 1), ("b", 2), ("b", 3), ("c", 4)]
                for track in (1, 2, 3)
            ]
            for item in inputs:
                await queue.put(item)
            for _ in range(100):
                if len(emitted) == 15:
                    break
                await asyncio.sleep(0.005)
            assert len(emitted) == 15
            assert len({p.payload["association"]["entity_id"] for p in emitted}) == 3
            for item in inputs:
                await queue.put(item)
            await asyncio.sleep(0.15)
            assert len(emitted) == 15
            assert all("identity_id" not in p.payload["association"] for p in emitted)
            assert inspection
        finally:
            cancel.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
        assert runtime.store.owner is None
        assert not [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]

    asyncio.run(asyncio.wait_for(scenario(), 2))


@pytest.mark.parametrize("revoked", [False, True])
def test_end_of_input_flushes_final_batch_unless_revoked(tmp_path, revoked):
    async def scenario():
        repository = AssociationRepository(tmp_path)
        services = ServiceRegistry()
        services.register("vision.association.store", repository.get)
        runtime = AssociationRuntime(
            config().model_dump(), PipelineRuntimeDependencies(services=services)
        )
        inputs = iter([packet(), None])
        cancel = asyncio.Event()
        emitted = []

        async def read():
            item = next(inputs)
            if item is None and revoked:
                cancel.set()
            return item

        async def emit(item):
            emitted.append(item)

        context = SimpleNamespace(
            read=read, emit=emit, cancel_event=cancel, inspect=lambda *a, **kw: None
        )
        await asyncio.wait_for(runtime.run(context), 1)
        assert len(emitted) == (0 if revoked else 1)
        assert runtime.store.owner is None

    asyncio.run(scenario())


def test_maintenance_releases_expired_inactive_scope_capacity(tmp_path, monkeypatch):
    repository = AssociationRepository(tmp_path)
    store = repository.get(config=config())
    store.ingest([observation_from_packet(packet(), config())])
    assert store.path.exists()
    import time

    future = time.time() + 121
    monkeypatch.setattr("toposync_ext_vision.association.time.time", lambda: future)
    repository.maintain()
    assert repository.stores == {}
    assert not store.path.exists()


def test_no_identification_output_removes_upstream_gallery_handles(tmp_path):
    from toposync.runtime.pipelines import Artifact
    from toposync_ext_vision.association import AssociationStore

    async def scenario():
        cfg = config()
        cfg.sources[0].identity_policy = "no_identification"
        runtime = AssociationRuntime(cfg.model_dump(), PipelineRuntimeDependencies())
        runtime.store = AssociationStore(cfg)
        original = packet(recognition={"occurrence_id": "private-gallery-link"}).with_artifact(
            Artifact(
                name="identity_decision",
                data=b"private decision",
                mime_type="application/json",
                private=True,
            )
        )
        emitted = []

        async def emit(value):
            emitted.append(value)

        await runtime._flush(
            [(original, observation_from_packet(original, cfg))],
            SimpleNamespace(emit=emit, inspect=lambda *a, **kw: None),
        )
        assert "recognition" not in emitted[0].payload
        assert "identity_decision" not in emitted[0].artifacts
        assert emitted[0].payload["capture_evidence"] == original.payload["capture_evidence"]
        assert (
            runtime.store.snapshot()["observations"][observation_from_packet(original, cfg).id][
                "observation"
            ]["occurrence"]
            is None
        )

    asyncio.run(scenario())
