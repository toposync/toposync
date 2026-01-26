from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .processing.runtime import CameraSpec, CameraWorker, _parse_detections  # noqa: PLC2701
from .processing.tracking_db import TrackingDatabase
from .processing.events import EventBroadcaster


logger = logging.getLogger("toposync.cameras.processor")


class ProcessorCamera(BaseModel):
    id: str
    name: str = ""
    rtsp_url: str
    username: str = ""
    password: str = ""
    fps: float = Field(default=5.0, ge=1.0, le=60.0)
    enabled: bool = True
    detections: list[dict[str, Any]] = Field(default_factory=list)


class ProcessorConfig(BaseModel):
    cameras: list[ProcessorCamera] = Field(default_factory=list)


class ProcessorRuntime:
    def __init__(self, *, data_dir: Path, files_dir: Path) -> None:
        self._data_dir = data_dir
        self._files_dir = files_dir
        self.db = TrackingDatabase(data_dir / "tracking.sqlite3")
        self.broadcaster = EventBroadcaster()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._workers: dict[str, CameraWorker] = {}
        self._worker_sigs: dict[str, str] = {}

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        for worker in list(self._workers.values()):
            try:
                worker.stop()
            except Exception:
                pass
        self._workers.clear()
        self._worker_sigs.clear()

    def apply_config(self, config: ProcessorConfig) -> None:
        desired: dict[str, CameraSpec] = {}
        for cam in config.cameras:
            cid = cam.id.strip()
            if not cid:
                continue
            detections = _parse_detections(cam.detections)
            desired[cid] = CameraSpec(
                id=cid,
                name=cam.name,
                rtsp_url=cam.rtsp_url,
                username=cam.username,
                password=cam.password,
                fps=float(cam.fps),
                enabled=bool(cam.enabled),
                processing_server_id="",
                detections=tuple(detections),
            )

        # Stop removed
        for cid in list(self._workers.keys()):
            if cid not in desired or not desired[cid].enabled or not desired[cid].rtsp_url:
                try:
                    self._workers[cid].stop()
                except Exception:
                    pass
                self._workers.pop(cid, None)
                self._worker_sigs.pop(cid, None)

        # Start/restart
        for cid, spec in desired.items():
            if not spec.enabled or not spec.rtsp_url:
                continue
            sig = spec.signature()
            if cid in self._workers and self._worker_sigs.get(cid) == sig:
                continue
            if cid in self._workers:
                try:
                    self._workers[cid].stop()
                except Exception:
                    pass
                self._workers.pop(cid, None)
                self._worker_sigs.pop(cid, None)
            try:
                worker = CameraWorker(
                    spec=spec,
                    files_dir=self._files_dir,
                    on_event=self._publish_from_thread,
                )
            except Exception as exc:
                logger.warning("failed to start camera worker camera_id=%s: %s", cid, exc)
                continue
            self._workers[cid] = worker
            self._worker_sigs[cid] = sig

    def _publish_from_thread(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._ingest_event, event)

    def _ingest_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        camera_id = str(event.get("camera_id") or "").strip()
        kind = str(event.get("kind") or "").strip()
        if not camera_id or not kind:
            return

        try:
            ts = float(event.get("ts") or 0.0) or None
        except Exception:
            ts = None

        detection_id = str(event.get("detection_id") or "").strip() or None
        tracking_id = str(event.get("tracking_id") or "").strip() or None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        image_path = str(event.get("image_path") or "").strip() or None

        image = event.get("image") if isinstance(event.get("image"), dict) else {}
        image_u = image.get("u")
        image_v = image.get("v")
        try:
            image_u_f = float(image_u) if image_u is not None else None
            image_v_f = float(image_v) if image_v is not None else None
        except Exception:
            image_u_f = None
            image_v_f = None

        bbox = event.get("bbox") if isinstance(event.get("bbox"), dict) else {}
        bbox01 = None
        try:
            if all(k in bbox for k in ("x1", "y1", "x2", "y2")):
                bbox01 = (float(bbox["x1"]), float(bbox["y1"]), float(bbox["x2"]), float(bbox["y2"]))
        except Exception:
            bbox01 = None

        world = event.get("world") if isinstance(event.get("world"), dict) else None
        world_x = world_z = None
        if world and world.get("x") is not None and world.get("z") is not None:
            try:
                world_x = float(world.get("x"))
                world_z = float(world.get("z"))
            except Exception:
                world_x = world_z = None

        composition_id = str(event.get("composition_id") or "").strip() or None

        try:
            self.db.insert_event(
                camera_id=camera_id,
                composition_id=composition_id,
                tracking_id=tracking_id,
                detection_id=detection_id,
                kind=kind,
                payload=payload,
                ts=ts,
                image_path=image_path,
                image_u=image_u_f,
                image_v=image_v_f,
                bbox01=bbox01,
                world_x=world_x,
                world_z=world_z,
            )
        except Exception:
            pass

        self.broadcaster.publish(event)


def create_app(*, data_dir: Path, files_dir: Path) -> FastAPI:
    app = FastAPI(title="Toposync Cameras Processor", version="0.1.0")
    runtime = ProcessorRuntime(data_dir=data_dir, files_dir=files_dir)
    snapshot_cache: dict[str, tuple[float, float, bytes]] = {}
    snapshot_locks: dict[str, asyncio.Lock] = {}
    snapshot_cache_ttl_s = float(os.getenv("TOPOSYNC_PROCESSOR_SNAPSHOT_TTL_S", "0.8") or "0.8")
    snapshot_max_frame_age_s = float(os.getenv("TOPOSYNC_PROCESSOR_SNAPSHOT_MAX_FRAME_AGE_S", "5.0") or "5.0")

    @app.on_event("startup")
    async def _startup() -> None:
        runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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

    @app.get("/api/processor/cameras/{camera_id}/snapshot")
    async def camera_snapshot(camera_id: str) -> Response:
        cid = camera_id.strip()
        if not cid:
            raise HTTPException(status_code=400, detail="camera_id is required")

        lock = _get_lock(cid)
        async with lock:
            now = time.time()
            cached = snapshot_cache.get(cid)
            if cached and (now - cached[0]) <= snapshot_cache_ttl_s:
                return Response(
                    content=cached[2],
                    media_type="image/jpeg",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Toposync-Snapshot-Source": "processor_cache",
                    },
                )

            worker = runtime._workers.get(cid)
            if worker is None:
                raise HTTPException(status_code=404, detail="Unknown camera")

            frame, ts = worker.get_latest_frame()
            if frame is None or not ts:
                raise HTTPException(status_code=503, detail="No frame available yet")
            age_s = max(0.0, now - float(ts))
            if age_s > snapshot_max_frame_age_s:
                raise HTTPException(status_code=503, detail="Camera frame is stale")

            blob = await _encode_jpeg(frame)
            if not blob:
                raise HTTPException(status_code=501, detail="Failed to encode JPEG snapshot")

            snapshot_cache[cid] = (now, float(ts), blob)
            return Response(
                content=blob,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "no-store",
                    "X-Toposync-Snapshot-Source": "processor",
                    "X-Toposync-Snapshot-Frame-Age-Ms": str(int(age_s * 1000)),
                },
            )

    @app.post("/api/processor/config")
    async def set_config(body: ProcessorConfig) -> dict[str, Any]:
        runtime.apply_config(body)
        return {"ok": True, "cameras": len(body.cameras)}

    @app.get("/api/processor/detections/recent")
    async def recent(
        camera_id: str | None = None,
        composition_id: str | None = None,
        tracking_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        cam = (camera_id or "").strip() or None
        comp = (composition_id or "").strip() or None
        track = (tracking_id or "").strip() or None
        return {"events": runtime.db.list_events(camera_id=cam, composition_id=comp, tracking_id=track, limit=limit)}

    @app.get("/api/processor/detections/stream")
    async def stream(request: Request) -> StreamingResponse:
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

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException):
        return {"detail": exc.detail}

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="toposync-cameras-processor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    base = Path(args.data_dir).expanduser().resolve() if args.data_dir else Path.cwd() / ".camera-processor"
    base.mkdir(parents=True, exist_ok=True)
    files = base / "files"
    files.mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        create_app(data_dir=base, files_dir=files),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        factory=False,
    )


if __name__ == "__main__":
    main()
