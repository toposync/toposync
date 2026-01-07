from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toposync.runtime.config_store import Composition, ConfigStore
from toposync.runtime.services import ServiceRegistry

from .events import EventBroadcaster
from .frame_grabber import FrameGrabber
from .mapping import ControlPointMapper, ControlPointPair
from .motion import MotionDetector
from .remote import RemoteProcessorClient, RemoteProcessorServer
from .tracking_db import TrackingDatabase
from .tracker import BBoxTracker, Detection, iou01
from .yolo import YoloTracker, YoloOutput


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
        files_dir: Path,
        on_event: callable,
        motion_threshold: float = 0.010,
    ) -> None:
        self.camera_id = spec.id
        self._spec = spec
        self._signature = spec.signature()
        self._files_dir = files_dir
        self._on_event = on_event
        self._last_processed_ts = 0.0
        self._last_capture_ts = 0.0
        self._capture_min_interval_s = 2.0

        url = _safe_rtsp_url_with_auth(spec.rtsp_url, spec.username, spec.password)
        self._grabber = FrameGrabber(url, target_fps=spec.fps).start()
        self._motion = MotionDetector(threshold=motion_threshold)
        self._tracker = BBoxTracker()
        self._yolo: YoloTracker | None = None
        self._yolo_failed = False
        self._yolo_last_run_ts = 0.0
        self._yolo_min_interval_s = 0.45
        self._yolo_cache: YoloOutput | None = None
        self._yolo_cache_ts = 0.0
        self._yolo_cache_ttl_s = 1.2
        self._yolo_track_state: dict[str, dict[str, Any]] = {}

        import threading

        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def signature(self) -> str:
        return self._signature

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

            rules = list(self._spec.detections)
            if not rules:
                time.sleep(0.05)
                continue

            motion_needed = any(
                (r.trigger.kind == "motion") or any(f.kind == "motion" for f in r.filters)
                for r in rules
            )
            object_needed = any(
                (r.trigger.kind == "object") or any(f.kind == "object" for f in r.filters)
                for r in rules
            )

            motion_active = False
            motion_result = None
            if motion_needed:
                try:
                    motion_result = self._motion.process(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("motion processing failed for camera=%s: %s", self.camera_id, exc)
                    motion_result = None
                if motion_result is not None:
                    motion_active = bool(motion_result.active)

            # Determine whether YOLO is needed and if so, run it with throttling.
            yolo_output: YoloOutput | None = None
            present_classes: set[str] = set()

            def _rule_gate_passes_without_yolo(rule: DetectionRule) -> bool:
                if rule.trigger.kind == "motion" and not motion_active:
                    return False
                for f in rule.filters:
                    if f.kind == "motion" and not motion_active:
                        return False
                return True

            desired_classes: set[str] = set()
            if object_needed:
                for r in rules:
                    if r.trigger.kind == "object" and r.trigger.category:
                        desired_classes.add(r.trigger.category)
                    for f in r.filters:
                        if f.kind == "object" and f.category:
                            desired_classes.add(f.category)

                needs_yolo_now = any(
                    ((r.trigger.kind == "object") or any(f.kind == "object" for f in r.filters))
                    and _rule_gate_passes_without_yolo(r)
                    for r in rules
                )

                if needs_yolo_now:
                    if self._yolo_cache and (ts - self._yolo_cache_ts) <= self._yolo_cache_ttl_s:
                        yolo_output = self._yolo_cache
                    elif (ts - self._yolo_last_run_ts) >= self._yolo_min_interval_s:
                        if not self._yolo_failed:
                            try:
                                if self._yolo is None:
                                    self._yolo = YoloTracker()
                                yolo_output = self._yolo.process(frame, classes=desired_classes or None)
                                self._yolo_cache = yolo_output
                                self._yolo_cache_ts = ts
                                self._yolo_last_run_ts = ts
                            except Exception as exc:  # noqa: BLE001
                                # Don't spam: disable YOLO for this worker unless it restarts.
                                self._yolo_failed = True
                                logger.warning("YOLO processing disabled for camera_id=%s: %s", self.camera_id, exc)
                                yolo_output = None

            if yolo_output is not None:
                for obj in yolo_output.objects:
                    present_classes.add(obj.label)

            def _has_object(category: str) -> bool:
                if not category:
                    return bool(yolo_output and yolo_output.objects)
                return category in present_classes

            def _filters_ok(rule: DetectionRule) -> bool:
                for f in rule.filters:
                    if f.kind == "motion":
                        if not motion_active:
                            return False
                        continue
                    if f.kind == "object":
                        if not _has_object(f.category):
                            return False
                        continue
                    # Unsupported filters (HA etc) are not satisfied yet.
                    return False
                return True

            motion_emit_tracks = []
            if motion_result is not None and motion_active:
                detections = [Detection(bbox01=b, label="motion", conf=motion_result.score) for b in motion_result.bboxes01]
                motion_emit_tracks = self._tracker.update(detections, ts=ts)
            else:
                self._tracker.update([], ts=ts)

            motion_rules = [r for r in rules if r.trigger.kind == "motion"]
            object_rules = [r for r in rules if r.trigger.kind == "object"]

            if not motion_emit_tracks and not (yolo_output and yolo_output.objects and object_rules):
                time.sleep(0.01)
                continue

            image_path = self._maybe_capture(frame, ts)

            # Emit motion events (optionally gated by filters, including object presence).
            if motion_emit_tracks and motion_result is not None:
                for tr in motion_emit_tracks:
                    x1, y1, x2, y2 = tr.bbox01
                    u = float(x1 + x2) / 2.0
                    v = float(y2)
                    payload = {
                        "type": "motion",
                        "score": motion_result.score,
                        "threshold": self._motion.threshold,
                        "latency_ms": motion_result.last_latency_ms,
                        "fps": motion_result.fps,
                    }
                    base = {
                        "ts": ts,
                        "camera_id": self.camera_id,
                        "tracking_id": tr.id,
                        "kind": "motion",
                        "payload": payload,
                        "image_path": image_path,
                        "image": {"u": u, "v": v},
                        "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                        "composition_id": None,
                        "world": None,
                    }

                    for rule in motion_rules:
                        if not _filters_ok(rule):
                            continue
                        event = dict(base)
                        event["detection_id"] = rule.id
                        try:
                            self._on_event(event)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("failed to publish detection event: %s", exc)

            # Emit object events (YOLO tracking IDs).
            if yolo_output is not None and yolo_output.objects and object_rules:
                now = ts
                # Garbage-collect old tracks.
                cutoff = now - 3.0
                for tid in list(self._yolo_track_state.keys()):
                    if float(self._yolo_track_state[tid].get("last_ts") or 0.0) < cutoff:
                        self._yolo_track_state.pop(tid, None)

                for obj in yolo_output.objects:
                    if obj.track_id is None:
                        continue
                    track_key = f"yolo:{self.camera_id}:{obj.track_id}"
                    state = self._yolo_track_state.get(track_key)
                    if state is None:
                        state = {"last_ts": now, "last_emit_ts": 0.0, "last_emit_bbox": None}
                        self._yolo_track_state[track_key] = state
                    state["last_ts"] = now

                    # Emit gating (throttle + motion-based dedupe).
                    last_emit_ts = float(state.get("last_emit_ts") or 0.0)
                    last_bbox = state.get("last_emit_bbox")
                    if last_emit_ts and (now - last_emit_ts) < 0.45:
                        continue
                    if isinstance(last_bbox, tuple) and len(last_bbox) == 4:
                        try:
                            if iou01(last_bbox, obj.bbox01) >= 0.985:
                                continue
                        except Exception:
                            pass

                    x1, y1, x2, y2 = obj.bbox01
                    u = float(x1 + x2) / 2.0
                    v = float(y2)
                    payload = {
                        "type": "object",
                        "label": obj.label,
                        "confidence": obj.confidence,
                        "model": yolo_output.model,
                        "tracker": yolo_output.tracker,
                        "latency_ms": yolo_output.last_latency_ms,
                        "fps": yolo_output.fps,
                    }

                    base = {
                        "ts": ts,
                        "camera_id": self.camera_id,
                        "tracking_id": track_key,
                        "kind": "object",
                        "payload": payload,
                        "image_path": image_path,
                        "image": {"u": u, "v": v},
                        "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                        "composition_id": None,
                        "world": None,
                    }

                    emitted = False
                    for rule in object_rules:
                        if rule.trigger.category and rule.trigger.category != obj.label:
                            continue
                        if not _filters_ok(rule):
                            continue
                        event = dict(base)
                        event["detection_id"] = rule.id
                        emitted = True
                        try:
                            self._on_event(event)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("failed to publish detection event: %s", exc)

                    if emitted:
                        state["last_emit_ts"] = now
                        state["last_emit_bbox"] = obj.bbox01


class CamerasProcessingRuntime:
    def __init__(
        self,
        *,
        config_store: ConfigStore,
        extension_id: str,
        data_dir: Path,
        files_dir: Path,
        services: ServiceRegistry | None = None,
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
        self._services = services
        self._notification_last_emit: dict[str, float] = {}

        self._workers: dict[str, CameraWorker] = {}
        self._worker_sigs: dict[str, str] = {}
        self._remote_clients: dict[str, RemoteProcessorClient] = {}
        self._opencv_available = _opencv_available()
        self._logged_missing_opencv = False
        self._camera_mappers: dict[str, list[tuple[str, ControlPointMapper]]] = {}

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
        loop.call_soon_threadsafe(self._ingest_event, event)

    def _ingest_event(self, event: dict[str, Any]) -> None:
        # Map to compositions and persist.
        camera_id = str(event.get("camera_id") or "").strip()
        kind = str(event.get("kind") or "").strip()
        if not camera_id or not kind:
            return

        try:
            ts = float(event.get("ts") or 0.0) or time.time()
        except Exception:
            ts = time.time()

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

        explicit_comp = str(event.get("composition_id") or "").strip() or None

        mappers = self._camera_mappers.get(camera_id) or []
        entries: list[tuple[str | None, ControlPointMapper | None]] = []
        if explicit_comp:
            mapper = None
            for cid, m in mappers:
                if cid == explicit_comp:
                    mapper = m
                    break
            entries = [(explicit_comp, mapper)]
        elif mappers:
            entries = [(cid, m) for (cid, m) in mappers]
        else:
            entries = [(None, None)]

        for comp_id, mapper in entries:
            world = None
            world_x = world_z = None
            if mapper is not None and image_u_f is not None and image_v_f is not None:
                try:
                    mapped = mapper.map(image_u_f, image_v_f)
                except Exception:
                    mapped = None
                if mapped is not None:
                    world_x, world_z = float(mapped[0]), float(mapped[1])
                    world = {"x": world_x, "z": world_z}

            try:
                self.db.insert_event(
                    camera_id=camera_id,
                    composition_id=comp_id,
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
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to persist detection event: %s", exc)

            enriched = dict(event)
            enriched["composition_id"] = comp_id
            enriched["world"] = world
            self.broadcaster.publish(enriched)
            self._maybe_publish_notification(enriched)

    def status(self) -> dict[str, Any]:
        return {
            "local_workers": [{"camera_id": cid} for cid in sorted(self._workers.keys())],
            "remote_servers": [
                {"server_id": sid, "url": client.server.url} for sid, client in sorted(self._remote_clients.items())
            ],
        }

    def _maybe_publish_notification(self, event: dict[str, Any]) -> None:
        if self._services is None:
            return
        tracking_id = str(event.get("tracking_id") or "").strip()
        if not tracking_id:
            return
        camera_id = str(event.get("camera_id") or "").strip()
        if not camera_id:
            return

        comp_id = str(event.get("composition_id") or "").strip() or None
        kind = str(event.get("kind") or "").strip()

        try:
            ts = float(event.get("ts") or 0.0) or time.time()
        except Exception:
            ts = time.time()

        image_path = str(event.get("image_path") or "").strip() or None
        dedupe_key = f"camera:{camera_id}:comp:{comp_id or '-'}:track:{tracking_id}"

        last = float(self._notification_last_emit.get(dedupe_key) or 0.0)
        if last and (ts - last) < 1.25 and not image_path:
            return
        self._notification_last_emit[dedupe_key] = ts

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        label = str(payload.get("label") or "").strip()
        conf = payload.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else None
        except Exception:
            conf_f = None

        title = "Detecção na câmera"
        description = f"Câmera {camera_id}"
        if kind == "motion":
            title = "Movimento detectado"
        elif kind == "object":
            title = f"Objeto detectado: {label}" if label else "Objeto detectado"
            if label and conf_f is not None:
                description = f"Câmera {camera_id} • {label} ({conf_f:.2f})"

        notif_payload: dict[str, Any] = {
            "source": "cameras",
            "camera_id": camera_id,
            "composition_id": comp_id,
            "tracking_id": tracking_id,
            "kind": kind,
        }
        if label:
            notif_payload["label"] = label
        if conf_f is not None:
            notif_payload["confidence"] = conf_f

        async def _call() -> None:
            try:
                await self._services.call(
                    "notifications.upsert",
                    type="cameras.tracking",
                    title=title,
                    description=description,
                    image_path=image_path,
                    payload=notif_payload,
                    dedupe_key=dedupe_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to publish notification: %s", exc)

        try:
            asyncio.create_task(_call())
        except Exception:
            pass

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
            fps = _as_float(rec.get("fps"), 5.0)
            if not math.isfinite(fps):
                fps = 5.0
            fps = max(1.0, min(60.0, fps))
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

    async def _load_control_point_mappers(self) -> dict[str, list[tuple[str, ControlPointMapper]]]:
        cfg = await self._config_store.get_config()
        out: dict[str, list[tuple[str, ControlPointMapper]]] = {}

        for comp in cfg.compositions:
            seen: set[str] = set()
            for el in comp.elements:
                props = el.props if isinstance(el.props, dict) else {}
                camera_id = _as_str(props.get("camera_id")).strip()
                if not camera_id or camera_id in seen:
                    continue
                raw_points = props.get("control_points")
                pairs = _parse_control_point_pairs(raw_points)
                if len(pairs) < 4:
                    continue
                try:
                    mapper = ControlPointMapper(pairs)
                except Exception:
                    continue
                out.setdefault(camera_id, []).append((comp.id, mapper))
                seen.add(camera_id)

        return out

    def _reconcile(
        self,
        specs: dict[str, CameraSpec],
        servers: dict[str, RemoteProcessorServer],
        mappers: dict[str, list[tuple[str, ControlPointMapper]]],
    ) -> None:
        self._camera_mappers = mappers
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
                    on_event=self._ingest_event,
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
