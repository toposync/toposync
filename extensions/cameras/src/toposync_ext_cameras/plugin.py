from __future__ import annotations

import asyncio
import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from toposync.extensions import BaseExtension
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry

from .processing.mapping import ControlPointMapper, ControlPointPair
from .processing.runtime import CamerasProcessingRuntime


EXTENSION_ID = "com.toposync.cameras"


class RtspSnapshotRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=9000, ge=1500, le=30000)


class ControlPointMapImage(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ControlPointMapWorld(BaseModel):
    x: float
    z: float


class ControlPointMapPair(BaseModel):
    image: ControlPointMapImage
    world: ControlPointMapWorld


class ControlPointMapQuery(BaseModel):
    kind: Literal["image", "world"]
    x: float
    y: float | None = None
    z: float | None = None


class ControlPointMapRequest(BaseModel):
    pairs: list[ControlPointMapPair]
    query: ControlPointMapQuery


def _rtsp_url_with_auth(url: str, username: str, password: str) -> str:
    raw = url.strip()
    if not raw:
        raise ValueError("Missing RTSP URL")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() != "rtsp" or not parsed.netloc:
        raise ValueError("RTSP URL must start with rtsp://")

    if "@" in parsed.netloc:
        return raw

    user = username.strip()
    pwd = password.strip()
    if not user and not pwd:
        return raw

    user_enc = urllib.parse.quote(user, safe="")
    pwd_enc = urllib.parse.quote(pwd, safe="")

    host = parsed.netloc
    if pwd_enc:
        netloc = f"{user_enc}:{pwd_enc}@{host}"
    else:
        netloc = f"{user_enc}@{host}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def _redact_rtsp_credentials(text: str) -> str:
    # Redact userinfo in RTSP URLs: rtsp://user:pass@host -> rtsp://***@host
    return re.sub(r"rtsp://[^@\s]+@", "rtsp://***@", text)


def _rtsp_stream2_fallback(rtsp_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(rtsp_url)
    except Exception:
        return None

    path = parsed.path or ""
    trailing = "/" if path.endswith("/") else ""
    stripped = path.rstrip("/")
    if not stripped.endswith("/stream1"):
        return None

    base = stripped[: -len("/stream1")]
    new_path = f"{base}/stream2{trailing}"
    return urllib.parse.urlunsplit(parsed._replace(path=new_path))


@dataclass(frozen=True, slots=True)
class RtspSnapshotResult:
    blob: bytes
    source: str
    transport: str


async def _ffmpeg_snapshot(rtsp_url: str, *, timeout_ms: int) -> RtspSnapshotResult:
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=500, detail="ffmpeg is required to capture RTSP snapshots")

    timeout_s = max(1.5, timeout_ms / 1000)
    timeout_us = int(max(0, timeout_ms) * 1000)

    # Some RTSP servers misbehave when clients negotiate audio+video; for snapshots we only need video.
    # Also, a few servers only work reliably over UDP even when TCP is requested.
    attempts: list[tuple[str, list[str]]] = [
        ("tcp", ["-rtsp_transport", "tcp"]),
        ("udp", ["-rtsp_transport", "udp"]),
    ]

    url_candidates: list[tuple[str, str]] = [("configured", rtsp_url)]
    stream2 = _rtsp_stream2_fallback(rtsp_url)
    if stream2 and stream2 != rtsp_url:
        url_candidates.append(("fallback_stream2", stream2))

    last_error = "Failed to capture RTSP snapshot"

    for source, url in url_candidates:
        for name, rtsp_args in attempts:
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-timeout",
                str(timeout_us),
                *rtsp_args,
                "-allowed_media_types",
                "video",
                "-i",
                url,
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 2.0)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                last_error = f"Snapshot timed out (transport={name}, source={source})"
                continue

            if proc.returncode == 0 and stdout:
                return RtspSnapshotResult(blob=stdout, source=source, transport=name)

            message = (stderr or b"").decode("utf-8", errors="ignore").strip()
            message = _redact_rtsp_credentials(message)
            if message:
                last_error = f"{message} (transport={name}, source={source})"
            else:
                last_error = f"Failed to capture RTSP snapshot (transport={name}, source={source})"

    raise HTTPException(status_code=502, detail=last_error)


class CamerasExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_cameras")

    async def setup(self, app: FastAPI, *, bus: EventBus, services: ServiceRegistry) -> None:  # noqa: ARG002
        def _config_store(request: Request) -> ConfigStore:
            store = getattr(request.app.state, "config_store", None)
            if store is None:
                raise RuntimeError("Toposync config_store not available")
            return store

        async def _read_ext_settings(request: Request) -> dict[str, Any]:
            settings = await _config_store(request).get_settings()
            ext = settings.extensions.get(EXTENSION_ID, {})
            return ext if isinstance(ext, dict) else {}

        config_store = getattr(app.state, "config_store", None)
        if isinstance(config_store, ConfigStore):
            runtime = CamerasProcessingRuntime(
                config_store=config_store,
                extension_id=EXTENSION_ID,
                data_dir=config_store.paths.data_dir,
                files_dir=config_store.paths.files_dir,
                services=services,
            )
            runtime.start()

            async def _stop_runtime() -> None:
                await runtime.stop()

            app.add_event_handler("shutdown", _stop_runtime)
            app.state.cameras_processing = runtime
        else:
            runtime = None

        @app.get("/api/cameras/index")
        async def cameras_index(request: Request) -> dict[str, Any]:
            ext = await _read_ext_settings(request)

            raw_servers = ext.get("processing_servers", [])
            raw_cameras = ext.get("cameras", [])

            servers: list[dict[str, Any]] = []
            if isinstance(raw_servers, list):
                for s in raw_servers:
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get("id", "")).strip()
                    if not sid:
                        continue
                    servers.append(
                        {
                            "id": sid,
                            "name": str(s.get("name", "")).strip(),
                            "url": str(s.get("url", "")).strip(),
                        }
                    )

            cameras: list[dict[str, Any]] = []
            if isinstance(raw_cameras, list):
                for c in raw_cameras:
                    if not isinstance(c, dict):
                        continue
                    cid = str(c.get("id", "")).strip()
                    if not cid:
                        continue
                    cameras.append(
                        {
                            "id": cid,
                            "name": str(c.get("name", "")).strip(),
                            "connection_type": str(c.get("connection_type", "rtsp")).strip() or "rtsp",
                            "processing_server_id": str(c.get("processing_server_id", "")).strip(),
                        }
                    )

            return {"processing_servers": servers, "cameras": cameras}

        @app.get("/api/cameras/detections/recent")
        async def recent_detections(
            request: Request,
            camera_id: str | None = None,
            composition_id: str | None = None,
            tracking_id: str | None = None,
            limit: int = 200,
        ) -> dict[str, Any]:
            if runtime is None:
                return {"events": []}
            cam = (camera_id or "").strip() or None
            comp = (composition_id or "").strip() or None
            track = (tracking_id or "").strip() or None
            return {"events": runtime.db.list_events(camera_id=cam, composition_id=comp, tracking_id=track, limit=limit)}

        @app.get("/api/cameras/processing/status")
        async def processing_status() -> dict[str, Any]:
            if runtime is None:
                return {"local_workers": [], "remote_servers": []}
            return runtime.status()

        @app.get("/api/cameras/detections/stream")
        async def detections_stream(request: Request) -> StreamingResponse:
            if runtime is None:
                async def empty_stream():
                    yield "event: ready\ndata: {}\n\n"
                return StreamingResponse(empty_stream(), media_type="text/event-stream")

            q = runtime.broadcaster.subscribe()

            async def gen():
                try:
                    yield "retry: 1000\n\n"
                    while True:
                        event = await q.get()
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.CancelledError:
                    raise
                finally:
                    runtime.broadcaster.unsubscribe(q)

            return StreamingResponse(gen(), media_type="text/event-stream")

        @app.post("/api/cameras/control_points/map")
        async def map_control_points(body: ControlPointMapRequest) -> dict[str, Any]:
            pairs = [
                ControlPointPair(
                    image_u=float(p.image.x),
                    image_v=float(p.image.y),
                    world_x=float(p.world.x),
                    world_z=float(p.world.z),
                )
                for p in body.pairs
            ]
            if len(pairs) < 4:
                return {"world": None} if body.query.kind == "image" else {"image": None}

            try:
                mapper = ControlPointMapper(pairs)
            except RuntimeError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except Exception:
                return {"world": None} if body.query.kind == "image" else {"image": None}

            if body.query.kind == "image":
                if body.query.y is None:
                    raise HTTPException(status_code=400, detail="y is required for image mapping")
                u = float(body.query.x)
                v = float(body.query.y)
                if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                    return {"world": None}
                mapped = mapper.map(u, v)
                if mapped is None:
                    return {"world": None}
                x, z = mapped
                return {"world": {"x": x, "z": z}}

            if body.query.z is None:
                raise HTTPException(status_code=400, detail="z is required for world mapping")
            x = float(body.query.x)
            z = float(body.query.z)
            mapped = mapper.map_world_to_image(x, z)
            if mapped is None:
                return {"image": None}
            u, v = mapped
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                return {"image": None}
            return {"image": {"x": u, "y": v}}

        @app.post("/api/cameras/rtsp/snapshot")
        async def rtsp_snapshot(body: RtspSnapshotRequest) -> Response:
            try:
                url = _rtsp_url_with_auth(body.url, body.username, body.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            result = await _ffmpeg_snapshot(url, timeout_ms=body.timeout_ms)
            headers = {
                "Cache-Control": "no-store",
                "X-Toposync-Snapshot-Source": result.source,
                "X-Toposync-Snapshot-Transport": result.transport,
            }
            return Response(content=result.blob, media_type="image/jpeg", headers=headers)

        @app.get("/api/cameras/cameras/{camera_id}/snapshot")
        async def camera_snapshot(request: Request, camera_id: str) -> Response:
            cid = camera_id.strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            ext = await _read_ext_settings(request)
            raw_cameras = ext.get("cameras", [])
            if not isinstance(raw_cameras, list):
                raise HTTPException(status_code=404, detail="Unknown camera")

            camera: dict[str, Any] | None = None
            for item in raw_cameras:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id", "")).strip() == cid:
                    camera = item
                    break
            if camera is None:
                raise HTTPException(status_code=404, detail="Unknown camera")

            ctype = str(camera.get("connection_type", "rtsp")).strip().lower() or "rtsp"
            if ctype != "rtsp":
                raise HTTPException(status_code=400, detail="Only RTSP cameras are supported for now")

            url_raw = str(camera.get("rtsp_url", "")).strip()
            username = str(camera.get("username", "")).strip()
            password = str(camera.get("password", "")).strip()
            if not url_raw:
                raise HTTPException(status_code=400, detail="Camera RTSP URL is not configured")

            try:
                url = _rtsp_url_with_auth(url_raw, username, password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            result = await _ffmpeg_snapshot(url, timeout_ms=9000)
            headers = {
                "Cache-Control": "no-store",
                "X-Toposync-Snapshot-Source": result.source,
                "X-Toposync-Snapshot-Transport": result.transport,
            }
            return Response(content=result.blob, media_type="image/jpeg", headers=headers)
