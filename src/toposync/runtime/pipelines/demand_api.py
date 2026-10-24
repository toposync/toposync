"""Authorized local leases and result manifests; no public artifact URLs."""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .demands import DemandRequest, pipeline_result_keys
from .operators_sinks import _encode_image_bytes
from .inspection_api import pipeline_image_encoders


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)


def install_demand_routes(app: FastAPI, principal, manage) -> None:
    encoders = pipeline_image_encoders(app)

    def artifact_identity(demand, packet, name):
        basis = [
            demand.request.result_key,
            demand.request.production_revision,
            packet.packet_id,
            packet.metadata.get("frame"),
            name,
        ]
        return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()

    @app.get("/api/result-demands")
    async def demands_status(request: Request, response: Response):
        owner = principal(request)
        registry = request.app.state.pipeline_demands
        response.headers["Cache-Control"] = "no-store"
        return {
            "demands": registry.snapshot(owner),
            "events": [
                {key: value for key, value in event.items() if key != "owner"}
                for event in registry.events
                if event["owner"] == owner
            ],
        }

    def checked(request, identifier):
        owner = principal(request)
        registry = request.app.state.pipeline_demands
        demand = registry.demands.get(identifier)
        if demand is None or demand.owner != owner:
            raise HTTPException(404, "Demand unavailable")
        if not registry.allowed(identifier):
            raise HTTPException(403, "Result access revoked")
        return registry, demand

    @app.get("/api/pipeline-results")
    async def results(request: Request, response: Response):
        owner = principal(request)
        registry = request.app.state.pipeline_demands
        registry.bind(await request.app.state.config_store.list_pipelines())
        response.headers["Cache-Control"] = "no-store"
        return {
            "results": [
                {
                    "key": output.key,
                    "name": output.name,
                    "contract": output.contract.model_dump(),
                    "production_revision": revision,
                }
                for _, output, revision in registry.outputs.values()
                if not registry.is_blocked(owner, output.key)
                and registry.authorized(owner, output.key, "catalog")
            ]
        }

    @app.post("/api/result-demands")
    async def open_demand(request: Request, body: DemandRequest, response: Response):
        owner = principal(request)
        registry = request.app.state.pipeline_demands
        registry.bind(await request.app.state.config_store.list_pipelines())
        try:
            demand = registry.open(owner, body)
        except PermissionError as error:
            raise HTTPException(403, "Result access denied") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        try:
            await request.app.state.pipelines_orchestrator._reconcile()
        except BaseException:
            registry.release(demand.id, "activation_interrupted")
            raise
        if not registry.allowed(demand.id):
            await request.app.state.pipelines_orchestrator._reconcile()
            raise HTTPException(409, "Demand expired during activation")
        if body.purpose != "inspection":
            bundle = request.app.state.pipelines_orchestrator._local_bundle
            if bundle is None or f"demand_{demand.id}" not in {
                pipeline.name for pipeline in bundle.pipelines
            }:
                registry.release(demand.id, "activation_rejected")
                raise HTTPException(409, "Result activation unavailable")
        response.headers["Cache-Control"] = "no-store"
        return next(item for item in registry.snapshot(owner) if item["id"] == demand.id)

    @app.post("/api/result-demands/{identifier}/renew")
    async def renew_demand(request: Request, identifier: str):
        registry, _ = checked(request, identifier)
        registry.renew(identifier)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.delete("/api/result-demands/{identifier}")
    async def release_demand(request: Request, identifier: str):
        registry, _ = checked(request, identifier)
        registry.release(identifier)
        await request.app.state.pipelines_orchestrator._reconcile()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/api/result-demands/{identifier}")
    async def demand_result(request: Request, identifier: str, response: Response):
        registry, demand = checked(request, identifier)
        if demand.latest_expires_at <= registry.clock():
            demand.latest = None
        packet = demand.latest
        value = next(item for item in registry.snapshot(demand.owner) if item["id"] == identifier)
        value["result"] = (
            None
            if packet is None
            else {
                "packet_id": packet.packet_id,
                "stream_id": packet.stream_id,
                "lifecycle": packet.lifecycle.value,
                "frame": packet.metadata.get("frame"),
                "payload": packet.payload,
                "artifacts": [
                    {
                        "id": artifact_identity(demand, packet, name),
                        "name": name,
                        "uses": [
                            item.id
                            for item in list(registry.demands.values())
                            if item.owner == demand.owner
                            and item.request.result_key == demand.request.result_key
                            and item.request.production_revision
                            == demand.request.production_revision
                            and item.latest is not None
                            and name in item.latest.artifacts
                            and registry.allowed(item.id)
                            and item.latest.packet_id == packet.packet_id
                        ],
                    }
                    for name in packet.artifacts
                ],
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return value

    @app.get("/api/result-demands/{identifier}/artifacts/{artifact_id}")
    async def demand_artifact(request: Request, identifier: str, artifact_id: str):
        registry, demand = checked(request, identifier)
        packet = demand.latest
        if packet is None or demand.latest_expires_at <= registry.clock():
            raise HTTPException(410, "Result expired")
        artifact = next(
            (
                artifact
                for name, artifact in packet.artifacts.items()
                if artifact_identity(demand, packet, name) == artifact_id
            ),
            None,
        )
        if artifact is None or artifact.data is None:
            raise HTTPException(404, "Artifact unavailable")
        if encoders.locked():
            raise HTTPException(429, "Result encoder busy")
        async with encoders:
            task = asyncio.create_task(
                asyncio.to_thread(_encode_image_bytes, artifact.data, fmt="jpg", jpeg_quality=80)
            )
            try:
                blob, _, mime = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        _, current = checked(request, identifier)
        if current.latest is not packet or current.latest_expires_at <= registry.clock():
            raise HTTPException(410, "Result expired")
        return Response(
            blob,
            media_type=mime,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/pipeline-results/{key}/revoke")
    async def revoke_result(request: Request, key: str, body: RevokeRequest):
        manage(request)
        registry = request.app.state.pipeline_demands
        pipelines = {pipeline.name: pipeline for pipeline in await request.app.state.config_store.list_pipelines()}
        manage(request)
        registry.revoke(body.owner_id, key)
        inspection = request.app.state.pipeline_inspection
        for token, session in list(inspection.sessions.items()):
            pipeline = pipelines.get(session.pipeline)
            if (
                pipeline is not None
                and (body.owner_id == "*" or session.owner == body.owner_id)
                and any(registry.is_blocked(session.owner, result, "inspection")
                        for result in pipeline_result_keys(pipeline))
            ):
                inspection.close(token)
        await request.app.state.pipelines_orchestrator._reconcile()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
