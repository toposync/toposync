"""Read-only state projection, owned by live runtimes on the application's event loop."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.pipelines.preview import PipelinePreviewError, reference_input_pipeline

from .preparation import visual_preparation_revision
from .schemas import PublishedState, VisualStateConfig


@dataclass
class _Execution:
    configuration: VisualStateConfig
    snapshot: Callable[[], PublishedState | None]


class VisualStatePublication:
    def __init__(self):
        self.server_epoch = uuid.uuid4().hex
        self.version = 0
        self.executions: dict[tuple[str, str], _Execution] = {}
        self.closed = False

    def register(self, pipeline_name, node_id, configuration, snapshot):
        if self.closed:
            raise RuntimeError("visual_state_publication_closed")
        execution = _Execution(configuration.model_copy(deep=True), snapshot)
        key = (pipeline_name, node_id)
        self.executions[key] = execution

        def release():
            # An old runtime's delayed cleanup must not remove its replacement.
            if self.executions.get(key) is execution:
                del self.executions[key]

        return release

    def close(self):
        self.closed = True
        self.executions.clear()

    def read(self, pipeline, node_id, configuration):
        execution = self.executions.get((pipeline.name, node_id))
        reason = "not_running"
        if not pipeline.enabled:
            reason = "pipeline_disabled"
        elif pipeline.processing_server_id != "local":
            reason = "remote_server_unsupported"
        elif execution is not None:
            reason = "configuration_changed"
            try:
                compatible = execution.configuration == configuration and visual_preparation_revision(
                    configuration, reference_input_pipeline(pipeline, node_id)) == configuration.preparation_revision
            except (ValueError, PipelinePreviewError):
                compatible = False
            if compatible:
                state = execution.snapshot()
                if state is not None:
                    # Do not expose images, references, descriptions or provider responses to state consumers.
                    return state.model_dump(mode="json", include={
                        "runtime_epoch", "state_version", "status", "class_id", "class_key", "reason", "observed_at",
                        "confirmed_at", "valid_for_ms", "sample", "method", "freshness_basis",
                        "preparation_revision", "reference_revision", "model_revision",
                    })
                reason = "not_running"
        return self.unavailable(reason)

    @staticmethod
    def unavailable(reason):
        return {"runtime_epoch": None, "state_version": None, "status": "unavailable", "class_id": None,
                "class_key": None, "reason": reason, "observed_at": None, "confirmed_at": None, "valid_for_ms": 0,
                "sample": None, "method": None, "freshness_basis": None, "preparation_revision": None,
                "reference_revision": None, "model_revision": None}


def create_visual_state_publication_router(publication: VisualStatePublication):
    router = APIRouter(prefix="/api/vision/visual-state")

    @router.get("/states")
    async def states(request: Request, response: Response):
        auth = getattr(request.app.state, "auth", None)
        context = getattr(request.state, "auth_context", None)
        if not isinstance(auth, AuthRuntime) or not isinstance(context, AuthContext):
            raise HTTPException(401, detail="authentication_required")
        principal = auth.authorize(context=context, action="core:extension:use",
                                   resource_type="core:extension", resource_selector="com.toposync.vision")
        if publication.closed:
            raise HTTPException(503, detail="visual_state_publication_unavailable")
        pipelines = await request.app.state.config_store.list_pipelines()
        if publication.closed:
            raise HTTPException(503, detail="visual_state_publication_unavailable")
        configured = []
        for pipeline in pipelines:
            for node in pipeline.graph.get("nodes", []):
                if node.get("operator") != "vision.visual_state":
                    continue
                try:
                    configuration = VisualStateConfig.model_validate(node["config"])
                except (KeyError, ValidationError):
                    continue
                if configuration.sensor_id != "unconfigured":
                    configured.append((pipeline, node["id"], configuration))
        counts = Counter(configuration.sensor_id for _, _, configuration in configured)
        result = {}
        demands = getattr(request.app.state, "pipeline_demands", None)
        for pipeline, node_id, configuration in configured:
            sensor_id = configuration.sensor_id
            key = f"visual_state:{sensor_id}"
            try:
                auth.authorize(context=context, action="core:pipeline_results:read",
                               resource_type="core:pipeline_result", resource_selector=key)
            except HTTPException as error:
                if error.status_code == 403:
                    continue
                raise
            if demands is not None and demands.is_blocked(principal.user_id, key, "read"):
                continue
            if counts[sensor_id] != 1:
                result[sensor_id] = {"sensor_id": sensor_id, "pipeline_name": None, "node_id": None, "classes": [],
                                     "state": publication.unavailable("duplicate_sensor_identity")}
                continue
            result[sensor_id] = {"sensor_id": sensor_id, "pipeline_name": pipeline.name, "node_id": node_id,
                "home_assistant": configuration.home_assistant.model_dump(mode="json"),
                "classes": [{"class_id": item.class_id, "key": item.key, "label": item.label, "enabled": item.enabled}
                            for item in configuration.classes], "state": publication.read(pipeline, node_id, configuration)}
        publication.version += 1
        response.headers["Cache-Control"] = "private, no-store"
        return {"schema_version": 1, "server_epoch": publication.server_epoch, "snapshot_version": publication.version,
                "capabilities": {"local_states": True, "remote_states": False}, "states": list(result.values())}

    return router
