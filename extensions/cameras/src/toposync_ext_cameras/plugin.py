from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from toposync.extensions import BaseExtension
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .pipelines import register_camera_pipeline_operators
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


@dataclass(frozen=True, slots=True)
class SnapshotCacheEntry:
    blob: bytes
    created_ts: float
    frame_ts: float
    headers: dict[str, str]


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
        registry = getattr(app.state, "pipeline_operator_registry", None)
        if isinstance(registry, OperatorRegistry):
            register_camera_pipeline_operators(registry)

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
            start_legacy_runtime = True
            if str(os.getenv("TOPOSYNC_ROLE") or "").strip().lower() == "processing":
                start_legacy_runtime = False
            else:
                try:
                    start_legacy_runtime = not bool(await config_store.get_pipelines_feature_flag())
                except Exception:
                    start_legacy_runtime = True

            if start_legacy_runtime:
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
        else:
            runtime = None

        snapshot_cache: dict[str, SnapshotCacheEntry] = {}
        snapshot_locks: dict[str, asyncio.Lock] = {}
        snapshot_cache_ttl_s = float(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_TTL_S", "0.8") or "0.8")
        snapshot_max_frame_age_s = float(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_MAX_FRAME_AGE_S", "5.0") or "5.0")
        snapshot_ffmpeg_concurrency = int(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY", "2") or "2")
        snapshot_ffmpeg_sema = asyncio.Semaphore(max(1, snapshot_ffmpeg_concurrency))
        remote_snapshot_timeout_s = float(os.getenv("TOPOSYNC_CAMERA_REMOTE_SNAPSHOT_TIMEOUT_S", "5.0") or "5.0")
        remote_http = httpx.AsyncClient(timeout=remote_snapshot_timeout_s)
        app.add_event_handler("shutdown", remote_http.aclose)

        def _get_lock(key: str) -> asyncio.Lock:
            lock = snapshot_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                snapshot_locks[key] = lock
            return lock

        async def _encode_jpeg(frame: Any) -> bytes | None:
            def _work() -> bytes | None:
                try:
                    import cv2  # type: ignore
                except Exception:
                    return None
                try:
                    ok, buf = cv2.imencode(".jpg", frame)
                except Exception:
                    return None
                if not ok or buf is None:
                    return None
                try:
                    return buf.tobytes()
                except Exception:
                    return None

            return await asyncio.to_thread(_work)

        async def _fetch_remote_snapshot(server_url: str, camera_id: str) -> bytes | None:
            base = str(server_url or "").strip().rstrip("/")
            if not base:
                return None
            cid = str(camera_id or "").strip()
            if not cid:
                return None
            url = f"{base}/api/processor/cameras/{urllib.parse.quote(cid)}/snapshot"
            try:
                res = await remote_http.get(url)
            except Exception:
                return None
            if res.status_code != 200:
                return None
            content = res.content
            return content if content else None

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

            key = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:24]
            cache_key = f"rtsp:{key}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(content=cached.blob, media_type="image/jpeg", headers=cached.headers)

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(url, timeout_ms=body.timeout_ms)
            headers = {
                "Cache-Control": "no-store",
                "X-Toposync-Snapshot-Source": result.source,
                "X-Toposync-Snapshot-Transport": result.transport,
            }
            snapshot_cache[cache_key] = SnapshotCacheEntry(
                blob=result.blob,
                created_ts=time.time(),
                frame_ts=time.time(),
                headers=headers,
            )
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

            cache_key = f"cam:{cid}"
            lock = _get_lock(cache_key)
            async with lock:
                now = time.time()
                cached = snapshot_cache.get(cache_key)
                if cached and (now - cached.created_ts) <= snapshot_cache_ttl_s:
                    return Response(content=cached.blob, media_type="image/jpeg", headers=cached.headers)

                if runtime is not None:
                    frame, frame_ts = runtime.get_latest_frame(cid)
                    if frame is not None and frame_ts:
                        age_s = max(0.0, now - float(frame_ts))
                        if age_s <= snapshot_max_frame_age_s:
                            blob = await _encode_jpeg(frame)
                            if blob:
                                headers = {
                                    "Cache-Control": "no-store",
                                    "X-Toposync-Snapshot-Source": "local_grabber",
                                    "X-Toposync-Snapshot-Frame-Age-Ms": str(int(age_s * 1000)),
                                }
                                snapshot_cache[cache_key] = SnapshotCacheEntry(
                                    blob=blob,
                                    created_ts=time.time(),
                                    frame_ts=float(frame_ts),
                                    headers=headers,
                                )
                                return Response(content=blob, media_type="image/jpeg", headers=headers)

                processing_server_id = str(camera.get("processing_server_id", "")).strip()
                if processing_server_id:
                    servers_raw = ext.get("processing_servers", [])
                    server_url = ""
                    if isinstance(servers_raw, list):
                        for s in servers_raw:
                            if not isinstance(s, dict):
                                continue
                            if str(s.get("id", "")).strip() == processing_server_id:
                                server_url = str(s.get("url", "")).strip()
                                break

                    remote_blob = await _fetch_remote_snapshot(server_url, cid)
                    if remote_blob:
                        headers = {
                            "Cache-Control": "no-store",
                            "X-Toposync-Snapshot-Source": f"remote[{processing_server_id}]",
                        }
                        snapshot_cache[cache_key] = SnapshotCacheEntry(
                            blob=remote_blob,
                            created_ts=time.time(),
                            frame_ts=time.time(),
                            headers=headers,
                        )
                        return Response(content=remote_blob, media_type="image/jpeg", headers=headers)

                url_raw = str(camera.get("rtsp_url", "")).strip()
                username = str(camera.get("username", "")).strip()
                password = str(camera.get("password", "")).strip()
                if not url_raw:
                    raise HTTPException(status_code=400, detail="Camera RTSP URL is not configured")

                try:
                    url = _rtsp_url_with_auth(url_raw, username, password)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                async with snapshot_ffmpeg_sema:
                    result = await _ffmpeg_snapshot(url, timeout_ms=9000)
                headers = {
                    "Cache-Control": "no-store",
                    "X-Toposync-Snapshot-Source": result.source,
                    "X-Toposync-Snapshot-Transport": result.transport,
                }
                snapshot_cache[cache_key] = SnapshotCacheEntry(
                    blob=result.blob,
                    created_ts=time.time(),
                    frame_ts=time.time(),
                    headers=headers,
                )
                return Response(content=result.blob, media_type="image/jpeg", headers=headers)
