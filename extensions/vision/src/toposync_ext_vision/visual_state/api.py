from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from contextlib import contextmanager
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from PIL import Image
from pydantic import Field, ValidationError

from toposync.runtime.auth import AuthContext, AuthRuntime
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.preview import PipelinePreviewError

from ..processing.runtime_backends.embedding import embedding_model_revision
from ..registry import build_default_model_registry
from .preview import VisualPreviewBusy, VisualStatePreviewRunner
from .preparation import VisualRegionUnavailable
from .schemas import Counter, Identifier, ReferenceEvidence, VisualStateConfig, VisualStateModel
from .store import ReferenceConflict, ReferenceLimit, VisualReferenceStore


class DraftUpdate(VisualStateModel):
    expected_revision: Counter
    configuration: VisualStateConfig


class VisualCaptureRequest(VisualStateModel):
    configuration: VisualStateConfig
    pipeline: Pipeline


class SuggestionReviewRequest(VisualStateModel):
    action: Literal["accept", "reject", "undo"]
    operation_id: Identifier
    expected_revision: Counter
    expected_suggestion_revision: Counter
    class_id: Identifier | None = None


class VisualPreviewRequest(VisualCaptureRequest):
    duration_ms: int = Field(default=3000, strict=True, ge=500, le=10_000)


def _authorize(request: Request, *, write: bool = False) -> str:
    auth = getattr(request.app.state, "auth", None)
    context = getattr(request.state, "auth_context", None)
    if not isinstance(auth, AuthRuntime) or not isinstance(context, AuthContext):
        raise HTTPException(401, detail="authentication_required")
    principal = auth.authorize(context=context, action="core:extension:use",
                               resource_type="core:extension", resource_selector="com.toposync.vision")
    # Match pixel inspection until source-specific permissions can cover an entire prepared path.
    if principal.role != "owner":
        raise HTTPException(403, detail="reference_owner_required")
    auth.authorize(context=context, action="core:pipelines:write" if write else "core:pipelines:read",
                   resource_type=None, resource_selector="*")
    return principal.user_id


@contextmanager
def _storage_errors():
    try:
        yield
    except ReferenceLimit as error:
        raise HTTPException(413, detail={"code": "reference_limit", "message": str(error)}) from error
    except ReferenceConflict as error:
        raise HTTPException(409, detail={"code": "reference_conflict", "message": str(error)}) from error
    except ValidationError as error:
        raise HTTPException(422, detail={"code": "reference_configuration_invalid"}) from error
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(503, detail={"code": "reference_storage_unavailable"}) from error


def _private_image(data: bytes, thumbnail: bool) -> Response:
    if thumbnail:
        with Image.open(io.BytesIO(data)) as image:
            image.thumbnail((160, 160))
            output = io.BytesIO()
            image.save(output, format="PNG")
            data = output.getvalue()
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, no-store",
                                                            "X-Content-Type-Options": "nosniff"})


def create_visual_reference_router(get_store: Callable[[], VisualReferenceStore], preview_runner: VisualStatePreviewRunner) -> APIRouter:
    router = APIRouter(prefix="/api/vision/visual-state")
    uploads = asyncio.Semaphore(2)

    async def execute(request, body, *, capture=False):
        owner = _authorize(request, write=True)
        dependencies = PipelineRuntimeDependencies(config_store=request.app.state.config_store,
                                                   services=request.app.state.services)

        async def disconnected():
            # The body is already parsed; polling is_disconnected loses events through HTTP middleware.
            while (await request.receive())["type"] != "http.disconnect":
                pass

        task = asyncio.create_task(preview_runner.run(owner=owner, configuration=body.configuration,
            pipeline=body.pipeline, registry=request.app.state.pipeline_operator_registry,
            dependencies=dependencies, duration_ms=body.duration_ms if not capture else 0, capture=capture))
        disconnect = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({task, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                raise HTTPException(499, detail="visual_preview_cancelled")
            try:
                return task.result()
            except VisualPreviewBusy as error:
                raise HTTPException(429, detail="visual_preview_busy") from error
            except PipelinePreviewError as error:
                raise HTTPException(409, detail={"code": "visual_preview_input_invalid", "reason": error.code}) from error
            except ReferenceConflict as error:
                raise HTTPException(409, detail="reference_conflict") from error
            except VisualRegionUnavailable as error:
                raise HTTPException(409, detail="region_unavailable") from error
            except ValueError as error:
                raise HTTPException(422, detail="visual_preview_configuration_invalid") from error
            except TimeoutError as error:
                raise HTTPException(504, detail="visual_preview_timeout") from error
            except Exception as error:
                raise HTTPException(503, detail="visual_preview_failed") from error
        finally:
            task.cancel()
            disconnect.cancel()
            await asyncio.gather(task, disconnect, return_exceptions=True)

    @router.post("/preview")
    async def preview(request: Request, body: VisualPreviewRequest):
        return await execute(request, body)

    @router.post("/capture-frame")
    async def capture_frame(request: Request, body: VisualCaptureRequest):
        owner = _authorize(request, write=True)
        result = await execute(request, body, capture=True)
        evidence = ReferenceEvidence.model_validate(result["evidence"]).model_dump(mode="json")
        return Response(result["image"], media_type="image/png", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Toposync-Capture-Evidence": json.dumps(evidence, separators=(",", ":")),
            "X-Toposync-Capture-Receipt": request.app.state.capture_receipts.issue(owner, result["image"], evidence),
        })

    @router.get("/models")
    def models(request: Request):
        _authorize(request)
        choices = []
        for manifest in build_default_model_registry().list_manifests(task="embedding"):
            try:
                revision = embedding_model_revision(manifest)
            except ValueError:
                continue
            try:
                present = manifest.resolve_artifact_path().is_file()
            except OSError:
                present = False
            choices.append({"model_id": manifest.model_id, "name": manifest.display_name,
                            "revision": revision, "artifact_present": present})
        return {"choices": choices}

    @router.get("/ai-options")
    async def ai_options(request: Request, sensor_id: Identifier, response: Response):
        _authorize(request)
        request.app.state.auth.authorize(context=request.state.auth_context, action="core:extension:use",
            resource_type="core:extension", resource_selector="com.toposync.ai")
        response.headers["Cache-Control"] = "no-store"
        try:
            return await request.app.state.services.call("ai.visual_state.options", sensor_id=sensor_id)
        except Exception as error:
            raise HTTPException(503, detail="visual_ai_options_unavailable") from error

    @router.post("/drafts", status_code=201)
    def create_draft(request: Request, configuration: VisualStateConfig):
        _authorize(request, write=True)
        with _storage_errors():
            store = get_store()
            store.collect()
            return store.create_draft(configuration)

    @router.get("/drafts/{draft_id}")
    def get_draft(request: Request, draft_id: Identifier):
        _authorize(request)
        with _storage_errors():
            return get_store().get_draft(draft_id)

    @router.put("/drafts/{draft_id}")
    def update_draft(request: Request, draft_id: Identifier, body: DraftUpdate):
        _authorize(request, write=True)
        with _storage_errors():
            return get_store().update_draft(draft_id, body.configuration, expected_revision=body.expected_revision)

    @router.post("/drafts/{draft_id}/captures")
    async def capture(request: Request, draft_id: Identifier, class_id: Identifier,
                      operation_id: Identifier, expected_revision: int = Query(ge=0)):
        owner = _authorize(request, write=True)
        receipt = request.headers.get("X-Toposync-Capture-Receipt", "")
        if not receipt or len(receipt) > 4096:
            raise HTTPException(409, detail="reference_capture_receipt_invalid")
        if request.headers.get("content-type", "").split(";")[0] != "image/png":
            raise HTTPException(415, detail="reference_capture_requires_png")
        try:
            await asyncio.wait_for(uploads.acquire(), timeout=1)
        except TimeoutError as error:
            raise HTTPException(429, detail="reference_capture_busy") from error
        try:
            content = bytearray()
            try:
                async with asyncio.timeout(15):
                    async for chunk in request.stream():
                        if len(content) + len(chunk) > 8 * 1024 * 1024:
                            raise HTTPException(413, detail="reference_capture_image_limit")
                        content.extend(chunk)
            except TimeoutError as error:
                raise HTTPException(408, detail="reference_capture_upload_timeout") from error
            image = bytes(content)
            try:
                evidence = ReferenceEvidence.model_validate(request.app.state.capture_receipts.verify(receipt, owner, image))
            except ValueError as error:
                raise HTTPException(409, detail="reference_capture_receipt_invalid") from error

            def save():
                with _storage_errors():
                    draft, reference = get_store().capture(draft_id, class_id, image, evidence,
                        operation_id=operation_id, expected_revision=expected_revision, actor_id=owner)
                    return {"draft": draft, "reference": reference}

            return await asyncio.to_thread(save)
        finally:
            uploads.release()

    @router.get("/drafts/{draft_id}/suggestions")
    def suggestions(request: Request, draft_id: Identifier, response: Response, offset: int = Query(default=0, ge=0, le=3000)):
        _authorize(request)
        response.headers["Cache-Control"] = "private, no-store"
        with _storage_errors():
            store = get_store()
            items = store.list_suggestions(draft_id, offset=offset)
            context = store._suggestion_context(store.get_draft(draft_id).configuration)
            return {"items": [{**item.model_dump(mode="json"), "compatible": item.context_digest == context} for item in items],
                    "summary": store.suggestion_summary(draft_id), "next_offset": offset + 100 if len(items) == 100 else None}

    @router.post("/drafts/{draft_id}/suggestions/{suggestion_id}/review")
    def review_suggestion(request: Request, draft_id: Identifier, suggestion_id: Identifier, body: SuggestionReviewRequest, response: Response):
        actor = _authorize(request, write=True)
        response.headers["Cache-Control"] = "private, no-store"
        if body.action != "accept" and body.class_id is not None:
            raise HTTPException(422, detail="invalid_suggestion_review")
        with _storage_errors():
            draft, suggestion = get_store().review_suggestion(draft_id, suggestion_id, actor_id=actor, **body.model_dump())
            return {"draft": draft, "suggestion": suggestion}

    @router.get("/drafts/{draft_id}/suggestions/{suggestion_id}/image")
    def suggestion_image(request: Request, draft_id: Identifier, suggestion_id: Identifier, thumbnail: bool = True):
        _authorize(request)
        with _storage_errors():
            return _private_image(get_store().read_suggestion_image(draft_id, suggestion_id), thumbnail)

    @router.get("/drafts/{draft_id}/references/{reference_id}/image")
    def image(request: Request, draft_id: Identifier, reference_id: Identifier, thumbnail: bool = True):
        _authorize(request)
        with _storage_errors():
            store = get_store()
            draft = store.get_draft(draft_id)
            if not any(reference_id in category.reference_ids for category in draft.configuration.classes):
                raise HTTPException(404, detail="reference_not_in_draft")
            _, data = store.read_image(reference_id)
            return _private_image(data, thumbnail)

    return router
