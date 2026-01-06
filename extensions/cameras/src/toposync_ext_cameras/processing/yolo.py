from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


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
        self.device = str(device) if device else None
        self.tracker = str(tracker) if tracker else "bytetrack"
        self._tracker_arg = _normalize_tracker(self.tracker)

        self._yolo: Any | None = None
        self._class_name_to_idx: dict[str, int] = {}

        self.last_latency_ms: float = 0.0
        self._fps_count: int = 0
        self._fps_window_start: float = time.time()
        self.fps: float = 0.0

    def _ensure_model(self) -> None:
        if self._yolo is not None:
            return
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

    def process(self, frame_bgr: Any, *, classes: set[str] | None = None) -> YoloOutput:
        _require_opencv()
        self._ensure_model()
        if self._yolo is None:
            raise RuntimeError("YOLO model is not initialized")

        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        classes_idx = self._resolve_classes(classes)

        t0 = time.time()
        res = self._yolo.track(
            source=rgb,
            imgsz=self.img_size,
            conf=self.conf,
            iou=self.iou,
            classes=classes_idx,
            verbose=False,
            device=self.device if self.device else None,
            tracker=self._tracker_arg,
            persist=True,
            stream=False,
        )
        t1 = time.time()
        self.last_latency_ms = (t1 - t0) * 1000.0

        self._fps_count += 1
        elapsed = t1 - self._fps_window_start
        if elapsed >= 1.5:
            self.fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_window_start = t1

        result = res[0]

        names = getattr(self._yolo, "names", {})

        objects: list[TrackedObject] = []
        try:
            boxes = result.boxes  # type: ignore[attr-defined]
        except Exception:
            boxes = []

        for box in boxes:
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
                try:
                    track_id = int(box.id[0])
                except Exception:
                    track_id = None

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
        )

