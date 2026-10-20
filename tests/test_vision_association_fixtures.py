"""Official post-tracker fixtures; oracles are read only after product execution."""

import json
from pathlib import Path

from toposync_ext_vision.association import (
    AssociationConfig,
    AssociationSource,
    AssociationTransition,
    AssociationStore,
)
from toposync_ext_vision.association_runtime import observation_from_packet
from toposync.runtime.pipelines import Packet
from test_vision_association import config, observation


def official_inputs():
    data = json.loads((Path(__file__).parent / "fixtures/multicamera_inputs.json").read_text())
    return {case["identifier"]: case["inputs"] for case in data["cases"]}


def adapter(case):
    profiles = {}
    packets = []
    original_ids = {}
    for row in case["observations"]:
        camera = row["camera_identifier"]
        source = row["source_identifier"]
        profiles[camera] = AssociationSource(
            camera_id=camera,
            source_id=source,
            identity_policy=case.get("identity_policy", {}).get(camera, "keep_confirmed_identity"),
            clock_domain=row.get("clock_domain_identifier", ""),
            clock_uncertainty_seconds=row.get("clock_uncertainty_seconds"),
            coordinate_reference=row.get("coordinate_reference", ""),
            position_error_metres=row.get("position_error_bound_metres"),
        )
        point = row.get("world_position")
        subject = {
            "type": "event",
            "id": row["local_track_identifier"],
            "category": row["category"],
        }
        if point is not None:
            subject["world_anchor"] = {"x": point[0], "z": point[1]}
        packets.append(
            Packet.create(
                stream_id=source,
                payload={
                    "source": {
                        "source_id": source,
                        "clock_domain": row.get("clock_domain_identifier", ""),
                    },
                    "camera_id": camera,
                    "capture_evidence": {
                        "capture_instance": row.get(
                            "capture_instance_identifier", "fixture-instance-" + source
                        ),
                        "generation": row["capture_generation"],
                        "sequence": row.get("frame_sequence", int(row["observed_at_seconds"]) + 1),
                    },
                    "media": {"ts": row["observed_at_seconds"]},
                    "tracklet_id": row["local_track_identifier"],
                    "subject": subject,
                    "correlation_id": "synthetic-fixture-episode",
                    "mapping": {
                        "status": "mapped" if point is not None else "unmapped",
                        "composition_id": row.get("coordinate_reference"),
                        "calibrated_view_id": "fixture-calibration",
                        "panorama_revision": row.get("mapping_revision"),
                    },
                },
            )
        )
    captures = {}
    for packet in packets:
        capture = packet.payload["capture_evidence"]
        key = (packet.payload["camera_id"], packet.payload["source"]["source_id"],
               capture["capture_instance"], capture["generation"], capture["sequence"])
        captures.setdefault(key, []).append(packet)
    for capture_packets in captures.values():
        tracks = sorted({packet.payload["tracklet_id"] for packet in capture_packets})
        for packet in capture_packets:
            packet.payload["frame_tracks"] = tracks
    prepared = case.get("prepared_context", {})
    cfg = AssociationConfig(
        scope="official",
        sources=list(profiles.values()),
        transitions=[
            AssociationTransition(
                source_camera=a, target_camera=b, max_seconds=3, residual_metres=0.5
            )
            for a, b in prepared.get("known_transitions", [])
        ],
        group_distance_metres=prepared.get("group_distance_limit_metres"),
    )
    observations = [observation_from_packet(packet, cfg) for packet in packets]
    for row, item in zip(case["observations"], observations, strict=True):
        original_ids[item.id] = row["observation_identifier"]
    return cfg, observations, original_ids


def test_official_positive_and_ambiguous_partitions_without_oracle_inputs():
    results = {}
    for name, case in official_inputs().items():
        cfg, observations, original_ids = adapter(case)
        store = AssociationStore(cfg)
        store.ingest(observations)
        results[name] = (store.snapshot(), original_ids)
    oracles = {
        c["identifier"]: c["expected"]
        for c in json.loads(
            (Path(__file__).parent / "fixtures/multicamera_oracles.json").read_text()
        )["cases"]
    }
    positive, ids = results["F01"]
    actual = {
        frozenset(
            ids[key] for key, value in positive["observations"].items() if value["entity"] == entity
        )
        for entity in {record["entity"] for record in positive["observations"].values()}
    }
    assert actual == {
        frozenset(values) for values in oracles["F01"]["entity_observation_partitions"].values()
    }
    assert [(len(g["members"]), g["start"], g["end"]) for g in positive["groups"]] == [
        (3, 0, 6),
        (2, 6, 7),
        (3, 7, None),
    ]
    negative, ids = results["F02"]
    entities = {ids[key]: value["entity"] for key, value in negative["observations"].items()}
    for left, right in oracles["F02"]["forbidden_accepted_pairs"]:
        assert entities[left] != entities[right]


def test_holdout_diagonal_two_member_route_has_no_local_id_hint():
    store = AssociationStore(config())
    expected = [[], []]
    for camera, stamps, tracks in [
        ("a", [0, 1], [91, 18]),
        ("b", [2, 3], [7, 5]),
        ("c", [4, 5], [5, 91]),
    ]:
        for stamp in stamps:
            batch = []
            for index, track in enumerate(tracks):
                item = observation(
                    camera,
                    track,
                    stamp,
                    10 - stamp + index * 0.8,
                    position=(10 - stamp + index * 0.8, 2 * stamp),
                )
                expected[index].append(item.id)
                batch.append(item)
            store.ingest(batch[::-1])
    assert len({record["entity"] for record in store.observations.values()}) == 2
    for trajectory in expected:
        assert len({store.observations[key]["entity"] for key in trajectory}) == 1


def test_holdout_competing_crossing_abstains_even_when_order_is_reversed():
    store = AssociationStore(config())
    store.ingest([observation("a", 8, 0, -1), observation("a", 3, 0, 1)])
    store.ingest([observation("a", 8, 1, -0.5), observation("a", 3, 1, 0.5)])
    new = [observation("b", 3, 2, 0), observation("b", 8, 2, 0)]
    store.ingest(new[::-1])
    assert all(store.observations[item.id]["reason"] == "ambiguous_competitors" for item in new)
    assert len({record["entity"] for record in store.observations.values()}) == 4
