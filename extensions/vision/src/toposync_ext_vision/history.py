"""Public object categories in notification history; no identity-gallery access."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from toposync.runtime.notifications.store import parse_data_filter
from toposync.runtime.notifications.time_query import resolve_history_time


def matching_category_ids(store, category: str, *, cancel_check=None) -> list[str]:
    if category not in {"person", "dog", "cat"}:
        raise ValueError("Unsupported object category")
    path = f"$.data.category_summary.categories.{category}.member_count"

    def read(connection):
        return connection.execute("""
            WITH visible AS (
                SELECT id, CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END AS payload
                FROM notification WHERE priority_bucket IN ('low', 'medium', 'high')
            )
            SELECT id FROM visible
            WHERE json_type(payload, ?) IN ('integer', 'real') AND json_extract(payload, ?) > 0
        """, (path, path)).fetchall()

    return [row["id"] for row in store._run_read(read, cancel_check=cancel_check)]


def create_history_router() -> APIRouter:
    router = APIRouter(prefix="/api/vision/history", tags=["vision"])

    @router.get("/notifications")
    async def notifications(
        request: Request, response: Response, category: Literal["person", "dog", "cat"],
        query: str = Query("", max_length=1000), mode: Literal["literal", "text", "visual"] = "literal",
        limit: int = Query(40, ge=1, le=50), before: int | None = Query(None, ge=0),
        priority: list[str] | None = Query(None), notification_type: list[str] | None = Query(None, alias="type"),
        data_filter: str | None = Query(None, max_length=8192),
        created_after: float | None = Query(None, ge=0, allow_inf_nan=False),
        created_before: float | None = Query(None, ge=0, allow_inf_nan=False),
    ):
        from toposync.app import _run_cancelable_request_work

        auth = request.app.state.auth
        context = request.state.auth_context
        auth.authorize(context=context, action="core:extension:use", resource_type="core:extension",
                       resource_selector="com.toposync.vision")
        auth.authorize(context=context, action="core:notifications:read", resource_type=None, resource_selector="*")
        response.headers["Cache-Control"] = "private, no-store"
        try:
            filters = parse_data_filter(data_filter)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if created_after is not None and created_before is not None and created_after >= created_before:
            raise HTTPException(422, "Notification time interval must have positive duration")
        interpretation = None
        if created_after is not None or created_before is not None:
            query, _ = resolve_history_time(query, None, explicit_range=True)
        if created_after is None and created_before is None:
            settings = await request.app.state.config_store.get_settings()
            query, interpretation = resolve_history_time(query, settings.core.get("timezone"))
            if interpretation and interpretation.get("reason"):
                return {"notifications": [], "next_cursor": None,
                        "search": {"mode": "literal", "status": "degraded", **interpretation}}
            if interpretation:
                created_after = interpretation["time_range"]["created_after"]
                created_before = interpretation["time_range"]["created_before"]
        runtime = request.app.state.notifications
        parameters = dict(limit=limit, priorities=priority, types=notification_type, data_filter=filters,
                          created_after=created_after, created_before=created_before)

        def read(check_cancelled):
            identifiers = matching_category_ids(runtime.store, category, cancel_check=check_cancelled)
            if mode != "literal" and query.strip():
                return identifiers
            items, cursor = runtime.list_sync(query=query, before=before, notification_ids=identifiers,
                                              cancel_check=check_cancelled, **parameters)
            return {"notifications": items, "next_cursor": cursor,
                    "search": {"mode": "literal", "status": "ready", **(interpretation or {})}}

        result = await _run_cancelable_request_work(request, read,
            limiter=request.app.state.notification_read_limiters["list"])
        if isinstance(result, dict):
            return result
        service = "ai.search_visual_notifications" if mode == "visual" else "ai.search_notifications"
        try:
            selected = await request.app.state.services.call(service, query=query, notification_ids=result, **parameters)
            selected["next_cursor"] = None
        except KeyError:
            items, cursor = await runtime.list(query=query, before=before, notification_ids=result, **parameters)
            selected = {"notifications": items, "next_cursor": cursor,
                        "search": {"mode": "literal", "status": "degraded"}}
        selected["search"].update(interpretation or {})
        return selected

    return router
