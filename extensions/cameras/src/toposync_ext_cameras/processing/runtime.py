from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toposync.runtime.config_store import Composition, ConfigStore

from .events import EventBroadcaster
from .frame_grabber import FrameGrabber
from .mapping import ControlPointMapper, ControlPointPair
from .motion import MotionDetector
from .remote import RemoteProcessorClient, RemoteProcessorServer
from .tracking_db import TrackingDatabase


logger = logging.getLogger(__name__)


def _opencv_available() -> bool:
    try:
        import cv2  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def _as_record(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _as_str(v: Any) -> str:
    return str(v) if isinstance(v, str) else ""


def _as_float(v: Any, fallback: float) -> float:
    try:
        return float(v)
    except Exception:
        return fallback


def _as_bool(v: Any, fallback: bool) -> bool:
    if isinstance(v, bool):
        return v
    return fallback


def _safe_rtsp_url_with_auth(url: str, username: str, password: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user and not pwd:
        return raw
    # Best-effort: keep it simple and only support credentials when the URL has scheme.
    if raw.startswith("rtsp://"):
        rest = raw[len("rtsp://") :]
        auth = f"{user}:{pwd}@" if pwd else f"{user}@"
        return f"rtsp://{auth}{rest}"
    return raw


def _condition_to_dict(cond: "DetectionCondition") -> dict[str, Any]:
    return {
        "kind": cond.kind,
        "category": cond.category,
        "entity_id": cond.entity_id,
        "state": cond.state,
    }


@dataclass(frozen=True, slots=True)
class DetectionCondition:
    kind: str
    category: str = ""
    entity_id: str = ""
    state: str = ""


@dataclass(frozen=True, slots=True)
class DetectionRule:
    id: str
    trigger: DetectionCondition
    filters: tuple[DetectionCondition, ...] = ()


@dataclass(frozen=True, slots=True)
class CameraSpec:
    id: str
    name: str
    rtsp_url: str
    username: str
    password: str
    fps: float
    enabled: bool
    processing_server_id: str
    detections: tuple[DetectionRule, ...]

    def signature(self) -> str:
        return json.dumps(
            {
                "rtsp_url": self.rtsp_url,
                "username": self.username,
                "password": "***" if self.password else "",
                "fps": round(self.fps, 3),
                "enabled": self.enabled,
                "processing_server_id": self.processing_server_id,
                "detections": [
                    {
                        "id": d.id,
                        "trigger": _condition_to_dict(d.trigger),
                        "filters": [_condition_to_dict(f) for f in d.filters],
                    }
                    for d in self.detections
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class CameraWorker:
    def __init__(
        self,
        *,
        spec: CameraSpec,
        mapper: ControlPointMapper | None,
        files_dir: Path,
        db: TrackingDatabase,
        on_event: callable,
        motion_threshold: float = 0.010,
    ) -> None:
        self.camera_id = spec.id
        self._spec = spec
        self._signature = spec.signature()
        self._files_dir = files_dir
        self._db = db
        self._on_event = on_event
        self._mapper = mapper
        self._last_processed_ts = 0.0
        self._last_capture_ts = 0.0
        self._capture_min_interval_s = 2.0

        url = _safe_rtsp_url_with_auth(spec.rtsp_url, spec.username, spec.password)
        self._grabber = FrameGrabber(url, target_fps=spec.fps).start()
        self._motion = MotionDetector(threshold=motion_threshold)

        import threading

        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def signature(self) -> str:
        return self._signature

    def update_mapper(self, mapper: ControlPointMapper | None) -> None:
        self._mapper = mapper

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._grabber.stop()
        except Exception:
            pass
        self._thread.join(timeout=1.5)

    def _maybe_capture(self, frame: Any, ts: float) -> str | None:
        if ts and (ts - self._last_capture_ts) < self._capture_min_interval_s:
            return None
        try:
            import cv2  # type: ignore

            ok, buf = cv2.imencode(".jpg", frame)
            if not ok or buf is None:
                return None
            folder = self._files_dir / "cameras" / self.camera_id
            folder.mkdir(parents=True, exist_ok=True)
            filename = f"{int(ts * 1000)}.jpg"
            path = folder / filename
            path.write_bytes(buf.tobytes())
            self._last_capture_ts = ts
            # Stored as a /files relative path so the UI can fetch later.
            return f"cameras/{self.camera_id}/{filename}"
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stopped.is_set():
            frame, ts = self._grabber.get_latest()
            if frame is None or not ts:
                time.sleep(0.05)
                continue
            if ts <= self._last_processed_ts:
                time.sleep(0.01)
                continue
            self._last_processed_ts = ts

            motion_result = None
            for rule in self._spec.detections:
                if rule.trigger.kind != "motion":
                    continue
                if motion_result is None:
                    try:
                        motion_result = self._motion.process(frame)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("motion processing failed for camera=%s: %s", self.camera_id, exc)
                        motion_result = None
                        break
                if not motion_result.active:
                    continue

                image_path = self._maybe_capture(frame, ts)

                payload = {
                    "type": "motion",
                    "score": motion_result.score,
                    "threshold": self._motion.threshold,
                    "latency_ms": motion_result.last_latency_ms,
                    "fps": motion_result.fps,
                }
                event = {
                    "ts": ts,
                    "camera_id": self.camera_id,
                    "detection_id": rule.id,
                    "kind": "motion",
                    "payload": payload,
                    "image_path": image_path,
                    "world": None,
                }

                try:
                    self._db.insert_event(
                        camera_id=self.camera_id,
                        kind="motion",
                        payload=payload,
                        ts=ts,
                        detection_id=rule.id,
                        image_path=image_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to persist detection event: %s", exc)

                try:
                    self._on_event(event)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to publish detection event: %s", exc)


class CamerasProcessingRuntime:
    def __init__(
        self,
        *,
        config_store: ConfigStore,
        extension_id: str,
        data_dir: Path,
        files_dir: Path,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._config_store = config_store
        self._extension_id = extension_id
        self._poll_interval_s = max(0.5, float(poll_interval_s))
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        self.broadcaster = EventBroadcaster()
        self.db = TrackingDatabase(data_dir / "cameras" / "tracking.sqlite3")
        self._files_dir = files_dir

        self._workers: dict[str, CameraWorker] = {}
        self._worker_sigs: dict[str, str] = {}
        self._remote_clients: dict[str, RemoteProcessorClient] = {}
        self._opencv_available = _opencv_available()
        self._logged_missing_opencv = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="cameras.processing")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for client in list(self._remote_clients.values()):
            try:
                await client.stop()
            except Exception:
                pass
        self._remote_clients.clear()

        for worker in list(self._workers.values()):
            try:
                worker.stop()
            except Exception:
                pass
        self._workers.clear()
        self._worker_sigs.clear()

    def _publish_from_thread(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self.broadcaster.publish, event)

    def status(self) -> dict[str, Any]:
        return {
            "local_workers": [{"camera_id": cid} for cid in sorted(self._workers.keys())],
            "remote_servers": [
                {"server_id": sid, "url": client.server.url} for sid, client in sorted(self._remote_clients.items())
            ],
        }

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                specs = await self._load_camera_specs()
                servers = await self._load_processing_servers()
                mappers = await self._load_control_point_mappers()
                self._reconcile(specs, servers, mappers)
            except Exception as exc:  # noqa: BLE001
                logger.warning("camera processing reconcile failed: %s", exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                pass

    async def _load_camera_specs(self) -> dict[str, CameraSpec]:
        settings = await self._config_store.get_settings()
        ext = settings.extensions.get(self._extension_id, {})
        ext_rec = ext if isinstance(ext, dict) else {}
        cameras_raw = _as_list(ext_rec.get("cameras"))

        out: dict[str, CameraSpec] = {}
        for item in cameras_raw:
            rec = _as_record(item)
            cid = _as_str(rec.get("id")).strip()
            if not cid:
                continue
            enabled = _as_bool(rec.get("enabled"), True)
            rtsp_url = _as_str(rec.get("rtsp_url")).strip()
            username = _as_str(rec.get("username")).strip()
            password = _as_str(rec.get("password")).strip()
            fps = _as_float(rec.get("fps"), 15.0)
            processing_server_id = _as_str(rec.get("processing_server_id")).strip()
            detections = _parse_detections(rec.get("detections"))
            out[cid] = CameraSpec(
                id=cid,
                name=_as_str(rec.get("name")).strip(),
                rtsp_url=rtsp_url,
                username=username,
                password=password,
                fps=fps,
                enabled=enabled,
                processing_server_id=processing_server_id,
                detections=tuple(detections),
            )
        return out

    async def _load_processing_servers(self) -> dict[str, RemoteProcessorServer]:
        settings = await self._config_store.get_settings()
        ext = settings.extensions.get(self._extension_id, {})
        ext_rec = ext if isinstance(ext, dict) else {}
        raw = _as_list(ext_rec.get("processing_servers"))
        out: dict[str, RemoteProcessorServer] = {}
        for item in raw:
            rec = _as_record(item)
            sid = _as_str(rec.get("id")).strip()
            url = _as_str(rec.get("url")).strip()
            if not sid or not url:
                continue
            out[sid] = RemoteProcessorServer(id=sid, url=url)
        return out

    async def _load_control_point_mappers(self) -> dict[str, ControlPointMapper | None]:
        try:
            composition: Composition = await self._config_store.get_active_composition()
        except Exception:
            return {}

        # Prefer the first element that contains a complete set for a camera_id.
        out: dict[str, ControlPointMapper | None] = {}

        for el in composition.elements:
            props = el.props if isinstance(el.props, dict) else {}
            camera_id = _as_str(props.get("camera_id")).strip()
            if not camera_id or camera_id in out:
                continue
            raw_points = props.get("control_points")
            pairs = _parse_control_point_pairs(raw_points)
            if len(pairs) < 4:
                continue
            try:
                out[camera_id] = ControlPointMapper(pairs)
            except Exception:
                continue
        return out

    def _reconcile(
        self,
        specs: dict[str, CameraSpec],
        servers: dict[str, RemoteProcessorServer],
        mappers: dict[str, ControlPointMapper | None],
    ) -> None:
        remote_groups: dict[str, list[CameraSpec]] = {}
        desired: dict[str, CameraSpec] = {}
        for cid, spec in specs.items():
            if not spec.enabled or not spec.rtsp_url:
                continue
            sid = spec.processing_server_id
            if sid and sid in servers:
                remote_groups.setdefault(sid, []).append(spec)
            else:
                desired[cid] = spec

        if desired and not self._opencv_available:
            # Don't spam logs on every reconcile: warn once and keep the app running.
            if not self._logged_missing_opencv:
                self._logged_missing_opencv = True
                logger.warning(
                    "OpenCV (cv2) is not installed, so local camera processing is disabled. "
                    "Install with: `uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python`, "
                    "restart Toposync, or assign cameras to a remote processing server."
                )
            # Make sure any previous workers are stopped.
            for cid, worker in list(self._workers.items()):
                try:
                    worker.stop()
                except Exception:
                    pass
                self._workers.pop(cid, None)
                self._worker_sigs.pop(cid, None)
            desired = {}

        # Stop removed workers
        for cid in list(self._workers.keys()):
            if cid not in desired:
                try:
                    self._workers[cid].stop()
                except Exception:
                    pass
                self._workers.pop(cid, None)
                self._worker_sigs.pop(cid, None)

        # Start/restart desired workers
        for cid, spec in desired.items():
            sig = spec.signature()
            if cid in self._workers and self._worker_sigs.get(cid) == sig:
                self._workers[cid].update_mapper(mappers.get(cid))
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
                    mapper=mappers.get(cid),
                    files_dir=self._files_dir,
                    db=self.db,
                    on_event=self._publish_from_thread,
                )
            except Exception as exc:
                logger.warning("failed to start camera worker camera_id=%s: %s", cid, exc)
                continue
            self._workers[cid] = worker
            self._worker_sigs[cid] = sig

        # Remote processors
        for sid in list(self._remote_clients.keys()):
            if sid not in remote_groups:
                client = self._remote_clients.pop(sid)
                asyncio.create_task(client.stop())

        for sid, camera_specs in remote_groups.items():
            server = servers.get(sid)
            if server is None:
                continue
            client = self._remote_clients.get(sid)
            if client is None or client.server.url != server.url:
                if client is not None:
                    asyncio.create_task(client.stop())
                client = RemoteProcessorClient(
                    server=server,
                    broadcaster=self.broadcaster,
                    db=self.db,
                    stop_event=self._stopped,
                )
                client.start()
                self._remote_clients[sid] = client

            payload = {
                "cameras": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "rtsp_url": s.rtsp_url,
                        "username": s.username,
                        "password": s.password,
                        "fps": s.fps,
                        "enabled": s.enabled,
                        "detections": [
                            {
                                "id": d.id,
                                "trigger": _condition_to_dict(d.trigger),
                                "filters": [_condition_to_dict(f) for f in d.filters],
                            }
                            for d in s.detections
                        ],
                    }
                    for s in camera_specs
                ]
            }
            client.update_config(payload)


def _parse_condition(v: Any) -> DetectionCondition | None:
    rec = _as_record(v)
    kind = _as_str(rec.get("kind")).strip()
    if not kind:
        return None
    if kind == "motion":
        return DetectionCondition(kind="motion")
    if kind == "object":
        return DetectionCondition(kind="object", category=_as_str(rec.get("category")).strip())
    if kind == "ha_sensor":
        return DetectionCondition(kind="ha_sensor", entity_id=_as_str(rec.get("entity_id")).strip())
    if kind == "ha_state":
        return DetectionCondition(
            kind="ha_state",
            entity_id=_as_str(rec.get("entity_id")).strip(),
            state=_as_str(rec.get("state")).strip(),
        )
    return DetectionCondition(kind=kind)


def _parse_detections(v: Any) -> list[DetectionRule]:
    raw = _as_list(v)
    out: list[DetectionRule] = []
    for item in raw:
        rec = _as_record(item)
        did = _as_str(rec.get("id")).strip()
        if not did:
            continue
        trigger = _parse_condition(rec.get("trigger")) or DetectionCondition(kind="motion")
        filters_raw = _as_list(rec.get("filters"))
        filters = tuple(c for c in (_parse_condition(f) for f in filters_raw) if c)
        out.append(DetectionRule(id=did, trigger=trigger, filters=filters))
    return out


def _parse_control_point_pairs(v: Any) -> list[ControlPointPair]:
    raw = _as_list(v)
    out: list[ControlPointPair] = []
    for item in raw:
        rec = _as_record(item)
        img = _as_record(rec.get("image"))
        world = _as_record(rec.get("world"))
        try:
            u = float(img.get("x"))
            v01 = float(img.get("y"))
            x = float(world.get("x"))
            z = float(world.get("z"))
        except Exception:
            continue
        if not (0.0 <= u <= 1.0 and 0.0 <= v01 <= 1.0):
            continue
        out.append(ControlPointPair(image_u=u, image_v=v01, world_x=x, world_z=z))
    return out
