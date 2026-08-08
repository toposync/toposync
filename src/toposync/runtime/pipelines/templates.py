from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import Any


CAMERAS_EXTENSION_ID = "com.toposync.cameras"

_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]+")
_VIDEO_OPERATORS = {
    "camera.source",
    "camera.motion_bgsub_adaptive",
    "camera.motion_gate",
    "camera.camera_mapping",
    "cinematic.director_source",
    "core.demo_frame_sequence_source",
    "core.fps_reducer",
    "stream.publish_video",
    "vision.detect",
    "vision.segment_instances",
    "vision.track",
}


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
        selected = next(
            (source for source in video_sources if bool(source.get("is_default"))), None
        )
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


def build_pipeline_graph_v2(
    *,
    graph_uid: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    layout: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operators_by_node = {
        str(node.get("id") or "").strip(): str(
            node.get("operator") or node.get("operator_id") or ""
        ).strip()
        for node in nodes
        if isinstance(node, dict)
    }
    graph: dict[str, Any] = {
        "schema_version": 2,
        "uid": safe_pipeline_name(graph_uid or "graph"),
        "nodes": [_graph_v2_node(node) for node in nodes],
        "edges": [
            _graph_v2_edge(edge, index=index, operators_by_node=operators_by_node)
            for index, edge in enumerate(edges)
        ],
    }
    if layout:
        graph["layout"] = dict(layout)
    if limits:
        graph["limits"] = dict(limits)
    if meta:
        graph["meta"] = dict(meta)
    return graph


def _graph_v2_node(node: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("id") or "").strip()
    operator_id = str(node.get("operator") or node.get("operator_id") or "").strip()
    out = {
        "uid": str(node.get("uid") or node_id),
        "id": node_id,
        "operator": operator_id,
        "config": dict(_as_record(node.get("config"))),
    }
    if isinstance(node.get("ui"), dict):
        out["ui"] = dict(node["ui"])
    if isinstance(node.get("state"), dict):
        out["state"] = dict(node["state"])
    return out


def _graph_v2_edge(
    edge: dict[str, Any],
    *,
    index: int,
    operators_by_node: dict[str, str],
) -> dict[str, Any]:
    source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
    target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
    source_node = str(source.get("node") or "")
    source_port = str(source.get("port") or "out")
    target_node = str(target.get("node") or "")
    target_port = str(target.get("port") or "in")
    modality, semantic_class, continuous = _edge_traffic(
        operators_by_node.get(source_node, ""),
        operators_by_node.get(target_node, ""),
        target_port=target_port,
    )
    traffic = dict(_as_record(edge.get("traffic"))) or {
        "modality": modality,
        "semantic_class": semantic_class,
        "continuous": continuous,
    }
    effective_continuous = bool(traffic.get("continuous", continuous))
    out: dict[str, Any] = {
        "uid": str(
            edge.get("uid")
            or f"edge_{index}_{_uid_part(source_node)}_{_uid_part(source_port)}_{_uid_part(target_node)}_{_uid_part(target_port)}"
        ),
        "from": {"node": source_node, "port": source_port},
        "to": {"node": target_node, "port": target_port},
        "traffic": traffic,
        "queue": dict(_as_record(edge.get("queue")))
        or {
            "max_items": int(edge.get("maxsize") or 1),
            "drop_policy": str(edge.get("drop_policy") or "latest_only"),
        },
    }
    if isinstance(edge.get("backpressure"), dict):
        out["backpressure"] = dict(edge["backpressure"])
    else:
        out["backpressure"] = {
            "mode": "reduce_source_rate" if effective_continuous else "pause_upstream"
        }
    if isinstance(edge.get("lifecycle"), dict):
        out["lifecycle"] = dict(edge["lifecycle"])
    if isinstance(edge.get("debug"), dict):
        out["debug"] = dict(edge["debug"])
    return out


def _edge_traffic(
    source_operator: str, target_operator: str, *, target_port: str
) -> tuple[str, str, bool]:
    if target_port == "gate" or source_operator.endswith(".demand_gate"):
        return "control.gate", "control", False
    if source_operator in _VIDEO_OPERATORS and target_operator in _VIDEO_OPERATORS:
        return "video.frame", "frame", True
    return "data.event", "event", False


def _uid_part(value: str) -> str:
    return _NAME_CLEAN_RE.sub("_", str(value or "").strip().lower()).strip("_") or "item"
