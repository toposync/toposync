import json

import pytest
from pydantic import ValidationError

from toposync_ext_vision.association import (
    AssociationConfig,
    AssociationSource,
    AssociationStore,
    AssociationTransition,
    AssociationConflict,
    Observation,
)


def config():
    return AssociationConfig(
        scope="test",
        group_distance_metres=2,
        sources=[
            AssociationSource(
                camera_id=c,
                source_id=c,
                identity_policy="recognize_here" if c == "b" else "keep_confirmed_identity",
                clock_domain="clock",
                clock_uncertainty_seconds=0,
                coordinate_reference="ground",
                position_error_metres=0.02,
            )
            for c in ("a", "b", "c")
        ],
        transitions=[
            AssociationTransition(
                source_camera=a, target_camera=b, max_seconds=3, residual_metres=0.5
            )
            for a, b in [("a", "b"), ("b", "c")]
        ],
    )


def observation(camera, track, stamp, x, *, instance="capture", **kwargs):
    data = dict(
        id=f"{camera}-{instance}-{track}-{stamp}",
        camera=camera,
        source=camera,
        instance=instance,
        generation=1,
        sequence=int(stamp) + 1,
        track=str(track),
        category="person",
        observed_at=stamp,
        clock="clock",
        uncertainty=0,
        position=(x, stamp),
        coordinate_reference="ground",
        geometry="map-v1",
        position_error=0.02,
        policy="recognize_here" if camera == "b" else "keep_confirmed_identity",
    )
    return Observation(**{**data, **kwargs})


def test_trio_permuted_local_ids_preserves_three_partitions_and_group_episodes():
    store = AssociationStore(config())
    partitions = [set() for _ in range(3)]
    for stamp in range(8):
        camera = "a" if stamp < 2 else "b" if stamp < 4 else "c"
        tracks = (1, 2, 3) if camera == "a" else (3, 1, 2) if camera == "b" else (2, 3, 1)
        batch = []
        for index, track in enumerate(tracks):
            x = 2 + 0.7 * index if not (stamp == 6 and index == 2) else 7
            item = observation(camera, track, stamp, x)
            batch.append(item)
            partitions[index].add(item.id)
        store.ingest(list(reversed(batch)))
    snapshot = store.snapshot()
    actual = {
        frozenset(
            key for key, record in snapshot["observations"].items() if record["entity"] == entity
        )
        for entity in {r["entity"] for r in snapshot["observations"].values()}
    }
    assert actual == {frozenset(partition) for partition in partitions}
    assert [(len(g["members"]), g["start"], g["end"]) for g in snapshot["groups"]] == [
        (3, 0, 6),
        (2, 6, 7),
        (3, 7, None),
    ]
    assert not any("identity_id" in r for r in snapshot["observations"].values())


@pytest.mark.parametrize("uncertainty,positions", [(None, True), (0, False), (0, True)])
def test_ambiguity_and_missing_clock_or_geometry_never_force_cross_camera(uncertainty, positions):
    store = AssociationStore(config())
    for stamp in (0, 1):
        store.ingest(
            [
                observation(
                    "a",
                    t,
                    stamp,
                    0,
                    uncertainty=uncertainty,
                    position=(0, stamp) if positions else None,
                )
                for t in (1, 2)
            ]
        )
    store.ingest(
        [
            observation(
                "b", t, 2, 0, uncertainty=uncertainty, position=(0, 2) if positions else None
            )
            for t in (1, 2)
        ]
    )
    values = store.snapshot()["observations"]
    assert {values[f"a-capture-{t}-0"]["entity"] for t in (1, 2)}.isdisjoint(
        {values[f"b-capture-{t}-2"]["entity"] for t in (1, 2)}
    )


def test_correction_precedes_inference_survives_restart_and_is_revision_checked(tmp_path):
    path = tmp_path / "state.json"
    store = AssociationStore(config(), path)
    a = observation("a", 1, 0, 0)
    b = observation("b", 9, 2, 0)
    store.ingest([a, b])
    entity = store.observations[a.id]["entity"]
    store.correct(b.id, entity, store.revision, "tester")
    with pytest.raises(AssociationConflict):
        store.correct(b.id, None, 0, "tester")
    restored = AssociationStore(config(), path)
    next_b = observation("b", 9, 3, 9)
    restored.ingest([next_b])
    assert restored.observations[next_b.id]["entity"] == entity
    assert restored.observations[next_b.id]["gap"] is True
    assert len(restored.history) == 1
    assert not list(tmp_path.glob("*.pending"))


def test_duplicate_out_of_order_restart_geometry_and_policy_boundaries():
    store = AssociationStore(config())
    a = observation("a", 1, 1, 0)
    store.ingest([a])
    revision = store.revision
    store.ingest([a])
    assert store.revision == revision
    store.ingest([observation("a", 1, 0, 0)])
    assert store.revision == revision
    for item in (
        observation("a", 1, 2, 0, instance="new"),
        observation("a", 1, 3, 0, instance="new", geometry="changed"),
        observation("a", 1, 4, 0, instance="new", policy="no_identification"),
    ):
        store.ingest([item])
        assert store.observations[item.id]["entity"] != store.observations[a.id]["entity"]
    with pytest.raises(AssociationConflict):
        store.correct(item.id, store.observations[a.id]["entity"], store.revision, "tester")


def test_same_camera_simultaneous_tracks_cannot_be_human_merged():
    store = AssociationStore(config())
    a, b = observation("a", 1, 1, 0), observation("a", 2, 1, 0)
    store.ingest([a, b])
    with pytest.raises(AssociationConflict):
        store.correct(a.id, store.observations[b.id]["entity"], store.revision, "tester")


def test_finite_values_and_bounded_state_and_single_owner():
    with pytest.raises(ValidationError):
        observation("a", 1, 1, float("nan"))
    cfg = config().model_copy(update={"max_observations": 16})
    store = AssociationStore(cfg)
    for stamp in range(30):
        store.ingest([observation("a", 1, stamp, 0)])
    assert len(store.observations) == 16
    assert len(json.dumps(store.snapshot())) < 40000
    owner = object()
    store.claim(owner)
    with pytest.raises(AssociationConflict):
        store.claim(object())
    store.release(owner)
    store.claim(object())


def test_overlap_requires_consistency_with_every_simultaneous_view():
    cfg = config().model_copy(
        update={
            "transitions": [
                AssociationTransition(
                    source_camera=a,
                    target_camera=b,
                    mode="overlap",
                    max_seconds=0.1,
                    residual_metres=0.5,
                )
                for a, b in [("a", "b"), ("b", "c")]
            ]
        }
    )
    store = AssociationStore(cfg)
    a, b, c = [observation(camera, 1, 0, 0) for camera in ("a", "b", "c")]
    store.ingest([a, b, c])
    assert store.observations[a.id]["entity"] == store.observations[b.id]["entity"]
    assert store.observations[c.id]["entity"] != store.observations[a.id]["entity"]


def test_retired_capture_cannot_resume_and_duplicate_close_preserves_lifecycle():
    store = AssociationStore(config())
    old = observation("a", 1, 0, 0)
    fresh = observation("a", 1, 1, 0, instance="new")
    store.ingest([old, fresh])
    revision = store.revision
    store.ingest([observation("a", 1, 2, 0)])
    assert store.revision == revision
    store.ingest([fresh.model_copy(update={"closed": True})])
    assert store.observations[fresh.id]["observation"]["closed"]
    with pytest.raises(AssociationConflict):
        store.ingest([fresh.model_copy(update={"position": (99, 99)})])


def test_correction_checks_entire_trajectory_and_keeps_group_times_valid():
    store = AssociationStore(config())
    first = observation("a", 1, 0, 0)
    later = observation("a", 1, 1, 0)
    other = observation("a", 2, 1, 1)
    store.ingest([first, later, other])
    with pytest.raises(AssociationConflict, match="simultaneous_local_tracks"):
        store.correct(first.id, store.observations[other.id]["entity"], store.revision, "tester")
    store.correct(first.id, None, store.revision, "tester")
    snapshot = store.snapshot()
    assert all(g["end"] is None or g["end"] >= g["start"] for g in snapshot["groups"])
    assert snapshot["groups"][-1]["start"] == 1
    assert snapshot["history"][-1]["observation_count"] == 2
    assert snapshot["history"][-1]["previous_entities"]


def test_replayed_duplicates_do_not_reopen_group_episodes_after_restart(tmp_path):
    path = tmp_path / "association.json"
    store = AssociationStore(config(), path)
    batches = [
        [observation("a", track, stamp, track if stamp == 0 else track * 5) for track in (1, 2)]
        for stamp in (0, 1)
    ]
    for batch in batches:
        store.ingest(batch)
    before = store.snapshot()
    restored = AssociationStore(config(), path)
    for batch in batches:
        restored.ingest(batch)
    after = restored.snapshot()
    assert after["groups"] == before["groups"]
    assert after["revision"] == before["revision"]


def test_conflicting_duplicate_is_rejected_before_partial_ingest():
    store = AssociationStore(config())
    original = observation("a", 1, 1, 0)
    store.ingest([original])
    before = store.snapshot()
    with pytest.raises(AssociationConflict, match="identifier_conflict"):
        store.ingest([observation("a", 2, 0, 1), original.model_copy(update={"position": (8, 8)})])
    assert store.snapshot() == before
