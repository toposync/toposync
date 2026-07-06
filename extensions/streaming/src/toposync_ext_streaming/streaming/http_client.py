from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import request as urllib_request


@dataclass(frozen=True, slots=True)
class StreamingHttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]


def build_auth_header(*, bearer_token: str = "", username: str = "", password: str = "") -> str:
    token = str(bearer_token or "").strip()
    if token:
        return f"Bearer {token}"
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return ""
    encoded = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


async def request_json(
    *,
    url: str,
    method: Literal["GET", "POST"] = "GET",
    body: dict[str, Any] | None = None,
    timeout_s: float = 6.0,
    bearer_token: str = "",
    username: str = "",
    password: str = "",
) -> dict[str, Any]:
    response = await request_raw(
        url=url,
        method=method,
        body=body,
        timeout_s=timeout_s,
        bearer_token=bearer_token,
        username=username,
        password=password,
        headers={"accept": "application/json"},
    )
    try:
        parsed = json.loads(response.body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError("Invalid JSON response") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Invalid JSON payload")
    return parsed


async def request_raw(
    *,
    url: str,
    method: Literal["GET", "POST"] = "GET",
    body: dict[str, Any] | None = None,
    timeout_s: float = 6.0,
    bearer_token: str = "",
    username: str = "",
    password: str = "",
    headers: dict[str, str] | None = None,
    return_http_error: bool = False,
) -> StreamingHttpResponse:
    def _do_request() -> StreamingHttpResponse:
        request_headers = dict(headers or {})
        auth_header = build_auth_header(
            bearer_token=bearer_token,
            username=username,
            password=password,
        )
        if auth_header:
            request_headers["authorization"] = auth_header
        data: bytes | None = None
        if body is not None:
            request_headers.setdefault("content-type", "application/json")
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib_request.Request(url=url, data=data, headers=request_headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=max(1.0, float(timeout_s))) as response:
                return StreamingHttpResponse(
                    status_code=int(getattr(response, "status", 200) or 200),
                    body=response.read(),
                    headers=_response_headers(response),
                )
        except urllib_error.HTTPError as exc:
            body_bytes = _read_http_error_body(exc)
            body_text = body_bytes.decode("utf-8", errors="replace").strip() or str(exc.reason or "")
            if return_http_error:
                return StreamingHttpResponse(
                    status_code=int(exc.code),
                    body=body_bytes,
                    headers=_response_headers(exc),
                )
            raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
        except urllib_error.URLError as exc:
            reason = str(getattr(exc, "reason", "") or exc)
            raise RuntimeError(f"Connection failed: {reason}") from exc

    return await asyncio.to_thread(_do_request)


def _response_headers(response: Any) -> dict[str, str]:
    raw_headers = getattr(response, "headers", None)
    if raw_headers is None:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in raw_headers.items()}
    except Exception:
        return {}


def _read_http_error_body(exc: urllib_error.HTTPError) -> bytes:
    try:
        return exc.read()
    except Exception:
        return b""
