"""Owner-only inspection reads; every delivery rechecks access and graph revision."""

from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response

from .inspection import InspectionSnapshot, PipelineInspectionStore, pipeline_inspection_revision
from .operators_sinks import _encode_image_bytes


def install_inspection_routes(app: FastAPI, authorize: Callable[[Request], str]) -> None:
    image_encoders = asyncio.Semaphore(2)

    async def checked(request: Request, token: str | None = None, pipeline_name: str | None = None):
        store: PipelineInspectionStore = request.app.state.pipeline_inspection
        try:
            owner = authorize(request)
            session = store.sessions.get(token) if token else None
            if token and (session is None or session.owner != owner):
                raise HTTPException(404, "inspection_session_expired")
            name = session.pipeline if session else pipeline_name
            pipeline = await request.app.state.config_store.get_pipeline(name)
            if pipeline is None:
                raise HTTPException(404, "Unknown pipeline")
            revision = pipeline_inspection_revision(pipeline)
            if session and session.revision != revision:
                raise HTTPException(409, "inspection_revision_changed")
            return store, pipeline, revision, owner
        except HTTPException:
            if token:
                store.close(token)
            raise

    @app.post("/api/pipelines/{pipeline_name}/inspection", response_model=InspectionSnapshot)
    async def open_inspection(request: Request, pipeline_name: str, response: Response):
        store, pipeline, revision, owner = await checked(request, pipeline_name=pipeline_name)
        if pipeline.processing_server_id not in ("", "local", None):
            raise HTTPException(409, "inspection_remote_runtime_unavailable")
        nodes = {str(node["id"]): str(node["operator"]) for node in pipeline.graph.get("nodes", [])}
        try:
            token = store.open(pipeline.name, revision, nodes, owner=owner)
        except ValueError as error:
            raise HTTPException(429, "inspection_capacity") from error
        response.headers["Cache-Control"] = "no-store"
        return store.snapshot(token)

    @app.get("/api/pipeline-inspection/{token}", response_model=InspectionSnapshot)
    async def inspection_snapshot(request: Request, token: str, response: Response):
        store, _, _, _ = await checked(request, token)
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.snapshot(token)
        except KeyError as error:
            raise HTTPException(410, "inspection_session_expired") from error

    @app.get("/api/pipeline-inspection/{token}/images/{node}/{sequence}")
    async def inspection_image(request: Request, token: str, node: str, sequence: int):
        store, _, _, _ = await checked(request, token)
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
        await checked(request, token)
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
        store, _, _, _ = await checked(request, token)
        store.close(token)
        return Response(status_code=204)
