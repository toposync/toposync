"""Owner-managed intentions; runtime mutations share the existing reconciliation lock."""

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import Field

from .automatic import AutomaticState, AutomaticValue
from .config_store import PipelineAlreadyExistsError


class AutomaticUpdate(AutomaticValue):
    expected_revision: int = Field(ge=0)
    configuration: AutomaticState


class AutomaticRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validate(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                # Rejected input can contain nonfinite floats or private consequence parameters.
                details = [
                    {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
                ]
                raise HTTPException(422, details) from error

        return validate


def install_automatic_routes(app: FastAPI, owner) -> None:
    router = APIRouter(route_class=AutomaticRoute)

    @router.get("/api/automatic")
    async def status(request: Request, response: Response):
        owner(request)
        response.headers["Cache-Control"] = "no-store"
        controller = request.app.state.automatic
        orchestrator = request.app.state.pipelines_orchestrator
        async with orchestrator._reconcile_lock:
            config = await controller.store.get_config()
            controller.prepare_config(config)
            snapshot = controller.snapshot(
                orchestrator._activation_errors, orchestrator._last_error
            )
            snapshot["catalog"] = await controller.catalog(config)
            snapshot["activation_errors"] = dict(orchestrator._activation_errors)
            return snapshot

    @router.post("/api/automatic/preview")
    async def preview(request: Request, body: AutomaticUpdate, response: Response):
        owner(request)
        response.headers["Cache-Control"] = "no-store"
        controller = request.app.state.automatic
        async with request.app.state.pipelines_orchestrator._reconcile_lock:
            config = await controller.store.get_config()
            previous = AutomaticState.model_validate(config.settings.core.get("automatic", {}))
            if previous.revision != body.expected_revision:
                raise HTTPException(409, "Automatic revision changed; refresh before saving")
            try:
                pipelines, activities = await controller.preview(body.configuration, config)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            return {
                "activities": activities,
                "pipelines": [pipeline.model_dump(mode="json") for pipeline in pipelines],
            }

    @router.put("/api/automatic")
    async def update(request: Request, body: AutomaticUpdate, response: Response):
        actor = owner(request)
        response.headers["Cache-Control"] = "no-store"
        controller = request.app.state.automatic
        orchestrator = request.app.state.pipelines_orchestrator
        async with orchestrator._reconcile_lock:
            try:
                await controller.apply(
                    body.configuration, expected_revision=body.expected_revision, owner=actor
                )
            except PipelineAlreadyExistsError as error:
                raise HTTPException(409, str(error)) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            await orchestrator._reconcile_plan()
            snapshot = controller.snapshot(
                orchestrator._activation_errors, orchestrator._last_error
            )
            snapshot["activation_errors"] = dict(orchestrator._activation_errors)
            return snapshot

    app.include_router(router)
