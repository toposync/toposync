from __future__ import annotations

from typing import Any, Literal

from toposync.runtime.pipelines.templates import safe_pipeline_name

WizardPresetId = Literal[
    "simple_stream",
    "motion_gate_stream",
    "detection_stream",
    "tracking_stream",
    "segmentation_stream",
]

STREAMING_WIZARD_PRESETS: tuple[WizardPresetId, ...] = (
    "simple_stream",
    "motion_gate_stream",
    "detection_stream",
    "tracking_stream",
    "segmentation_stream",
)


def suggested_streaming_wizard_pipeline_name(
    *,
    transmission_id: str,
    camera_id: str,
    preset_id: WizardPresetId,
) -> str:
    base = f"stream_{preset_id}__{camera_id}__{transmission_id}"
    return safe_pipeline_name(base)


def build_streaming_wizard_graph(
    *,
    transmission_id: str,
    camera_id: str,
    preset_id: WizardPresetId,
    optional_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if preset_id not in STREAMING_WIZARD_PRESETS:
        raise ValueError("Unknown preset_id")

    options = optional_parameters if isinstance(optional_parameters, dict) else {}

    source_backend = _pick_choice(options.get("source_backend"), allowed={"auto", "opencv", "ffmpeg"}, default="auto")
    resize_mode = _pick_choice(options.get("resize_mode"), allowed={"contain", "none"}, default="contain")
    bypass_mode = _pick_choice(options.get("bypass_mode"), allowed={"auto", "force_on", "force_off"}, default="auto")
    writer_priority = _coerce_int(options.get("writer_priority"), default=0)

    fps_limit = _coerce_float(options.get("fps_limit"), default=0.0, min_value=0.0, max_value=60.0)
    default_motion_fps = 5.0
    use_fps_reducer_flag = options.get("use_fps_reducer")
    force_fps_reducer = preset_id == "motion_gate_stream"
    use_fps_reducer = force_fps_reducer or bool(use_fps_reducer_flag) or fps_limit > 0.0
    target_fps = fps_limit if fps_limit > 0.0 else default_motion_fps

    motion_sensitivity = _coerce_float(options.get("motion_sensitivity"), default=0.010, min_value=0.0001, max_value=1.0)
    motion_hold_seconds = _coerce_float(options.get("motion_hold_seconds"), default=6.0, min_value=0.0, max_value=120.0)
    yolo_confidence = _coerce_float(options.get("yolo_confidence_threshold"), default=0.55, min_value=0.01, max_value=1.0)
    yolo_filter_enabled = _coerce_bool(options.get("yolo_filter_enabled"), default=True)
    yolo_emit_mode = "events" if yolo_filter_enabled else "annotate"

    detection_categories = _sanitize_categories(options.get("detection_categories"))
    tracking_categories = _sanitize_categories(options.get("tracking_categories"))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    nodes.append(
        {
            "id": "source",
            "operator": "camera.source",
            "config": {
                "camera_id": camera_id,
                "backend": source_backend,
            },
        }
    )

    current_node_id = "source"

    if use_fps_reducer:
        nodes.append(
            {
                "id": "fps",
                "operator": "core.fps_reducer",
                "config": {"target_fps": float(target_fps)},
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="fps", maxsize=2)
        current_node_id = "fps"

    if preset_id == "motion_gate_stream":
        nodes.append(
            {
                "id": "motion",
                "operator": "camera.motion_gate",
                "config": {
                    "threshold": float(motion_sensitivity),
                    "activation_frames": 2,
                    "hold_seconds": float(motion_hold_seconds),
                    "emit_when_idle": False,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="motion", maxsize=2)
        current_node_id = "motion"

    if preset_id == "detection_stream":
        nodes.append(
            {
                "id": "detect",
                "operator": "vision.object_detection_yolo",
                "config": {
                    "categories": detection_categories,
                    "confidence_threshold": float(yolo_confidence),
                    "emit_mode": yolo_emit_mode,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="detect", maxsize=2)
        current_node_id = "detect"

    if preset_id == "tracking_stream":
        nodes.append(
            {
                "id": "track",
                "operator": "vision.object_tracking_yolo",
                "config": {
                    "categories": tracking_categories,
                    "confidence_threshold": float(yolo_confidence),
                    "close_after_seconds": 5.0,
                    "emit_mode": yolo_emit_mode,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="track", maxsize=2)
        current_node_id = "track"

    if preset_id == "segmentation_stream":
        nodes.append(
            {
                "id": "segment",
                "operator": "camera.object_segmentation",
                "config": {},
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="segment", maxsize=4)
        current_node_id = "segment"

    nodes.append(
        {
            "id": "stream",
            "operator": "stream.publish_video",
            "config": {
                "transmission_id": transmission_id,
                "frame_with_fallback": ["frame", "best_frame", "segmented", "frame_original"],
                "resize_mode": resize_mode,
                "writer_priority": int(writer_priority),
                "bypass_mode": bypass_mode,
            },
        }
    )
    _append_edge(edges, source_node_id=current_node_id, target_node_id="stream", maxsize=8)

    return {"schema_version": 1, "nodes": nodes, "edges": edges}


def _append_edge(
    edges: list[dict[str, Any]],
    *,
    source_node_id: str,
    target_node_id: str,
    maxsize: int,
) -> None:
    edges.append(
        {
            "from": {"node": source_node_id, "port": "out"},
            "to": {"node": target_node_id, "port": "in"},
            "maxsize": int(max(1, maxsize)),
            "drop_policy": "drop_oldest",
        }
    )


def _pick_choice(value: Any, *, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return int(parsed)


def _coerce_float(
    value: Any,
    *,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    parsed = max(float(min_value), min(float(max_value), parsed))
    return float(parsed)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _sanitize_categories(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        category = str(item or "").strip().lower()
        if not category or category in seen:
            continue
        seen.add(category)
        normalized.append(category)
    return normalized
