from __future__ import annotations

import unicodedata
from typing import Any, Literal

from toposync.runtime.pipelines.templates import safe_pipeline_name

WizardPresetId = Literal[
    "simple_stream",
    "motion_gate_stream",
    "detection_stream",
    "tracking_stream",
    "segmentation_stream",
]
StreamBehavior = Literal["continuous", "event_gated"]

STREAMING_WIZARD_PRESETS: tuple[WizardPresetId, ...] = (
    "simple_stream",
    "motion_gate_stream",
    "detection_stream",
    "tracking_stream",
    "segmentation_stream",
)

DEFAULT_STREAMING_DETECTION_MODEL_ID = "rfdetr_det_medium"

_GENERIC_NAME_COMPONENTS = {"stream", "transmission", "transmissao", "fluxo"}
_PRESET_NAME_COMPONENTS: dict[str, str] = {
    "simple_stream": "stream",
    "motion_gate_stream": "motion",
    "detection_stream": "detection",
    "tracking_stream": "tracking",
    "segmentation_stream": "segmentation",
}


def suggested_streaming_wizard_pipeline_name(
    *,
    transmission_id: str,
    camera_id: str,
    camera_source_id: str = "",
    preset_id: WizardPresetId,
    transmission_name: str | None = None,
    transmission_path: str | None = None,
    camera_name: str | None = None,
    camera_source_name: str | None = None,
) -> str:
    transmission_component = _pick_name_component(
        transmission_path,
        transmission_name,
        transmission_id,
        fallback="stream",
        skip_generic=True,
    )
    camera_component = _pick_name_component(camera_id, camera_name)
    source_component = _pick_name_component(camera_source_id, camera_source_name, fallback="", skip_generic=True)
    preset_component = _PRESET_NAME_COMPONENTS.get(str(preset_id), "stream")

    components = [transmission_component, camera_component, source_component, preset_component]
    if camera_component and _is_generic_component(transmission_component):
        components = [camera_component, source_component, preset_component]

    base = "__".join(_dedupe_name_components(components))
    return safe_pipeline_name(base)


def build_streaming_wizard_graph(
    *,
    transmission_id: str,
    camera_id: str,
    camera_source_id: str = "main",
    preset_id: WizardPresetId,
    optional_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if preset_id not in STREAMING_WIZARD_PRESETS:
        raise ValueError("Unknown preset_id")

    options = optional_parameters if isinstance(optional_parameters, dict) else {}

    source_backend = _pick_choice(options.get("source_backend"), allowed={"auto", "opencv", "ffmpeg"}, default="auto")
    resize_mode = _pick_choice(options.get("resize_mode"), allowed={"contain", "none"}, default="contain")
    bypass_mode = _pick_choice(options.get("bypass_mode"), allowed={"auto", "force_on", "force_off"}, default="auto")
    stream_behavior = _pick_choice(
        options.get("stream_behavior"),
        allowed={"continuous", "event_gated"},
        default="continuous",
    )
    event_gated = stream_behavior == "event_gated"
    writer_priority = _coerce_int(options.get("writer_priority"), default=0)
    demand_gate = _coerce_bool(options.get("demand_gate"), default=False)
    demand_gate_fail_open = _coerce_bool(options.get("demand_gate_fail_open"), default=True)
    demand_gate_output_id = _safe_text(options.get("demand_gate_output_id"))
    demand_gate_quality_profile_id = _safe_text(options.get("demand_gate_quality_profile_id"))
    demand_gate_poll_interval_ms = _coerce_int(
        options.get("demand_gate_poll_interval_ms"),
        default=500,
        min_value=100,
        max_value=10_000,
    )

    fps_limit = _coerce_float(options.get("fps_limit"), default=0.0, min_value=0.0, max_value=60.0)
    default_motion_fps = 5.0
    use_fps_reducer_flag = options.get("use_fps_reducer")
    force_fps_reducer = preset_id == "motion_gate_stream"
    use_fps_reducer = force_fps_reducer or bool(use_fps_reducer_flag) or fps_limit > 0.0
    target_fps = fps_limit if fps_limit > 0.0 else default_motion_fps

    motion_sensitivity = _coerce_float(options.get("motion_sensitivity"), default=0.010, min_value=0.0001, max_value=1.0)
    motion_hold_seconds = _coerce_float(options.get("motion_hold_seconds"), default=6.0, min_value=0.0, max_value=120.0)
    yolo_confidence = _coerce_float(options.get("yolo_confidence_threshold"), default=0.55, min_value=0.01, max_value=1.0)
    tracking_detection_confidence = _coerce_float(
        options.get("yolo_confidence_threshold"),
        default=0.25,
        min_value=0.01,
        max_value=1.0,
    )
    yolo_filter_enabled = _coerce_bool(options.get("yolo_filter_enabled"), default=True)
    detection_emit_mode = "filter" if event_gated and yolo_filter_enabled else "annotate"

    detection_categories = _sanitize_categories(options.get("detection_categories"))
    tracking_categories = _sanitize_categories(options.get("tracking_categories"))
    segmentation_categories = _sanitize_categories(options.get("segmentation_categories"))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    nodes.append(
        {
            "id": "source",
            "operator": "camera.source",
            "config": {
                "camera_id": camera_id,
                "source_id": camera_source_id,
                "backend": source_backend,
            },
        }
    )

    current_node_id = "source"
    if demand_gate:
        nodes.append(
            {
                "id": "demand",
                "operator": "stream.demand_gate",
                "config": {
                    "transmission_id": transmission_id,
                    "output_id": demand_gate_output_id,
                    "quality_profile_id": demand_gate_quality_profile_id,
                    "poll_interval_ms": int(demand_gate_poll_interval_ms),
                    "fail_open": bool(demand_gate_fail_open),
                },
            }
        )
        _append_gate_edge(edges, source_node_id="demand", target_node_id="source")

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

    continuous_stream_source_node_id = current_node_id
    if not event_gated:
        _append_stream_node(
            nodes,
            transmission_id=transmission_id,
            resize_mode=resize_mode,
            writer_priority=writer_priority,
            bypass_mode=bypass_mode,
        )
        _append_edge(
            edges,
            source_node_id=continuous_stream_source_node_id,
            target_node_id="stream",
            maxsize=8,
        )

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
                "operator": "vision.detect",
                "config": {
                    "model_id": DEFAULT_STREAMING_DETECTION_MODEL_ID,
                    "categories": detection_categories,
                    "confidence_threshold": float(yolo_confidence),
                    "emit_mode": detection_emit_mode,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="detect", maxsize=2)
        current_node_id = "detect"

    if preset_id == "tracking_stream":
        nodes.append(
            {
                "id": "detect",
                "operator": "vision.detect",
                "config": {
                    "model_id": DEFAULT_STREAMING_DETECTION_MODEL_ID,
                    "categories": tracking_categories,
                    "confidence_threshold": float(tracking_detection_confidence),
                    "emit_mode": "annotate",
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="detect", maxsize=2)
        current_node_id = "detect"
        nodes.append(
            {
                "id": "track",
                "operator": "vision.track",
                "config": {
                    "tracker_id": "byte_world",
                    "open_confidence_threshold": 0.50,
                    "continue_confidence_threshold": 0.25,
                    "close_after_seconds": 10.0,
                    "stitch_gap_seconds": 30.0,
                    "default_interval_seconds": 0.25,
                    "use_world_anchor": "auto",
                    "world_match_distance_meters": 3.0,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="track", maxsize=2)
        current_node_id = "track"

    if preset_id == "segmentation_stream":
        nodes.append(
            {
                "id": "segment",
                "operator": "vision.segment_instances",
                "config": {
                    "model_id": "rtmdet_ins_small",
                    "categories": segmentation_categories,
                    "attach_mask_artifacts": True,
                },
            }
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="segment", maxsize=4)
        current_node_id = "segment"

    if event_gated:
        _append_stream_node(
            nodes,
            transmission_id=transmission_id,
            resize_mode=resize_mode,
            writer_priority=writer_priority,
            bypass_mode=bypass_mode,
        )
        _append_edge(edges, source_node_id=current_node_id, target_node_id="stream", maxsize=8)

    return {
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "streaming": {
                "transmission_id": transmission_id,
                "camera_id": camera_id,
                "camera_source_id": camera_source_id,
                "preset_id": preset_id,
                "stream_behavior": stream_behavior,
                "demand_driven": bool(demand_gate),
            },
        },
    }


def _append_stream_node(
    nodes: list[dict[str, Any]],
    *,
    transmission_id: str,
    resize_mode: str,
    writer_priority: int,
    bypass_mode: str,
) -> None:
    nodes.append(
        {
            "id": "stream",
            "operator": "stream.publish_video",
            "config": {
                "transmission_id": transmission_id,
                "resize_mode": resize_mode,
                "writer_priority": int(writer_priority),
                "bypass_mode": bypass_mode,
            },
        }
    )


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


def _append_gate_edge(
    edges: list[dict[str, Any]],
    *,
    source_node_id: str,
    target_node_id: str,
) -> None:
    edges.append(
        {
            "from": {"node": source_node_id, "port": "out"},
            "to": {"node": target_node_id, "port": "gate"},
            "maxsize": 1,
            "drop_policy": "drop_oldest",
        }
    )


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _pick_name_component(
    *values: str | None,
    fallback: str = "",
    skip_generic: bool = False,
) -> str:
    candidates = [_safe_name_component(value) for value in values]
    candidates = [item for item in candidates if item]
    for candidate in candidates:
        if skip_generic and _is_generic_component(candidate):
            continue
        if _is_uuid_component(candidate):
            continue
        return candidate
    for candidate in candidates:
        if skip_generic and (_is_generic_component(candidate) or _is_uuid_component(candidate)):
            continue
        return candidate
    return _safe_name_component(fallback)


def _dedupe_name_components(values: list[str]) -> list[str]:
    components: list[str] = []
    seen: set[str] = set()
    for value in values:
        component = _safe_name_component(value)
        key = _component_key(component)
        if not component or not key or key in seen:
            continue
        seen.add(key)
        components.append(component)
    return components


def _safe_name_component(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    ascii_raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    return safe_pipeline_name(ascii_raw or raw)


def _component_key(value: str | None) -> str:
    return _safe_name_component(value).strip("_").lower()


def _is_generic_component(value: str | None) -> bool:
    return _component_key(value) in _GENERIC_NAME_COMPONENTS


def _is_uuid_component(value: str | None) -> bool:
    key = _component_key(value).replace("_", "-")
    if len(key) != 36:
        return False
    for index, char in enumerate(key):
        if index in {8, 13, 18, 23}:
            if char != "-":
                return False
            continue
        if char not in "0123456789abcdef":
            return False
    return True


def _pick_choice(value: Any, *, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    return default


def _coerce_int(
    value: Any,
    *,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if min_value is not None:
        parsed = max(int(min_value), parsed)
    if max_value is not None:
        parsed = min(int(max_value), parsed)
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
