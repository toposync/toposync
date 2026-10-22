"""Owner attestations about local evidence, never physical certification or activation."""
from __future__ import annotations

import hashlib
import json

from toposync.runtime.pipelines.preview import reference_input_pipeline

from ..processing.runtime_backends.embedding import embedding_model_revision
from .operator import visual_state_cadence_diagnostics
from .preparation import visual_preparation_revision
from .schemas import QualificationRecord, VisualStateConfig
from .store import ReferenceConflict, ReferenceLimit


def operational_suspension_reasons(pipeline, sensor, node_id, registry, publication):
    """Inspect existing diagnostics and a live lease; never infer qualification from their absence."""
    prefix = reference_input_pipeline(pipeline, node_id)
    upstream = [{"node_id": node["id"], "operator_id": node["operator"],
                 "normalized_config": registry.normalize_config(node["operator"], node.get("config"))}
                for node in prefix.graph["nodes"]]
    reasons = []
    if visual_state_cadence_diagnostics(sensor, {"upstream_nodes": upstream}):
        reasons.append("cadence_incompatible")
    if publication is None or publication.closed:
        return [*reasons, "execution_unavailable"]
    state = publication.read(pipeline, node_id, sensor)
    if state["status"] == "unavailable":
        reasons.append("execution_unavailable")
    elif state["status"] != "confirmed":
        reasons.append("observation_unconfirmed")
    elif state["valid_for_ms"] <= 0 or sensor.temporal.validity_ms - state["valid_for_ms"] > sensor.temporal.maximum_gap_ms:
        # The monotonic lease avoids trusting wall-clock timestamps or refreshing a silent source.
        reasons.append("observation_too_old")
    return reasons


def context_revision(configuration, pipeline, sensor, node_id, registry, references, stamp):
    sensor.validate_activation()
    if pipeline.processing_server_id != "local":
        raise ReferenceConflict("qualification requires local execution")
    prefix = reference_input_pipeline(pipeline, node_id)
    if visual_preparation_revision(sensor, prefix) != sensor.preparation_revision:
        raise ReferenceConflict("qualification preparation changed")
    with references._lock:
        identifiers = references._check_configuration(sensor)
        image_stamps = [references._image_path(references._reference(identifier).sha256).stat() for identifier in identifiers]
    manifest = None
    if sensor.strategy != "direct":
        manifest = next((item for item in registry.list_manifests(task="embedding") if item.model_id == sensor.model_id), None)
        if manifest is None or embedding_model_revision(manifest) != sensor.model_revision:
            raise ReferenceConflict("qualification model changed")
        path = manifest.resolve_artifact_path()
        if not 0 < path.stat().st_size <= 512 * 1024 * 1024:
            raise ReferenceConflict("qualification model unavailable")
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > 512 * 1024 * 1024:
                    raise ReferenceLimit("qualification model byte limit")
                digest.update(chunk)
        if digest.hexdigest() != manifest.sha256:
            raise ReferenceConflict("qualification model integrity failed")
    # shortcut: any installation save invalidates this evidence; narrow only with a durable dependency revision journal.
    resource_stamps = image_stamps + ([path.stat()] if manifest is not None else [])
    value = {"contract": 1, "installation_revision": stamp,
        "resources": [(item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns) for item in resource_stamps],
        "settings": configuration.settings.model_dump(mode="json"),
        "compositions": [item.model_dump(mode="json") for item in configuration.compositions],
        "pipeline": pipeline.model_dump(mode="json"), "sensor": sensor.model_dump(mode="json"),
        "model": manifest.model_dump(mode="json") if manifest is not None else None}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sensor_configuration(configuration, sensor_id):
    found = [(pipeline, node) for pipeline in configuration.pipelines for node in pipeline.graph.get("nodes", [])
             if node.get("operator") == "vision.visual_state" and node.get("config", {}).get("sensor_id") == sensor_id]
    if len(found) != 1:
        raise ReferenceConflict("qualification requires one saved sensor")
    pipeline, node = found[0]
    return pipeline, VisualStateConfig.model_validate(node["config"]), node["id"]


def record_qualification(store, sensor_id, submission, actor_id, current_revision):
    with store._transaction():
        existing = store._connection.execute("SELECT metadata FROM qualification WHERE operation_id=?",
                                             (submission.operation_id,)).fetchone()
        if existing is not None:
            record = QualificationRecord.model_validate_json(existing[0])
            if (record.sensor_id != sensor_id or record.actor_id != actor_id
                    or record.model_dump(exclude={"sensor_id", "actor_id", "created_at"}) != submission.model_dump()):
                raise ReferenceConflict("qualification operation reused with different content")
            return record
        if current_revision is None or submission.context_revision != current_revision:
            raise ReferenceConflict("qualification context changed; review the current revision")
        if store._connection.execute("SELECT count(*) FROM qualification").fetchone()[0] >= store.limits.qualifications:
            raise ReferenceLimit("qualification history limit reached; preserve and review the existing records")
        record = QualificationRecord(**submission.model_dump(), sensor_id=sensor_id, actor_id=actor_id, created_at=store._now())
        store._connection.execute("INSERT INTO qualification(operation_id,sensor_id,scope,metadata) VALUES (?,?,?,?)",
            (submission.operation_id, sensor_id, record.scope, record.model_dump_json()))
        return record


def qualification_history(store, sensor_id, revision, *, offset=0, limit=20):
    with store._lock:
        latest = dict(store._connection.execute("SELECT scope,max(sequence) FROM qualification WHERE sensor_id=? GROUP BY scope",
                                               (sensor_id,)).fetchall())
        rows = store._connection.execute("SELECT sequence,metadata FROM qualification WHERE sensor_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                                         (sensor_id, limit + 1, offset)).fetchall()
    items = []
    for row in rows[:limit]:
        record = QualificationRecord.model_validate_json(row["metadata"])
        status = "suspended"
        if latest[record.scope] != row["sequence"]:
            status = "superseded"
        elif revision is not None and record.context_revision == revision:
            status = "recorded_pass" if record.decision == "passed" else "recorded_failure"
        items.append({"record": record, "status": status})
    return {"context_revision": revision, "items": items, "next_offset": offset + limit if len(rows) > limit else None,
            "physical_qualification": False, "activation_authorized": False}
