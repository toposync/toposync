from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from toposync.extensions import BaseExtension
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.services import ServiceRegistry

from .pipelines import register_camera_pipeline_operators
from .processing.mapping import ControlPointMapper, ControlPointPair
from .pipelines.postprocess import _parse_control_point_pairs  # noqa: PLC2701


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

        snapshot_cache: dict[str, SnapshotCacheEntry] = {}
        snapshot_locks: dict[str, asyncio.Lock] = {}
        snapshot_cache_ttl_s = float(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_TTL_S", "0.8") or "0.8")
        snapshot_ffmpeg_concurrency = int(os.getenv("TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY", "2") or "2")
        snapshot_ffmpeg_sema = asyncio.Semaphore(max(1, snapshot_ffmpeg_concurrency))

        def _get_lock(key: str) -> asyncio.Lock:
            lock = snapshot_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                snapshot_locks[key] = lock
            return lock

        @app.get("/api/cameras/index")
        async def cameras_index(request: Request) -> dict[str, Any]:
            ext = await _read_ext_settings(request)
            raw_cameras = ext.get("cameras", [])

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
                        }
                    )

            return {"cameras": cameras}

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

        @app.get("/api/cameras/cameras/{camera_id}/contexts")
        async def camera_contexts(request: Request, camera_id: str) -> dict[str, Any]:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            store = _config_store(request)
            cfg = await store.get_config()

            compositions_out: list[dict[str, Any]] = []
            for composition in cfg.compositions:
                camera_elements: list[dict[str, Any]] = []
                for element in composition.elements:
                    props = element.props if isinstance(element.props, dict) else {}
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    pairs = _parse_control_point_pairs(props.get("control_points"))
                    camera_elements.append(
                        {
                            "id": element.id,
                            "name": str(element.name or "").strip() or element.id,
                            "control_points_pairs": len(pairs),
                            "has_mapping": len(pairs) >= 4,
                        }
                    )

                if not camera_elements:
                    continue

                areas: list[dict[str, Any]] = []
                for element in composition.elements:
                    if str(element.type or "").strip() != "com.toposync.structural.area":
                        continue
                    props = element.props if isinstance(element.props, dict) else {}
                    vertices = props.get("vertices")
                    if not isinstance(vertices, list) or len(vertices) < 3:
                        continue
                    name = str(element.name or "").strip()
                    areas.append(
                        {
                            "id": element.id,
                            "name": name or element.id,
                            "vertices_count": len(vertices),
                        }
                    )

                compositions_out.append(
                    {
                        "id": composition.id,
                        "name": composition.name,
                        "camera_elements": camera_elements,
                        "areas": areas,
                    }
                )

            return {"camera_id": cid, "compositions": compositions_out}
