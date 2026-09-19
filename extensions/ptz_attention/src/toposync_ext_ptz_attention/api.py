from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from toposync.runtime.auth import AuthContext, AuthPrincipal, AuthRuntime

from .bindings import attention_bindings, live_observer_binding_issue
from .constants import API_PREFIX, OPERATOR_ID_REQUEST
from .controller import PtzAttentionController
from .models import AttentionProfile
from .store import (
    AttentionStore,
    DeviceProfileConflictError,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
)


_LOGGER = logging.getLogger(__name__)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileCommand(_StrictBody):
    profile_id: str


def create_router(
    controller: PtzAttentionController,
    store: AttentionStore,
    config_store: Any | None = None,
) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX, tags=["ptz-attention"])

    @router.get("/catalog")
    async def catalog(request: Request) -> dict[str, Any]:
        _require_authenticated(request)
        profiles = [
            profile
            for profile in store.list_profiles()
            if _can_auth(
                request,
                action="core:camera:read",
                camera_id=profile.camera_id,
            )
        ]
        event_types: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            for policy in profile.event_policies:
                event_types.setdefault(
                    policy.event_type,
                    {"event_type": policy.event_type, "profile_ids": []},
                )["profile_ids"].append(profile.id)
        partial_errors: list[dict[str, str]] = []
        raw_cameras: list[dict[str, Any]] = []
        if controller.has_service("cameras.catalog.list"):
            try:
                camera_catalog = await controller.services.call("cameras.catalog.list")
                values = camera_catalog.get("cameras") if isinstance(camera_catalog, dict) else None
                raw_cameras = (
                    [dict(item) for item in values if isinstance(item, dict)]
                    if isinstance(values, list)
                    else []
                )
            except Exception:
                partial_errors.append(
                    {
                        "source": "cameras.catalog.list",
                        "code": "camera_catalog_unavailable",
                        "message": "The safe camera catalog is unavailable.",
                    }
                )
        else:
            partial_errors.append(
                {
                    "source": "cameras.catalog.list",
                    "code": "camera_catalog_service_missing",
                    "message": "The safe camera catalog service is not registered.",
                }
            )

        compositions: list[Any] = []
        pipelines: list[Any] = []
        if config_store is not None and callable(getattr(config_store, "get_config", None)):
            try:
                app_config = await config_store.get_config()
                compositions = list(getattr(app_config, "compositions", []) or [])
                pipelines = list(getattr(app_config, "pipelines", []) or [])
            except Exception:
                partial_errors.append(
                    {
                        "source": "config_store",
                        "code": "config_catalog_unavailable",
                        "message": "Compositions and pipeline bindings are unavailable.",
                    }
                )
        else:
            partial_errors.append(
                {
                    "source": "config_store",
                    "code": "config_store_unavailable",
                    "message": "Compositions and pipeline bindings are unavailable.",
                }
            )

        views_by_camera = _calibrated_views_by_camera(compositions)
        cameras = [
            _safe_camera_catalog_item(
                item, views=views_by_camera.get(str(item.get("id") or "").strip(), [])
            )
            for item in raw_cameras
            if str(item.get("id") or "").strip()
            and _can_auth(
                request,
                action="core:camera:read",
                camera_id=str(item.get("id") or "").strip(),
            )
        ]
        for camera in cameras:
            camera_id = str(camera.get("id") or "")
            camera["permissions"] = {
                "configure": _can_auth(
                    request,
                    action="core:camera:configure",
                    camera_id=camera_id,
                ),
                "control": _can_auth(
                    request,
                    action="core:camera:control",
                    camera_id=camera_id,
                ),
            }
        readable_profile_ids = {profile.id for profile in profiles}
        bindings = [
            binding.public_payload()
            for binding in attention_bindings(pipelines)
            if _binding_is_visible(
                request,
                store,
                binding.public_payload(),
                readable_profile_ids=readable_profile_ids,
            )
        ]
        for binding in bindings:
            event_type = str(binding.get("event_type") or "").strip()
            if event_type:
                event_types.setdefault(
                    event_type,
                    {"event_type": event_type, "profile_ids": []},
                )
        return {
            "generated_at": time.time(),
            "operator_id": OPERATOR_ID_REQUEST,
            "modes": ["disabled", "shadow", "live_preset", "paused"],
            "states": [
                "IDLE",
                "CANDIDATE",
                "ACQUIRING",
                "FOCUSED",
                "GRACE",
                "RETURNING",
                "MANUAL_OVERRIDE",
                "FAULT",
            ],
            "event_types": sorted(event_types.values(), key=lambda item: item["event_type"]),
            "cameras": cameras,
            "bindings": bindings,
            "partial_errors": partial_errors,
            "permissions": {
                "configure": _can_auth(request, action="core:camera:configure"),
                "control": _can_auth(request, action="core:camera:control"),
            },
            "services": controller.service_status(),
        }

    @router.get("/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        _require_authenticated(request)
        return {
            "profiles": [
                item.model_dump(mode="json")
                for item in store.list_profiles()
                if _can_auth(
                    request,
                    action="core:camera:read",
                    camera_id=item.camera_id,
                )
            ]
        }

    @router.post("/profiles", status_code=201)
    async def create_profile(request: Request, body: AttentionProfile) -> dict[str, Any]:
        _require_auth(
            request,
            action="core:camera:configure",
            camera_id=body.camera_id,
        )
        if body.mode == "live_preset":
            _require_auth(
                request,
                action="core:camera:control",
                camera_id=body.camera_id,
            )
        if store.get_profile(body.id) is not None:
            raise HTTPException(status_code=409, detail=f"Profile already exists: {body.id}")
        conflict = store.get_profile_by_device(body.ptz_device_id)
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"ptz_device_id already belongs to profile '{conflict.id}'",
            )
        if store.is_recovery_required(body.ptz_device_id) and not (
            body.mode == "live_preset"
            or (body.mode == "paused" and body.resume_mode == "live_preset")
        ):
            raise HTTPException(
                status_code=409,
                detail="A live PTZ profile is required to return this camera home",
            )
        await _require_live_observer_binding_safe(config_store, body)
        await _require_live_profile_ready(controller, body)
        try:
            await controller.synchronize_profile(None, body)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            store.create_profile(body)
        except ProfileAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=f"Profile already exists: {exc}") from exc
        except DeviceProfileConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        controller.events.publish(
            {
                "type": "profiles_changed",
                "profile_id": body.id,
                "camera_id": body.camera_id,
            }
        )
        return body.model_dump(mode="json")

    @router.post("/profiles/validate")
    async def validate_profile_body(request: Request, body: AttentionProfile) -> dict[str, Any]:
        _require_auth(
            request,
            action="core:camera:read",
            camera_id=body.camera_id,
        )
        return await _validate_profile(controller, body)

    @router.get("/profiles/{profile_id}")
    async def get_profile(request: Request, profile_id: str) -> dict[str, Any]:
        profile = _profile_or_404(store, profile_id)
        _require_auth(
            request,
            action="core:camera:read",
            camera_id=profile.camera_id,
        )
        return profile.model_dump(mode="json")

    @router.put("/profiles/{profile_id}")
    async def replace_profile(
        request: Request,
        profile_id: str,
        body: AttentionProfile,
    ) -> dict[str, Any]:
        if body.id != profile_id:
            raise HTTPException(status_code=400, detail="Profile id cannot be changed")
        previous = _profile_or_404(store, profile_id)
        _require_auth(
            request,
            action="core:camera:configure",
            camera_id=previous.camera_id,
        )
        if body.camera_id != previous.camera_id:
            _require_auth(
                request,
                action="core:camera:configure",
                camera_id=body.camera_id,
            )
        if body.mode == "live_preset":
            for camera_id in dict.fromkeys((previous.camera_id, body.camera_id)):
                _require_auth(
                    request,
                    action="core:camera:control",
                    camera_id=camera_id,
                )
        if store.is_recovery_required(previous.ptz_device_id):
            raise HTTPException(
                status_code=409,
                detail="Return the camera home before changing this profile",
            )
        if controller.is_profile_active(profile_id):
            raise HTTPException(status_code=409, detail="Pause the profile before changing it")
        conflict = store.get_profile_by_device(body.ptz_device_id)
        if conflict is not None and conflict.id != body.id:
            raise HTTPException(
                status_code=409,
                detail=f"ptz_device_id already belongs to profile '{conflict.id}'",
            )
        await _require_live_observer_binding_safe(config_store, body)
        await _require_live_profile_ready(controller, body)
        try:
            await controller.synchronize_profile(previous, body)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            store.replace_profile(body)
        except DeviceProfileConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        controller.events.publish(
            {
                "type": "profiles_changed",
                "profile_id": body.id,
                "camera_id": body.camera_id,
            }
        )
        return body.model_dump(mode="json")

    @router.delete("/profiles/{profile_id}", status_code=204)
    async def delete_profile(request: Request, profile_id: str) -> None:
        profile = _profile_or_404(store, profile_id)
        _require_auth(
            request,
            action="core:camera:configure",
            camera_id=profile.camera_id,
        )
        if store.is_recovery_required(profile.ptz_device_id):
            raise HTTPException(
                status_code=409,
                detail="Return the camera home before deleting this profile",
            )
        if controller.is_profile_active(profile_id):
            raise HTTPException(status_code=409, detail="Pause the profile before deleting it")
        try:
            await controller.synchronize_profile(profile, None)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not store.delete_profile(profile_id):
            raise HTTPException(status_code=404, detail="Profile not found")
        controller.events.publish(
            {
                "type": "profiles_changed",
                "profile_id": profile_id,
                "camera_id": profile.camera_id,
            }
        )

    @router.post("/profiles/{profile_id}/validate")
    async def validate_stored_profile(request: Request, profile_id: str) -> dict[str, Any]:
        profile = _profile_or_404(store, profile_id)
        _require_auth(
            request,
            action="core:camera:read",
            camera_id=profile.camera_id,
        )
        return await _validate_profile(controller, profile)

    @router.get("/status")
    async def status(
        request: Request,
        profile_id: str = "",
        ptz_device_id: str = "",
    ) -> dict[str, Any]:
        _require_authenticated(request)
        allowed_profile_ids = {
            profile.id
            for profile in store.list_profiles()
            if _can_auth(
                request,
                action="core:camera:read",
                camera_id=profile.camera_id,
            )
        }
        return {
            "generated_at": time.time(),
            "devices": [
                item.public_payload()
                for item in controller.status(profile_id=profile_id, ptz_device_id=ptz_device_id)
                if item.profile_id in allowed_profile_ids
            ],
            "services": controller.service_status(),
        }

    @router.get("/decisions")
    async def decisions(
        request: Request,
        before: int | None = None,
        limit: int = Query(default=100, ge=1, le=250),
        profile_id: str = "",
        ptz_device_id: str = "",
    ) -> dict[str, Any]:
        _require_authenticated(request)
        allowed_device_ids = [
            device_id
            for device_id in store.list_decision_device_ids()
            if _can_auth(
                request,
                action="core:camera:read",
                camera_id=device_id,
            )
        ]
        records, next_cursor = store.list_decisions(
            before=before,
            limit=limit,
            profile_id=profile_id,
            ptz_device_id=ptz_device_id,
            ptz_device_ids=allowed_device_ids,
        )
        return {
            "decisions": [item.public_payload() for item in records],
            "next_cursor": next_cursor,
        }

    @router.post("/pause")
    async def pause(request: Request, body: ProfileCommand) -> dict[str, Any]:
        selected_profile = _profile_or_404(store, body.profile_id)
        _require_auth(
            request,
            action="core:camera:control",
            camera_id=selected_profile.camera_id,
        )
        try:
            profile = await controller.pause(body.profile_id)
        except ProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "profile": profile.model_dump(mode="json")}

    @router.post("/resume")
    async def resume(request: Request, body: ProfileCommand) -> dict[str, Any]:
        selected_profile = _profile_or_404(store, body.profile_id)
        _require_auth(
            request,
            action="core:camera:control",
            camera_id=selected_profile.camera_id,
        )
        if selected_profile.mode == "paused" and selected_profile.resume_mode == "live_preset":
            await _require_live_observer_binding_safe(config_store, selected_profile)
            await _require_live_profile_ready(
                controller,
                selected_profile,
                allow_paused_resume=True,
            )
        try:
            profile = await controller.resume(body.profile_id)
        except ProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "profile": profile.model_dump(mode="json")}

    @router.post("/return-home")
    async def return_home(request: Request, body: ProfileCommand) -> dict[str, Any]:
        selected_profile = _profile_or_404(store, body.profile_id)
        _require_auth(
            request,
            action="core:camera:control",
            camera_id=selected_profile.camera_id,
        )
        try:
            started = await controller.return_home(body.profile_id)
        except ProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        if not started:
            raise HTTPException(status_code=409, detail="Return home could not be started")
        return {"ok": True, "profile_id": body.profile_id}

    @router.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        _require_authenticated(request)

        async def generate():  # noqa: ANN202
            queue = controller.events.subscribe()
            try:
                yield "retry: 3000\n\n"
                yield "event: ready\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if not _event_is_visible(request, store, event):
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            finally:
                controller.events.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


async def _validate_profile(
    controller: PtzAttentionController,
    profile: AttentionProfile,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    resolved_views: dict[str, dict[str, Any]] = {}
    live = profile.mode == "live_preset" or (
        profile.mode == "paused" and profile.resume_mode == "live_preset"
    )

    def issue(code: str, message: str, *, blocking: bool) -> None:
        issues.append(
            {
                "severity": "error" if blocking else "warning",
                "code": code,
                "message": message,
                "blocking": blocking,
            }
        )

    for service_id, available in controller.service_status().items():
        if not available:
            issue(
                "service_unavailable",
                f"Service is unavailable: {service_id}",
                blocking=live,
            )

    resolver_available = controller.has_service("cameras.views.resolve_target")
    if resolver_available:
        for view_id in profile.eligible_view_ids:
            try:
                raw = await controller.services.call(
                    "cameras.views.resolve_target",
                    camera_id=profile.camera_id,
                    source_id=profile.source_id,
                    ptz_device_id=profile.ptz_device_id,
                    composition_id=profile.composition_id,
                    target={"bbox01": [0.0, 0.0, 1.0, 1.0]},
                    preferred_view_id=view_id,
                    eligible_view_ids=profile.eligible_view_ids,
                )
                resolved_id = str(raw.get("view_id") or "") if isinstance(raw, dict) else ""
                preset_token = str(raw.get("preset_token") or "") if isinstance(raw, dict) else ""
                if resolved_id != view_id or not preset_token:
                    raise ValueError("resolver did not return the requested view and preset")
                resolved_views[view_id] = dict(raw)
            except Exception:
                _LOGGER.debug(
                    "PTZ Attention view validation failed for profile=%s view=%s",
                    profile.id,
                    view_id,
                    exc_info=True,
                )
                issue(
                    "view_unresolved",
                    f"View '{view_id}' cannot be resolved.",
                    blocking=live,
                )

    automation_ready: bool | None = None
    automation_ready_reason = ""
    if controller.has_service("cameras.control.snapshot"):
        try:
            snapshot = await controller.services.call(
                "cameras.control.snapshot",
                camera_id=profile.camera_id,
                source_id=profile.source_id,
                ptz_device_id=profile.ptz_device_id,
            )
            if not isinstance(snapshot, dict):
                raise ValueError("invalid snapshot response")
            automation_ready = snapshot.get("automation_ready") is True
            automation_ready_reason = (
                "" if automation_ready else "automation_exclusive_control_not_confirmed"
            )
            if live and not automation_ready:
                issue(
                    "automation_exclusive_control_not_confirmed",
                    "Camera automation exclusive control has not been confirmed.",
                    blocking=True,
                )
        except Exception:
            _LOGGER.debug(
                "PTZ Attention readiness snapshot failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            issue("snapshot_failed", "Camera snapshot is unavailable.", blocking=live)

    known_preset_tokens: set[str] = set()
    preset_catalog_loaded = False
    if controller.has_service("cameras.ptz.list_presets"):
        try:
            presets = await controller.services.call(
                "cameras.ptz.list_presets",
                camera_id=profile.camera_id,
                camera_source_id=profile.source_id or None,
            )
            preset_catalog_loaded = True
            known_preset_tokens = {
                str(item.get("token") or item.get("preset_token") or "").strip()
                for item in _normalize_presets(presets)
            }
            known_preset_tokens.discard("")
        except Exception:
            _LOGGER.debug(
                "PTZ Attention preset validation failed for profile=%s",
                profile.id,
                exc_info=True,
            )
            issue("preset_catalog_failed", "Preset catalog is unavailable.", blocking=live)
    else:
        issue(
            "preset_catalog_unavailable",
            "Preset catalog service is unavailable.",
            blocking=live,
        )
    if preset_catalog_loaded and not known_preset_tokens:
        issue(
            "preset_catalog_empty",
            "No PTZ presets are available for this camera source.",
            blocking=live,
        )
    if known_preset_tokens:
        for view_id, resolved in resolved_views.items():
            token = str(resolved.get("preset_token") or "")
            if token not in known_preset_tokens:
                issue(
                    "preset_not_found",
                    f"View '{view_id}' resolves to an unavailable preset.",
                    blocking=live,
                )

    public_resolved_views = {
        view_id: {
            "view_id": str(raw.get("view_id") or ""),
            "confidence": float(raw.get("confidence") or 0.0),
            "reason": "resolved",
        }
        for view_id, raw in resolved_views.items()
    }
    return {
        "ok": not any(bool(item["blocking"]) for item in issues),
        "profile_id": profile.id,
        "mode": profile.mode,
        "issues": issues,
        "resolved_views": public_resolved_views,
        "automation_ready": automation_ready,
        "automation_ready_reason": automation_ready_reason,
        "services": controller.service_status(),
    }


async def _require_live_profile_ready(
    controller: PtzAttentionController,
    profile: AttentionProfile,
    *,
    allow_paused_resume: bool = False,
) -> None:
    if profile.mode != "live_preset" and not (
        allow_paused_resume and profile.mode == "paused" and profile.resume_mode == "live_preset"
    ):
        return
    result = await _validate_profile(controller, profile)
    if result.get("ok") is True:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "live_profile_not_ready",
            "issues": result.get("issues", []),
            "automation_ready": result.get("automation_ready"),
        },
    )


async def _require_live_observer_binding_safe(
    config_store: Any | None,
    profile: AttentionProfile,
) -> None:
    if profile.mode != "live_preset" and not (
        profile.mode == "paused" and profile.resume_mode == "live_preset"
    ):
        return
    get_config = getattr(config_store, "get_config", None)
    if not callable(get_config):
        raise HTTPException(
            status_code=409,
            detail={"code": "observer_binding_check_unavailable"},
        )
    try:
        app_config = await get_config()
    except Exception as exc:
        _LOGGER.debug(
            "PTZ Attention observer binding validation failed for profile=%s",
            profile.id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "observer_binding_check_unavailable"},
        ) from exc
    issue = live_observer_binding_issue(
        profile,
        attention_bindings(list(getattr(app_config, "pipelines", []) or [])),
    )
    if issue is None:
        return
    raise HTTPException(status_code=409, detail={"code": issue})


def _normalize_presets(raw: Any) -> list[dict[str, Any]]:
    values = raw.get("presets") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            result.append(dict(item))
    return result


def _safe_camera_catalog_item(
    raw: dict[str, Any],
    *,
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    sources_raw = raw.get("sources")
    sources = []
    if isinstance(sources_raw, list):
        for source in sources_raw:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("id") or "").strip()
            if not source_id:
                continue
            sources.append(
                {
                    "id": source_id,
                    "name": str(source.get("name") or "").strip(),
                    "role": str(source.get("role") or "").strip(),
                    "kind": str(source.get("kind") or "video").strip() or "video",
                    "enabled": bool(source.get("enabled", True)),
                    "is_default": bool(source.get("is_default", False)),
                    "view_id": str(source.get("view_id") or "").strip(),
                    "has_ptz": bool(source.get("has_ptz", False)),
                }
            )
    control = raw.get("control")
    control = control if isinstance(control, dict) else {}
    camera_id = str(raw.get("id") or "").strip()
    control_type = str(control.get("type") or "none").strip().lower()
    reported_device_id = str(control.get("ptz_device_id") or "").strip()
    actuator_id = camera_id if control_type != "none" and reported_device_id == camera_id else ""
    ready_raw = control.get(
        "automation_exclusive_control_confirmed",
        raw.get("automation_exclusive_control_confirmed"),
    )
    automation_confirmed = ready_raw if isinstance(ready_raw, bool) else None
    control_source_id = str(raw.get("control_source_id") or "").strip()
    eligible_control_source_ids = {
        str(item["id"])
        for item in sources
        if item["enabled"] and item["kind"] == "video" and item["has_ptz"]
    }
    if control_source_id not in eligible_control_source_ids:
        control_source_id = ""
    if not control_source_id:
        default_source = next(
            (
                item
                for item in sources
                if item["enabled"]
                and item["kind"] == "video"
                and item["has_ptz"]
                and item["is_default"]
            ),
            None,
        ) or next(
            (
                item
                for item in sources
                if item["enabled"] and item["kind"] == "video" and item["has_ptz"]
            ),
            None,
        )
        control_source_id = str(default_source.get("id") or "") if default_source else ""
    if not actuator_id:
        control_source_id = ""
    return {
        "id": camera_id,
        "name": str(raw.get("name") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "actuator_id": actuator_id,
        "control_source_id": control_source_id,
        "automation_exclusive_control_confirmed": automation_confirmed,
        "automation_ready_reason": str(
            control.get("automation_ready_reason")
            or raw.get("automation_ready_reason")
            or (
                "exclusive_control_confirmed"
                if automation_confirmed
                else "exclusive_control_not_confirmed"
            )
        ).strip(),
        "sources": sources,
        "views": views,
    }


def _calibrated_views_by_camera(compositions: list[Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for composition in compositions:
        composition_id = str(getattr(composition, "id", "") or "").strip()
        composition_name = str(getattr(composition, "name", "") or "").strip()
        for element in list(getattr(composition, "elements", []) or []):
            props = getattr(element, "props", None)
            if not isinstance(props, dict):
                continue
            camera_id = str(props.get("camera_id") or "").strip()
            raw_views = props.get("calibrated_views")
            if not camera_id or not isinstance(raw_views, list):
                continue
            for raw_view in raw_views:
                if not isinstance(raw_view, dict):
                    continue
                view_id = str(raw_view.get("id") or "").strip()
                if not view_id:
                    continue
                pose = raw_view.get("pose_reference")
                pose = pose if isinstance(pose, dict) else {}
                scope = raw_view.get("stream_scope")
                scope = scope if isinstance(scope, dict) else {}
                quality = raw_view.get("projection_quality")
                quality = quality if isinstance(quality, dict) else {}
                has_preset = bool(str(pose.get("preset_token") or "").strip())
                requires_pose_evidence = bool(raw_view.get("requires_pose_evidence"))
                has_numeric_pose = any(
                    isinstance(pose.get(axis), (int, float))
                    and not isinstance(pose.get(axis), bool)
                    and math.isfinite(float(pose[axis]))
                    for axis in ("pan", "tilt", "zoom")
                )
                result.setdefault(camera_id, []).append(
                    {
                        "id": view_id,
                        "label": str(raw_view.get("label") or view_id).strip() or view_id,
                        "composition_id": composition_id,
                        "composition_name": composition_name,
                        "camera_element_id": str(getattr(element, "id", "") or "").strip(),
                        "pose_bound": has_preset
                        and (not requires_pose_evidence or has_numeric_pose),
                        "quality": str(quality.get("status") or "incomplete").strip()
                        or "incomplete",
                        "compatible_source_ids": _safe_text_list(
                            scope.get("compatible_source_ids")
                        ),
                        "compatible_roles": _safe_text_list(scope.get("compatible_roles")),
                        "physical_view_id": str(scope.get("physical_view_id") or "").strip(),
                        "preset_name": str(pose.get("preset_name") or "").strip(),
                    }
                )
    for views in result.values():
        views.sort(key=lambda item: (item["composition_name"], item["label"], item["id"]))
    return result


def _safe_text_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))


def _profile_or_404(store: AttentionStore, profile_id: str) -> AttentionProfile:
    profile = store.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _require_authenticated(request: Request) -> None:
    auth = getattr(request.app.state, "auth", None)
    if not isinstance(auth, AuthRuntime):
        return
    context = getattr(request.state, "auth_context", None)
    if not isinstance(context, AuthContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    auth.require_authenticated(context)


def _event_is_visible(
    request: Request,
    store: AttentionStore,
    event: Any,
) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or "")
    camera_id = str(event.get("camera_id") or "").strip()
    if event_type == "decision":
        decision = event.get("decision")
        camera_id = (
            str(decision.get("ptz_device_id") or "").strip() if isinstance(decision, dict) else ""
        )
    if not camera_id:
        return False
    return _can_auth(
        request,
        action="core:camera:read",
        camera_id=camera_id,
    )


def _binding_is_visible(
    request: Request,
    store: AttentionStore,
    binding: dict[str, Any],
    *,
    readable_profile_ids: set[str],
) -> bool:
    profile_id = str(binding.get("profile_id") or "").strip()
    observer_camera_id = str(binding.get("observer_camera_id") or "").strip()
    if observer_camera_id and not _can_auth(
        request,
        action="core:camera:read",
        camera_id=observer_camera_id,
    ):
        return False
    if profile_id in readable_profile_ids:
        return True
    return bool(observer_camera_id and store.get_profile(profile_id) is None)


def _can_auth(request: Request, *, action: str, camera_id: str = "") -> bool:
    auth = getattr(request.app.state, "auth", None)
    if not isinstance(auth, AuthRuntime):
        return True
    context = getattr(request.state, "auth_context", None)
    if not isinstance(context, AuthContext):
        return False
    try:
        _authorize_camera(
            auth,
            context,
            action=action,
            camera_id=camera_id,
        )
    except Exception:
        return False
    return True


def _require_auth(request: Request, *, action: str, camera_id: str = "") -> None:
    auth = getattr(request.app.state, "auth", None)
    if not isinstance(auth, AuthRuntime):
        return
    context = getattr(request.state, "auth_context", None)
    if not isinstance(context, AuthContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    _authorize_camera(
        auth,
        context=context,
        action=action,
        camera_id=camera_id,
    )


def _authorize_camera(
    auth: AuthRuntime,
    context: AuthContext,
    *,
    action: str,
    camera_id: str,
) -> AuthPrincipal:
    if camera_id:
        return auth.authorize(
            context=context,
            action=action,
            resource_type="core:camera",
            resource_selector=camera_id,
        )
    return auth.authorize(context=context, action=action)
