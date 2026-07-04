import { Position, type NodeHandle } from "@xyflow/react";

import type { PipelineAlert, PipelineOperatorDefinition } from "../../../../util/api";
import { PIPELINE_OPERATOR_GROUPS } from "../constants";
import { isRecord, prettyOperatorDescription, prettyOperatorName, resolvePipelineOperatorUx } from "../utils";
import { layoutTopologyNodes } from "./topologyLayout";
import type {
  TopologyBuildOptions,
  TopologyBuildResult,
  TopologyEdge,
  TopologyEdgeData,
  TopologyNode,
  TopologyNodeData,
  TopologyPressureState,
} from "./topologyTypes";

type RawEndpoint = { node: string; port: string };
type ParsedEdge = {
  uid: string;
  source: RawEndpoint;
  target: RawEndpoint;
  modality: string;
  semanticClass: string;
  continuous: boolean;
  maxItems: number;
  dropPolicy: string;
  pressureMode: string;
};

const UNKNOWN_GROUP_COLOR = "#64748B";
const TOPOLOGY_NODE_WIDTH = 232;
const TOPOLOGY_NODE_MIN_HEIGHT = 112;
const HANDLE_SIZE = 10;

const SEVERITY_RANK: Record<PipelineAlert["severity"], number> = {
  info: 1,
  warning: 2,
  error: 3,
};

function text(value: unknown, fallback = ""): string {
  const out = String(value ?? "").trim();
  return out || fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function intValue(value: unknown, fallback = 0): number {
  return Math.round(numberValue(value, fallback));
}

function endpoint(value: unknown, fallbackPort: string): RawEndpoint | null {
  if (!isRecord(value)) return null;
  const node = text(value.node);
  if (!node) return null;
  return { node, port: text(value.port, fallbackPort) };
}

function edgeKey(sourceNode: string, sourcePort: string, targetNode: string, targetPort: string): string {
  return `${sourceNode}.${sourcePort}->${targetNode}.${targetPort}`;
}

function parsedEdgeKey(edge: ParsedEdge): string {
  return edgeKey(edge.source.node, edge.source.port, edge.target.node, edge.target.port);
}

function savedNodePositions(graph: Record<string, unknown>): Map<string, { x: number; y: number }> {
  const layout = isRecord(graph.layout) ? graph.layout : {};
  const rawNodes = isRecord(layout.nodes) ? layout.nodes : {};
  const positions = new Map<string, { x: number; y: number }>();
  for (const [nodeId, value] of Object.entries(rawNodes)) {
    if (!isRecord(value)) continue;
    const x = numberValue(value.x, Number.NaN);
    const y = numberValue(value.y, Number.NaN);
    if (Number.isFinite(x) && Number.isFinite(y)) positions.set(nodeId, { x, y });
  }
  return positions;
}

function applySavedPositions(nodes: TopologyNode[], positions: Map<string, { x: number; y: number }>): TopologyNode[] {
  if (positions.size === 0) return nodes;
  return nodes.map((node) => {
    const position = positions.get(node.id);
    return position ? { ...node, position } : node;
  });
}

function handleY(index: number, total: number): number {
  const ratio = total <= 1 ? 0.5 : (index + 1) / (total + 1);
  return Math.round(TOPOLOGY_NODE_MIN_HEIGHT * ratio - HANDLE_SIZE / 2);
}

function nodeHandles(inputPorts: string[], outputPorts: string[]): NodeHandle[] {
  return [
    ...inputPorts.map((port, index) => ({
      id: port,
      type: "target" as const,
      position: Position.Left,
      x: -HANDLE_SIZE / 2,
      y: handleY(index, inputPorts.length),
      width: HANDLE_SIZE,
      height: HANDLE_SIZE,
    })),
    ...outputPorts.map((port, index) => ({
      id: port,
      type: "source" as const,
      position: Position.Right,
      x: TOPOLOGY_NODE_WIDTH - HANDLE_SIZE / 2,
      y: handleY(index, outputPorts.length),
      width: HANDLE_SIZE,
      height: HANDLE_SIZE,
    })),
  ];
}

function alertEdgeKey(alert: PipelineAlert): string | null {
  const edge = isRecord(alert.edge) ? alert.edge : null;
  if (!edge) return null;
  const source = endpoint(edge.from, "out");
  const target = endpoint(edge.to, "in");
  if (!source || !target) return null;
  return edgeKey(source.node, source.port, target.node, target.port);
}

function highestSeverity(alerts: PipelineAlert[]): PipelineAlert["severity"] | null {
  let selected: PipelineAlert["severity"] | null = null;
  for (const alert of alerts) {
    if (!selected || SEVERITY_RANK[alert.severity] > SEVERITY_RANK[selected]) selected = alert.severity;
  }
  return selected;
}

function stateFromSeverity(severity: PipelineAlert["severity"] | null): TopologyPressureState {
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  return "none";
}

function combinePressure(left: TopologyPressureState, right: TopologyPressureState): TopologyPressureState {
  const rank: Record<TopologyPressureState, number> = { none: 0, warning: 1, critical: 2, error: 3 };
  return rank[right] > rank[left] ? right : left;
}

function nodeRuntimePressure(runtime: TopologyNodeData["runtime"]): TopologyPressureState {
  if (!runtime) return "none";
  const state = text(runtime.runtime_state || runtime.task_state).toLowerCase();
  const errors = intValue(runtime.progress?.error_count ?? runtime.metrics?.error_count, 0);
  if (state === "failed" || state === "canceled" || errors > 0 || runtime.last_error) return "error";
  if (state === "degraded") return "warning";
  return "none";
}

function edgeRuntimePressure(runtime: TopologyEdgeData["runtime"]): TopologyPressureState {
  if (!runtime) return "none";
  const cause = text(runtime.pressure_cause, "none");
  const utilization = numberValue(runtime.utilization, 0);
  const dropped = intValue(runtime.progress?.dropped_total ?? runtime.metrics?.dropped_total, 0);
  if (cause === "queue_full" || utilization >= 0.9) return "critical";
  if (cause !== "none" || utilization >= 0.7 || dropped > 0) return "warning";
  return "none";
}

function parseEdge(raw: unknown, index: number): ParsedEdge | null {
  if (!isRecord(raw)) return null;
  const source = endpoint(raw.from, "out");
  const target = endpoint(raw.to, "in");
  if (!source || !target) return null;
  const queue = isRecord(raw.queue) ? raw.queue : {};
  const traffic = isRecord(raw.traffic) ? raw.traffic : {};
  const backpressure = isRecord(raw.backpressure) ? raw.backpressure : {};
  const uid = text(raw.uid, `edge:${parsedEdgeKey({ source, target, uid: "", modality: "", semanticClass: "", continuous: false, maxItems: 1, dropPolicy: "latest_only", pressureMode: "pause_upstream" })}:${index}`);
  return {
    uid,
    source,
    target,
    modality: text(traffic.modality, "data.record"),
    semanticClass: text(traffic.semantic_class, "data"),
    continuous: Boolean(traffic.continuous),
    maxItems: Math.max(1, intValue(queue.max_items, 1)),
    dropPolicy: text(queue.drop_policy, "latest_only"),
    pressureMode: text(backpressure.mode, "pause_upstream"),
  };
}

function nodeAlerts(nodeId: string, operatorId: string, alerts: PipelineAlert[]): PipelineAlert[] {
  return alerts.filter((alert) => alert.node_id === nodeId || (!alert.node_id && alert.operator_id === operatorId));
}

function edgeAlerts(edge: ParsedEdge, alerts: PipelineAlert[]): PipelineAlert[] {
  const key = parsedEdgeKey(edge);
  return alerts.filter((alert) => alertEdgeKey(alert) === key);
}

export function buildTopologyModel(options: TopologyBuildOptions): TopologyBuildResult {
  const graph = isRecord(options.graph) ? options.graph : null;
  if (!graph) {
    return {
      ok: false,
      title: "Graph unavailable",
      detail: "The pipeline graph is not a JSON object.",
      titleKey: "core.ui.pipelines.topology.fallback.graph_unavailable.title",
      detailKey: "core.ui.pipelines.topology.fallback.graph_unavailable.detail",
    };
  }

  const schemaVersion = intValue(graph.schema_version, 1);
  if (schemaVersion !== 2) {
    return {
      ok: false,
      title: "Topology view needs graph v2",
      detail: "This pipeline still uses the legacy graph shape. Inspect it in JSON mode.",
      titleKey: "core.ui.pipelines.topology.fallback.needs_v2.title",
      detailKey: "core.ui.pipelines.topology.fallback.needs_v2.detail",
    };
  }

  const rawNodes = Array.isArray(graph.nodes) ? graph.nodes : null;
  const rawEdges = Array.isArray(graph.edges) ? graph.edges : null;
  if (!rawNodes || !rawEdges) {
    return {
      ok: false,
      title: "Graph v2 is incomplete",
      detail: "The topology view needs nodes and edges arrays.",
      titleKey: "core.ui.pipelines.topology.fallback.incomplete.title",
      detailKey: "core.ui.pipelines.topology.fallback.incomplete.detail",
    };
  }

  const parsedEdges = rawEdges.map(parseEdge).filter(Boolean) as ParsedEdge[];
  const incomingPorts = new Map<string, Set<string>>();
  const outgoingPorts = new Map<string, Set<string>>();
  for (const edge of parsedEdges) {
    incomingPorts.set(edge.target.node, (incomingPorts.get(edge.target.node) ?? new Set()).add(edge.target.port));
    outgoingPorts.set(edge.source.node, (outgoingPorts.get(edge.source.node) ?? new Set()).add(edge.source.port));
  }

  const runtimeNodes = options.runtimeInfo?.nodes ?? {};
  const runtimeEdges = options.runtimeInfo?.edges ?? {};

  const nodes: TopologyNode[] = [];
  const nodeIds = new Set<string>();
  for (const raw of rawNodes) {
    if (!isRecord(raw)) continue;
    const nodeId = text(raw.id);
    const operatorId = text(raw.operator);
    if (!nodeId || !operatorId) continue;
    const uid = text(raw.uid, nodeId);
    const operator = options.operatorsById[operatorId] as PipelineOperatorDefinition | undefined;
    const ux = operator ? resolvePipelineOperatorUx(operator) : null;
    const groupId = ux?.group ?? "unknown";
    const group = groupId === "unknown" ? null : PIPELINE_OPERATOR_GROUPS[groupId];
    const alerts = nodeAlerts(nodeId, operatorId, options.alerts);
    const alertSeverity = highestSeverity(alerts);
    const runtime = runtimeNodes[uid] ?? Object.values(runtimeNodes).find((item) => item.node_id === nodeId) ?? null;
    const pressureState = combinePressure(stateFromSeverity(alertSeverity), nodeRuntimePressure(runtime));
    const inputPorts = new Set((operator?.inputs ?? []).map((port) => port.name));
    const outputPorts = new Set((operator?.outputs ?? []).map((port) => port.name));
    for (const port of incomingPorts.get(nodeId) ?? []) inputPorts.add(port);
    for (const port of outgoingPorts.get(nodeId) ?? []) outputPorts.add(port);
    const sortedInputPorts = [...inputPorts].sort();
    const sortedOutputPorts = [...outputPorts].sort();
    nodeIds.add(nodeId);
    nodes.push({
      id: nodeId,
      type: "topologyNode",
      position: { x: 0, y: 0 },
      initialWidth: TOPOLOGY_NODE_WIDTH,
      initialHeight: TOPOLOGY_NODE_MIN_HEIGHT,
      handles: nodeHandles(sortedInputPorts, sortedOutputPorts),
      data: {
        uid,
        nodeId,
        operatorId,
        config: isRecord(raw.config) ? raw.config : {},
        label: prettyOperatorName(operatorId) || nodeId,
        description: operator ? prettyOperatorDescription(operator) : operatorId,
        groupId,
        color: group?.color ?? UNKNOWN_GROUP_COLOR,
        inputPorts: sortedInputPorts,
        outputPorts: sortedOutputPorts,
        alerts,
        alertCount: alerts.length,
        alertSeverity,
        runtime,
        pressureState,
        resourceKind: text(runtime?.resource_kind ?? operator?.resource_kind, "none"),
        pressureBehavior: text(runtime?.pressure_behavior ?? operator?.pressure_behavior, "ignore"),
      },
    });
  }

  const edges: TopologyEdge[] = parsedEdges
    .filter((edge) => nodeIds.has(edge.source.node) && nodeIds.has(edge.target.node))
    .map((edge) => {
      const alerts = edgeAlerts(edge, options.alerts);
      const alertSeverity = highestSeverity(alerts);
      const runtime = runtimeEdges[edge.uid] ?? null;
      const pressureState = combinePressure(stateFromSeverity(alertSeverity), edgeRuntimePressure(runtime));
      const labelParts = [edge.dropPolicy, edge.maxItems > 1 ? `q${edge.maxItems}` : ""].filter(Boolean);
      return {
        id: edge.uid,
        type: "topologyEdge",
        source: edge.source.node,
        target: edge.target.node,
        sourceHandle: edge.source.port,
        targetHandle: edge.target.port,
        data: {
          uid: edge.uid,
          sourceNodeId: edge.source.node,
          sourcePort: edge.source.port,
          targetNodeId: edge.target.node,
          targetPort: edge.target.port,
          label: labelParts.join(" / "),
          modality: edge.modality,
          continuous: edge.continuous,
          semanticClass: edge.semanticClass,
          maxItems: edge.maxItems,
          dropPolicy: edge.dropPolicy,
          pressureMode: edge.pressureMode,
          alerts,
          alertCount: alerts.length,
          alertSeverity,
          runtime,
          pressureState,
        },
      };
    });

  const sourceIds = new Set(edges.map((edge) => edge.source));
  const targetIds = new Set(edges.map((edge) => edge.target));
  const pressure = options.runtimeInfo?.pressure ?? {};
  const laidOutNodes = applySavedPositions(layoutTopologyNodes(nodes, edges), savedNodePositions(graph));
  const model = {
    nodes: laidOutNodes,
    edges,
    summary: {
      graphUid: text(graph.uid, "graph"),
      schemaVersion,
      nodeCount: nodes.length,
      edgeCount: edges.length,
      sourceCount: nodes.filter((node) => !targetIds.has(node.id)).length,
      sinkCount: nodes.filter((node) => !sourceIds.has(node.id)).length,
      pressureActive: Boolean(pressure.active),
      pressureCause: text(pressure.cause, "none"),
    },
  };
  return { ok: true, model };
}
