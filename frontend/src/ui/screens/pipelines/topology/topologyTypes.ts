import type { Edge, Node } from "@xyflow/react";

import type {
  GraphRuntimeEdgeInfo,
  GraphRuntimeInfo,
  GraphRuntimeNodeInfo,
  PipelineAlert,
  PipelineOperatorDefinition,
} from "../../../../util/api";
import type { PipelineOperatorGroupId } from "../constants";

export type TopologyPressureState = "none" | "warning" | "critical" | "error";

export type TopologyRuntimeStatus = {
  loading: boolean;
  error: string | null;
  stale: boolean;
};

export type TopologyNodeData = {
  [key: string]: unknown;
  uid: string;
  nodeId: string;
  operatorId: string;
  config: Record<string, unknown>;
  label: string;
  description: string;
  groupId: PipelineOperatorGroupId | "unknown";
  color: string;
  inputPorts: string[];
  outputPorts: string[];
  alerts: PipelineAlert[];
  alertCount: number;
  alertSeverity: PipelineAlert["severity"] | null;
  runtime: GraphRuntimeNodeInfo | null;
  pressureState: TopologyPressureState;
  resourceKind: string;
  pressureBehavior: string;
};

export type TopologyEdgeData = {
  [key: string]: unknown;
  uid: string;
  sourceNodeId: string;
  sourcePort: string;
  targetNodeId: string;
  targetPort: string;
  label: string;
  modality: string;
  continuous: boolean;
  semanticClass: string;
  maxItems: number;
  dropPolicy: string;
  pressureMode: string;
  alerts: PipelineAlert[];
  alertCount: number;
  alertSeverity: PipelineAlert["severity"] | null;
  runtime: GraphRuntimeEdgeInfo | null;
  pressureState: TopologyPressureState;
};

export type TopologyNode = Node<TopologyNodeData, "topologyNode">;
export type TopologyEdge = Edge<TopologyEdgeData, "topologyEdge">;

export type TopologySelection =
  | { kind: "node"; id: string }
  | { kind: "edge"; id: string }
  | { kind: "summary" };

export type TopologySummary = {
  graphUid: string;
  schemaVersion: number;
  nodeCount: number;
  edgeCount: number;
  sourceCount: number;
  sinkCount: number;
  pressureActive: boolean;
  pressureCause: string;
};

export type TopologyModel = {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  summary: TopologySummary;
};

export type TopologyBuildResult =
  | { ok: true; model: TopologyModel }
  | { ok: false; title: string; detail: string; titleKey?: string; detailKey?: string };

export type TopologyBuildOptions = {
  graph: unknown;
  operatorsById: Record<string, PipelineOperatorDefinition>;
  alerts: PipelineAlert[];
  runtimeInfo: GraphRuntimeInfo | null;
};
