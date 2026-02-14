from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

from toposync.runtime.config_store import Pipeline

from .operator_registry import OperatorRegistry


class GraphCompileError(ValueError):
    pass


class GraphEndpoint(BaseModel):
    node: str
    port: str = "out"

    @field_validator("node")
    @classmethod
    def _validate_node(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("Endpoint node is required")
        return name

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: str) -> str:
        port = str(value or "").strip()
        if not port:
            raise ValueError("Endpoint port is required")
        return port


class PipelineGraphNode(BaseModel):
    id: str
    operator_id: str = Field(alias="operator")
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        node_id = str(value or "").strip()
        if not node_id:
            raise ValueError("Node id is required")
        return node_id

    @field_validator("operator_id")
    @classmethod
    def _validate_operator(cls, value: str) -> str:
        operator_id = str(value or "").strip()
        if not operator_id:
            raise ValueError("Node operator is required")
        return operator_id


class PipelineGraphEdge(BaseModel):
    source: GraphEndpoint = Field(alias="from")
    target: GraphEndpoint = Field(alias="to")


class PipelineGraphSpec(BaseModel):
    schema_version: int = Field(ge=1)
    nodes: list[PipelineGraphNode] = Field(default_factory=list)
    edges: list[PipelineGraphEdge] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CompiledNode:
    node_id: str
    operator_id: str
    normalized_config: dict[str, Any]
    signature: str
    shareable: bool


@dataclass(frozen=True, slots=True)
class CompiledPipeline:
    name: str
    pipeline_type: str
    schema_version: int
    nodes: tuple[CompiledNode, ...]
    topological_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedNodeOccurrence:
    pipeline_name: str
    node_id: str
    signature: str


@dataclass(frozen=True, slots=True)
class CompilationReport:
    pipelines: tuple[CompiledPipeline, ...]
    shared_signatures: dict[str, tuple[SharedNodeOccurrence, ...]]


class PipelineGraphCompiler:
    def __init__(self, registry: OperatorRegistry) -> None:
        self._registry = registry

    def compile_pipeline(self, pipeline: Pipeline) -> CompiledPipeline:
        try:
            graph = PipelineGraphSpec.model_validate(pipeline.graph)
        except Exception as exc:  # noqa: BLE001
            raise GraphCompileError(f"Invalid graph schema: {exc}") from exc

        node_map: dict[str, PipelineGraphNode] = {}
        for node in graph.nodes:
            if node.id in node_map:
                raise GraphCompileError(f"Duplicate node id: {node.id}")
            node_map[node.id] = node

        edge_list = list(graph.edges)
        adjacency: dict[str, set[str]] = defaultdict(set)
        indegree: dict[str, int] = {node_id: 0 for node_id in node_map}
        incoming_edges: dict[str, list[PipelineGraphEdge]] = defaultdict(list)

        for edge in edge_list:
            src = edge.source.node
            dst = edge.target.node
            if src not in node_map:
                raise GraphCompileError(f"Edge source node not found: {src}")
            if dst not in node_map:
                raise GraphCompileError(f"Edge target node not found: {dst}")
            if dst not in adjacency[src]:
                adjacency[src].add(dst)
                indegree[dst] += 1
            incoming_edges[dst].append(edge)

        normalized_config_by_node: dict[str, dict[str, Any]] = {}
        share_strategy_by_node: dict[str, str] = {}
        input_ports_by_node: dict[str, set[str]] = {}
        output_ports_by_node: dict[str, set[str]] = {}

        for node in graph.nodes:
            operator = self._registry.get(node.operator_id)
            if operator is None:
                raise GraphCompileError(f"Unknown operator id: {node.operator_id}")

            try:
                normalized_config_by_node[node.id] = self._registry.normalize_config(node.operator_id, node.config)
            except Exception as exc:  # noqa: BLE001
                raise GraphCompileError(
                    f"Invalid config for operator '{node.operator_id}' in node '{node.id}': {exc}",
                ) from exc
            share_strategy_by_node[node.id] = operator.definition.share_strategy
            input_ports_by_node[node.id] = {port.name for port in operator.definition.inputs}
            output_ports_by_node[node.id] = {port.name for port in operator.definition.outputs}

            required_input_ports = {port.name for port in operator.definition.inputs if port.required}
            available_inputs = {edge.target.port for edge in incoming_edges.get(node.id, [])}
            missing_required = sorted(required_input_ports - available_inputs)
            if missing_required:
                raise GraphCompileError(
                    f"Node '{node.id}' is missing required inputs: {', '.join(missing_required)}",
                )

        for edge in edge_list:
            src_ports = output_ports_by_node[edge.source.node]
            if edge.source.port not in src_ports:
                raise GraphCompileError(
                    f"Node '{edge.source.node}' has no output port '{edge.source.port}'",
                )
            dst_ports = input_ports_by_node[edge.target.node]
            if edge.target.port not in dst_ports:
                raise GraphCompileError(
                    f"Node '{edge.target.node}' has no input port '{edge.target.port}'",
                )

        topological_order = _topological_sort(node_map=node_map, adjacency=adjacency, indegree=indegree)
        signature_by_node: dict[str, str] = {}
        compiled_nodes: list[CompiledNode] = []

        for node_id in topological_order:
            node = node_map[node_id]
            incoming = incoming_edges.get(node_id, [])
            upstream = sorted(
                [
                    {
                        "target_port": edge.target.port,
                        "source_node": edge.source.node,
                        "source_port": edge.source.port,
                        "source_signature": signature_by_node.get(edge.source.node, ""),
                    }
                    for edge in incoming
                ],
                key=lambda item: (
                    item["target_port"],
                    item["source_node"],
                    item["source_port"],
                    item["source_signature"],
                ),
            )

            signature_payload = {
                "operator_id": node.operator_id,
                "config": normalized_config_by_node[node_id],
                "upstream": upstream,
            }
            shareable = share_strategy_by_node[node_id] == "by_signature"
            if not shareable:
                signature_payload["node_id"] = node_id
                signature_payload["pipeline_name"] = pipeline.name
            signature = _signature(signature_payload)
            signature_by_node[node_id] = signature
            compiled_nodes.append(
                CompiledNode(
                    node_id=node_id,
                    operator_id=node.operator_id,
                    normalized_config=normalized_config_by_node[node_id],
                    signature=signature,
                    shareable=shareable,
                ),
            )

        return CompiledPipeline(
            name=pipeline.name,
            pipeline_type=pipeline.type,
            schema_version=graph.schema_version,
            nodes=tuple(compiled_nodes),
            topological_order=tuple(topological_order),
        )

    def compile_many(self, pipelines: list[Pipeline]) -> CompilationReport:
        compiled = [self.compile_pipeline(pipeline) for pipeline in pipelines]
        grouped: dict[str, list[SharedNodeOccurrence]] = defaultdict(list)
        for pipeline in compiled:
            for node in pipeline.nodes:
                if not node.shareable:
                    continue
                grouped[node.signature].append(
                    SharedNodeOccurrence(
                        pipeline_name=pipeline.name,
                        node_id=node.node_id,
                        signature=node.signature,
                    ),
                )
        shared = {
            signature: tuple(occurrences)
            for signature, occurrences in grouped.items()
            if len(occurrences) > 1
        }
        return CompilationReport(
            pipelines=tuple(compiled),
            shared_signatures=shared,
        )


def _topological_sort(
    *,
    node_map: dict[str, PipelineGraphNode],
    adjacency: dict[str, set[str]],
    indegree: dict[str, int],
) -> list[str]:
    queue = deque(sorted([node_id for node_id, degree in indegree.items() if degree == 0]))
    order: list[str] = []
    local_indegree = dict(indegree)

    while queue:
        current = queue.popleft()
        order.append(current)
        for nxt in sorted(adjacency.get(current, set())):
            local_indegree[nxt] -= 1
            if local_indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(node_map):
        cycle_nodes = sorted([node_id for node_id, degree in local_indegree.items() if degree > 0])
        raise GraphCompileError(f"Graph must be a DAG (cycle detected in nodes: {', '.join(cycle_nodes)})")

    return order


def _signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
