from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from toposync.runtime.pipelines.execution import SourceOperatorRuntime
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.packet_contract import build_media_descriptor, build_source_descriptor
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet

from ...pipelines.schemas import VisionSyntheticDetectionConfig, VisionSyntheticDetectionSourceConfig
from ..contracts import DetectionObject


class VisionSyntheticDetectionSourceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any]) -> None:
        parsed = VisionSyntheticDetectionSourceConfig.model_validate(config)
        self._stream_id = parsed.stream_id or "camera:synthetic"
        self._camera_id = parsed.camera_id or "synthetic-camera"
        self._camera_name = parsed.camera_name or "Synthetic Camera"
        self._source_id = parsed.source_id or "synthetic"
        self._source_name = parsed.source_name or "Synthetic"
        self._model_id = parsed.model_id or "synthetic.detector"
        self._width = int(parsed.width)
        self._height = int(parsed.height)
        self._frames = int(parsed.frames)
        self._interval_s = float(parsed.interval_seconds)
        self._close_on_last_frame = bool(parsed.close_on_last_frame)
        self._detections = list(parsed.detections)
        self._index = 0
        self._next_tick = time.monotonic()

    async def produce(self, context) -> Packet | None:  # noqa: ANN001
        if self._index >= self._frames:
            return None
        now = time.monotonic()
        if now < self._next_tick:
            await context.sleep(self._next_tick - now)
        self._next_tick = max(self._next_tick + self._interval_s, time.monotonic())

        lifecycle = Lifecycle.UPDATE
        if self._index == 0:
            lifecycle = Lifecycle.OPEN
        elif self._close_on_last_frame and self._index == self._frames - 1:
            lifecycle = Lifecycle.CLOSE

        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("vision.synthetic_detection_source requires numpy") from exc

        frame = np.full((self._height, self._width, 3), 34, dtype=np.uint8)
        detections = [self._detection_payload(item) for item in self._detections]
        self._draw_detections(frame, detections)

        current_ts = time.time()
        payload = {
            "source": build_source_descriptor(
                device_id=self._camera_id,
                source_id=self._source_id,
                source_name=self._source_name,
                view_id=self._source_id,
                role="main",
                kind="camera",
                modality="video",
                name=self._camera_name,
                transport="synthetic",
                clock_domain=f"device:{self._camera_id}",
            ),
            "media": build_media_descriptor(
                modality="video",
                ts=current_ts,
                width=self._width,
                height=self._height,
                frame_rate=(1.0 / self._interval_s) if self._interval_s > 0 else None,
            ),
            "frame_ts": current_ts,
            "frame_index": self._index,
            "camera_id": self._camera_id,
            "camera_name": self._camera_name,
            "frame_width": self._width,
            "frame_height": self._height,
            "source_stream_id": self._stream_id,
            "vision": {
                "task": "detection",
                "model_id": self._model_id,
                "runtime": "synthetic",
                "detections": detections,
            },
        }
        packet = Packet.create(
            stream_id=self._stream_id,
            lifecycle=lifecycle,
            payload=payload,
            artifacts={
                MAIN_ARTIFACT_NAME: Artifact(
                    name=MAIN_ARTIFACT_NAME,
                    data=frame,
                    mime_type="image/raw",
                    metadata={
                        "source": "vision.synthetic_detection_source",
                        "width": self._width,
                        "height": self._height,
                    },
                )
            },
            metadata={
                "source": "vision.synthetic_detection_source",
                "camera_id": self._camera_id,
                "motion_gate_open": True,
            },
        )
        self._index += 1
        return packet

    def _detection_payload(self, item: VisionSyntheticDetectionConfig) -> dict[str, Any]:
        detection = DetectionObject(
            label=item.label,
            label_id=item.label_id,
            score=float(item.score),
            bbox01=tuple(item.bbox01),
            model_id=self._model_id,
            metadata={"source": "vision.synthetic_detection_source"},
        )
        payload = asdict(detection)
        payload["bbox01"] = [float(value) for value in detection.bbox01]
        return payload

    def _draw_detections(self, frame: Any, detections: list[dict[str, Any]]) -> None:
        for item in detections:
            bbox = item.get("bbox01")
            if not isinstance(bbox, list) or len(bbox) < 4:
                continue
            x1 = max(0, min(self._width - 1, int(float(bbox[0]) * self._width)))
            y1 = max(0, min(self._height - 1, int(float(bbox[1]) * self._height)))
            x2 = max(0, min(self._width - 1, int(float(bbox[2]) * self._width)))
            y2 = max(0, min(self._height - 1, int(float(bbox[3]) * self._height)))
            if x2 <= x1 or y2 <= y1:
                continue
            frame[y1:y2, x1:x2, 1] = 160
            frame[y1:y2, x1:x2, 2] = 80
            frame[y1 : min(y1 + 3, y2), x1:x2, :] = (240, 240, 240)
            frame[max(y2 - 3, y1) : y2, x1:x2, :] = (240, 240, 240)
            frame[y1:y2, x1 : min(x1 + 3, x2), :] = (240, 240, 240)
            frame[y1:y2, max(x2 - 3, x1) : x2, :] = (240, 240, 240)
