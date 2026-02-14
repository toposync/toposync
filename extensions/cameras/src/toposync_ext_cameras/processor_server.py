from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import logging
import os
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .processing.runtime import CameraSpec, CameraWorker, _parse_detections, summarize_capacity_estimate  # noqa: PLC2701
from .processing.events import EventBroadcaster


logger = logging.getLogger("toposync.cameras.processor")


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


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


class ProcessorAck(BaseModel):
    last_event_id: int = Field(default=0, ge=0)


class ProcessorRuntime:
    def __init__(
        self,
        *,
        max_recent_events: int = 1200,
        max_replay_events: int = 250,
    ) -> None:
        self.broadcaster = EventBroadcaster()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._workers: dict[str, CameraWorker] = {}
        self._worker_sigs: dict[str, str] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=max(100, int(max_recent_events)))
        # Replay buffer keeps full events (including optional inline image bytes) to allow
        # the client to resume after short disconnects without relying on disk persistence.
        self._replay_events: deque[dict[str, Any]] = deque(maxlen=max(25, int(max_replay_events)))
        self._event_seq: int = 0
        self._last_acked_event_id: int = 0

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
        self._replay_events.clear()
        self._recent_events.clear()

    def status(self) -> dict[str, Any]:
        workers: list[dict[str, Any]] = []
        for cid, worker in sorted(self._workers.items()):
            try:
                workers.append(worker.status())
            except Exception:
                workers.append({"camera_id": cid})
        return {
            "workers": workers,
            "capacity_estimate": summarize_capacity_estimate(workers),
        }

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
                    files_dir=None,
                    on_event=self._publish_from_thread,
                    emit_image_jpeg_b64=True,
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
        # Assign a monotonic event_id so clients can resume from disconnects.
        self._event_seq += 1
        enriched = dict(event)
        enriched["event_id"] = self._event_seq

        compact = dict(enriched)
        compact.pop("image_jpeg_b64", None)
        self._recent_events.append(compact)
        self._replay_events.append(enriched)
        self.broadcaster.publish(enriched)

    def list_recent_events(
        self,
        *,
        camera_id: str | None = None,
        composition_id: str | None = None,
        tracking_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        cam = str(camera_id or "").strip() or None
        comp = str(composition_id or "").strip() or None
        track = str(tracking_id or "").strip() or None
        out: list[dict[str, Any]] = []
        wanted = max(1, min(2000, int(limit)))
        for rec in reversed(self._recent_events):
            if cam and str(rec.get("camera_id") or "").strip() != cam:
                continue
            if comp and str(rec.get("composition_id") or "").strip() != comp:
                continue
            if track and str(rec.get("tracking_id") or "").strip() != track:
                continue
            out.append(rec)
            if len(out) >= wanted:
                break
        return out

    def replay_after(self, last_event_id: int) -> list[dict[str, Any]]:
        after = max(0, int(last_event_id))
        if after <= 0:
            return list(self._replay_events)
        out: list[dict[str, Any]] = []
        for rec in self._replay_events:
            try:
                rid = int(rec.get("event_id") or 0)
            except Exception:
                rid = 0
            if rid > after:
                out.append(rec)
        return out

    def ack(self, last_event_id: int) -> None:
        acked = max(0, int(last_event_id))
        if acked <= self._last_acked_event_id:
            return
        self._last_acked_event_id = acked
        while self._replay_events:
            try:
                rid = int(self._replay_events[0].get("event_id") or 0)
            except Exception:
                rid = 0
            if rid <= acked:
                self._replay_events.popleft()
                continue
            break

    @property
    def last_acked_event_id(self) -> int:
        return self._last_acked_event_id


def create_app(*, max_recent_events: int = 1200, max_replay_events: int = 250) -> FastAPI:
    app = FastAPI(title="Toposync Cameras Processor", version="0.2.0")
    runtime = ProcessorRuntime(max_recent_events=max_recent_events, max_replay_events=max_replay_events)
    snapshot_cache: dict[str, tuple[float, float, bytes]] = {}
    snapshot_locks: dict[str, asyncio.Lock] = {}
    snapshot_cache_ttl_s = _env_float("TOPOSYNC_PROCESSOR_SNAPSHOT_TTL_S", 0.8)
    snapshot_max_frame_age_s = _env_float("TOPOSYNC_PROCESSOR_SNAPSHOT_MAX_FRAME_AGE_S", 5.0)
    sse_ping_interval_s = _env_float("TOPOSYNC_PROCESSOR_SSE_PING_S", 15.0)

    @app.on_event("startup")
    async def _startup() -> None:
        runtime.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.stop()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/processor/status")
    async def status() -> dict[str, Any]:
        return runtime.status()

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
        return {
            "events": runtime.list_recent_events(
                camera_id=camera_id,
                composition_id=composition_id,
                tracking_id=tracking_id,
                limit=limit,
            )
        }

    @app.post("/api/processor/detections/ack")
    async def ack(body: ProcessorAck) -> dict[str, Any]:
        runtime.ack(body.last_event_id)
        return {"ok": True, "last_event_id": runtime.last_acked_event_id}

    @app.get("/api/processor/detections/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = runtime.broadcaster.subscribe()
        raw_last = str(request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id") or "").strip()
        try:
            last_event_id = int(raw_last) if raw_last else 0
        except Exception:
            last_event_id = 0
        replay = runtime.replay_after(last_event_id)

        async def gen():
            last_sent_event_id = last_event_id
            if replay:
                try:
                    last_sent_event_id = int(replay[-1].get("event_id") or last_event_id)
                except Exception:
                    last_sent_event_id = last_event_id
            try:
                yield "retry: 1000\n\n"
                for event in replay:
                    eid = event.get("event_id")
                    if eid is not None:
                        yield f"id: {eid}\n"
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=max(1.0, sse_ping_interval_s))
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    eid = event.get("event_id")
                    if eid is not None:
                        try:
                            eid_i = int(eid)
                        except Exception:
                            eid_i = 0
                        if eid_i and eid_i <= last_sent_event_id:
                            continue
                        if eid_i:
                            last_sent_event_id = eid_i
                        yield f"id: {eid}\n"
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                runtime.broadcaster.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="toposync-cameras-processor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Deprecated/ignored: processor is stateless (captures + tracking are persisted by the global instance).",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--max-recent-events",
        type=int,
        default=_env_int("TOPOSYNC_PROCESSOR_MAX_RECENT_EVENTS", 1200),
        help="Max recent events kept in memory for /api/processor/detections/recent.",
    )
    parser.add_argument(
        "--max-replay-events",
        type=int,
        default=_env_int("TOPOSYNC_PROCESSOR_MAX_REPLAY_EVENTS", 250),
        help="Max events kept in memory for SSE replay (includes inline JPEG, if present).",
    )
    args = parser.parse_args(argv)

    uvicorn.run(
        create_app(max_recent_events=args.max_recent_events, max_replay_events=args.max_replay_events),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        factory=False,
    )


if __name__ == "__main__":
    main()
