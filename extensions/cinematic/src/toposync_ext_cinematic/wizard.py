from __future__ import annotations

from typing import Any

from toposync.runtime.pipelines.templates import safe_pipeline_name

from .constants import OPERATOR_ID_DIRECTOR_SOURCE


def suggested_cinematic_pipeline_name(
    *,
    transmission_id: str,
    transmission_name: str = "",
    transmission_path: str = "",
) -> str:
    label = transmission_path or transmission_name or transmission_id or "cinematic"
    return safe_pipeline_name(f"{label}_cinematic")


def unique_cinematic_pipeline_name(base: str, *, existing_names: set[str]) -> str:
    normalized = safe_pipeline_name(base)
    if normalized not in existing_names:
        return normalized
    suffix = 2
    while True:
        candidate = safe_pipeline_name(f"{normalized}_{suffix}")
        if candidate not in existing_names:
            return candidate
        suffix += 1


def build_cinematic_wizard_graph(
    *,
    transmission_id: str,
    optional_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transmission = str(transmission_id or "").strip()
    if not transmission:
        raise ValueError("transmission_id is required")

    options = optional_parameters if isinstance(optional_parameters, dict) else {}
    director_config = _director_config(options)

    demand_config = {
        "transmission_id": transmission,
        "output_id": _safe_text(options.get("demand_gate_output_id")),
        "quality_profile_id": _safe_text(options.get("demand_gate_quality_profile_id")),
        "poll_interval_ms": _coerce_int(
            options.get("demand_gate_poll_interval_ms"),
            default=500,
            min_value=100,
            max_value=10_000,
        ),
        "fail_open": _coerce_bool(options.get("demand_gate_fail_open"), default=True),
    }
    publish_config = {
        "transmission_id": transmission,
        "resize_mode": _pick_choice(options.get("resize_mode"), allowed={"contain", "none"}, default="contain"),
        "writer_priority": _coerce_int(options.get("writer_priority"), default=0),
        "publication_enabled": True,
        "publication_role": "custom",
        "publication_label": _safe_text(options.get("publication_label")) or "Cinematic",
    }

    return {
        "schema_version": 1,
        "nodes": [
            {"id": "demand", "operator": "stream.demand_gate", "config": demand_config},
            {"id": "director", "operator": OPERATOR_ID_DIRECTOR_SOURCE, "config": director_config},
            {"id": "publish", "operator": "stream.publish_video", "config": publish_config},
        ],
        "edges": [
            {
                "from": {"node": "demand", "port": "out"},
                "to": {"node": "director", "port": "gate"},
                "maxsize": 1,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "director", "port": "out"},
                "to": {"node": "publish", "port": "in"},
                "maxsize": 1,
                "drop_policy": "latest_only",
            },
        ],
    }


def _director_config(options: dict[str, Any]) -> dict[str, Any]:
    behavior = _pick_choice(
        options.get("behavior"),
        allowed={"rotation_with_events", "primary_with_events"},
        default="rotation_with_events",
    )
    cameras_mode = _pick_choice(options.get("cameras_mode"), allowed={"all", "include", "exclude"}, default="all")
    primary_camera_id = _safe_text(options.get("primary_camera_id"))
    camera_ids = _text_list(options.get("camera_ids"))
    if cameras_mode == "all":
        camera_ids = []
    elif behavior == "primary_with_events" and primary_camera_id:
        if cameras_mode == "include" and primary_camera_id not in camera_ids:
            camera_ids.insert(0, primary_camera_id)
        if cameras_mode == "exclude":
            camera_ids = [camera_id for camera_id in camera_ids if camera_id != primary_camera_id]

    return {
        "behavior": behavior,
        "cameras_mode": cameras_mode,
        "camera_ids": camera_ids,
        "primary_camera_id": primary_camera_id,
        "priority_filter": _priority_list(options.get("priority_filter")),
        "include_pipelines": _text_list(options.get("include_pipelines")),
        "exclude_pipelines": _text_list(options.get("exclude_pipelines")),
        "pipeline_camera_map": _text_map(options.get("pipeline_camera_map")),
        "manual_camera_priorities": _int_map(options.get("manual_camera_priorities")),
        "manual_event_type_priorities": _int_map(options.get("manual_event_type_priorities")),
        "preferred_source_role": _pick_choice(
            options.get("preferred_source_role"),
            allowed={"main", "sub", "zoom", "auto"},
            default="auto",
        ),
        "idle_dwell_seconds": _coerce_float(options.get("idle_dwell_seconds"), default=8.0, min_value=2.0, max_value=120.0),
        "event_min_seconds": _coerce_float(options.get("event_min_seconds"), default=10.0, min_value=1.0, max_value=300.0),
        "cut_cooldown_seconds": _coerce_float(options.get("cut_cooldown_seconds"), default=1.5, min_value=0.0, max_value=60.0),
        "close_hold_seconds": _coerce_float(options.get("close_hold_seconds"), default=3.0, min_value=0.0, max_value=60.0),
        "current_camera_sticky_seconds": _coerce_float(
            options.get("current_camera_sticky_seconds"),
            default=4.0,
            min_value=0.0,
            max_value=60.0,
        ),
        "max_event_hold_seconds": _coerce_float(options.get("max_event_hold_seconds"), default=60.0, min_value=5.0, max_value=3600.0),
        "max_cuts_per_minute": _coerce_int(options.get("max_cuts_per_minute"), default=12, min_value=1, max_value=120),
        "fps": _coerce_float(options.get("fps"), default=8.0, min_value=1.0, max_value=60.0),
        "width": _coerce_int(options.get("width"), default=1280, min_value=160, max_value=7680),
        "height": _coerce_int(options.get("height"), default=720, min_value=90, max_value=4320),
        "warmup_mode": _pick_choice(options.get("warmup_mode"), allowed={"off", "next_idle", "event_high", "adaptive"}, default="off"),
        "max_warm_cameras": _coerce_int(options.get("max_warm_cameras"), default=0, min_value=0, max_value=8),
        "handoff_timeout_seconds": _coerce_float(options.get("handoff_timeout_seconds"), default=3.0, min_value=0.1, max_value=30.0),
        "stale_frame_max_age_seconds": _coerce_float(
            options.get("stale_frame_max_age_seconds"),
            default=2.0,
            min_value=0.1,
            max_value=30.0,
        ),
        "ignore_own_pipeline_events": _coerce_bool(options.get("ignore_own_pipeline_events"), default=True),
    }


def _pick_choice(value: Any, *, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_float(value: Any, *, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return float(parsed)


def _coerce_int(value: Any, *, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return int(parsed)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _text_list(value: Any) -> list[str]:
    raw = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _safe_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _priority_list(value: Any) -> list[str]:
    return [item for item in _text_list(value) if item in {"silent", "low", "medium", "high"}]


def _text_map(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    out: dict[str, str] = {}
    for key, item in raw.items():
        normalized_key = _safe_text(key)
        normalized_item = _safe_text(item)
        if normalized_key and normalized_item:
            out[normalized_key] = normalized_item
    return out


def _int_map(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    out: dict[str, int] = {}
    for key, item in raw.items():
        normalized_key = _safe_text(key)
        if not normalized_key:
            continue
        out[normalized_key] = _coerce_int(item, default=0)
    return out
