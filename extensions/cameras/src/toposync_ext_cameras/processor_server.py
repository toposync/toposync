from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
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
    fps: float = Field(default=15.0, ge=1.0, le=60.0)
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
                    mapper=None,
                    files_dir=self._files_dir,
                    db=self.db,
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
        loop.call_soon_threadsafe(self.broadcaster.publish, event)


def create_app(*, data_dir: Path, files_dir: Path) -> FastAPI:
    app = FastAPI(title="Toposync Cameras Processor", version="0.1.0")
    runtime = ProcessorRuntime(data_dir=data_dir, files_dir=files_dir)

    @app.on_event("startup")
    async def _startup() -> None:
        runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/processor/config")
    async def set_config(body: ProcessorConfig) -> dict[str, Any]:
        runtime.apply_config(body)
        return {"ok": True, "cameras": len(body.cameras)}

    @app.get("/api/processor/detections/recent")
    async def recent(camera_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        cam = (camera_id or "").strip() or None
        return {"events": runtime.db.list_events(camera_id=cam, limit=limit)}

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

