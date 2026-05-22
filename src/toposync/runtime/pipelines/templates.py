from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Any


CAMERAS_EXTENSION_ID = "com.toposync.cameras"

_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]+")


class PipelineTemplateError(ValueError):
    pass


def safe_pipeline_name(value: str) -> str:
    raw = str(value or "").strip()
    cleaned = _NAME_CLEAN_RE.sub("_", raw).strip("_")
    if not cleaned:
        cleaned = "pipeline"
    if not re.match(r"^[A-Za-z_]", cleaned):
        cleaned = f"_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned[:120]


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""


def camera_names_by_id(extensions_settings: dict[str, Any]) -> dict[str, str]:
    ext = extensions_settings.get(CAMERAS_EXTENSION_ID)
    ext_record = ext if isinstance(ext, dict) else {}
    devices_raw = _as_list(ext_record.get("devices"))

    out: dict[str, str] = {}
    for item in devices_raw:
        camera = _as_record(item)
        camera_id = _as_str(camera.get("id")).strip()
        if not camera_id:
            continue
        name = _as_str(camera.get("name")).strip()
        out[camera_id] = name
    return out


def camera_default_source_ids_by_id(extensions_settings: dict[str, Any]) -> dict[str, str]:
    ext = extensions_settings.get(CAMERAS_EXTENSION_ID)
    ext_record = ext if isinstance(ext, dict) else {}
    devices_raw = _as_list(ext_record.get("devices"))

    out: dict[str, str] = {}
    for item in devices_raw:
        camera = _as_record(item)
        camera_id = _as_str(camera.get("id")).strip()
        if not camera_id:
            continue
        sources = [_as_record(source) for source in _as_list(camera.get("sources"))]
        video_sources = [
            source
            for source in sources
            if _as_str(source.get("kind")).strip().lower() in {"", "video"}
            and bool(source.get("enabled", True))
        ]
        selected = next((source for source in video_sources if bool(source.get("is_default"))), None)
        if selected is None and len(video_sources) == 1:
            selected = video_sources[0]
        source_id = _as_str(selected.get("id")).strip() if selected is not None else ""
        if source_id:
            out[camera_id] = source_id
    return out


@dataclass(frozen=True, slots=True)
class CameraTemplateResult:
    pipeline_name: str
    graph: dict[str, Any]


def instantiate_camera_template_graph(
    *,
    template_graph: dict[str, Any],
    camera_id: str,
    camera_source_id: str,
) -> dict[str, Any]:
    graph = dict(template_graph or {})
    raw_nodes = graph.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []

    updated_nodes: list[dict[str, Any]] = []
    camera_source_nodes: list[str] = []
    for item in nodes:
        node = item if isinstance(item, dict) else {}
        operator_id = str(node.get("operator") or "").strip()
        next_node = dict(node)
        if operator_id == "camera.source":
            cfg = dict(_as_record(node.get("config")))
            cfg["camera_id"] = str(camera_id or "").strip()
            cfg["source_id"] = str(camera_source_id or "").strip()
            cfg["rtsp_url"] = ""
            cfg["username"] = ""
            cfg["password"] = ""
            next_node["config"] = cfg
            camera_source_nodes.append(str(node.get("id") or ""))
        updated_nodes.append(next_node)

    if not camera_source_nodes:
        raise PipelineTemplateError("Template graph has no camera.source node to instantiate")
    if len(camera_source_nodes) > 1:
        raise PipelineTemplateError(
            "Template graph has multiple camera.source nodes; this endpoint currently supports exactly one",
        )

    graph["nodes"] = updated_nodes
    return graph


def default_instance_name(*, template_name: str, camera_id: str, camera_source_id: str = "") -> str:
    # Keep the name predictable and compatible with a Python identifier.
    source = _as_str(camera_source_id).strip()
    suffix = f"{camera_id}__{source}" if source else camera_id
    return safe_pipeline_name(f"{template_name}__{suffix}")
