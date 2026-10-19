"""Source authorization shared by history, retrieval and queued delivery."""
from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Any

from fastapi import HTTPException, Request

ReadFilter = Callable[[dict[str, Any]], bool]


def retained(payload: dict[str, Any]) -> bool:
    if payload.get("source") != "occurrences":
        return True
    data = payload.get("data")
    document = data.get("occurrence") if isinstance(data, dict) else None
    if not isinstance(document, dict):
        return False
    expiry = document.get("expires_at")
    return type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time() and document.get("status") in ("OPEN", "UPDATE", "CLOSE")


class NotificationAccess:
    def __init__(self, request: Request) -> None:
        self.request = request

    def principal(self, action: str = "core:notifications:read"):
        auth = self.request.app.state.auth
        # Streams and inference can outlive a session, role or grant.
        context = auth.resolve_request(self.request, refresh=False)
        return auth.authorize(context=context, action=action), context

    def __call__(self, payload: dict[str, Any]) -> bool:
        if not retained(payload):
            return False
        try:
            principal, context = self.principal()
            if principal.bypass or principal.role == "owner":
                return True
            scope = payload.get("access_scope")
            if not isinstance(scope, dict) or scope.get("complete") is not True:
                return False
            cameras, areas = scope.get("camera_ids"), scope.get("area_ids")
            if not isinstance(cameras, list) or not cameras or not isinstance(areas, list):
                return False
            for kind, identifiers in (("camera", cameras), ("area", areas)):
                if len(identifiers) > 128:
                    return False
                for identifier in identifiers:
                    if not isinstance(identifier, str) or not identifier or len(identifier) > 256 or "*" in identifier:
                        return False
                    self.request.app.state.auth.authorize(
                        context=context, action=f"core:{kind}:read",
                        resource_type=f"core:{kind}", resource_selector=identifier,
                    )
            return True
        except HTTPException:
            return False
