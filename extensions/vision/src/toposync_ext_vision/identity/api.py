from __future__ import annotations

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


def create_identity_router(get_store: Callable[[], IdentityStore]) -> APIRouter:
    router = APIRouter(prefix="/api/vision/identities", tags=["identity"])

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
    def image(request: Request, observation_id: str):
        _authorize(request)
        gallery = store()
        _authorize(request, cameras=gallery.camera_ids(observation_id=observation_id))
        try:
            blob = gallery.image(observation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Image not available") from exc
        if blob is None:
            raise HTTPException(status_code=404, detail="Image not retained")
        return Response(
            blob,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

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
