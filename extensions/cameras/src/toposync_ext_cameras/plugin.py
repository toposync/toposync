from __future__ import annotations

import asyncio
import hashlib
import math
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
from toposync.runtime.config_store import ConfigStore, Pipeline, PipelineAlreadyExistsError, PipelineValidationError
from toposync.runtime.event_bus import EventBus
from toposync.runtime.pipelines.compiler import GraphCompileError, PipelineGraphCompiler
from toposync.runtime.pipelines.operator_registry import OperatorRegistry
from toposync.runtime.pipelines.templates import safe_pipeline_name
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


class CameraPipelineWizardRequest(BaseModel):
    preset: Literal["people", "vehicles_stopped", "pets"]
    pipeline_name: str = ""
    enabled: bool = True
    processing_server_id: str = "local"
    composition_id: str = ""
    area_id: str = ""
    notification_title: str = ""
    notification_description: str = ""


class CameraPipelineWizardResponse(BaseModel):
    pipeline_name: str


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

        def _unique_pipeline_name(base: str, *, existing_names: set[str]) -> str:
            base_safe = safe_pipeline_name(base)
            if base_safe not in existing_names:
                return base_safe
            suffix = 2
            while True:
                candidate = safe_pipeline_name(f"{base_safe}_{suffix}")
                if candidate not in existing_names:
                    return candidate
                suffix += 1

        def _default_mapping_composition_id(cfg: Any, *, camera_id: str) -> str | None:
            cid = str(camera_id or "").strip()
            if not cid:
                return None
            for composition in getattr(cfg, "compositions", []):
                for element in getattr(composition, "elements", []):
                    props = element.props if isinstance(getattr(element, "props", None), dict) else {}
                    if str(props.get("camera_id", "")).strip() != cid:
                        continue
                    pairs = _parse_control_point_pairs(props.get("control_points"))
                    if len(pairs) >= 4:
                        return str(getattr(composition, "id", "") or "").strip() or None
            return None

        def _resolve_area_polygon(cfg: Any, *, composition_id: str, area_id: str) -> tuple[str, list[dict[str, float]]]:
            comp_id = str(composition_id or "").strip()
            aid = str(area_id or "").strip()
            if not comp_id or not aid:
                raise ValueError("composition_id and area_id are required")

            for composition in getattr(cfg, "compositions", []):
                if str(getattr(composition, "id", "") or "").strip() != comp_id:
                    continue
                for element in getattr(composition, "elements", []):
                    if str(getattr(element, "id", "") or "").strip() != aid:
                        continue
                    if str(getattr(element, "type", "") or "").strip() != "com.toposync.structural.area":
                        raise ValueError("Selected element is not an area")
                    props = element.props if isinstance(getattr(element, "props", None), dict) else {}
                    vertices = props.get("vertices")
                    if not isinstance(vertices, list) or len(vertices) < 3:
                        raise ValueError("Area is missing vertices")
                    points: list[dict[str, float]] = []
                    for vertex in vertices:
                        if not isinstance(vertex, dict):
                            continue
                        try:
                            x = float(vertex.get("x"))
                            z = float(vertex.get("z"))
                        except Exception:
                            continue
                        if not math.isfinite(x) or not math.isfinite(z):
                            continue
                        points.append({"x": x, "z": z})
                    if len(points) < 3:
                        raise ValueError("Area vertices are invalid")
                    name = str(getattr(element, "name", "") or "").strip() or aid
                    return name, points
                raise ValueError("Unknown area_id in composition")
            raise ValueError("Unknown composition_id")

        def _build_wizard_graph(
            *,
            preset: str,
            camera_id: str,
            composition_id: str,
            area_name: str,
            area_points: list[dict[str, float]],
            notification_title: str,
            notification_description: str,
        ) -> dict[str, Any]:
            motion_hold_seconds = 6.0
            if preset == "vehicles_stopped":
                motion_hold_seconds = 10.0

            base_nodes: list[dict[str, Any]] = [
                {"id": "source", "operator": "camera.source", "config": {"camera_id": camera_id}},
                {
                    "id": "motion",
                    "operator": "camera.motion_gate",
                    "config": {
                        "threshold": 0.010,
                        "activation_frames": 2,
                        "hold_seconds": motion_hold_seconds,
                        "emit_when_idle": preset == "vehicles_stopped",
                    },
                },
            ]

            if preset == "people":
                nodes = [
                    *base_nodes,
                    {
                        "id": "track",
                        "operator": "vision.object_tracking_yolo",
                        "config": {
                            "categories": ["person"],
                            "close_after_seconds": 5.0,
                            "confidence_threshold": 0.55,
                        },
                    },
                    {"id": "map", "operator": "camera.camera_mapping", "config": {}},
                    {"id": "throttle", "operator": "core.throttle", "config": {"interval_seconds": 5.0}},
                    {"id": "segment", "operator": "camera.object_segmentation", "config": {}},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {"image_with_fallback": "best_frame,original,treated,segmented", "subdir": "pipelines", "format": "png"},
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: Person detected",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "medium",
                            "thumbnail_with_fallback": ["best_frame", "original", "treated", "segmented"],
                        },
                    },
                ]
                edges = [
                    {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "motion", "port": "out"}, "to": {"node": "track", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "track", "port": "out"}, "to": {"node": "map", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "map", "port": "out"}, "to": {"node": "throttle", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "throttle", "port": "out"}, "to": {"node": "segment", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "segment", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                    {"from": {"node": "store", "port": "out"}, "to": {"node": "notify", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            if preset == "pets":
                nodes = [
                    *base_nodes,
                    {"id": "track", "operator": "vision.object_tracking_yolo", "config": {"categories": ["cat", "dog"], "close_after_seconds": 5.0}},
                    {"id": "map", "operator": "camera.camera_mapping", "config": {}},
                    {"id": "throttle", "operator": "core.throttle", "config": {"interval_seconds": 8.0}},
                    {"id": "segment", "operator": "camera.object_segmentation", "config": {"padding_ratio": 0.12}},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {"image_with_fallback": "best_frame,original,treated,segmented", "subdir": "pipelines", "format": "png"},
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.tracking",
                            "title": notification_title or "{{camera_name}}: Pet detected",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "medium",
                            "thumbnail_with_fallback": ["best_frame", "original", "treated", "segmented"],
                        },
                    },
                ]
                edges = [
                    {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "motion", "port": "out"}, "to": {"node": "track", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "track", "port": "out"}, "to": {"node": "map", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "map", "port": "out"}, "to": {"node": "throttle", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "throttle", "port": "out"}, "to": {"node": "segment", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "segment", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                    {"from": {"node": "store", "port": "out"}, "to": {"node": "notify", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            if preset == "vehicles_stopped":
                if not composition_id:
                    raise ValueError("composition_id is required for vehicles_stopped")

                nodes = [
                    *base_nodes,
                    {
                        "id": "track",
                        "operator": "vision.object_tracking_yolo",
                        "config": {
                            "categories": ["car", "motorcycle", "bicycle"],
                            "close_after_seconds": 8.0,
                            "confidence_threshold": 0.55,
                            "default_interval_seconds": 0.25,
                            "inference_interval_seconds": 0.7,
                            "pause_when_gate_closed": True,
                            "max_paused_seconds": 900.0,
                        },
                    },
                    {"id": "map", "operator": "camera.camera_mapping", "config": {"composition_id": composition_id}},
                    {
                        "id": "area",
                        "operator": "camera.area_restriction",
                        "config": (
                            {
                                "areas": [{"name": area_name, "points": area_points}],
                                "include_area_names": [area_name],
                                "drop_when_unmapped": True,
                            }
                            if area_name and area_points
                            else {"areas": [], "include_area_names": [], "drop_when_unmapped": True}
                        ),
                    },
                    {
                        "id": "velocity",
                        "operator": "camera.velocity_estimation",
                        "config": {
                            "filter_mode": "stopped_now",
                            "min_elapsed_seconds": 0.05,
                            "stopped_speed_threshold": 0.07,
                        },
                    },
                    {"id": "throttle", "operator": "core.velocity_throttle", "config": {"moving_interval_seconds": 2.5, "stopped_interval_seconds": 120.0}},
                    {"id": "segment", "operator": "camera.object_segmentation", "config": {"padding_ratio": 0.16}},
                    {
                        "id": "store",
                        "operator": "core.store_images",
                        "config": {"image_with_fallback": "best_frame,original,treated,segmented", "subdir": "pipelines", "format": "png"},
                    },
                    {
                        "id": "notify",
                        "operator": "core.notify",
                        "config": {
                            "notification_type": "pipelines.event",
                            "title": notification_title or "{{camera_name}}: Vehicle stopped",
                            "description": notification_description or "{{camera_name}}",
                            "priority": "high",
                            "thumbnail_with_fallback": ["best_frame", "original", "treated", "segmented"],
                        },
                    },
                ]
                edges = [
                    {"from": {"node": "source", "port": "out"}, "to": {"node": "motion", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "motion", "port": "out"}, "to": {"node": "track", "port": "in"}, "maxsize": 2, "drop_policy": "drop_oldest"},
                    {"from": {"node": "track", "port": "out"}, "to": {"node": "map", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "map", "port": "out"}, "to": {"node": "area", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "area", "port": "out"}, "to": {"node": "velocity", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "velocity", "port": "out"}, "to": {"node": "throttle", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "throttle", "port": "out"}, "to": {"node": "segment", "port": "in"}, "maxsize": 8, "drop_policy": "drop_oldest"},
                    {"from": {"node": "segment", "port": "out"}, "to": {"node": "store", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                    {"from": {"node": "store", "port": "out"}, "to": {"node": "notify", "port": "in"}, "maxsize": 16, "drop_policy": "drop_oldest"},
                ]
                return {"schema_version": 1, "nodes": nodes, "edges": edges}

            raise ValueError("Unknown preset")

        @app.post("/api/cameras/cameras/{camera_id}/pipeline-wizard", response_model=CameraPipelineWizardResponse)
        async def create_camera_pipeline_from_wizard(
            request: Request,
            camera_id: str,
            body: CameraPipelineWizardRequest,
        ) -> CameraPipelineWizardResponse:
            cid = str(camera_id or "").strip()
            if not cid:
                raise HTTPException(status_code=400, detail="camera_id is required")

            preset = str(body.preset or "").strip()
            if preset not in {"people", "vehicles_stopped", "pets"}:
                raise HTTPException(status_code=400, detail="preset must be one of: people, vehicles_stopped, pets")

            store = _config_store(request)
            compiler: PipelineGraphCompiler = request.app.state.pipeline_graph_compiler

            ext = await _read_ext_settings(request)
            raw_cameras = ext.get("cameras", [])
            if not isinstance(raw_cameras, list):
                raise HTTPException(status_code=404, detail="Unknown camera")
            if not any(isinstance(item, dict) and str(item.get("id", "")).strip() == cid for item in raw_cameras):
                raise HTTPException(status_code=404, detail="Unknown camera")

            cfg = await store.get_config()

            composition_id = str(body.composition_id or "").strip()
            area_id = str(body.area_id or "").strip()
            area_name = ""
            area_points: list[dict[str, float]] = []

            if preset == "vehicles_stopped":
                if not composition_id:
                    composition_id = _default_mapping_composition_id(cfg, camera_id=cid) or ""
                if not composition_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Vehicle preset requires camera mapping. Add control points (>=4) in a composition first.",
                    )
                if area_id:
                    try:
                        area_name, area_points = _resolve_area_polygon(cfg, composition_id=composition_id, area_id=area_id)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc

            requested_name = str(body.pipeline_name or "").strip()
            existing_names = {p.name for p in await store.list_pipelines()}

            if requested_name:
                pipeline_name = safe_pipeline_name(requested_name)
                if pipeline_name in existing_names:
                    raise HTTPException(status_code=409, detail=f"Pipeline already exists: {pipeline_name}")
            else:
                pipeline_name = _unique_pipeline_name(f"camera_{cid}__{preset}", existing_names=existing_names)

            try:
                graph = _build_wizard_graph(
                    preset=preset,
                    camera_id=cid,
                    composition_id=composition_id,
                    area_name=area_name,
                    area_points=area_points,
                    notification_title=str(body.notification_title or "").strip(),
                    notification_description=str(body.notification_description or "").strip(),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            processing_server_id = str(body.processing_server_id or "").strip() or "local"
            pipeline = Pipeline(
                name=pipeline_name,
                type="final",
                enabled=bool(body.enabled),
                processing_server_id=processing_server_id,
                editor_mode="interactive",
                python_source="",
                graph=graph,
            )

            try:
                compiler.compile_pipeline(pipeline)
            except GraphCompileError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            try:
                await store.create_pipeline(pipeline)
            except PipelineAlreadyExistsError:
                raise HTTPException(status_code=409, detail=f"Pipeline already exists: {pipeline_name}") from None
            except PipelineValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            orchestrator = getattr(request.app.state, "pipelines_orchestrator", None)
            if orchestrator is not None:
                try:
                    orchestrator.trigger_reload()
                except Exception:
                    pass

            return CameraPipelineWizardResponse(pipeline_name=pipeline_name)
