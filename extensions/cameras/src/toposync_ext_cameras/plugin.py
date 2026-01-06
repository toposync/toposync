from __future__ import annotations

import asyncio
import shutil
import urllib.parse
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from toposync.extensions import BaseExtension
from toposync.runtime.config_store import ConfigStore
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry


EXTENSION_ID = "com.toposync.cameras"


class RtspSnapshotRequest(BaseModel):
    url: str
    username: str = ""
    password: str = ""
    timeout_ms: int = Field(default=9000, ge=1500, le=30000)


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


async def _ffmpeg_snapshot(rtsp_url: str, *, timeout_ms: int) -> bytes:
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=500, detail="ffmpeg is required to capture RTSP snapshots")

    timeout_s = max(1.5, timeout_ms / 1000)

    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
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
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="Snapshot timed out") from exc

    if proc.returncode != 0 or not stdout:
        message = (stderr or b"").decode("utf-8", errors="ignore").strip()
        raise HTTPException(status_code=502, detail=message or "Failed to capture RTSP snapshot")

    return stdout


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

        @app.post("/api/cameras/rtsp/snapshot")
        async def rtsp_snapshot(body: RtspSnapshotRequest) -> Response:
            try:
                url = _rtsp_url_with_auth(body.url, body.username, body.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            blob = await _ffmpeg_snapshot(url, timeout_ms=body.timeout_ms)
            return Response(content=blob, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

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

            blob = await _ffmpeg_snapshot(url, timeout_ms=9000)
            return Response(content=blob, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
