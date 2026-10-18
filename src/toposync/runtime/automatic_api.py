"""Owner-managed intentions; runtime mutations share the existing reconciliation lock."""

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import Field

from .automatic import AutomaticState, AutomaticValue
from .config_store import Pipeline, PipelineAlreadyExistsError
from .pipelines.demands import DemandRequest, FlowResultBinding
from .pipelines.templates import build_pipeline_graph_v2


class AutomaticUpdate(AutomaticValue):
    expected_revision: int = Field(ge=0)
    configuration: AutomaticState


class AutomaticFlowRequest(AutomaticValue):
    expected_revision: int = Field(ge=0)
    production_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


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
            snapshot["targets"] = await controller.targets(config)
            snapshot["provider_errors"] = sorted(controller.provider_failures)
            snapshot["activation_errors"] = dict(orchestrator._activation_errors)
            snapshot["manual_flows"] = dict(controller.demands.flow_status)
            return snapshot

    @router.post("/api/automatic/{intent_id}/flow")
    async def use_in_flow(request: Request, intent_id: str, body: AutomaticFlowRequest, response: Response):
        actor = owner(request)
        response.headers["Cache-Control"] = "no-store"
        controller = request.app.state.automatic
        orchestrator = request.app.state.pipelines_orchestrator
        async with orchestrator._reconcile_lock:
            config = await controller.store.get_config()
            controller.prepare_config(config)
            state = controller.state
            if state.revision != body.expected_revision:
                raise HTTPException(409, "Automatic revision changed; review before creating the flow")
            intent = next((item for item in state.intents if item.id == intent_id), None)
            if intent is None:
                raise HTTPException(404, "Activity unavailable")
            registry = controller.demands
            registry.bind(config.pipelines)
            key = "automatic." + intent_id
            current = registry.outputs.get(key)
            if current is None:
                raise HTTPException(409, "Prepare the activity before using its result")
            if registry.is_blocked(actor, key, "manual") or not registry.authorized(actor, key, "manual"):
                raise HTTPException(403, "Result access denied")
            name = "manual_" + intent_id
            existing = next((p for p in config.pipelines if p.name == name), None)
            _, output, revision = current
            if existing is not None:
                binding = existing.graph.get("meta", {}).get("result_consumer", {})
                if binding.get("owner") != actor or binding.get("request", {}).get("result_key") != key:
                    raise HTTPException(409, "Flow name already belongs to another configuration")
                if body.production_revision is None:
                    return {"pipeline": existing, "status": registry.flow_status.get(name)}
                if body.production_revision != revision:
                    raise HTTPException(409, "Result changed since review; refresh before accepting")
                updated = existing.model_copy(deep=True)
                binding = FlowResultBinding.model_validate(binding)
                node = next((n for n in updated.graph["nodes"] if n["uid"] == binding.node_uid), None)
                if node is None or node["operator"] != "core.result_input":
                    raise HTTPException(409, "Restore the shared result input before reviewing its binding")
                binding.request = binding.request.model_copy(update={
                    "production_revision": revision, "contract": output.contract,
                    "payload_keys": output.contract.payload_keys, "artifacts": output.contract.artifacts,
                })
                node["config"] = {"key": key, "contract": output.contract.model_dump()}
                updated.graph["meta"]["result_consumer"] = binding.model_dump(mode="json")
                try:
                    await controller.store.replace_pipeline(name, updated, expected_pipeline=existing, result_binding=True)
                except PipelineAlreadyExistsError as error:
                    raise HTTPException(409, str(error)) from error
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
                await orchestrator._reconcile_plan()
                return {"pipeline": updated, "status": registry.flow_status.get(name)}
            graph = build_pipeline_graph_v2(
                graph_uid=name,
                nodes=[
                    {"id": "result", "operator": "core.result_input", "config": {
                        "key": key, "contract": output.contract.model_dump(),
                    }},
                    {"id": "activity", "operator": "core.passthrough", "config": {}},
                ],
                edges=[{"from": {"node": "result", "port": "out"}, "to": {"node": "activity", "port": "in"},
                        "queue": {"max_items": 64, "drop_policy": "block"},
                        "backpressure": {"mode": "block"}, "lifecycle": {"compact_updates": False}}],
            )
            graph.setdefault("meta", {})["result_consumer"] = FlowResultBinding(
                owner=actor, node_uid=graph["nodes"][0]["uid"],
                request=DemandRequest(
                    consumer_id=name, purpose="manual", result_key=key, contract=output.contract,
                    production_revision=revision, lease_seconds=30,
                    payload_keys=output.contract.payload_keys, artifacts=output.contract.artifacts,
                ),
            ).model_dump(mode="json")
            pipeline = Pipeline(name=name, display_name=intent.name[:150] + " · Manual", editor_mode="interactive", graph=graph)
            try:
                await controller.store.create_pipeline(pipeline, result_binding=True)
            except PipelineAlreadyExistsError as error:
                raise HTTPException(409, str(error)) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            await orchestrator._reconcile_plan()
            return {"pipeline": pipeline, "status": registry.flow_status.get(name)}

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
