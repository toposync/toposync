from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .compiler import CompilationReport, CompiledEdge, CompiledNode, CompiledPipeline
from .execution import PipelineRuntime, PipelineRuntimeDependencies
from .operator_registry import OperatorRegistry


class SharedRuntimeBuildError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MergedNodeOccurrence:
    pipeline_name: str
    node_id: str


@dataclass(slots=True)
class MergedPipelinePlan:
    merged_pipeline: CompiledPipeline
    occurrence_to_merged_node_id: dict[tuple[str, str], str] = field(default_factory=dict)
    merged_node_occurrences: dict[str, tuple[MergedNodeOccurrence, ...]] = field(
        default_factory=dict
    )


def build_merged_pipeline_plan(
    report: CompilationReport,
    *,
    bundle_name: str = "pipeline_bundle",
) -> MergedPipelinePlan:
    pipelines = sorted(report.pipelines, key=lambda item: item.name)
    if not pipelines:
        raise SharedRuntimeBuildError("Compilation report does not contain pipelines")

    merged_node_by_key: dict[tuple[Any, ...], CompiledNode] = {}
    node_id_by_key: dict[tuple[Any, ...], str] = {}
    occurrence_to_merged: dict[tuple[str, str], str] = {}
    occurrences_by_merged_id: dict[str, list[MergedNodeOccurrence]] = defaultdict(list)
    edge_by_key: dict[tuple[str, str, str, str], CompiledEdge] = {}

    for pipeline in pipelines:
        incoming_by_node_id: dict[str, list[CompiledEdge]] = defaultdict(list)
        for edge in pipeline.edges:
            incoming_by_node_id[edge.target_node_id].append(edge)
        for node in pipeline.nodes:
            if node.shareable:
                incoming = incoming_by_node_id.get(node.node_id) or []
                incoming_policy_key = tuple(
                    sorted(
                        (
                            edge.target_port,
                            edge.source_port,
                            occurrence_to_merged.get(
                                (pipeline.name, edge.source_node_id),
                                f"unmerged:{pipeline.name}:{edge.source_node_id}",
                            ),
                            int(edge.channel_maxsize),
                            str(edge.channel_drop_policy.value),
                        )
                        for edge in incoming
                    ),
                )
                merge_key = ("shared", node.signature, incoming_policy_key)
            else:
                merge_key = ("isolated", f"{pipeline.name}:{node.node_id}")
            if merge_key not in node_id_by_key:
                merged_node_id = _merged_node_id_for_key(node=node, merge_key=merge_key)
                node_id_by_key[merge_key] = merged_node_id
                merged_node_by_key[merge_key] = CompiledNode(
                    node_id=merged_node_id,
                    operator_id=node.operator_id,
                    normalized_config=dict(node.normalized_config),
                    signature=node.signature,
                    shareable=node.shareable,
                )
            merged_node_id = node_id_by_key[merge_key]
            occurrence = MergedNodeOccurrence(pipeline_name=pipeline.name, node_id=node.node_id)
            occurrence_to_merged[(pipeline.name, node.node_id)] = merged_node_id
            occurrences_by_merged_id[merged_node_id].append(occurrence)

    for pipeline in pipelines:
        for edge in pipeline.edges:
            source_merged = occurrence_to_merged[(pipeline.name, edge.source_node_id)]
            target_merged = occurrence_to_merged[(pipeline.name, edge.target_node_id)]
            edge_key = (source_merged, edge.source_port, target_merged, edge.target_port)
            existing = edge_by_key.get(edge_key)
            if existing is None:
                edge_by_key[edge_key] = CompiledEdge(
                    source_node_id=source_merged,
                    source_port=edge.source_port,
                    target_node_id=target_merged,
                    target_port=edge.target_port,
                    channel_maxsize=edge.channel_maxsize,
                    channel_drop_policy=edge.channel_drop_policy,
                )
                continue
            if (
                existing.channel_maxsize != edge.channel_maxsize
                or existing.channel_drop_policy != edge.channel_drop_policy
            ):
                raise SharedRuntimeBuildError(
                    "Conflicting channel settings for merged edge "
                    f"{source_merged}.{edge.source_port}->{target_merged}.{edge.target_port}",
                )

    merged_nodes = list(merged_node_by_key.values())
    merged_edges = list(edge_by_key.values())
    topological_order = _topological_order(merged_nodes=merged_nodes, merged_edges=merged_edges)
    node_by_id = {node.node_id: node for node in merged_nodes}
    ordered_nodes = [node_by_id[node_id] for node_id in topological_order]

    merged_pipeline = CompiledPipeline(
        name=bundle_name,
        schema_version=max(pipeline.schema_version for pipeline in pipelines),
        nodes=tuple(ordered_nodes),
        edges=tuple(
            sorted(
                merged_edges,
                key=lambda item: (
                    item.source_node_id,
                    item.source_port,
                    item.target_node_id,
                    item.target_port,
                ),
            )
        ),
        topological_order=tuple(topological_order),
        limits={},
    )
    normalized_occurrences = {
        merged_node_id: tuple(items) for merged_node_id, items in occurrences_by_merged_id.items()
    }
    return MergedPipelinePlan(
        merged_pipeline=merged_pipeline,
        occurrence_to_merged_node_id=occurrence_to_merged,
        merged_node_occurrences=normalized_occurrences,
    )


@dataclass(slots=True)
class PipelineBundleRuntime:
    report: CompilationReport
    registry: OperatorRegistry
    dependencies: PipelineRuntimeDependencies = field(default_factory=PipelineRuntimeDependencies)
    bundle_name: str = "pipeline_bundle"
    plan: MergedPipelinePlan = field(init=False)
    _runtime: PipelineRuntime = field(init=False)

    def __post_init__(self) -> None:
        self.plan = build_merged_pipeline_plan(self.report, bundle_name=self.bundle_name)
        # Even if stats are disabled, this map is useful for sink operators
        # (e.g. store_images) to resolve the logical pipeline name when
        # execution is running inside a bundle (compiled.name=bundle_name).
        self.dependencies.pipeline_stats_node_occurrences = _build_bundle_stats_node_occurrences(
            plan=self.plan
        )
        self.dependencies.pipeline_graph_limits_by_pipeline = {
            pipeline.name: dict(pipeline.limits) for pipeline in self.report.pipelines
        }
        self._runtime = PipelineRuntime(
            compiled=self.plan.merged_pipeline,
            registry=self.registry,
            dependencies=self.dependencies,
        )

    async def start(self) -> None:
        await self._runtime.start()

    async def stop(self) -> None:
        await self._runtime.stop()

    async def run_for(self, duration_s: float) -> dict[str, Any]:
        await self._runtime.run_for(duration_s)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        runtime_snapshot = self._runtime.snapshot()
        return {
            "bundle_name": self.bundle_name,
            "pipelines": [pipeline.name for pipeline in self.report.pipelines],
            "node_occurrences": {
                node_id: [
                    {
                        "pipeline_name": item.pipeline_name,
                        "node_id": item.node_id,
                    }
                    for item in occurrences
                ]
                for node_id, occurrences in self.plan.merged_node_occurrences.items()
            },
            "shared_nodes": {
                node_id: [
                    {
                        "pipeline_name": item.pipeline_name,
                        "node_id": item.node_id,
                    }
                    for item in occurrences
                ]
                for node_id, occurrences in self.plan.merged_node_occurrences.items()
                if len(occurrences) > 1
            },
            "runtime": runtime_snapshot,
        }


def _merged_node_id_for_key(*, node: CompiledNode, merge_key: tuple[Any, ...]) -> str:
    if merge_key[0] == "shared":
        base = node.operator_id.replace(".", "_").replace("-", "_")
        incoming_key = merge_key[2] if len(merge_key) > 2 else ()
        incoming_encoded = json.dumps(
            incoming_key, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        incoming_digest = hashlib.sha256(incoming_encoded.encode("utf-8")).hexdigest()[:8]
        return f"shared_{base}_{node.signature[:12]}_{incoming_digest}"
    isolated_suffix = str(merge_key[1]).replace(":", "__").replace(".", "_").replace("-", "_")
    return f"isolated_{isolated_suffix}"


def _topological_order(
    *, merged_nodes: list[CompiledNode], merged_edges: list[CompiledEdge]
) -> list[str]:
    node_ids = {node.node_id for node in merged_nodes}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    adjacency: dict[str, set[str]] = defaultdict(set)

    for edge in merged_edges:
        if edge.source_node_id not in node_ids:
            raise SharedRuntimeBuildError(f"Merged edge source not found: {edge.source_node_id}")
        if edge.target_node_id not in node_ids:
            raise SharedRuntimeBuildError(f"Merged edge target not found: {edge.target_node_id}")
        if edge.target_node_id in adjacency[edge.source_node_id]:
            continue
        adjacency[edge.source_node_id].add(edge.target_node_id)
        indegree[edge.target_node_id] += 1

    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for nxt in sorted(adjacency.get(current, set())):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(node_ids):
        cyc_nodes = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise SharedRuntimeBuildError(
            "Merged runtime graph must be acyclic (cycle detected in nodes: "
            + ", ".join(cyc_nodes)
            + ")",
        )
    return order


def _build_bundle_stats_node_occurrences(
    *,
    plan: MergedPipelinePlan,
) -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        merged_node_id: tuple((occ.pipeline_name, occ.node_id) for occ in occurrences)
        for merged_node_id, occurrences in plan.merged_node_occurrences.items()
    }
