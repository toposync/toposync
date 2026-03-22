from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Any

from toposync.runtime.config_store import Pipeline


CAMERAS_EXTENSION_ID = "com.toposync.cameras"

_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_pipeline_name(value: str) -> str:
    raw = str(value or "").strip()
    cleaned = _NAME_CLEAN_RE.sub("_", raw).strip("_")
    if not cleaned:
        cleaned = "pipeline"
    if not re.match(r"^[A-Za-z_]", cleaned):
        cleaned = f"_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned[:120]


@dataclass(frozen=True, slots=True)
class LegacyCameraRule:
    camera_id: str
    camera_name: str
    processing_server_id: str
    rule_id: str
    trigger_kind: str
    category: str
    raw_rule: dict[str, Any]


def _as_record(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _as_str(v: Any) -> str:
    return str(v) if isinstance(v, str) else ""


def extract_legacy_camera_rules(settings: dict[str, Any]) -> list[LegacyCameraRule]:
    extensions = settings.get("extensions") if isinstance(settings.get("extensions"), dict) else {}
    ext = extensions.get(CAMERAS_EXTENSION_ID) if isinstance(extensions.get(CAMERAS_EXTENSION_ID), dict) else {}
    cameras_raw = _as_list(ext.get("cameras"))

    rules: list[LegacyCameraRule] = []
    for cam_item in cameras_raw:
        cam = _as_record(cam_item)
        camera_id = _as_str(cam.get("id")).strip()
        if not camera_id:
            continue
        enabled = cam.get("enabled")
        if enabled is False:
            continue
        camera_name = _as_str(cam.get("name")).strip()
        processing_server_id = _as_str(cam.get("processing_server_id")).strip() or "local"
        for rule_item in _as_list(cam.get("detections")):
            rule = _as_record(rule_item)
            rule_id = _as_str(rule.get("id")).strip()
            if not rule_id:
                continue
            trigger = _as_record(rule.get("trigger"))
            trigger_kind = (_as_str(trigger.get("kind")).strip() or "motion").lower()
            category = _as_str(trigger.get("category")).strip().lower()
            rules.append(
                LegacyCameraRule(
                    camera_id=camera_id,
                    camera_name=camera_name,
                    processing_server_id=processing_server_id,
                    rule_id=rule_id,
                    trigger_kind=trigger_kind,
                    category=category,
                    raw_rule=rule,
                ),
            )
    return rules


def build_pipeline_from_legacy_camera_rule(rule: LegacyCameraRule, *, existing_names: set[str]) -> Pipeline | None:
    trigger_kind = str(rule.trigger_kind or "").strip().lower() or "motion"
    base_name = _safe_pipeline_name(f"legacy_{rule.camera_id}_{rule.rule_id}")
    name = base_name
    suffix = 2
    while name in existing_names:
        name = _safe_pipeline_name(f"{base_name}_{suffix}")
        suffix += 1
    existing_names.add(name)

    if trigger_kind == "motion":
        graph = {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "source",
                    "operator": "camera.source",
                    "config": {"camera_id": rule.camera_id},
                },
                {
                    "id": "motion",
                    "operator": "camera.motion_gate",
                    "config": {"emit_when_idle": True},
                },
                {
                    "id": "lifecycle",
                    "operator": "core.lifecycle_from_boolean",
                    "config": {"field": "metadata.motion_gate_open"},
                },
                {
                    "id": "best",
                    "operator": "camera.best_frame_selector",
                    "config": {},
                },
                {
                    "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "artifact_names": ["best_frame", "frame_original"],
                        "subdir": "pipelines",
                        "format": "png",
                        "drop_data_after_store": True,
                    },
                },
                {
                    "id": "notify",
                    "operator": "core.notify",
                    "config": {
                        "notification_type": "pipelines.event",
                        "title": "Motion detected!",
                        "description": "{{camera_name}}",
                        "priority": "medium",
                        "update_interval_seconds": 1.0,
                        "thumbnail_with_fallback": ["best_frame", "frame_original"],
                    },
                },
            ],
            "edges": [
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "motion", "port": "in"},
                    "maxsize": 2,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "motion", "port": "out"},
                    "to": {"node": "lifecycle", "port": "in"},
                    "maxsize": 2,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "lifecycle", "port": "out"},
                    "to": {"node": "best", "port": "in"},
                    "maxsize": 8,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "best", "port": "out"},
                    "to": {"node": "store", "port": "in"},
                    "maxsize": 16,
                    "drop_policy": "drop_oldest",
                },
                {
                    "from": {"node": "store", "port": "out"},
                    "to": {"node": "notify", "port": "in"},
                    "maxsize": 16,
                    "drop_policy": "drop_oldest",
                },
            ],
        }

        return Pipeline(
            name=name,
            type="final",
            enabled=True,
            processing_server_id=rule.processing_server_id or "local",
            editor_mode="json",
            python_source="",
            graph=graph,
        )

    if trigger_kind != "object":
        return None

    categories = [rule.category] if rule.category else []

    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "source",
                "operator": "camera.source",
                "config": {"camera_id": rule.camera_id},
            },
            {
                "id": "motion",
                "operator": "camera.motion_gate",
                "config": {},
            },
            {
                "id": "detect",
                "operator": "vision.detect",
                "config": {
                    "model_id": "rtmdet_det_small",
                    "categories": categories,
                    "emit_mode": "annotate",
                },
            },
            {
                "id": "track",
                "operator": "vision.track",
                "config": {"tracker_id": "simple_iou_kalman"},
            },
            {
                "id": "best",
                "operator": "camera.best_frame_selector",
                "config": {},
            },
            {
                "id": "store",
                    "operator": "core.store_images",
                    "config": {
                        "artifact_names": ["best_frame", "frame_original"],
                        "subdir": "pipelines",
                        "format": "png",
                        "drop_data_after_store": True,
                    },
                },
            {
                "id": "notify",
                "operator": "core.notify",
                "config": {
                    "notification_type": "pipelines.tracking",
                    "title": "{{object_category_label}} detectada!",
                    "description": "Está em {{area_label}} ({{camera_name}})",
                    "priority": "medium",
                    "update_interval_seconds": 1.0,
                    "thumbnail_with_fallback": ["best_frame", "frame_original"],
                },
            },
        ],
        "edges": [
            {
                "from": {"node": "source", "port": "out"},
                "to": {"node": "motion", "port": "in"},
                "maxsize": 2,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "motion", "port": "out"},
                "to": {"node": "detect", "port": "in"},
                "maxsize": 2,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "detect", "port": "out"},
                "to": {"node": "track", "port": "in"},
                "maxsize": 2,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "track", "port": "out"},
                "to": {"node": "best", "port": "in"},
                "maxsize": 8,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "best", "port": "out"},
                "to": {"node": "store", "port": "in"},
                "maxsize": 16,
                "drop_policy": "drop_oldest",
            },
            {
                "from": {"node": "store", "port": "out"},
                "to": {"node": "notify", "port": "in"},
                "maxsize": 16,
                "drop_policy": "drop_oldest",
            },
        ],
    }

    return Pipeline(
        name=name,
        type="final",
        enabled=True,
        processing_server_id=rule.processing_server_id or "local",
        editor_mode="json",
        python_source="",
        graph=graph,
    )
