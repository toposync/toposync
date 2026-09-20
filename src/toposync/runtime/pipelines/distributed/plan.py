from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from toposync.runtime.config_store import Pipeline

from ..operator_registry import OperatorRegistry


class DistributedPlanError(ValueError):
    pass


_SAFE_NODE_ID_RE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_node_id(value: str, *, prefix: str) -> str:
    raw = str(value or "").strip()
    raw = _SAFE_NODE_ID_RE.sub("_", raw)
    raw = raw.strip("_")
    if not raw:
        raw = "node"
    return f"{prefix}{raw}"[:120]


@dataclass(frozen=True, slots=True)
class DistributedGraphs:
    pipeline_name: str
    origin_graph: dict[str, Any] | None
    processing_graph: dict[str, Any] | None
    cross_edges: tuple[dict[str, Any], ...]


def build_distributed_graphs(
    pipeline: Pipeline,
    registry: OperatorRegistry,
    *,
    origin_inbox_node_id: str = "inbox",
) -> DistributedGraphs:
    graph = dict(pipeline.graph or {})
    schema_version = int(graph.get("schema_version") or 1)
    limits = dict(graph.get("limits") or {}) if isinstance(graph.get("limits"), dict) else {}
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])

    def bridge_edge(edge: dict[str, Any], *, source: dict[str, str],
                    target: dict[str, str], suffix: str) -> dict[str, Any]:
        if schema_version == 2:
            # A bridge changes endpoints, not the user's queue/lifecycle policy.
            result = deepcopy(edge)
            result.update({"uid": f"{edge['uid']}:{suffix}", "from": source, "to": target})
            if suffix == "filter":
                # The inbox broadcasts all destinations. Coalescing here could
                # replace A's update with B's before A's target filter runs.
                # Apply the user's lossy policy only after target routing.
                queue = dict(result.get("queue") or {})
                queue.update({"drop_policy": "block", "key_policy": "none", "key_path": ""})
                result["queue"] = queue
            return result
        return {
            "from": source, "to": target,
            "maxsize": int(edge.get("maxsize") or 8),
            "drop_policy": str(edge.get("drop_policy") or "drop_oldest"),
        }

    node_by_id: dict[str, dict[str, Any]] = {}
    for item in nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        if not node_id:
            continue
        node_by_id[node_id] = dict(item)

    def _placement(node_id: str) -> Literal["origin", "processing"]:
        node = node_by_id.get(node_id) or {}
        operator_id = str(node.get("operator") or "").strip()
        registered = registry.get(operator_id)
        if registered is None:
            raise DistributedPlanError(f"Unknown operator '{operator_id}' for node '{node_id}'")
        caps = set(registered.definition.capabilities)
        if "origin_only" in caps:
            return "origin"
        return "processing"

    placement_by_node = {node_id: _placement(node_id) for node_id in node_by_id}
    origin_nodes = [node_by_id[nid] for nid, where in placement_by_node.items() if where == "origin"]
    processing_nodes = [node_by_id[nid] for nid, where in placement_by_node.items() if where == "processing"]

    processing_edges: list[dict[str, Any]] = []
    origin_edges: list[dict[str, Any]] = []
    cross_edges: list[dict[str, Any]] = []

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        src_node = str(source.get("node") or "").strip()
        tgt_node = str(target.get("node") or "").strip()
        if not src_node or not tgt_node:
            continue
        src_place = placement_by_node.get(src_node)
        tgt_place = placement_by_node.get(tgt_node)
        if src_place is None or tgt_place is None:
            continue
        if src_place == "origin" and tgt_place == "processing":
            raise DistributedPlanError(
                "Distributed execution currently does not support origin->processing edges "
                f"({src_node} -> {tgt_node})",
            )
        if src_place == "processing" and tgt_place == "origin":
            cross_edges.append(dict(edge))
            continue
        if src_place == "origin":
            origin_edges.append(dict(edge))
        else:
            processing_edges.append(dict(edge))

    origin_graph: dict[str, Any] | None = None
    processing_graph: dict[str, Any] | None = None

    if processing_nodes:
        proc_nodes = list(processing_nodes)
        proc_edges = list(processing_edges)

        for i, edge in enumerate(cross_edges):
            source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
            target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
            src_node = str(source.get("node") or "").strip()
            src_port = str(source.get("port") or "out").strip() or "out"
            tgt_node = str(target.get("node") or "").strip()
            tgt_port = str(target.get("port") or "in").strip() or "in"

            project_node_id = _safe_node_id(f"{src_node}__to__{tgt_node}__{tgt_port}__{i}", prefix="project__")
            proc_nodes.append(
                {
                    "id": project_node_id,
                    "operator": "dist.project_to_origin",
                    "config": {
                        "pipeline_name": pipeline.name,
                        "target_node_id": tgt_node,
                        "target_port": tgt_port,
                    },
                },
            )

            proc_edges.append(
                bridge_edge(edge, source={"node": src_node, "port": src_port},
                            target={"node": project_node_id, "port": "in"}, suffix="project"),
            )

        processing_graph = {
            "schema_version": schema_version,
            "nodes": proc_nodes,
            "edges": proc_edges,
            "limits": limits,
        }

    if origin_nodes:
        orig_nodes = list(origin_nodes)
        orig_edges = list(origin_edges)

        if cross_edges:
            orig_nodes.append(
                {
                    "id": origin_inbox_node_id,
                    "operator": "dist.remote_source",
                    "config": {},
                },
            )

            for i, edge in enumerate(cross_edges):
                target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
                tgt_node = str(target.get("node") or "").strip()
                tgt_port = str(target.get("port") or "in").strip() or "in"

                filter_node_id = _safe_node_id(f"{tgt_node}__{tgt_port}__{i}", prefix="filter__")
                orig_nodes.append(
                    {
                        "id": filter_node_id,
                        "operator": "dist.target_filter",
                        "config": {"target_node_id": tgt_node, "target_port": tgt_port},
                    },
                )

                orig_edges.append(
                    bridge_edge(edge, source={"node": origin_inbox_node_id, "port": "out"},
                                target={"node": filter_node_id, "port": "in"}, suffix="filter"),
                )
                orig_edges.append(
                    bridge_edge(edge, source={"node": filter_node_id, "port": "out"},
                                target={"node": tgt_node, "port": tgt_port}, suffix="deliver"),
                )

        origin_graph = {
            "schema_version": schema_version,
            "nodes": orig_nodes,
            "edges": orig_edges,
            "limits": limits,
        }

    if schema_version == 2:
        for side, partition in (("processing", processing_graph), ("origin", origin_graph)):
            if partition is None:
                continue
            partition["uid"] = f"{graph.get('uid') or pipeline.name}:{side}"
            partition["revision"] = graph.get("revision", 1)
            for node in partition["nodes"]:
                if "uid" not in node:
                    node["uid"] = f"{partition['uid']}:{node['id']}"

    return DistributedGraphs(
        pipeline_name=pipeline.name,
        origin_graph=origin_graph,
        processing_graph=processing_graph,
        cross_edges=tuple(cross_edges),
    )
