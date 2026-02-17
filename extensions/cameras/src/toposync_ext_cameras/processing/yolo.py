from __future__ import annotations

import logging
import os
import platform
import time
import weakref
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)
_YOLO_TRACKERS: weakref.WeakSet["YoloTracker"] = weakref.WeakSet()


try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]


def _require_opencv() -> None:
    if cv2 is None:
        raise RuntimeError(
            "OpenCV (cv2) is required for YOLO processing. Install with: "
            "`uv pip install opencv-python-headless` (recommended) or `uv pip install opencv-python` (then restart Toposync)."
        )


def _normalize_tracker(tracker: str | None) -> str | None:
    if not tracker:
        return None
    t = str(tracker).strip().lower()
    if not t:
        return None
    if t.endswith(".yaml") or t.endswith(".yml"):
        return t
    if t in {"bytetrack", "byte", "bt"}:
        return "bytetrack.yaml"
    if t in {"botsort", "bo", "bs"}:
        return "botsort.yaml"
    return f"{t}.yaml"


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _clean_device(v: str | None) -> str | None:
    raw = str(v).strip() if v is not None else ""
    if not raw:
        return None
    low = raw.lower()
    if low in {"", "none", "null", "auto", "default"}:
        return None
    return raw


def _normalize_track_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if hasattr(value, "tolist"):
            value = value.tolist()
        while isinstance(value, (list, tuple)):
            if not value:
                return None
            value = value[0]
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return int(float(stripped))
        return int(value)
    except Exception:
        return None


BBox01 = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class TrackedObject:
    track_id: int | None
    label: str
    confidence: float
    bbox01: BBox01


@dataclass(frozen=True, slots=True)
class YoloOutput:
    objects: tuple[TrackedObject, ...]
    last_latency_ms: float
    fps: float
    model: str
    tracker: str
    device_requested: str
    device_selected: str
    device_effective: str
    device_reason: str


class YoloTracker:
    def __init__(
        self,
        *,
        model: str = "yolo11n",
        conf: float = 0.25,
        iou: float = 0.7,
        img_size: int = 640,
        device: str | None = None,
        tracker: str = "bytetrack",
    ) -> None:
        _require_opencv()
        self.model_name = str(model)
        self.conf = float(conf)
        self.iou = float(iou)
        self.img_size = int(img_size)
        env_device = _clean_device(os.getenv("TOPOSYNC_YOLO_DEVICE"))
        self.device = _clean_device(device) or env_device
        self.device_requested = self.device or "auto"
        self.tracker = str(tracker) if tracker else "bytetrack"
        self._tracker_arg = _normalize_tracker(self.tracker)

        self._yolo: Any | None = None
        self._class_name_to_idx: dict[str, int] = {}
        self._selected_device: str | None = None
        self._effective_device: str = "unknown"
        self._device_reason: str = "not_initialized"
        self._runtime_info_logged: bool = False
        self._torch_info: dict[str, Any] = {}
        self._platform_info: dict[str, str] = {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        }

        self.last_latency_ms: float = 0.0
        self._fps_count: int = 0
        self._fps_window_start: float = time.time()
        self.fps: float = 0.0
        try:
            _YOLO_TRACKERS.add(self)
        except Exception:
            pass

    def _ensure_model(self) -> None:
        if self._yolo is not None:
            return
        if self._selected_device is None:
            self._selected_device = self._resolve_device()
        try:
            from ultralytics import YOLO  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Ultralytics is required for YOLO tracking. Install with: "
                "`uv pip install ultralytics lap` (and a compatible `torch` build), then restart."
            ) from exc

        try:
            self._yolo = YOLO(self.model_name)
            names = getattr(self._yolo, "names", {})
            if isinstance(names, dict):
                self._class_name_to_idx = {str(v): int(k) for k, v in names.items()}
            elif isinstance(names, list):
                self._class_name_to_idx = {str(n): i for i, n in enumerate(names)}
            else:
                self._class_name_to_idx = {}
        except Exception as exc:  # noqa: BLE001
            self._yolo = None
            raise RuntimeError(f"Failed to load YOLO model '{self.model_name}': {exc}") from exc

    def _resolve_device(self) -> str:
        self._torch_info = self._collect_torch_info()
        explicit = _clean_device(self.device)
        if explicit is not None:
            self._device_reason = "explicit_config"
            return explicit

        if bool(self._torch_info.get("cuda_available")):
            self._device_reason = "torch_cuda_available"
            return "0"
        if bool(self._torch_info.get("mps_available")):
            self._device_reason = "torch_mps_available"
            return "mps"

        self._device_reason = "torch_cpu_fallback"
        return "cpu"

    def _collect_torch_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "imported": False,
            "torch_version": "",
            "cuda_version": "",
            "hip_version": "",
            "cuda_available": False,
            "cuda_device_count": 0,
            "cuda_devices": [],
            "mps_available": False,
            "mps_built": False,
        }
        try:
            import torch  # type: ignore
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
            return info

        info["imported"] = True
        info["torch_version"] = str(getattr(torch, "__version__", "") or "")

        version = getattr(torch, "version", None)
        info["cuda_version"] = str(getattr(version, "cuda", "") or "")
        info["hip_version"] = str(getattr(version, "hip", "") or "")

        try:
            cuda_available = bool(torch.cuda.is_available())
        except Exception:
            cuda_available = False
        info["cuda_available"] = cuda_available

        if cuda_available:
            try:
                count = int(torch.cuda.device_count())
            except Exception:
                count = 0
            info["cuda_device_count"] = max(0, count)
            devices: list[str] = []
            for idx in range(max(0, count)):
                try:
                    devices.append(str(torch.cuda.get_device_name(idx)))
                except Exception:
                    devices.append(f"cuda:{idx}")
            info["cuda_devices"] = devices

        try:
            mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
            info["mps_available"] = bool(mps_backend and mps_backend.is_available())
            info["mps_built"] = bool(mps_backend and mps_backend.is_built())
        except Exception:
            info["mps_available"] = False
            info["mps_built"] = False

        return info

    def _resolve_classes(self, classes: set[str] | None) -> list[int] | None:
        if not classes:
            return None
        out: list[int] = []
        for name in sorted(classes):
            idx = self._class_name_to_idx.get(str(name))
            if idx is None:
                continue
            out.append(int(idx))
        return out or None

    def _track_once(self, rgb: Any, *, classes_idx: list[int] | None, device: str) -> Any:
        if self._yolo is None:
            raise RuntimeError("YOLO model is not initialized")
        return self._yolo.track(
            source=rgb,
            imgsz=self.img_size,
            conf=self.conf,
            iou=self.iou,
            classes=classes_idx,
            verbose=False,
            device=device,
            tracker=self._tracker_arg,
            persist=True,
            stream=False,
        )

    def _infer_result_device(self, result: Any) -> str | None:
        holders = []
        try:
            holders.append(getattr(result, "boxes", None))
        except Exception:
            pass
        try:
            holders.append(getattr(result, "masks", None))
        except Exception:
            pass
        try:
            holders.append(getattr(result, "probs", None))
        except Exception:
            pass

        for holder in holders:
            if holder is None:
                continue
            try:
                data = getattr(holder, "data", None)
                dev = getattr(data, "device", None)
                if dev is not None:
                    return str(dev)
            except Exception:
                continue
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "tracker": self.tracker,
            "fps": float(self.fps),
            "last_latency_ms": float(self.last_latency_ms),
            "device_requested": self.device_requested,
            "device_selected": self._selected_device or "unknown",
            "device_effective": self._effective_device,
            "device_reason": self._device_reason,
            "torch": dict(self._torch_info),
            "platform": dict(self._platform_info),
        }

    def process(self, frame_bgr: Any, *, classes: set[str] | None = None) -> YoloOutput:
        _require_opencv()
        self._ensure_model()
        if self._yolo is None:
            raise RuntimeError("YOLO model is not initialized")
        if self._selected_device is None:
            self._selected_device = self._resolve_device()

        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        classes_idx = self._resolve_classes(classes)

        t0 = time.time()
        try:
            res = self._track_once(rgb, classes_idx=classes_idx, device=self._selected_device)
        except Exception as exc:  # noqa: BLE001
            if self._selected_device != "cpu":
                logger.warning(
                    "YOLO inference failed on device=%s; retrying on CPU: %s",
                    self._selected_device,
                    exc,
                )
                self._selected_device = "cpu"
                self._device_reason = "fallback_cpu_after_runtime_error"
                res = self._track_once(rgb, classes_idx=classes_idx, device="cpu")
            else:
                raise
        t1 = time.time()
        self.last_latency_ms = (t1 - t0) * 1000.0

        self._fps_count += 1
        elapsed = t1 - self._fps_window_start
        if elapsed >= 1.5:
            self.fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_window_start = t1

        result = res[0]
        self._effective_device = self._infer_result_device(result) or self._selected_device or "unknown"
        if not self._runtime_info_logged:
            logger.info(
                "YOLO runtime model=%s tracker=%s requested=%s selected=%s effective=%s reason=%s "
                "torch=%s cuda=%s hip=%s cuda_available=%s platform=%s/%s",
                self.model_name,
                self.tracker,
                self.device_requested,
                self._selected_device or "unknown",
                self._effective_device,
                self._device_reason,
                str(self._torch_info.get("torch_version") or ""),
                str(self._torch_info.get("cuda_version") or ""),
                str(self._torch_info.get("hip_version") or ""),
                bool(self._torch_info.get("cuda_available")),
                self._platform_info.get("system") or "",
                self._platform_info.get("machine") or "",
            )
            self._runtime_info_logged = True

        names = getattr(self._yolo, "names", {})

        objects: list[TrackedObject] = []
        try:
            boxes = result.boxes  # type: ignore[attr-defined]
        except Exception:
            boxes = []

        raw_box_ids: list[Any] = []
        if boxes is not None and hasattr(boxes, "id"):
            try:
                ids = getattr(boxes, "id", None)
                if ids is not None:
                    if hasattr(ids, "tolist"):
                        ids = ids.tolist()
                    if isinstance(ids, (list, tuple)):
                        raw_box_ids = list(ids)
                    else:
                        raw_box_ids = [ids]
            except Exception:
                raw_box_ids = []

        for idx, box in enumerate(boxes):
            try:
                xyxy = box.xyxy.tolist()[0]
            except Exception:
                try:
                    xyxy = [float(v) for v in box.xyxy[0]]
                except Exception:
                    continue

            try:
                confidence = float(box.conf[0]) if hasattr(box, "conf") else float(box.confidence)
            except Exception:
                confidence = 0.0

            class_id = None
            try:
                class_id = int(box.cls[0]) if hasattr(box, "cls") else int(box.class_id)
            except Exception:
                class_id = None

            track_id = None
            if hasattr(box, "id"):
                raw_id = getattr(box, "id", None)
                if raw_id is None and idx < len(raw_box_ids):
                    raw_id = raw_box_ids[idx]
                track_id = _normalize_track_id(raw_id)

            name = None
            if class_id is not None:
                if isinstance(names, dict):
                    name = names.get(class_id)
                elif isinstance(names, list) and 0 <= class_id < len(names):
                    name = names[class_id]
            label = str(name) if name is not None else (str(class_id) if class_id is not None else "unknown")

            x1 = _clamp01(float(xyxy[0]) / float(max(1, w)))
            y1 = _clamp01(float(xyxy[1]) / float(max(1, h)))
            x2 = _clamp01(float(xyxy[2]) / float(max(1, w)))
            y2 = _clamp01(float(xyxy[3]) / float(max(1, h)))
            if x2 <= x1 or y2 <= y1:
                continue

            objects.append(
                TrackedObject(
                    track_id=track_id,
                    label=label,
                    confidence=confidence,
                    bbox01=(x1, y1, x2, y2),
                )
            )

        return YoloOutput(
            objects=tuple(objects),
            last_latency_ms=self.last_latency_ms,
            fps=self.fps,
            model=self.model_name,
            tracker=self.tracker,
            device_requested=self.device_requested,
            device_selected=self._selected_device or "unknown",
            device_effective=self._effective_device,
            device_reason=self._device_reason,
        )


def registered_yolo_trackers_diagnostics(limit: int = 8) -> list[dict[str, Any]]:
    try:
        items = list(_YOLO_TRACKERS)
    except Exception:
        items = []

    out: list[dict[str, Any]] = []
    for tracker in items:
        try:
            out.append(tracker.diagnostics())
        except Exception:
            continue

    out.sort(key=lambda item: (str(item.get("model") or ""), str(item.get("tracker") or "")))
    if limit <= 0:
        return out
    return out[: int(limit)]
