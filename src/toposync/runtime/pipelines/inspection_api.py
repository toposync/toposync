"""Owner-only inspection reads; every delivery rechecks access and graph revision."""

from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response

from .inspection import InspectionSnapshot, PipelineInspectionStore, pipeline_inspection_revision
from .operators_sinks import _encode_image_bytes


def pipeline_image_encoders(app: FastAPI) -> asyncio.Semaphore:
    if not hasattr(app.state, "pipeline_image_encoders"):
        app.state.pipeline_image_encoders = asyncio.Semaphore(2)
    return app.state.pipeline_image_encoders


async def checked_inspection(request: Request, token: str | None = None, pipeline_name: str | None = None):
    store: PipelineInspectionStore = request.app.state.pipeline_inspection
    try:
        authorize = request.app.state.authorize_pipeline_inspection
        owner = authorize(request)
        session = store.sessions.get(token) if token else None
        if token and (session is None or session.owner != owner):
            raise HTTPException(404, "inspection_session_expired")
        name = session.pipeline if session else pipeline_name
        pipeline = await request.app.state.config_store.get_pipeline(name)
        # Configuration reads can suspend across a permission or session change.
        if authorize(request) != owner:
            raise HTTPException(403, "inspection_owner_changed")
        if token and store.sessions.get(token) is not session:
            raise HTTPException(410, "inspection_session_expired")
        if pipeline is None:
            raise HTTPException(404, "Unknown pipeline")
        demands = getattr(request.app.state, "pipeline_demands", None)
        if demands is not None:
            for output in pipeline.graph.get("meta", {}).get("outputs", []):
                key = output.get("key", "")
                if demands.is_blocked(owner, key, "inspection") or not demands.authorized(owner, key, "inspection"):
                    raise HTTPException(403, "inspection_result_access_revoked")
        revision = pipeline_inspection_revision(pipeline)
        if session and session.revision != revision:
            raise HTTPException(409, "inspection_revision_changed")
        return store, pipeline, revision, owner
    except HTTPException:
        if token:
            store.close(token)
        raise


def install_inspection_routes(app: FastAPI, authorize: Callable[[Request], str]) -> None:
    image_encoders = pipeline_image_encoders(app)
    app.state.authorize_pipeline_inspection = authorize

    @app.post("/api/pipelines/{pipeline_name}/inspection", response_model=InspectionSnapshot)
    async def open_inspection(request: Request, pipeline_name: str, response: Response):
        store, pipeline, revision, owner = await checked_inspection(request, pipeline_name=pipeline_name)
        if pipeline.processing_server_id not in ("", "local", None):
            raise HTTPException(409, "inspection_remote_runtime_unavailable")
        nodes = {str(node["id"]): str(node["operator"]) for node in pipeline.graph.get("nodes", [])}
        registry = getattr(request.app.state, "pipeline_demands", None)
        demand_ids = []
        try:
            if registry is not None:
                from .demands import DemandRequest
                registry.bind(await request.app.state.config_store.list_pipelines())
                for output in pipeline.graph.get("meta", {}).get("outputs", []):
                    _, definition, production_revision = registry.outputs[output["key"]]
                    demand = registry.open(owner, DemandRequest(consumer_id=f"inspection:{pipeline.name}", purpose="inspection",
                        result_key=definition.key, contract=definition.contract, production_revision=production_revision,
                        lease_seconds=store.lease_seconds))
                    demand_ids.append(demand.id)
            token = store.open(pipeline.name, revision, nodes, owner=owner, demand_ids=tuple(demand_ids))
        except (ValueError, PermissionError, KeyError) as error:
            if registry is not None:
                for identifier in demand_ids:
                    registry.release(identifier)
            if isinstance(error, PermissionError):
                raise HTTPException(403, "inspection_result_access_revoked") from error
            if isinstance(error, KeyError):
                raise HTTPException(409, "inspection_revision_changed") from error
            raise HTTPException(429, "inspection_capacity") from error
        response.headers["Cache-Control"] = "no-store"
        return store.snapshot(token)

    @app.get("/api/pipeline-inspection/{token}", response_model=InspectionSnapshot)
    async def inspection_snapshot(request: Request, token: str, response: Response):
        store, _, _, _ = await checked_inspection(request, token)
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.snapshot(token)
        except KeyError as error:
            raise HTTPException(410, "inspection_session_expired") from error

    @app.get("/api/pipeline-inspection/{token}/images/{node}/{sequence}")
    async def inspection_image(request: Request, token: str, node: str, sequence: int):
        store, _, _, _ = await checked_inspection(request, token)
        try:
            frame = store.image(token, node, sequence)
        except KeyError as error:
            raise HTTPException(410, "inspection_image_expired") from error
        # Encoding is outside the producer callback and off the event loop.
        if image_encoders.locked():
            raise HTTPException(429, "inspection_encoder_busy")
        async with image_encoders:
            encoding = asyncio.create_task(
                asyncio.to_thread(_encode_image_bytes, frame, fmt="jpg", jpeg_quality=80)
            )
            try:
                blob, _, mime = await asyncio.shield(encoding)
            except asyncio.CancelledError:
                # A canceled client must not release a slot while its native encoder still runs.
                await encoding
                raise
        await checked_inspection(request, token)
        try:
            store.image(token, node, sequence)
        except KeyError as error:
            raise HTTPException(410, "inspection_image_expired") from error
        return Response(
            blob,
            media_type=mime,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.delete("/api/pipeline-inspection/{token}", status_code=204)
    async def close_inspection(request: Request, token: str):
        store, _, _, _ = await checked_inspection(request, token)
        store.close(token)
        return Response(status_code=204)
