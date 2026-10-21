"""Private review of temporary entities, reauthorized against current graph and gallery."""

from __future__ import annotations

from pathlib import Path
import asyncio
import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from toposync.runtime.extension_management import disabled_extension_ids_from_settings

from .association import AssociationConfig, AssociationConflict, AssociationStore, Values
from .identity.api import _authorize


class AssociationRepository:
    def __init__(self, directory: Path):
        self.directory = directory
        self.stores = {}
        self.lock = threading.RLock()

    def get(self, *, config: AssociationConfig):
        if not config.scope or not config.sources:
            raise AssociationConflict("association_scope_and_sources_required")
        with self.lock:
            store = self.stores.get(config.scope)
            if store is None or store.config != config:
                if store is not None and store.owner is not None:
                    raise AssociationConflict("association_scope_configuration_conflict")
                if len(self.stores) >= 64 and store is None:
                    raise AssociationConflict("association_scope_capacity")
                store = AssociationStore(config, self.directory / (config.scope + ".json"))
                self.stores[config.scope] = store
            return store

    def maintain(self):
        with self.lock:
            for scope, store in list(self.stores.items()):
                store.snapshot()
                if not store.observations and store.owner is None:
                    if store.path is not None:
                        store.path.unlink(missing_ok=True)
                    del self.stores[scope]
            for path in self.directory.glob("*.json"):
                if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                    continue
                state = json.loads(path.read_text())
                if state.get("expires_at", float("inf")) < time.time():
                    path.unlink()


class Correction(Values):
    observation_id: str = Field(min_length=1, max_length=128)
    entity_id: str | None = Field(default=None, max_length=128)
    expected_revision: int = Field(ge=0)


def create_association_router(repository, identity_store):
    router = APIRouter(prefix="/api/vision/associations", tags=["association"])

    async def scope_store(request, scope, write=False):
        _authorize(request, write=write)
        configuration = await request.app.state.config_store.get_config()
        request.state.auth_context = request.app.state.auth.resolve_request(request, refresh=False)
        if "com.toposync.vision" in disabled_extension_ids_from_settings(configuration.settings):
            raise HTTPException(403, "Vision extension disabled")
        request.app.state.auth.authorize(context=request.state.auth_context, action="core:extension:use",
            resource_type="core:extension", resource_selector="com.toposync.vision")
        profiles = []
        for pipeline in configuration.pipelines:
            for node in pipeline.graph.get("nodes", []):
                if (
                    node.get("operator") == "vision.associate_entities"
                    and node.get("config", {}).get("scope") == scope
                ):
                    profiles.append(AssociationConfig.model_validate(node["config"]))
        if not profiles:
            raise HTTPException(404, "Association scope not configured")
        if any(profile != profiles[0] for profile in profiles):
            raise HTTPException(409, "Association scope has conflicting configurations")
        _authorize(
            request, write=write, cameras={source.camera_id for source in profiles[0].sources}
        )
        try:
            return repository().get(config=profiles[0])
        except AssociationConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    def project_review(store, request):
        snapshot = store.snapshot()
        entities = {}
        names = {}
        identity_cameras = {}
        try:
            gallery = identity_store(create=False)
        except Exception:
            gallery = None
        gallery_revision = gallery.revision if gallery is not None else None
        for identifier, record in snapshot["observations"].items():
            observation = record["observation"]
            entity = entities.setdefault(
                record["entity"],
                {"observations": [], "identities": {}, "recognition_status": "unknown"},
            )
            entity["observations"].append(identifier)
            if observation["policy"] != "recognize_here" or not observation["occurrence"]:
                continue
            if gallery is None:
                entity["recognition_status"] = "unavailable"
                continue
            decision = gallery.decision(observation["occurrence"])
            if decision is None:
                entity["recognition_status"] = "pending"
                continue
            decision = gallery.validated_decision(
                observation["occurrence"],
                decision_revision=decision.revision,
                observed_at=observation["observed_at"],
                max_age_seconds=store.config.horizon_seconds,
            )
            if decision is None:
                entity["recognition_status"] = "unavailable"
                continue
            entity["recognition_status"] = decision.status
            if decision.identity_id is not None:
                if decision.identity_id not in names:
                    identity_cameras[decision.identity_id] = gallery.camera_ids(identity_id=decision.identity_id)
                    _authorize(request, cameras=identity_cameras[decision.identity_id])
                    names[decision.identity_id] = next(
                        (item["name"] for item in gallery.list_identities(identity_ids={decision.identity_id})), None
                    )
                entity["identities"][decision.identity_id] = {
                    "evidence_observation": identifier,
                    "evidence_camera": observation["camera"],
                    "evidence_at": decision.evidence_at,
                    "decision_revision": decision.revision,
                    "provenance": decision.provenance,
                    "name": names[decision.identity_id],
                }
        for entity in entities.values():
            if len(entity["identities"]) > 1:
                entity["recognition_status"] = "ambiguous"
            elif entity["identities"]:
                entity["recognition_status"] = "recognized"
        # Never expose gallery occurrence handles, even in the private trajectory projection.
        for record in snapshot["observations"].values():
            record["observation"].pop("occurrence", None)
        snapshot["entities"] = entities
        return snapshot, gallery, gallery_revision, identity_cameras

    @router.get("/{scope}")
    async def review(scope: str, request: Request, response: Response):
        store = await scope_store(request, scope)
        response.headers["Cache-Control"] = "no-store"
        snapshot, gallery, gallery_revision, identity_cameras = await asyncio.to_thread(project_review, store, request)
        # The worker may outlive a permission, graph or gallery revision.
        if (await scope_store(request, scope)).config != store.config:
            raise HTTPException(409, "Association configuration changed; refresh the review")
        for cameras in identity_cameras.values():
            _authorize(request, cameras=cameras)
        if gallery is not None and gallery.revision != gallery_revision:
            raise HTTPException(409, "Identity gallery changed; refresh the review")
        try:
            _authorize(request, write=True, cameras={s.camera_id for s in store.config.sources})
            snapshot["editable"] = True
        except HTTPException:
            snapshot["editable"] = False
        return snapshot

    @router.post("/{scope}/corrections")
    async def correct(scope: str, values: Correction, request: Request, response: Response):
        store = await scope_store(request, scope, write=True)
        actor = _authorize(request, write=True)
        response.headers["Cache-Control"] = "no-store"
        try:
            revision = store.correct(
                values.observation_id, values.entity_id, values.expected_revision, actor
            )
        except AssociationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"revision": revision}

    return router
