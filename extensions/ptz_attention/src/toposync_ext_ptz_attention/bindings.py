from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .constants import OPERATOR_ID_REQUEST
from .models import AttentionProfile, effective_mode


ObserverBindingIssue = Literal[
    "observer_binding_not_effective",
    "observer_camera_indeterminate",
    "same_head_observer_acknowledgement_required",
]


@dataclass(frozen=True, slots=True)
class AttentionBinding:
    pipeline_name: str
    node_id: str
    profile_id: str
    event_type: str
    event_type_field: str
    enabled: bool
    observer_camera_ids: tuple[str, ...]
    observer_determined: bool

    @property
    def observer_camera_id(self) -> str | None:
        if not self.observer_determined:
            return None
        return self.observer_camera_ids[0]

    def public_payload(self) -> dict[str, Any]:
        return {
            "pipeline_name": self.pipeline_name,
            "node_id": self.node_id,
            "profile_id": self.profile_id,
            "event_type": self.event_type,
            "event_type_field": self.event_type_field,
            "enabled": self.enabled,
            "observer_camera_id": self.observer_camera_id,
        }


def attention_bindings(pipelines: list[Any]) -> list[AttentionBinding]:
    result: list[AttentionBinding] = []
    for pipeline in pipelines:
        graph = getattr(pipeline, "graph", None)
        if not isinstance(graph, dict) or graph.get("schema_version") != 2:
            continue
        raw_nodes = graph.get("nodes")
        raw_nodes = raw_nodes if isinstance(raw_nodes, list) else []
        nodes = {
            node_id: raw_node
            for raw_node in raw_nodes
            if isinstance(raw_node, dict)
            and (node_id := str(raw_node.get("id") or raw_node.get("uid") or "").strip())
        }
        incoming = _incoming_nodes(graph.get("edges"), known_node_ids=set(nodes))
        for node_id, raw_node in nodes.items():
            if str(raw_node.get("operator") or "").strip() != OPERATOR_ID_REQUEST:
                continue
            config = raw_node.get("config")
            config = config if isinstance(config, dict) else {}
            observer_camera_ids, observer_determined = _upstream_observer_cameras(
                node_id,
                nodes=nodes,
                incoming=incoming,
            )
            result.append(
                AttentionBinding(
                    pipeline_name=str(getattr(pipeline, "name", "") or "").strip(),
                    node_id=node_id,
                    profile_id=str(config.get("profile_id") or "").strip(),
                    event_type=str(config.get("event_type") or "").strip(),
                    event_type_field=str(config.get("event_type_field") or "").strip(),
                    enabled=bool(getattr(pipeline, "enabled", True)),
                    observer_camera_ids=observer_camera_ids,
                    observer_determined=observer_determined,
                )
            )
    result.sort(key=lambda item: (item.pipeline_name, item.node_id))
    return result


def live_observer_binding_issue(
    profile: AttentionProfile,
    bindings: list[AttentionBinding],
    *,
    pipeline_name: str = "",
    node_id: str = "",
    require_effective_binding: bool = False,
) -> ObserverBindingIssue | None:
    if effective_mode(profile) != "live_preset":
        return None
    matches = [
        binding
        for binding in bindings
        if binding.enabled
        and binding.profile_id == profile.id
        and (not pipeline_name or binding.pipeline_name == pipeline_name)
        and (not node_id or binding.node_id == node_id)
    ]
    if not matches:
        return "observer_binding_not_effective" if require_effective_binding else None
    if any(not binding.observer_determined for binding in matches):
        return "observer_camera_indeterminate"
    if not profile.same_head_observer_acknowledged and any(
        binding.observer_camera_id == profile.camera_id for binding in matches
    ):
        return "same_head_observer_acknowledgement_required"
    return None


def _incoming_nodes(raw_edges: Any, *, known_node_ids: set[str]) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = {}
    if not isinstance(raw_edges, list):
        return incoming
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        source = raw_edge.get("from")
        target = raw_edge.get("to")
        if not isinstance(source, dict) or not isinstance(target, dict):
            continue
        source_id = str(source.get("node") or "").strip()
        target_id = str(target.get("node") or "").strip()
        if source_id not in known_node_ids or target_id not in known_node_ids:
            continue
        incoming.setdefault(target_id, set()).add(source_id)
    return incoming


def _upstream_observer_cameras(
    node_id: str,
    *,
    nodes: dict[str, dict[str, Any]],
    incoming: dict[str, set[str]],
) -> tuple[tuple[str, ...], bool]:
    pending = list(incoming.get(node_id, ()))
    visited: set[str] = set()
    source_count = 0
    source_with_camera_count = 0
    camera_ids: set[str] = set()
    while pending:
        upstream_id = pending.pop()
        if upstream_id in visited:
            continue
        visited.add(upstream_id)
        raw_node = nodes.get(upstream_id, {})
        if str(raw_node.get("operator") or "").strip() == "camera.source":
            source_count += 1
            config = raw_node.get("config")
            config = config if isinstance(config, dict) else {}
            camera_id = str(config.get("camera_id") or "").strip()
            if camera_id:
                source_with_camera_count += 1
                camera_ids.add(camera_id)
        pending.extend(incoming.get(upstream_id, ()))
    determined = source_count == 1 and source_with_camera_count == 1 and len(camera_ids) == 1
    return tuple(sorted(camera_ids)), determined
