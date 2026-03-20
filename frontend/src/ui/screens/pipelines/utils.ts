import type { Pipeline, PipelineOperatorDefinition } from "../../../util/api";
import { i18n } from "../../../util/i18n";

import { HUMANIZE_ACRONYMS, NODE_ID_RE, OPERATOR_FRIENDLY_NAMES, PIPELINE_PRESET_OPERATOR_IDS } from "./constants";
import type { DragInsertPosition, InteractiveBuildResult, InteractiveFromGraphResult, InteractiveStep } from "./types";

let interactiveStepCounter = 0;

function nextInteractiveStepUid(): string {
  interactiveStepCounter += 1;
  return `step_${interactiveStepCounter.toString(36)}`;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function safeJsonParse(value: string): { ok: true; data: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, data: JSON.parse(value) };
  } catch (err: any) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}

export function jsonPretty(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

export function humanizeIdentifier(raw: string): string {
  const normalized = String(raw || "").trim();
  if (!normalized) return "";

  const tokens = normalized
    .replace(/[.-]+/g, "_")
    .split("_")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);

  return tokens
    .map((token) => {
      const lower = token.toLowerCase();
      const acronym = HUMANIZE_ACRONYMS[lower];
      if (acronym) return acronym;
      if (!lower) return token;
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

export function prettyConfigKeyLabel(key: string): string {
  const raw = String(key || "").trim();
  const lower = raw.toLowerCase();

  if (lower.endsWith("_seconds")) return `${humanizeIdentifier(raw.slice(0, -8))} (seconds)`;
  if (lower.endsWith("_ms")) return `${humanizeIdentifier(raw.slice(0, -3))} (ms)`;
  if (lower.endsWith("_kmh")) return `${humanizeIdentifier(raw.slice(0, -4))} (km/h)`;
  if (lower.endsWith("_mps")) return `${humanizeIdentifier(raw.slice(0, -4))} (m/s)`;

  return humanizeIdentifier(raw);
}

export function prettyOperatorName(operatorId: string): string {
  const raw = String(operatorId || "").trim();
  if (!raw) return "";
  const localized = i18n.t(`core.ui.pipelines.operator_name.${raw}`, {}, "");
  if (localized) return localized;
  const fromMap = OPERATOR_FRIENDLY_NAMES[raw];
  if (fromMap) return fromMap;
  const tail = raw.split(".").pop() || raw;
  return humanizeIdentifier(tail) || raw;
}

export function emptyGraph(): Record<string, unknown> {
  return { schema_version: 1, nodes: [], edges: [] };
}

export function defaultPipeline(name: string, type: "reuse" | "final"): Pipeline {
  return {
    name,
    type,
    enabled: true,
    processing_server_id: "local",
    editor_mode: "interactive",
    python_source: "",
    graph: emptyGraph(),
  };
}

function slugifyOperatorId(operatorId: string): string {
  const raw = String(operatorId || "").trim();
  if (!raw) return "step";
  const last = raw.split(".").pop() || raw;
  const slug = last.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
  return slug || "step";
}

function nextUniqueNodeId(base: string, used: Set<string>): string {
  const normalizedBase = (base || "step")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^\d/, "_")
    .replace(/^_+|_+$/g, "");
  const root = normalizedBase || "step";
  if (!used.has(root)) {
    used.add(root);
    return root;
  }
  let index = 2;
  while (used.has(`${root}_${index}`)) {
    index += 1;
  }
  const value = `${root}_${index}`;
  used.add(value);
  return value;
}

export function createInteractiveStep(
  operatorId: string,
  defaults: Record<string, unknown>,
  usedNodeIds: Set<string>,
): InteractiveStep {
  const nodeId = nextUniqueNodeId(slugifyOperatorId(operatorId), usedNodeIds);
  return {
    uid: nextInteractiveStepUid(),
    nodeId,
    operatorId,
    configText: jsonPretty(defaults),
    collapsed: false,
    showAdvanced: false,
  };
}

function edgePolicyFor(
  source: PipelineOperatorDefinition | null,
  target: PipelineOperatorDefinition | null,
): { maxsize: number; drop_policy: string } {
  const sourceCaps = new Set((source?.capabilities ?? []).map((value) => String(value).trim().toLowerCase()));
  const targetCaps = new Set((target?.capabilities ?? []).map((value) => String(value).trim().toLowerCase()));

  if (targetCaps.has("heavy_compute")) return { maxsize: 1, drop_policy: "latest_only" };
  if (sourceCaps.has("source")) return { maxsize: 1, drop_policy: "latest_only" };
  if (targetCaps.has("sink") || targetCaps.has("origin_only")) {
    return { maxsize: 128, drop_policy: "drop_oldest" };
  }
  if (sourceCaps.has("split_stream")) {
    return { maxsize: 64, drop_policy: "keyed_latest_only" };
  }
  return { maxsize: 32, drop_policy: "drop_oldest" };
}

function operatorCapabilities(definition: PipelineOperatorDefinition | null): Set<string> {
  return new Set((definition?.capabilities ?? []).map((value) => String(value || "").trim().toLowerCase()));
}

function isSourceOperator(definition: PipelineOperatorDefinition | null): boolean {
  return operatorCapabilities(definition).has("source");
}

function isGateControlOperator(definition: PipelineOperatorDefinition | null): boolean {
  return operatorCapabilities(definition).has("gate_control");
}

function resolveGatePortName(definition: PipelineOperatorDefinition | null): string | null {
  const inputs = Array.isArray(definition?.inputs) ? definition.inputs : [];
  for (const input of inputs) {
    const name = String(input?.name || "").trim();
    if (name === "gate") return name;
  }
  return null;
}

export function buildGraphFromInteractiveSteps(
  steps: InteractiveStep[],
  operatorsById: Record<string, PipelineOperatorDefinition>,
): InteractiveBuildResult {
  const usedNodeIds = new Set<string>();
  const nodes: Array<Record<string, unknown>> = [];

  for (let index = 0; index < steps.length; index += 1) {
    const step = steps[index];
    const operatorId = String(step.operatorId || "").trim();
    if (!operatorId) {
      return { graph: null, error: `Step ${index + 1} has no operator selected.` };
    }
    if (!operatorsById[operatorId]) {
      return { graph: null, error: `Step ${index + 1} uses unknown operator '${operatorId}'.` };
    }

    const nodeIdRaw = String(step.nodeId || "").trim();
    if (!nodeIdRaw) {
      return { graph: null, error: `Step ${index + 1} must have a node id.` };
    }
    if (!NODE_ID_RE.test(nodeIdRaw)) {
      return { graph: null, error: `Step ${index + 1} node id '${nodeIdRaw}' is invalid.` };
    }
    if (usedNodeIds.has(nodeIdRaw)) {
      return { graph: null, error: `Duplicate node id '${nodeIdRaw}'.` };
    }
    usedNodeIds.add(nodeIdRaw);

    const parsed = safeJsonParse(step.configText || "{}");
    if (!parsed.ok) {
      return { graph: null, error: `Step ${index + 1} has invalid config JSON: ${parsed.error}` };
    }
    if (!isRecord(parsed.data)) {
      return { graph: null, error: `Step ${index + 1} config must be a JSON object.` };
    }

    nodes.push({
      id: nodeIdRaw,
      operator: operatorId,
      config: parsed.data,
    });
  }

  const edges: Array<Record<string, unknown>> = [];
  for (let index = 0; index < nodes.length - 1; index += 1) {
    const sourceNode = nodes[index];
    const targetNode = nodes[index + 1];
    const sourceOperatorId = String(sourceNode.operator || "");
    const targetOperatorId = String(targetNode.operator || "");
    const sourceOperator = operatorsById[sourceOperatorId] ?? null;
    const targetOperator = operatorsById[targetOperatorId] ?? null;
    const policy = edgePolicyFor(sourceOperator, targetOperator);

    const isGateControl = isGateControlOperator(sourceOperator);

    let targetPort = "in";
    if (isSourceOperator(targetOperator)) {
      if (!isGateControl) {
        return {
          graph: null,
          error: `${prettyOperatorName(targetOperatorId)} must be the first step or be preceded by a gate control step.`,
        };
      }
      const gatePort = resolveGatePortName(targetOperator);
      if (!gatePort) {
        return {
          graph: null,
          error: `${prettyOperatorName(targetOperatorId)} does not expose a 'gate' input port for interactive gate control.`,
        };
      }
      targetPort = gatePort;
    } else if (isGateControl) {
      return {
        graph: null,
        error: "Gate control steps must be followed by a source operator in interactive mode.",
      };
    }

    const targetInputs = new Set(
      (targetOperator?.inputs ?? []).map((port) => String(port.name || "").trim()).filter((value) => value.length > 0),
    );
    if (targetInputs.size > 0 && !targetInputs.has(targetPort)) {
      return {
        graph: null,
        error: `Step ${index + 2} (${prettyOperatorName(targetOperatorId)}) has no input port '${targetPort}'.`,
      };
    }

    edges.push({
      from: { node: sourceNode.id, port: "out" },
      to: { node: targetNode.id, port: targetPort },
      maxsize: policy.maxsize,
      drop_policy: policy.drop_policy,
    });
  }

  return {
    graph: {
      schema_version: 1,
      nodes,
      edges,
    },
    error: null,
  };
}

function readLinearNodeOrder(graphValue: unknown): { order: string[]; warning: string | null } {
  if (!isRecord(graphValue)) {
    return { order: [], warning: null };
  }
  const rawNodes = Array.isArray(graphValue.nodes) ? graphValue.nodes : [];
  const rawEdges = Array.isArray(graphValue.edges) ? graphValue.edges : [];
  const nodeIds = rawNodes
    .map((value) => (isRecord(value) ? String(value.id || "").trim() : ""))
    .filter((value) => value.length > 0);

  if (nodeIds.length <= 1) {
    return { order: nodeIds, warning: null };
  }

  const inDegree = new Map<string, number>();
  const outEdges = new Map<string, string[]>();
  for (const id of nodeIds) {
    inDegree.set(id, 0);
    outEdges.set(id, []);
  }

  for (const edge of rawEdges) {
    if (!isRecord(edge)) continue;
    const from = isRecord(edge.from) ? String(edge.from.node || "").trim() : "";
    const to = isRecord(edge.to) ? String(edge.to.node || "").trim() : "";
    if (!from || !to) continue;
    if (!inDegree.has(from) || !inDegree.has(to)) continue;
    outEdges.set(from, [...(outEdges.get(from) ?? []), to]);
    inDegree.set(to, (inDegree.get(to) ?? 0) + 1);
  }

  if ([...outEdges.values()].some((targets) => targets.length > 1) || [...inDegree.values()].some((count) => count > 1)) {
    return {
      order: nodeIds,
      warning: "Graph is not a simple chain. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  const starts = nodeIds.filter((id) => (inDegree.get(id) ?? 0) === 0);
  if (starts.length !== 1) {
    return {
      order: nodeIds,
      warning: "Graph has multiple starts. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  const order: string[] = [];
  const visited = new Set<string>();
  let current: string | undefined = starts[0];
  while (current) {
    if (visited.has(current)) {
      return {
        order: nodeIds,
        warning: "Graph contains a cycle. Interactive mode loaded node list order and will rewrite edges sequentially.",
      };
    }
    visited.add(current);
    order.push(current);
    const nextNodes: string[] = outEdges.get(current) ?? [];
    current = nextNodes.length > 0 ? nextNodes[0] : undefined;
  }

  if (order.length !== nodeIds.length) {
    return {
      order: nodeIds,
      warning: "Graph has disconnected segments. Interactive mode loaded node list order and will rewrite edges sequentially.",
    };
  }

  return { order, warning: null };
}

export function buildInteractiveStepsFromGraph(
  graphValue: unknown,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): InteractiveFromGraphResult {
  if (!isRecord(graphValue)) {
    return { steps: [], warning: null };
  }
  const rawNodes = Array.isArray(graphValue.nodes) ? graphValue.nodes : [];
  const nodeById = new Map<string, Record<string, unknown>>();
  for (const node of rawNodes) {
    if (!isRecord(node)) continue;
    const nodeId = String(node.id || "").trim();
    if (!nodeId) continue;
    nodeById.set(nodeId, node);
  }

  const { order, warning } = readLinearNodeOrder(graphValue);
  const usedNodeIds = new Set<string>();
  const steps: InteractiveStep[] = [];

  for (const nodeId of order) {
    const node = nodeById.get(nodeId);
    if (!node) continue;
    const operatorId = String(node.operator || "").trim();
    const operator = operatorsById[operatorId];
    const config = isRecord(node.config) ? node.config : (operator?.defaults ?? {});

    usedNodeIds.add(nodeId);
    steps.push({
      uid: nextInteractiveStepUid(),
      nodeId,
      operatorId,
      configText: jsonPretty(config),
      collapsed: false,
      showAdvanced: false,
    });
  }

  return { steps, warning };
}

export function pickDefaultOperatorId(operators: PipelineOperatorDefinition[]): string {
  for (const operatorId of PIPELINE_PRESET_OPERATOR_IDS) {
    if (operators.some((operator) => operator.id === operatorId)) {
      return operatorId;
    }
  }
  return operators[0]?.id ?? "";
}

export function prettyOperatorLabel(operator: PipelineOperatorDefinition): string {
  return prettyOperatorName(operator.id);
}

export function moveStep(
  items: InteractiveStep[],
  draggedUid: string,
  targetUid: string,
  position: DragInsertPosition,
): InteractiveStep[] {
  if (draggedUid === targetUid) return items;
  const byUid = new Map(items.map((item) => [item.uid, item]));
  if (!byUid.has(draggedUid) || !byUid.has(targetUid)) return items;

  const withoutDragged = items.filter((item) => item.uid !== draggedUid);
  const targetIndex = withoutDragged.findIndex((item) => item.uid === targetUid);
  if (targetIndex < 0) return items;

  const insertIndex = position === "before" ? targetIndex : targetIndex + 1;
  const next = withoutDragged.slice();
  next.splice(insertIndex, 0, byUid.get(draggedUid)!);
  return next;
}
