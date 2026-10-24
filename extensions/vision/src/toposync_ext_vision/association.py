"""Bounded, revisioned continuity of observations; never a biometric gallery."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import tempfile
import time
import uuid
from functools import lru_cache
from itertools import combinations, groupby
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Values(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AssociationSource(Values):
    camera_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    identity_policy: Literal[
        "no_identification", "anonymous_continuity", "keep_confirmed_identity", "recognize_here"
    ] = "no_identification"
    clock_domain: str = Field(default="", max_length=128)
    clock_uncertainty_seconds: float | None = Field(default=None, ge=0, le=60)
    coordinate_reference: str = Field(default="", max_length=128)
    position_error_metres: float | None = Field(default=None, ge=0, le=100)


class AssociationTransition(Values):
    source_camera: str
    target_camera: str
    mode: Literal["transition", "overlap"] = "transition"
    max_seconds: float = Field(gt=0, le=300)
    residual_metres: float = Field(gt=0, le=100)


class AssociationConfig(Values):
    scope: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9_-]*$")
    sources: list[AssociationSource] = Field(default_factory=list, max_length=16)
    transitions: list[AssociationTransition] = Field(default_factory=list, max_length=128)
    batch_seconds: float = Field(default=0.1, ge=0.02, le=1)
    horizon_seconds: float = Field(default=120, ge=1, le=3600)
    competitor_margin_metres: float = Field(default=0.1, gt=0, le=10)
    group_distance_metres: float | None = Field(default=None, gt=0, le=20)
    max_observations: int = Field(default=2048, ge=16, le=8192)
    max_batch: int = Field(default=128, ge=1, le=512)

    @model_validator(mode="after")
    def unique_sources(self):
        keys = [(s.camera_id, s.source_id) for s in self.sources]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate source")
        cameras = {s.camera_id for s in self.sources}
        for transition in self.transitions:
            if transition.source_camera not in cameras or transition.target_camera not in cameras:
                raise ValueError("transition requires configured cameras")
            if transition.source_camera == transition.target_camera:
                raise ValueError("cross-camera transition required")
        return self

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Observation(Values):
    id: str = Field(min_length=1, max_length=128)
    camera: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    instance: str = Field(min_length=1, max_length=128)
    generation: int = Field(strict=True, ge=0)
    sequence: int = Field(strict=True, ge=1)
    track: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=64)
    observed_at: float
    clock: str = Field(default="", max_length=128)
    uncertainty: float | None = Field(default=None, ge=0, le=60)
    position: tuple[float, float] | None = None
    coordinate_reference: str = Field(default="", max_length=128)
    geometry: str = Field(default="", max_length=256)
    position_error: float | None = Field(default=None, ge=0, le=100)
    policy: Literal[
        "no_identification", "anonymous_continuity", "keep_confirmed_identity", "recognize_here"
    ]
    occurrence: str | None = Field(default=None, max_length=128)
    closed: bool = False
    frame_tracks: list[Annotated[str, Field(min_length=1, max_length=512)]] | None = Field(
        default=None, min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def validate_frame_tracks(self):
        if self.frame_tracks is not None and (
            len(set(self.frame_tracks)) != len(self.frame_tracks) or self.track not in self.frame_tracks
        ):
            raise ValueError("frame_tracks_require_unique_members_including_this_track")
        return self

    @property
    def epoch(self):
        return self.instance, self.generation

    @property
    def local_key(self):
        return json.dumps(
            [self.camera, self.source, *self.epoch, self.track, self.geometry, self.policy]
        )


class AssociationConflict(ValueError):
    pass


class AssociationStore:
    def __init__(self, config: AssociationConfig, path: Path | None = None):
        self.config, self.path = config, path
        self.lock = threading.RLock()
        self.owner = None
        self.revision = 0
        self.observations: dict[str, dict] = {}
        self.corrections: dict[str, str] = {}
        self.history: list[dict] = []
        self.groups: list[dict] = []
        self.restart = False
        self._resumed_keys = set()
        if path and path.exists():
            if path.stat().st_size > 16 * 1024 * 1024:
                raise AssociationConflict("association_state_too_large")
            state = json.loads(path.read_text())
            if state.get("config") == config.fingerprint():
                self.revision = int(state["revision"])
                self.observations = state["observations"]
                self.corrections = state["corrections"]
                self.history = state["history"]
                self.groups = state["groups"]
                for record in self.observations.values():
                    Observation.model_validate(record["observation"])
                self.restart = True
        self._expire()

    def claim(self, owner):
        with self.lock:
            if self.owner is not None and self.owner is not owner:
                raise AssociationConflict("association_scope_already_running")
            self.owner = owner

    def release(self, owner):
        with self.lock:
            if self.owner is owner:
                self.owner = None
                self._save()

    def _expire(self):
        deadline = time.time() - self.config.horizon_seconds
        self.observations = {
            key: value
            for key, value in self.observations.items()
            if value["received_at"] >= deadline
        }
        while len(self.observations) > self.config.max_observations:
            del self.observations[next(iter(self.observations))]
        active_keys = {
            Observation.model_validate(r["observation"]).local_key
            for r in self.observations.values()
        }
        self._resumed_keys.intersection_update(active_keys)
        entities = {r["entity"] for r in self.observations.values()}
        self.corrections = {
            key: entity
            for key, entity in self.corrections.items()
            if key in active_keys and entity in entities
        }
        self.history = [h for h in self.history[-256:] if h["at"] >= deadline]
        self.groups = [g for g in self.groups[-128:] if g["received_at"] >= deadline]

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "config": self.config.fingerprint(),
            "revision": self.revision,
            "observations": self.observations,
            "corrections": self.corrections,
            "history": self.history,
            "groups": self.groups,
            "expires_at": max(
                (r["received_at"] for r in self.observations.values()), default=time.time()
            )
            + self.config.horizon_seconds,
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(json.dumps(data, allow_nan=False, separators=(",", ":")))
                output.flush()
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _records(self):
        return [
            (Observation.model_validate(r["observation"]), r) for r in self.observations.values()
        ]

    def _score(self, observation: Observation, entity: str, records):
        members = [(o, r) for o, r in records if r["entity"] == entity]
        if observation.policy == "no_identification" or any(
            o.policy == "no_identification" for o, _ in members
        ):
            return None
        if (
            not observation.clock
            or observation.uncertainty is None
            or observation.position is None
            or not observation.geometry
            or observation.position_error is None
        ):
            return None
        if any(
            (o.policy == "anonymous_continuity") != (observation.policy == "anonymous_continuity")
            for o, _ in members
        ):
            return None
        compatible = []
        for previous, _ in members:
            if previous.camera == observation.camera:
                # Same-camera local tracks stay distinct; a human may review fragments.
                return None
            if (
                previous.category != observation.category
                or previous.clock != observation.clock
                or previous.uncertainty is None
                or previous.position is None
                or previous.position_error is None
                or not previous.geometry
                or previous.coordinate_reference != observation.coordinate_reference
            ):
                return None
            rules = [
                rule
                for rule in self.config.transitions
                if (rule.source_camera == previous.camera
                    and rule.target_camera == observation.camera)
                or (rule.mode == "overlap"
                    and rule.target_camera == previous.camera
                    and rule.source_camera == observation.camera)
            ]
            delta = observation.observed_at - previous.observed_at
            uncertainty = observation.uncertainty + previous.uncertainty
            compatible_before = len(compatible)
            for rule in rules:
                if rule.mode == "transition" and delta - uncertainty <= 0:
                    continue
                if abs(delta) + uncertainty > rule.max_seconds:
                    continue
                # Predict only for scoring, never append inferred positions to the trajectory.
                prediction = previous.position
                earlier = [
                    o
                    for o, _ in members
                    if o.local_key == previous.local_key and o.observed_at < previous.observed_at
                ]
                if rule.mode == "transition" and earlier:
                    before = max(earlier, key=lambda o: o.observed_at)
                    duration = previous.observed_at - before.observed_at
                    if before.position and duration > 2 * uncertainty:
                        prediction = tuple(
                            p + (p - b) / duration * delta
                            for p, b in zip(previous.position, before.position)
                        )
                residual = math.dist(observation.position, prediction)
                error = observation.position_error + previous.position_error
                if residual + error <= rule.residual_metres:
                    compatible.append((previous.observed_at, residual, error, rule.mode))
            if abs(delta) <= uncertainty and len(compatible) == compatible_before:
                return None
        if not compatible:
            return None
        _, residual, error, mode = max(compatible, key=lambda item: item[0])
        return residual, error, mode

    def ingest(self, observations: list[Observation]) -> dict[str, dict]:
        if len(observations) > self.config.max_batch:
            raise AssociationConflict("association_batch_limit")
        with self.lock:
            unique = {}
            for observation in observations:
                previous = unique.get(observation.id)
                if previous is not None:
                    if previous.model_copy(update={"closed": observation.closed}) != observation:
                        raise AssociationConflict("observation_identifier_conflict")
                    observation = observation.model_copy(update={"closed": previous.closed or observation.closed})
                unique[observation.id] = observation
            observations = list(unique.values())
            self._expire()
            for observation in observations:
                existing = self.observations.get(observation.id)
                if existing is not None:
                    previous = Observation.model_validate(existing["observation"])
                    if previous.model_copy(update={"closed": observation.closed}) != observation:
                        raise AssociationConflict("observation_identifier_conflict")
            result = {}
            batches: dict[tuple, list[Observation]] = {}
            for observation in observations:
                batches.setdefault(
                    (
                        observation.clock,
                        observation.observed_at,
                        observation.camera,
                        observation.source,
                        observation.epoch,
                        observation.sequence,
                    ),
                    [],
                ).append(observation)
            for _, batch in sorted(batches.items(), key=lambda item: item[0]):
                revision_before = self.revision
                records = self._records()
                pending = []
                seen = set()
                for observation in batch:
                    if observation.id in seen:
                        continue
                    seen.add(observation.id)
                    if observation.id in self.observations:
                        previous = Observation.model_validate(
                            self.observations[observation.id]["observation"]
                        )
                        if (
                            previous.model_copy(update={"closed": observation.closed})
                            != observation
                        ):
                            raise AssociationConflict("observation_identifier_conflict")
                        if observation.closed and not previous.closed:
                            record = self.observations[observation.id]
                            self._record(
                                observation,
                                record["entity"],
                                record["reason"],
                                record["components"],
                            )
                        result[observation.id] = self.observations[observation.id]
                        continue
                    source_records = [
                        o
                        for o, _ in records
                        if (o.camera, o.source) == (observation.camera, observation.source)
                    ]
                    if source_records and source_records[-1].epoch != observation.epoch:
                        if any(o.epoch == observation.epoch for o in source_records):
                            continue
                        latest = source_records[-1]
                        if (
                            latest.clock == observation.clock
                            and observation.observed_at < latest.observed_at
                        ):
                            continue
                    local = [(o, r) for o, r in records if o.local_key == observation.local_key]
                    if local and observation.sequence <= max(o.sequence for o, _ in local):
                        continue
                    if local and observation.observed_at < max(o.observed_at for o, _ in local):
                        continue
                    same_instance = [
                        o for o in source_records if o.instance == observation.instance
                    ]
                    if same_instance and observation.generation < max(
                        o.generation for o in same_instance
                    ):
                        continue
                    if local:
                        self._record(
                            observation,
                            local[-1][1]["entity"],
                            "human_correction"
                            if observation.local_key in self.corrections
                            else "local_track",
                            None,
                        )
                    else:
                        changed = bool(
                            source_records
                            and (
                                source_records[-1].epoch != observation.epoch
                                or source_records[-1].geometry != observation.geometry
                            )
                        )
                        pending.append((observation, changed))
                by_entity = {}
                for previous, record in records:
                    by_entity.setdefault(record["entity"], []).append((previous, record))
                entities = sorted(by_entity)
                scores = {
                    (o.id, entity): self._score(o, entity, by_entity[entity])
                    for o, changed in pending
                    if not changed
                    for entity in entities
                }
                for observation, changed in pending:
                    candidates = sorted(
                        (score[0], entity, score)
                        for (oid, entity), score in scores.items()
                        if oid == observation.id and score is not None
                    )
                    entity, reason, components = str(uuid.uuid4()), "insufficient_evidence", None
                    if changed:
                        reason = "source_or_geometry_changed"
                    elif candidates:
                        residual, proposed, score = candidates[0]
                        margin = self.config.competitor_margin_metres + score[1]
                        row_unique = len(candidates) == 1 or candidates[1][0] - residual > margin
                        competitors = [
                            s[0]
                            for (oid, eid), s in scores.items()
                            if eid == proposed and oid != observation.id and s is not None
                        ]
                        column_unique = not competitors or min(competitors) - residual > margin
                        if row_unique and column_unique:
                            entity, reason = proposed, score[2]
                        else:
                            reason = "ambiguous_competitors"
                        components = {
                            "residual_metres": residual,
                            "error_bound_metres": score[1],
                            "candidate_count": len(candidates),
                        }
                    if observation.local_key in self.corrections:
                        entity, reason = self.corrections[observation.local_key], "human_correction"
                    self._record(observation, entity, reason, components)
                for observation in batch:
                    if observation.id in self.observations:
                        result[observation.id] = self.observations[observation.id]
                if self.revision != revision_before:
                    self._update_groups(batch)
            self._expire()
            self._save()
            return result

    def _record(self, observation, entity, reason, components):
        self.revision += 1
        self.observations[observation.id] = {
            "observation": observation.model_dump(mode="json"),
            "entity": entity,
            "reason": reason,
            "components": components,
            "revision": self.revision,
            "group_status": "disabled" if self.config.group_distance_metres is None else "incomplete",
            "received_at": time.time(),
            "gap": reason in ("transition", "source_or_geometry_changed")
            or (self.restart and observation.local_key not in self._resumed_keys),
        }
        self._resumed_keys.add(observation.local_key)

    def group_for(self, observation: Observation, entity: str) -> dict | None:
        with self.lock:
            if self.observations.get(observation.id, {}).get("group_status") != "complete":
                return None
            matches = [group for group in self.groups if entity in group["members"]
                and group["clock"] == observation.clock and group["start"] <= observation.observed_at
                and (group["end"] is None or observation.observed_at < group["end"])]
            if len(matches) != 1:
                return None
            return {key: matches[0][key] for key in ("id", "members", "clock", "start", "end", "revision")}

    def _update_groups(self, batch):
        if not batch or self.config.group_distance_metres is None:
            return
        last = batch[-1]
        if not last.clock or last.uncertainty is None:
            return
        expected = set(last.frame_tracks or [])
        # A transport flush is not proof that every track in the capture arrived.
        if any(not item.closed for item in batch) and (
                not expected or {item.track for item in batch if not item.closed} != expected
                or any(set(item.frame_tracks or []) != expected for item in batch)
                or any(not item.closed and (item.position is None or item.uncertainty is None
                    or item.coordinate_reference != last.coordinate_reference) for item in batch)):
            return
        records = self._records()
        source_latest = {(item.camera, item.source): item for item, _ in records}
        if any(observation.clock == last.clock and observation.observed_at > last.observed_at
               for observation, _ in records):
            return
        current = {}
        latest = {}
        for observation, record in records:
            source = source_latest[observation.camera, observation.source]
            if (observation.epoch, observation.geometry) != (source.epoch, source.geometry):
                continue
            previous = latest.get(observation.local_key)
            if previous is None or observation.sequence >= previous[0].sequence:
                latest[observation.local_key] = (observation, record)
            if (
                observation.clock == last.clock
                and observation.observed_at == last.observed_at
                and observation.position is not None
                and observation.uncertainty is not None
                and not observation.closed
                and observation.coordinate_reference == last.coordinate_reference
            ):
                current[record["entity"]] = observation
        required = {record["entity"] for observation, record in latest.values()
                    if not observation.closed and observation.clock == last.clock
                    and observation.coordinate_reference == last.coordinate_reference}
        # Missing deltas do not prove that an active member left its group.
        if not required <= current.keys() or len(current) > 128:
            return
        distances = {frozenset((left, right)): math.dist(current[left].position, current[right].position)
                     for left, right in combinations(current, 2)}
        clusters = {entity: frozenset((entity,)) for entity in current}

        @lru_cache(maxsize=None)
        def compatible(members):
            return all(distances[frozenset(pair)] <= self.config.group_distance_metres
                       for pair in combinations(members, 2))

        edges = sorted((pair for pair, distance in distances.items()
                        if distance <= self.config.group_distance_metres), key=distances.__getitem__)
        for _, pairs in groupby(edges, key=lambda pair: round(distances[pair], 9)):
            merges = []
            for left, right in pairs:
                members = clusters[left] | clusters[right]
                if clusters[left] == clusters[right] or not compatible(members):
                    continue
                touching = [group for group in merges if group & members]
                merges = [group for group in merges if not group & members]
                merges.append(members.union(*touching))
            for members in merges:
                # Equal-distance competing partitions cannot be decided by random entity IDs.
                if not compatible(members):
                    return
                for entity in members:
                    clusters[entity] = members
        groups = {tuple(sorted(cluster)) for cluster in clusters.values() if len(cluster) > 1}
        for observation in batch:
            if observation.id in self.observations:
                self.observations[observation.id]["group_status"] = "complete"
        for episode in self.groups:
            if (
                episode["clock"] == last.clock
                and episode["end"] is None
                and tuple(episode["members"]) not in groups
            ):
                episode["end"] = last.observed_at
                episode["revision"] = self.revision
        active = {
            tuple(g["members"])
            for g in self.groups
            if g["clock"] == last.clock and g["end"] is None
        }
        for members in groups - active:
            self.groups.append(
                {
                    "id": str(uuid.uuid4()),
                    "members": list(members),
                    "clock": last.clock,
                    "start": last.observed_at,
                    "end": None,
                    "revision": self.revision,
                    "received_at": time.time(),
                }
            )

    def correct(self, observation_id: str, entity: str | None, expected_revision: int, actor: str):
        with self.lock:
            self._expire()
            if expected_revision != self.revision:
                raise AssociationConflict("association_revision_changed")
            if observation_id not in self.observations:
                raise AssociationConflict("observation_expired")
            observation = Observation.model_validate(
                self.observations[observation_id]["observation"]
            )
            records = self._records()
            target = [(o, r) for o, r in records if r["entity"] == entity]
            if entity is not None and not target:
                raise AssociationConflict("entity_expired")
            trajectory = [o for o, _ in records if o.local_key == observation.local_key]
            for other, _ in target:
                if other.category != observation.category:
                    raise AssociationConflict("category_conflict")
                if any(
                    (other.policy == policy) != (observation.policy == policy)
                    for policy in ("no_identification", "anonymous_continuity")
                ):
                    raise AssociationConflict("identity_policy_conflict")
                if any(
                    other.camera == moved.camera
                    and other.source == moved.source
                    and other.epoch == moved.epoch
                    and other.track != moved.track
                    and other.sequence == moved.sequence
                    for moved in trajectory
                ):
                    raise AssociationConflict("simultaneous_local_tracks")
            entity = entity or str(uuid.uuid4())
            self.revision += 1
            previous_entities = set()
            for other, record in records:
                if other.local_key == observation.local_key:
                    previous_entities.add(record["entity"])
                    record.update(entity=entity, reason="human_correction", revision=self.revision)
            self.corrections[observation.local_key] = entity
            self.history.append(
                {
                    "at": time.time(),
                    "actor": actor,
                    "revision": self.revision,
                    "entity": entity,
                    "local_key": observation.local_key,
                    "previous_entities": sorted(previous_entities),
                    "observation_count": len(trajectory),
                }
            )
            # Retain earlier decisions; recompute current membership at the latest observed time.
            clocks = {o.clock for o in trajectory if o.clock}
            for clock in clocks:
                latest = max(
                    (o for o, _ in records if o.clock == clock), key=lambda o: o.observed_at
                )
                for episode in self.groups:
                    if episode["clock"] == clock and episode["end"] is None:
                        episode["end"] = max(episode["start"], latest.observed_at)
                        episode["revision"] = self.revision
                self._update_groups([o for o, record in records if record.get("group_status") == "complete" and
                    (o.camera, o.source, o.epoch, o.sequence) ==
                    (latest.camera, latest.source, latest.epoch, latest.sequence)])
            self._expire()
            self._save()
            return self.revision

    def snapshot(self):
        with self.lock:
            self._expire()
            self._save()
            return json.loads(
                json.dumps(
                    {
                        "revision": self.revision,
                        "scope": self.config.scope,
                        "observations": self.observations,
                        "groups": self.groups,
                        "history": self.history,
                        "restart_gap": self.restart,
                    }
                )
            )
