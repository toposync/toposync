"""Private inspection is resolved at delivery, never copied into shared packet payloads."""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import HTTPException

from toposync.runtime.pipelines.packet_contract import resolve_media_ts
from .api import _authorize


SCOPE = "vision.identity"


def evidence_visuals(evidence, *, occurrence_id):
    properties = {"embedding_space": evidence.embedding_space,
                  "dimensions": len(evidence.vector) if evidence.vector is not None else 0}
    result = [{"layer": "embedding", "kind": "points", "points": [], "access_scope": SCOPE,
        "subject_id": occurrence_id, "state": evidence.status, "score": evidence.quality,
        "score_kind": "image_quality", "properties": properties}]
    if evidence.species == "person" and evidence.status == "ready":
        x1, y1, x2, y2 = evidence.region
        result.append({"layer": "faces", "kind": "polygon", "points": [[x1,y1],[x2,y1],[x2,y2],[x1,y2]],
            "access_scope": SCOPE, "subject_id": occurrence_id, "state": "observed",
            "score": evidence.quality, "score_kind": "image_quality"})
    return result


def recognition_visual(packet, decision):
    return {"layer": "identity", "kind": "points", "points": [], "access_scope": SCOPE,
        "subject_id": decision.occurrence_id, "state": decision.status,
        "properties": {"decision_revision": decision.revision, "observed_at": resolve_media_ts(packet)}}


async def project_identity_inspection(*, request, nodes, get_store):
    from toposync.runtime.extension_management import disabled_extension_ids_from_settings

    async def enabled():
        settings = await request.app.state.config_store.get_settings()
        return "com.toposync.vision" not in disabled_extension_ids_from_settings(settings)

    def authorize():
        request.state.auth_context = request.app.state.auth.resolve_request(request, refresh=False)
        request.app.state.auth.authorize(context=request.state.auth_context, action="core:extension:use",
            resource_type="core:extension", resource_selector="com.toposync.vision")
        _authorize(request, cameras={node.frame.get("device_id") or "*" for node in nodes if node.frame and node.visuals})

    configuration = await request.app.state.config_store.get_config()
    if not await enabled():
        return "disabled", {}, lambda: True
    try:
        authorize()
    except HTTPException:
        return "restricted", {}, lambda: True

    def project():
        gallery = get_store(create=False)
        revision = gallery.revision if gallery is not None else 0
        output = {}
        names = {}
        for node in nodes:
            values = []
            for visual in node.visuals:
                if visual.access_scope != SCOPE:
                    continue
                if visual.layer in {"faces", "embedding"}:
                    values.append(visual)
                elif visual.layer == "identity" and gallery is not None:
                    properties = visual.properties
                    if type(properties.get("decision_revision")) is not int or type(properties.get("observed_at")) not in (int, float):
                        continue
                    projected = gallery.inspection_identity(visual.subject_id,
                        decision_revision=properties["decision_revision"], observed_at=properties["observed_at"],
                        camera_id=node.frame.get("device_id") if node.frame else None,
                        authorize_cameras=lambda cameras: _authorize(request, cameras=cameras))
                    if projected is not None:
                        if projected.get("name"):
                            names[(visual.subject_id, json.dumps(node.frame, sort_keys=True))] = projected["name"]
                        values.append(visual.model_copy(update={"label": projected.pop("name", None),
                            "state": projected.pop("status"), "properties": projected}))
            output[node.node_id] = values
        for node in nodes:
            output[node.node_id] = [visual.model_copy(update={"label": names.get(
                (visual.subject_id, json.dumps(node.frame, sort_keys=True)))}) if visual.layer == "faces" else visual
                for visual in output[node.node_id]]
        if gallery is not None and gallery.revision != revision:
            return None, revision, {}
        return gallery, revision, output

    gallery, revision, output = await asyncio.to_thread(project)
    if not await enabled():
        return "disabled", {}, lambda: True
    try:
        authorize()
    except HTTPException:
        return "restricted", {}, lambda: True
    if gallery is not None and gallery.revision != revision:
        return "changed", {}, lambda: True
    def validate():
        request.app.state.config_store._check_validated_revision(configuration)
        authorize()
        if gallery is not None and gallery.revision != revision:
            return False
        for node in nodes:
            for visual in node.visuals:
                if visual.layer == "identity" and any(value.layer == "identity" and value.subject_id == visual.subject_id
                        for value in output.get(node.node_id, [])):
                    current = gallery.inspection_identity(visual.subject_id,
                        decision_revision=visual.properties["decision_revision"],
                        observed_at=visual.properties["observed_at"], camera_id=node.frame.get("device_id"),
                        authorize_cameras=lambda cameras: _authorize(request, cameras=cameras))
                    if current is None:
                        return False
        return True
    policy = hashlib.sha256(json.dumps([SCOPE, revision]).encode()).hexdigest()
    return policy, output, validate
