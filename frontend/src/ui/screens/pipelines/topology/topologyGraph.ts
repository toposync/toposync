import type { Connection, XYPosition } from "@xyflow/react";

import type { PipelineOperatorDefinition } from "../../../../util/api";
import { isRecord } from "../utils";

type JsonRecord = Record<string, unknown>;

export type TopologyGraphEditResult =
  | { ok: true; graph: JsonRecord; nodeId?: string; edgeId?: string }
  | { ok: false; message: string; messageKey?: string; messageParams?: Record<string, unknown> };

export type TopologyEdgePolicyPatch = {
  maxItems?: number;
  dropPolicy?: string;
  pressureMode?: string;
  continuous?: boolean;
  modality?: string;
  semanticClass?: string;
};

const NODE_ID_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const SCHEMA_V2_ERROR = {
  ok: false,
  message: "Topology editing requires graph schema v2.",
  messageKey: "core.ui.pipelines.topology.error.requires_v2",
} satisfies TopologyGraphEditResult;

function cloneGraph(graph: JsonRecord): JsonRecord {
  return structuredClone(graph) as JsonRecord;
}

function cloneRecord(value: unknown): JsonRecord {
  return isRecord(value) ? (structuredClone(value) as JsonRecord) : {};
}

function rawNodes(graph: JsonRecord): JsonRecord[] {
  return Array.isArray(graph.nodes) ? (graph.nodes.filter(isRecord) as JsonRecord[]) : [];
}

function rawEdges(graph: JsonRecord): JsonRecord[] {
  return Array.isArray(graph.edges) ? (graph.edges.filter(isRecord) as JsonRecord[]) : [];
}

function text(value: unknown, fallback = ""): string {
  const out = String(value ?? "").trim();
  return out || fallback;
}

function positiveInt(value: unknown, fallback: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.round(parsed));
}

function uniqueSuffix(): string {
  const cryptoValue = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : "";
  return (cryptoValue || `${Date.now()}_${Math.random().toString(16).slice(2)}`).replace(/[^A-Za-z0-9_]/g, "_").slice(0, 12);
}

function sanitizeNodeId(value: string): string {
  const normalized = value
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^\d/, "_")
    .replace(/^_+|_+$/g, "");
  return NODE_ID_RE.test(normalized) ? normalized : "node";
}

function uniqueValue(base: string, used: Set<string>): string {
  if (!used.has(base)) return base;
  let index = 2;
  while (used.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

function portName(definition: PipelineOperatorDefinition | null, direction: "inputs" | "outputs", fallback: string): string {
  const ports = Array.isArray(definition?.[direction]) ? definition[direction] : [];
  const required = ports.find((port) => port.required);
  return text(required?.name ?? ports[0]?.name, fallback);
}

function policySection(record: unknown, key: string): JsonRecord {
  return isRecord(record) && isRecord(record[key]) ? cloneRecord(record[key]) : {};
}

const EDGE_POLICY_SECTIONS = ["traffic", "queue", "backpressure", "lifecycle", "debug"] as const;

function defaultEdgePolicy(
  source: PipelineOperatorDefinition | null,
  target: PipelineOperatorDefinition | null,
): JsonRecord {
  const outputPolicy = cloneRecord(source?.default_output_policy);
  const inputPolicy = cloneRecord(target?.default_input_policy);
  const policy: JsonRecord = {};

  for (const section of EDGE_POLICY_SECTIONS) {
    const value = {
      ...policySection(outputPolicy, section),
      ...policySection(inputPolicy, section),
    };
    if (Object.keys(value).length > 0) policy[section] = value;
  }

  return policy;
}

export function isTopologyGraphV2(graph: unknown): graph is JsonRecord {
  return isRecord(graph) && Number(graph.schema_version) === 2 && Array.isArray(graph.nodes) && Array.isArray(graph.edges);
}

function readEndpoint(edge: JsonRecord, side: "from" | "to", fallbackPort: string): { node: string; port: string } | null {
  const endpoint = isRecord(edge[side]) ? edge[side] : null;
  const node = text(endpoint?.node);
  if (!node) return null;
  return { node, port: text(endpoint?.port, fallbackPort) };
}

function pathExists(edges: JsonRecord[], startNodeId: string, targetNodeId: string): boolean {
  const outgoing = new Map<string, string[]>();
  for (const edge of edges) {
    const source = readEndpoint(edge, "from", "out");
    const target = readEndpoint(edge, "to", "in");
    if (!source || !target) continue;
    outgoing.set(source.node, [...(outgoing.get(source.node) ?? []), target.node]);
  }
  const pending = [startNodeId];
  const seen = new Set<string>();
  while (pending.length) {
    const current = pending.pop() as string;
    if (current === targetNodeId) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    pending.push(...(outgoing.get(current) ?? []));
  }
  return false;
}

export function addTopologyGraphNode(
  graph: unknown,
  operatorId: string,
  operator: PipelineOperatorDefinition | null,
  position?: XYPosition,
): TopologyGraphEditResult {
  if (!isTopologyGraphV2(graph)) return SCHEMA_V2_ERROR;
  const next = cloneGraph(graph);
  const nodes = rawNodes(next);
  const usedIds = new Set(nodes.map((node) => text(node.id)).filter(Boolean));
  const baseId = sanitizeNodeId(operatorId.split(".").pop() || operatorId);
  const nodeId = uniqueValue(baseId, usedIds);
  const node = {
    uid: uniqueValue(`node_${nodeId}_${uniqueSuffix()}`, new Set(nodes.map((item) => text(item.uid)).filter(Boolean))),
    id: nodeId,
    operator: operatorId,
    config: cloneRecord(operator?.defaults),
  };
  next.nodes = [...nodes, node];
  if (position) next.layout = writeNodePosition(next.layout, nodeId, position);
  return { ok: true, graph: next, nodeId };
}

function writeNodePosition(layoutValue: unknown, nodeId: string, position: XYPosition): JsonRecord {
  const layout = cloneRecord(layoutValue);
  const nodes = cloneRecord(layout.nodes);
  nodes[nodeId] = { x: Math.round(position.x), y: Math.round(position.y) };
  layout.nodes = nodes;
  return layout;
}

export function updateTopologyGraphNodePosition(
  graph: unknown,
  nodeId: string,
  position: XYPosition,
): TopologyGraphEditResult {
  if (!isTopologyGraphV2(graph)) return SCHEMA_V2_ERROR;
  const next = cloneGraph(graph);
  if (!rawNodes(next).some((node) => text(node.id) === nodeId)) {
    return {
      ok: false,
      message: `Node '${nodeId}' was not found.`,
      messageKey: "core.ui.pipelines.topology.error.node_not_found",
      messageParams: { nodeId },
    };
  }
  next.layout = writeNodePosition(next.layout, nodeId, position);
  return { ok: true, graph: next, nodeId };
}

export function updateTopologyGraphNodeConfig(
  graph: unknown,
  nodeId: string,
  config: JsonRecord,
): TopologyGraphEditResult {
  if (!isTopologyGraphV2(graph)) return SCHEMA_V2_ERROR;
  const next = cloneGraph(graph);
  const nodes = rawNodes(next);
  let found = false;
  next.nodes = nodes.map((node) => {
    if (text(node.id) !== nodeId) return node;
    found = true;
    return { ...node, config: cloneRecord(config) };
  });
  return found
    ? { ok: true, graph: next, nodeId }
    : {
        ok: false,
        message: `Node '${nodeId}' was not found.`,
        messageKey: "core.ui.pipelines.topology.error.node_not_found",
        messageParams: { nodeId },
      };
}

export function updateTopologyGraphEdgePolicy(
  graph: unknown,
  edgeId: string,
  patch: TopologyEdgePolicyPatch,
): TopologyGraphEditResult {
  if (!isTopologyGraphV2(graph)) return SCHEMA_V2_ERROR;
  const next = cloneGraph(graph);
  let found = false;
  next.edges = rawEdges(next).map((edge) => {
    if (text(edge.uid) !== edgeId) return edge;
    found = true;
    const traffic = { ...cloneRecord(edge.traffic) };
    const queue = { ...cloneRecord(edge.queue) };
    const backpressure = { ...cloneRecord(edge.backpressure) };
    if (patch.maxItems !== undefined) queue.max_items = positiveInt(patch.maxItems, 1);
    if (patch.dropPolicy !== undefined) queue.drop_policy = patch.dropPolicy;
    if (patch.pressureMode !== undefined) backpressure.mode = patch.pressureMode;
    if (patch.continuous !== undefined) traffic.continuous = patch.continuous;
    if (patch.modality !== undefined) traffic.modality = patch.modality;
    if (patch.semanticClass !== undefined) traffic.semantic_class = patch.semanticClass;
    return { ...edge, traffic, queue, backpressure };
  });
  return found
    ? { ok: true, graph: next, edgeId }
    : {
        ok: false,
        message: `Edge '${edgeId}' was not found.`,
        messageKey: "core.ui.pipelines.topology.error.edge_not_found",
        messageParams: { edgeId },
      };
}

export function connectTopologyGraphEdge(
  graph: unknown,
  connection: Connection,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): TopologyGraphEditResult {
  if (!isTopologyGraphV2(graph)) return SCHEMA_V2_ERROR;
  const sourceNodeId = text(connection.source);
  const targetNodeId = text(connection.target);
  if (!sourceNodeId || !targetNodeId) {
    return {
      ok: false,
      message: "Connection must have source and target nodes.",
      messageKey: "core.ui.pipelines.topology.error.connection_needs_nodes",
    };
  }
  if (sourceNodeId === targetNodeId) {
    return {
      ok: false,
      message: "A node cannot connect to itself in a DAG.",
      messageKey: "core.ui.pipelines.topology.error.self_connection",
    };
  }

  const next = cloneGraph(graph);
  const nodes = rawNodes(next);
  const edges = rawEdges(next);
  const sourceNode = nodes.find((node) => text(node.id) === sourceNodeId) ?? null;
  const targetNode = nodes.find((node) => text(node.id) === targetNodeId) ?? null;
  if (!sourceNode || !targetNode) {
    return {
      ok: false,
      message: "Connection references a node that no longer exists.",
      messageKey: "core.ui.pipelines.topology.error.connection_missing_node",
    };
  }

  const sourceDefinition = operatorsById[text(sourceNode.operator)] ?? null;
  const targetDefinition = operatorsById[text(targetNode.operator)] ?? null;
  const sourcePort = text(connection.sourceHandle, portName(sourceDefinition, "outputs", "out"));
  const targetPort = text(connection.targetHandle, portName(targetDefinition, "inputs", "in"));

  for (const edge of edges) {
    const source = readEndpoint(edge, "from", "out");
    const target = readEndpoint(edge, "to", "in");
    if (!source || !target) continue;
    if (target.node === targetNodeId && target.port === targetPort) {
      return {
        ok: false,
        message: `${targetNodeId}.${targetPort} already has an incoming edge.`,
        messageKey: "core.ui.pipelines.topology.error.input_taken",
        messageParams: { target: `${targetNodeId}.${targetPort}` },
      };
    }
    if (
      source.node === sourceNodeId &&
      source.port === sourcePort &&
      target.node === targetNodeId &&
      target.port === targetPort
    ) {
      return {
        ok: false,
        message: "This connection already exists.",
        messageKey: "core.ui.pipelines.topology.error.connection_exists",
      };
    }
  }

  if (pathExists(edges, targetNodeId, sourceNodeId)) {
    return {
      ok: false,
      message: "This connection would create a cycle.",
      messageKey: "core.ui.pipelines.topology.error.cycle",
    };
  }

  const edgeIdBase = sanitizeNodeId(`edge_${sourceNodeId}_${sourcePort}_${targetNodeId}_${targetPort}`);
  const edgeUid = uniqueValue(edgeIdBase, new Set(edges.map((edge) => text(edge.uid)).filter(Boolean)));
  const edge = {
    uid: edgeUid,
    from: { node: sourceNodeId, port: sourcePort },
    to: { node: targetNodeId, port: targetPort },
    ...defaultEdgePolicy(sourceDefinition, targetDefinition),
  };
  next.edges = [...edges, edge];
  return { ok: true, graph: next, edgeId: edgeUid };
}
