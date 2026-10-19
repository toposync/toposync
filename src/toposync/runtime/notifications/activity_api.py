"""Authorized activity history over durable occurrences, independent of notification delivery."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .access import NotificationAccess
from .occurrences import OccurrenceConflict


class ActivityRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


def install_activity_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/api/activity", tags=["activity"])

    async def visible(request, identifier, *, write=False):
        access = NotificationAccess(request)
        access.principal("core:occurrences:write" if write else "core:notifications:read")
        store = request.app.state.notifications.occurrences
        await request.app.state.notifications.expire_occurrences()
        document = await asyncio.to_thread(store.get, identifier, read_filter=access)
        if document is None:
            raise HTTPException(404, "Occurrence unavailable")
        if document["status"] in {"deleted", "expired"}:
            raise HTTPException(410, document["status"])
        return store, document

    @router.get("")
    async def activity(request: Request, response: Response, limit: int = Query(50, ge=1, le=250),
                 before: str | None = Query(None, max_length=64),
                 camera_id: str | None = Query(None, min_length=1, max_length=256),
                 area_id: str | None = Query(None, min_length=1, max_length=256),
                 participant_id: str | None = Query(None, min_length=1, max_length=256),
                 created_after: float | None = Query(None, ge=0, allow_inf_nan=False),
                 created_before: float | None = Query(None, ge=0, allow_inf_nan=False)):
        access = NotificationAccess(request)
        access.principal()
        response.headers["Cache-Control"] = "private, no-store"
        try:
            await request.app.state.notifications.expire_occurrences()
            items, cursor = await asyncio.to_thread(request.app.state.notifications.occurrences.list, read_filter=access,
                limit=limit, before=before, camera_id=camera_id, area_id=area_id, participant_id=participant_id,
                created_after=created_after, created_before=created_before)
        except OccurrenceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"occurrences": items, "next_cursor": cursor, "coverage": {
            "basis": "recorded_occurrences", "time_filter": "recorded_at", "continuous_video": False,
            "expired_evidence_included": False, "complete_day": False}}

    @router.get("/{identifier}")
    async def detail(request: Request, response: Response, identifier: str):
        store, document = await visible(request, identifier)
        response.headers["Cache-Control"] = "private, no-store"
        return {"occurrence": document, "revisions": await asyncio.to_thread(store.revisions, identifier), "deliveries": await asyncio.to_thread(store.deliveries, identifier)}

    @router.get("/{identifier}/evidence")
    async def evidence(request: Request, response: Response, identifier: str, evidence_id: str = Query(min_length=1, max_length=256)):
        _, document = await visible(request, identifier)
        value = document["evidence"].get(evidence_id)
        if value is None:
            raise HTTPException(404, "Evidence unavailable")
        response.headers["Cache-Control"] = "private, no-store"
        return {"id": evidence_id, "kind": "observation_metadata", "evidence": value}

    @router.get("/{identifier}/manifest")
    async def manifest(request: Request, identifier: str):
        store, document = await visible(request, identifier)
        manifest = {"schema_version": 1, "id": identifier, "name": document["title"],
            "revision": document["revision"], "objective_id": document["objective_id"],
            "scope": document["scope"], "participants": list(document["participants"].values()),
            "coverage": document["coverage"], "evidence": [
                {"id": key, "kind": "observation_metadata", "name": f"{value['camera_id']} · {value['observed_at']}",
                 "camera_id": value["camera_id"], "source_id": value["source_id"],
                 "href": f"./evidence?evidence_id={quote(key, safe='')}",
                 "uses": [identifier], "expires_at": document["expires_at"]}
                for key, value in document["evidence"].items()],
            "revisions": await asyncio.to_thread(store.revisions, identifier), "deliveries": await asyncio.to_thread(store.deliveries, identifier)}
        return Response(json.dumps(manifest, ensure_ascii=False, allow_nan=False), media_type="application/json",
            headers={"Cache-Control": "private, no-store", "Content-Disposition": f'attachment; filename="occurrence-{identifier}.json"'})

    @router.delete("/{identifier}")
    async def erase(request: Request, response: Response, identifier: str, body: ActivityRevision):
        store, _ = await visible(request, identifier, write=True)
        try:
            await request.app.state.notifications.erase_occurrence(identifier, expected_revision=body.expected_revision)
        except OccurrenceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        response.headers["Cache-Control"] = "private, no-store"
        return {"id": identifier, "status": "deleted", "external_messages_retracted": False}

    @router.post("/{identifier}/revoke-enrichment")
    async def revoke(request: Request, response: Response, identifier: str, body: ActivityRevision):
        store, _ = await visible(request, identifier, write=True)
        try:
            document = await request.app.state.notifications.revoke_occurrence_enrichment(identifier, expected_revision=body.expected_revision)
        except OccurrenceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        response.headers["Cache-Control"] = "private, no-store"
        return {"occurrence": document, "external_messages_retracted": False}

    app.include_router(router)
