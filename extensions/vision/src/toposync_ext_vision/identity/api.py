from __future__ import annotations

import asyncio
import json

from typing import Annotated, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from toposync.runtime.auth import AuthContext, AuthRuntime

from .store import IdentityCapacityError, IdentityConflict, IdentityStore


Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
DisplayName = Annotated[str, Field(strict=True, min_length=1, max_length=120)]
Selection = Annotated[list[Identifier], Field(min_length=1, max_length=500)]


class _Values(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateValues(_Values):
    name: DisplayName
    species: Literal["person", "cat", "dog"]


class IdentifyValues(_Values):
    observation_ids: Selection
    identity_id: Identifier | None = None
    name: DisplayName | None = None
    species: Literal["person", "cat", "dog"] | None = None
    use_as_reference: StrictBool = False

    @model_validator(mode="after")
    def identity_or_name(self):
        if self.identity_id is None and (self.name is None or self.species is None):
            raise ValueError("new identity requires name and species")
        if self.identity_id is not None and self.name is not None:
            raise ValueError("select an existing identity or provide a new name")
        return self


class AssignValues(_Values):
    observation_ids: Selection
    identity_id: Identifier
    use_as_reference: StrictBool = False


class RenameValues(_Values):
    identity_id: Identifier
    name: DisplayName


class SelectionValues(_Values):
    observation_ids: Selection


class ReferenceValues(SelectionValues):
    enabled: StrictBool


class RejectValues(SelectionValues):
    identity_id: Identifier


class MergeValues(_Values):
    source_id: Identifier
    target_id: Identifier


class CurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "create", "identify", "rename", "assign", "unassign", "reference", "reject", "merge"
    ]
    values: dict = Field(default_factory=dict)
    request_key: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def action_values(self):
        schema = {
            "create": CreateValues,
            "identify": IdentifyValues,
            "rename": RenameValues,
            "assign": AssignValues,
            "unassign": SelectionValues,
            "reference": ReferenceValues,
            "reject": RejectValues,
            "merge": MergeValues,
        }[self.action]
        self.values = schema.model_validate(self.values).model_dump(
            exclude_none=True, exclude_unset=True
        )
        return self


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


class RetentionRequest(RevisionRequest):
    enabled: StrictBool


def _authorize(request: Request, *, write: bool = False, cameras: set[str] | None = None) -> str:
    auth = getattr(request.app.state, "auth", None)
    context = getattr(request.state, "auth_context", None)
    if not isinstance(auth, AuthRuntime) or not isinstance(context, AuthContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    principal = auth.authorize(
        context=context,
        action="vision:identities:write" if write else "vision:identities:read",
        resource_type="core:extension",
        resource_selector="com.toposync.vision",
    )
    for camera_id in cameras or set():
        if camera_id == "*":
            # Unknown provenance is owner-only. A literal wildcard authorization
            # does not prove access to cameras excluded by a member grant.
            if not principal.bypass and principal.role != "owner":
                raise HTTPException(
                    status_code=403, detail="Owner access required for unknown source scope"
                )
            continue
        auth.authorize(
            context=context,
            action="core:camera:read",
            resource_type="core:camera",
            resource_selector=camera_id,
        )
    return principal.user_id


async def identity_history_selection(*, request: Request, get_store: Callable[[], IdentityStore],
                                     identity_id: str, association: str = "human", data_filter: dict | None = None) -> dict:
    """Private selection shared by retrieval and answering; every call reauthorizes."""
    from .history import matching_notification_ids
    from toposync.runtime.notifications.access import NotificationAccess
    from toposync.runtime.extension_management import disabled_extension_ids_from_settings

    request.state.auth_context = request.app.state.auth.resolve_request(request, refresh=False)
    _authorize(request)
    if "com.toposync.vision" in disabled_extension_ids_from_settings(await request.app.state.config_store.get_settings()):
        raise HTTPException(503, "Identity history unavailable")
    request.app.state.auth.authorize(context=request.state.auth_context,
        action="core:notifications:read", resource_type=None, resource_selector="*")
    def select():
        try:
            gallery = get_store()
        except Exception as exc:
            raise HTTPException(503, "Identity gallery unavailable") from exc
        try:
            return gallery.history_scope(identity_id, association=association,
                authorize_cameras=lambda cameras: _authorize(request, cameras=cameras))
        except IdentityConflict as exc:
            raise HTTPException(409, "Identity changed; refresh the selection") from exc
    scope = await asyncio.to_thread(select)
    def owners():
        gallery = get_store()
        with gallery._lock:
            return [row[0] for row in gallery._connection.execute(
                "SELECT DISTINCT r.owner_id FROM activity_reference r JOIN observation o ON o.id=r.observation_id "
                "WHERE r.owner_id<>'independent' AND o.identity_id=? ORDER BY r.owner_id", (identity_id,))]
    scope["activities"] = []
    access = NotificationAccess(request)
    for identifier in await asyncio.to_thread(owners):
        document = await asyncio.to_thread(request.app.state.notifications.occurrences.get, identifier, read_filter=access)
        if document is None or document["status"] in {"deleted", "expired"}:
            continue
        projection = await activity_projection(request=request, document=document, get_store=get_store)
        participants = [key for key, value in projection["participants"].items()
            if value.get("identity_id") == identity_id and (association == "recognized" or value.get("provenance") == "human")]
        camera = (data_filter or {}).get("camera_id")
        if camera:
            participants = [key for key in participants if any(sample["participant_id"] == key and sample["camera_id"] == camera
                for sample in document["evidence"].values())]
        if participants:
            scope["activities"].append({"id": identifier, "revision": document["revision"], "participants": sorted(participants)})
    identifiers = await asyncio.to_thread(matching_notification_ids, request.app.state.notifications.store, scope)
    return {"scope": scope, "notification_ids": identifiers}


def activity_policies(config, document):
    """Resolve saved authorization independently of running producers or queued evidence."""
    from .pipelines import RecognizeIdentityConfig

    automatic = config.settings.core.get("automatic", {})
    profiles = []
    for intent in automatic.get("intents", []):
        if intent["id"] != document["objective_id"]:
            continue
        denied = automatic.get("denied_features", {})
        if (intent["capability"] == "vision.recognition" and "recognition" in intent.get("consents", [])
                and not any("recognition" in denied.get(camera, []) for camera in ["*", *document["scope"]["camera_ids"]])):
            profiles.append({"enabled": True, **intent.get("condition", {}).get("resource_parameters", {})})
    for pipeline in config.pipelines:
        if pipeline.graph.get("meta", {}).get("automatic", {}).get("intent_id"):
            continue
        nodes = {node['id']: node for node in pipeline.graph.get("nodes", [])}
        roots = {key for key, node in nodes.items() if node.get("operator") == "core.record_occurrence"
                 and node.get("config", {}).get("objective_id") == document["objective_id"]}
        pending, reached = list(roots), set()
        while pending:
            key = pending.pop()
            if key in reached:
                continue
            reached.add(key)
            node = nodes[key]
            if node.get("operator") == "core.record_occurrence" and key not in roots:
                continue
            if node.get("operator") == "vision.recognize_identity":
                profiles.append(node.get("config", {}))
            pending.extend(edge['to']['node'] for edge in pipeline.graph.get('edges', [])
                           if edge['from']['node'] == key and edge['to']['node'] in nodes)
    policies = {}
    conflicts = set()
    for values in profiles:
        profile = RecognizeIdentityConfig.model_validate(values)
        for policy in profile.policies:
            key = (policy.species, policy.embedding_space)
            if not profile.enabled or not policy.automatic_enabled or (key in policies and policies[key] != policy):
                conflicts.add(key)
            policies[key] = policy
    return {key: value for key, value in policies.items() if key not in conflicts}


async def activity_projection(*, request: Request, document: dict, get_store: Callable) -> dict:
    from toposync.runtime.extension_management import disabled_extension_ids_from_settings

    async def disabled():
        return "com.toposync.vision" in disabled_extension_ids_from_settings(await request.app.state.config_store.get_settings())

    def unavailable(status):
        return {"participants": {key: {"status": status} for key in document["participants"]},
                "private_evidence": {"state": status, "artifacts": []}}

    def authorize():
        request.state.auth_context = request.app.state.auth.resolve_request(request, refresh=False)
        request.app.state.auth.authorize(context=request.state.auth_context, action="core:extension:use",
            resource_type="core:extension", resource_selector="com.toposync.vision")
        _authorize(request, cameras=set(document["scope"]["camera_ids"]))

    if await disabled():
        return unavailable("unavailable")
    config = (await request.app.state.config_store.get_config()).model_copy(deep=True)
    policies = activity_policies(config, document)

    def project():
        try:
            authorize()
            gallery = get_store(create=False)
            if gallery is None:
                return {"participants": {key: {"status": "unknown"} for key in document["participants"]},
                        "private_evidence": {"state": "not_retained", "artifacts": []}}
            def allowed(cameras):
                try:
                    _authorize(request, cameras=cameras)
                    return True
                except HTTPException as exc:
                    if exc.status_code not in (401, 403):
                        raise
                    return False
            with gallery._lock:
                return {"participants": gallery.activity_participants(document, authorize_cameras=allowed, policies=policies),
                        "private_evidence": gallery.activity_artifacts(document)}
        except HTTPException as exc:
            if exc.status_code not in (401, 403):
                raise
            return unavailable("restricted")
    result = await asyncio.to_thread(project)
    if await disabled() or await request.app.state.config_store.get_config() != config:
        return unavailable("unavailable")
    try:
        authorize()
    except HTTPException as exc:
        if exc.status_code not in (401, 403):
            raise
        return unavailable("restricted")
    from urllib.parse import quote
    from toposync.app import _public_base_path_for_request
    prefix = _public_base_path_for_request(request)
    for item in result["private_evidence"]["artifacts"]:
        sample = document["evidence"][item["evidence_id"]]
        item.update(kind="private_identity_evidence", uses=[document["id"]],
            name=f"{sample.get('camera_name') or sample['camera_id']} · {sample.get('source_name') or sample['source_id']} · {sample['observed_at']}",
            image_href=f"{prefix}/api/vision/identities/observations/{quote(item['id'], safe='')}/image?activity_id={quote(document['id'], safe='')}" if item['has_image'] else None)
    return result


def create_identity_router(get_store: Callable[[], IdentityStore]) -> APIRouter:
    router = APIRouter(prefix="/api/vision/identities", tags=["identity"])

    @router.get("/recognition-options")
    def recognition_options(request: Request):
        _authorize(request, write=True)
        from ..registry.manifests import build_default_model_registry
        from .extraction import IdentityExtractor
        from .pipelines import IdentityEvidenceConfig

        registry = build_default_model_registry()
        defaults = IdentityEvidenceConfig()
        choices = []
        for species in ("person", "cat", "dog"):
            model_id = defaults.person_model_id if species == "person" else defaults.pet_model_id
            manifest = registry.get_manifest(model_id)
            detector = registry.get_manifest(defaults.face_detector_model_id) if species == "person" else None
            if manifest is None or (species == "person" and detector is None):
                continue
            # Construction fingerprints metadata only; no weights, inference or gallery access.
            extractor = IdentityExtractor(manifest, face_detector=detector)
            choices.append({"species": species, "embedding_space": extractor.embedding_space,
                            "model_name": manifest.display_name or model_id})
        return {"choices": choices}

    def store() -> IdentityStore:
        try:
            return get_store()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Identity gallery unavailable") from exc

    def can_curate(request: Request, cameras: set[str] | None = None) -> bool:
        try:
            _authorize(request, write=True, cameras=cameras)
            return True
        except HTTPException:
            return False

    def project_observation(request: Request, gallery: IdentityStore, item: dict) -> dict:
        item = {
            **item,
            "editable": can_curate(
                request, gallery.curation_camera_ids({"observation_ids": [item["id"]]})
            ),
        }
        if item["identity_id"]:
            try:
                _authorize(request, cameras=gallery.camera_ids(identity_id=item["identity_id"]))
            except HTTPException:
                return {
                    **item,
                    "identity_id": None,
                    "reference": False,
                    "confirmed": False,
                    "cluster_id": None,
                }
        return item

    @router.get("")
    def identities(
        request: Request, species: Literal["person", "cat", "dog"] | None = None, search: str = ""
    ):
        _authorize(request)
        gallery = store()
        result = []
        for item in gallery.list_identities(species=species, search=search[:120]):
            try:
                _authorize(request, cameras=gallery.camera_ids(identity_id=item["id"]))
            except HTTPException:
                continue
            result.append(item)
        return {
            "identities": result,
            "revision": gallery.revision,
            "permissions": {
                "curate": can_curate(request),
                "history": can_curate(request, gallery.camera_ids()),
            },
        }

    @router.get("/history-scope")
    def history_scope(request: Request, response: Response, identity_id: Identifier,
                      association: Literal["human", "recognized"] = "human"):
        _authorize(request)
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return store().history_scope(identity_id, association=association,
                authorize_cameras=lambda cameras: _authorize(request, cameras=cameras))
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail="Identity changed; refresh the selection") from exc

    @router.get("/{identity_id}/notifications")
    async def identity_notifications(
        request: Request, response: Response, identity_id: Identifier,
        association: Literal["human", "recognized"] = "human",
        query: str = Query("", max_length=1000), mode: Literal["literal", "text", "visual"] = "literal",
        limit: int = Query(40, ge=1, le=50), before: int | None = Query(None, ge=0),
        priority: list[str] | None = Query(None), notification_type: list[str] | None = Query(None, alias="type"),
        data_filter: str | None = Query(None, max_length=8192),
        created_after: float | None = Query(None, ge=0, allow_inf_nan=False),
        created_before: float | None = Query(None, ge=0, allow_inf_nan=False),
    ):
        from toposync.runtime.notifications.store import parse_data_filter
        from toposync.runtime.notifications.access import NotificationAccess
        from toposync.runtime.notifications.time_query import resolve_history_time
        response.headers["Cache-Control"] = "private, no-store"
        runtime = request.app.state.notifications
        try:
            filters = parse_data_filter(data_filter)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        selected = await identity_history_selection(request=request, get_store=get_store,
            identity_id=identity_id, association=association, data_filter=filters)
        scope, identifiers = selected["scope"], selected["notification_ids"]
        if created_after is not None and created_before is not None and created_after >= created_before:
            raise HTTPException(422, "Notification time interval must have positive duration")
        interpretation = None
        if created_after is not None or created_before is not None:
            query, _ = resolve_history_time(query, None, explicit_range=True)
        if created_after is None and created_before is None:
            settings = await request.app.state.config_store.get_settings()
            query, interpretation = resolve_history_time(query, settings.core.get("timezone"))
            if interpretation and interpretation.get("time_range"):
                created_after = interpretation["time_range"]["created_after"]
                created_before = interpretation["time_range"]["created_before"]
        parameters = dict(limit=limit, priorities=priority, types=notification_type, data_filter=filters,
            notification_ids=identifiers, created_after=created_after, created_before=created_before,
            read_filter=NotificationAccess(request))
        result = None
        if interpretation and interpretation.get("reason"):
            result = {"notifications": [], "next_cursor": None,
                      "search": {"mode": "literal", "status": "degraded", **interpretation}}
        elif not identifiers:
            result = {"notifications": [], "next_cursor": None, "search": {"mode": "literal", "status": "ready", "reason": "no_associated_notifications"}}
        elif mode != "literal" and query.strip():
            service = "ai.search_visual_notifications" if mode == "visual" else "ai.search_notifications"
            try:
                result = await request.app.state.services.call(service, query=query, **parameters)
                result["next_cursor"] = None
            except KeyError:
                pass  # Optional AI extension is absent; preserve the authorized literal selection.
        if result is None:
            items, cursor = await runtime.list(query=query, before=before, **parameters)
            result = {"notifications": items, "next_cursor": cursor, "search": {
                "mode": "literal", "status": "degraded" if mode != "literal" and query.strip() else "ready",
                **(interpretation or {})}}
        elif interpretation:
            result["search"].update(interpretation)
        selected = await identity_history_selection(request=request, get_store=get_store, identity_id=identity_id, association=association, data_filter=filters)
        current, current_ids = selected["scope"], selected["notification_ids"]
        if current != scope or not {item["id"] for item in result["notifications"]}.issubset(current_ids):
            raise HTTPException(409, "Identity history changed; refresh the selection")
        request.app.state.auth.authorize(context=request.state.auth_context,
            action="core:notifications:read", resource_type=None, resource_selector="*")
        result["identity_selection"] = {"association": association, "revision": current["revision"]}
        return result

    @router.get("/observations")
    def observations(
        request: Request,
        identity_id: str | None = None,
        species: Literal["person", "cat", "dog"] | None = None,
        unassigned: bool = False,
        limit: int = Query(100, ge=1, le=100),
        cursor: str | None = Query(None, max_length=4096),
    ):
        actor = _authorize(request)
        gallery = store()
        if identity_id:
            _authorize(request, cameras=gallery.camera_ids(identity_id=identity_id))

        def can_read_camera(camera: str) -> bool:
            try:
                _authorize(request, cameras={camera})
                return True
            except HTTPException:
                return False

        try:
            page = gallery.observation_page(
                identity_id=identity_id,
                species=species,
                unassigned=unassigned,
                limit=limit,
                cursor=cursor,
                principal=actor,
                can_read_camera=can_read_camera,
            )
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid photo page") from exc
        page["observations"] = [
            project_observation(request, gallery, item) for item in page["observations"]
        ]
        return page

    @router.get("/occurrences/{occurrence_id}")
    def occurrence(request: Request, occurrence_id: str):
        _authorize(request)
        gallery = store()
        detail = gallery.occurrence_details(occurrence_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Occurrence not available")
        _authorize(request, cameras={detail["camera_id"]})
        # Candidatos são privados e exigem acesso a todas as suas fontes.
        decision = detail["decision"]
        authorized = []
        for candidate_id in decision["candidate_ids"]:
            try:
                _authorize(request, cameras=gallery.camera_ids(identity_id=candidate_id))
            except HTTPException:
                continue
            authorized.append(candidate_id)
        decision["candidate_ids"] = authorized
        if decision["identity_id"]:
            _authorize(request, cameras=gallery.camera_ids(identity_id=decision["identity_id"]))
        visible = set(authorized)
        if decision["identity_id"]:
            visible.add(decision["identity_id"])
        detail["identities"] = gallery.list_identities(identity_ids=visible)
        detail["observations"] = [
            project_observation(request, gallery, item) for item in detail["observations"]
        ]
        detail["editable"] = bool(detail["observations"]) and all(
            item["editable"] for item in detail["observations"]
        )
        return detail

    @router.get("/observations/{observation_id}/context")
    def observation_context(request: Request, observation_id: str, response: Response):
        _authorize(request)
        response.headers["Cache-Control"] = "private, no-store"
        gallery = store()
        _authorize(request, cameras=gallery.camera_ids(observation_id=observation_id))
        try:
            return {"spatial_context": gallery.observation_context(observation_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Observation not available") from exc

    @router.get("/observations/{observation_id}/image")
    async def image(request: Request, observation_id: str, activity_id: Identifier | None = None):
        from toposync.runtime.extension_management import disabled_extension_ids_from_settings
        from toposync.runtime.notifications.access import NotificationAccess

        if "com.toposync.vision" in disabled_extension_ids_from_settings(await request.app.state.config_store.get_settings()):
            raise HTTPException(404, "Image unavailable")

        def authorized_gallery():
            request.state.auth_context = request.app.state.auth.resolve_request(request, refresh=False)
            _authorize(request)
            gallery = store()
            _authorize(request, cameras=gallery.camera_ids(observation_id=observation_id))
            with gallery._lock:
                if not gallery._connection.execute('SELECT 1 FROM available_observation WHERE id=? AND crop IS NOT NULL', (observation_id,)).fetchone():
                    raise HTTPException(404, 'Image unavailable')
            if activity_id is not None:
                access = NotificationAccess(request)
                access.principal()
                document = request.app.state.notifications.occurrences.get(activity_id, read_filter=access)
                if document is None or document['status'] in {'deleted', 'expired'}:
                    raise HTTPException(404, "Image unavailable")
                if not any(item['id'] == observation_id and item['has_image'] for item in gallery.activity_artifacts(document)['artifacts']):
                    raise HTTPException(404, "Image unavailable")
            return gallery

        def read():
            gallery = authorized_gallery()
            try:
                blob = gallery.image(observation_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Image not available") from exc
            if blob is None:
                raise HTTPException(status_code=404, detail="Image not retained")
            return blob
        blob = await asyncio.to_thread(read)
        if "com.toposync.vision" in disabled_extension_ids_from_settings(await request.app.state.config_store.get_settings()):
            raise HTTPException(404, "Image unavailable")
        authorized_gallery()
        return Response(blob, media_type="image/jpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/curation")
    def curate(request: Request, body: CurationRequest):
        actor = _authorize(request, write=True)
        gallery = store()
        values = body.values
        for name in ("identity_id", "source_id", "target_id"):
            if values.get(name):
                _authorize(
                    request, write=True, cameras=gallery.camera_ids(identity_id=values[name])
                )
        selected = values.get("observation_ids", [])
        if (
            not isinstance(selected, list)
            or len(selected) > 500
            or any(not isinstance(item, str) for item in selected)
        ):
            raise HTTPException(status_code=422, detail="Invalid observation selection")
        for observation_id in selected:
            _authorize(
                request, write=True, cameras=gallery.camera_ids(observation_id=observation_id)
            )
        try:
            return gallery.curate(
                action=body.action,
                values=values,
                actor=actor,
                request_key=body.request_key,
                expected_revision=body.expected_revision,
                authorize_cameras=lambda cameras: _authorize(request, write=True, cameras=cameras),
            )
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IdentityCapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid curation operation") from exc

    @router.get("/retention-preview")
    def retention_preview(request: Request):
        _authorize(request, write=True)
        gallery = store()
        _authorize(request, write=True, cameras=gallery.camera_ids())
        return gallery.retention_preview()

    @router.put("/retention")
    def retention(request: Request, body: RetentionRequest):
        _authorize(request, write=True)
        try:
            return store().configure_retention(
                enabled=body.enabled, expected_revision=body.expected_revision,
                authorize_cameras=lambda cameras: _authorize(request, write=True, cameras=cameras),
            )
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/history")
    def history(request: Request):
        _authorize(request, write=True)
        gallery = store()
        # Histórico cobre o escopo inteiro, logo não pode ignorar fontes restritas.
        _authorize(request, write=True, cameras=gallery.camera_ids())
        return {"operations": gallery.history(), "revision": gallery.revision}

    @router.post("/history/{operation_id}/undo")
    def undo(request: Request, operation_id: str, body: RevisionRequest):
        _authorize(request, write=True)
        gallery = store()
        _authorize(request, write=True, cameras=gallery.camera_ids())
        try:
            return gallery.undo(
                operation_id,
                expected_revision=body.expected_revision,
                authorize_cameras=lambda cameras: _authorize(request, write=True, cameras=cameras),
            )
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/{identity_id}")
    def delete(request: Request, identity_id: str, body: RevisionRequest):
        _authorize(request, write=True)
        gallery = store()
        _authorize(request, write=True, cameras=gallery.camera_ids(identity_id=identity_id))
        try:
            return gallery.delete_identity(
                identity_id,
                expected_revision=body.expected_revision,
                authorize_cameras=lambda cameras: _authorize(request, write=True, cameras=cameras),
            )
        except IdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
