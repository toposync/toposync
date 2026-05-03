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

const PYTHON_IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const PYTHON_KEYWORDS = new Set([
  "False",
  "None",
  "True",
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

function pythonString(value: string): string {
  return JSON.stringify(String(value));
}

function pythonLiteral(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "None";
  if (typeof value === "string") return pythonString(value);
  if (Array.isArray(value)) return `[${value.map((item) => pythonLiteral(item)).join(", ")}]`;
  if (isRecord(value)) {
    const entries = Object.entries(value).map(([key, item]) => `${pythonString(key)}: ${pythonLiteral(item)}`);
    return `{${entries.join(", ")}}`;
  }
  return pythonString(String(value));
}

function isPythonIdentifier(value: string): boolean {
  return PYTHON_IDENTIFIER_RE.test(value) && !PYTHON_KEYWORDS.has(value);
}

function pythonVariableName(raw: string, fallback: string, used: Set<string>): string {
  let value = String(raw || "")
    .trim()
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!value) value = fallback;
  if (/^[0-9]/.test(value)) value = `_${value}`;
  if (!isPythonIdentifier(value)) value = `node_${value}`;

  const root = value;
  let suffix = 2;
  while (used.has(value)) {
    value = `${root}_${suffix}`;
    suffix += 1;
  }
  used.add(value);
  return value;
}

function formatPythonCall(functionName: string, positionalArgs: string[], kwargs: Array<[string, unknown]>): string {
  const parts = [...positionalArgs];
  const dictOnlyKwargs: Array<[string, unknown]> = [];

  for (const [key, value] of kwargs) {
    const normalizedKey = String(key || "").trim();
    if (!normalizedKey) continue;
    if (isPythonIdentifier(normalizedKey)) {
      parts.push(`${normalizedKey}=${pythonLiteral(value)}`);
    } else {
      dictOnlyKwargs.push([normalizedKey, value]);
    }
  }

  if (dictOnlyKwargs.length > 0) {
    const dictItems = dictOnlyKwargs.map(([key, value]) => `${pythonString(key)}: ${pythonLiteral(value)}`);
    parts.push(`**{${dictItems.join(", ")}}`);
  }

  return `${functionName}(${parts.join(", ")})`;
}

function graphNodeFactoryCall(node: Record<string, unknown>): string {
  const operatorId = String(node.operator || "").trim();
  const nodeId = String(node.id || "").trim();
  const config = isRecord(node.config) ? node.config : {};
  return formatPythonCall("op", [pythonString(operatorId)], [
    ["_id", nodeId],
    ...Object.entries(config),
  ]);
}

function graphNodeHasRequiredInputs(
  node: Record<string, unknown>,
  operatorsById: Record<string, PipelineOperatorDefinition>,
  incomingEdges: Map<string, number>,
): boolean {
  const operatorId = String(node.operator || "").trim();
  const operator = operatorsById[operatorId] ?? null;
  if (!operator) return (incomingEdges.get(String(node.id || "").trim()) ?? 0) > 0;
  return (operator.inputs ?? []).some((port) => Boolean(port.required));
}

function streamExpressionForNode(nodeVar: string, streamVar: string | undefined, sourcePort: string): string {
  const port = String(sourcePort || "out").trim() || "out";
  const base = streamVar || nodeVar;
  if (port === "out") return base;
  return streamVar ? `${streamVar}.port(${pythonString(port)})` : `${nodeVar}.as_stream().port(${pythonString(port)})`;
}

function targetExpressionForEdge(nodeVar: string, edge: Record<string, unknown>): string {
  let expr = nodeVar;
  const target = isRecord(edge.to) ? edge.to : {};
  const targetPort = String(target.port || "").trim();
  if (targetPort) expr = `${expr}.with_input_port(${pythonString(targetPort)})`;

  const channelKwargs: Array<[string, unknown]> = [];
  if (edge.maxsize !== undefined && edge.maxsize !== null) channelKwargs.push(["maxsize", edge.maxsize]);
  if (edge.drop_policy !== undefined && edge.drop_policy !== null && String(edge.drop_policy || "").trim()) {
    channelKwargs.push(["drop_policy", String(edge.drop_policy || "").trim()]);
  }
  if (channelKwargs.length > 0) expr = `${expr}.${formatPythonCall("with_channel", [], channelKwargs)}`;
  return expr;
}

export function pythonSourceFromGraph(
  graphValue: unknown,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): { ok: true; source: string } | { ok: false; message: string } {
  if (!isRecord(graphValue)) return { ok: false, message: "Graph JSON must be an object." };

  const rawNodes = Array.isArray(graphValue.nodes) ? graphValue.nodes : [];
  const rawEdges = Array.isArray(graphValue.edges) ? graphValue.edges : [];
  const nodes = rawNodes.filter(isRecord);
  const edges = rawEdges.filter(isRecord);
  if (nodes.length === 0) return { ok: true, source: "" };

  const usedVariables = new Set<string>();
  const nodeVariables = new Map<string, string>();
  const nodesById = new Map<string, Record<string, unknown>>();
  const incomingEdgeCounts = new Map<string, number>();
  const edgeNodeIds = new Set<string>();

  for (const edge of edges) {
    const source = isRecord(edge.from) ? edge.from : {};
    const target = isRecord(edge.to) ? edge.to : {};
    const sourceNodeId = String(source.node || "").trim();
    const targetNodeId = String(target.node || "").trim();
    if (sourceNodeId) edgeNodeIds.add(sourceNodeId);
    if (targetNodeId) edgeNodeIds.add(targetNodeId);
    if (targetNodeId) incomingEdgeCounts.set(targetNodeId, (incomingEdgeCounts.get(targetNodeId) ?? 0) + 1);
  }

  const lines: string[] = [];
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index];
    const nodeId = String(node.id || "").trim();
    const operatorId = String(node.operator || "").trim();
    if (!nodeId) return { ok: false, message: `Graph node ${index + 1} has no id.` };
    if (!operatorId) return { ok: false, message: `Graph node ${nodeId} has no operator.` };
    if (nodesById.has(nodeId)) return { ok: false, message: `Duplicate graph node id '${nodeId}'.` };
    nodesById.set(nodeId, node);
    nodeVariables.set(nodeId, pythonVariableName(nodeId, `node_${index + 1}`, usedVariables));
  }

  for (const node of nodes) {
    const nodeId = String(node.id || "").trim();
    lines.push(`${nodeVariables.get(nodeId)} = ${graphNodeFactoryCall(node)}`);
  }

  const streamVariables = new Map<string, string>();
  const sourceCapableNodeIds = new Set<string>();
  for (const node of nodes) {
    const nodeId = String(node.id || "").trim();
    if (!graphNodeHasRequiredInputs(node, operatorsById, incomingEdgeCounts)) sourceCapableNodeIds.add(nodeId);
  }

  let lastStreamExpression = "";
  const pendingEdges = edges.map((edge, index) => ({ edge, index }));
  const emittedEdges = new Set<number>();

  while (emittedEdges.size < pendingEdges.length) {
    let progressed = false;

    for (const { edge, index } of pendingEdges) {
      if (emittedEdges.has(index)) continue;

      const source = isRecord(edge.from) ? edge.from : {};
      const target = isRecord(edge.to) ? edge.to : {};
      const sourceNodeId = String(source.node || "").trim();
      const targetNodeId = String(target.node || "").trim();
      if (!sourceNodeId || !targetNodeId) return { ok: false, message: `Graph edge ${index + 1} is missing source or target node.` };
      if (!nodesById.has(sourceNodeId)) return { ok: false, message: `Graph edge ${index + 1} references unknown source node '${sourceNodeId}'.` };
      if (!nodesById.has(targetNodeId)) return { ok: false, message: `Graph edge ${index + 1} references unknown target node '${targetNodeId}'.` };

      const sourceVar = nodeVariables.get(sourceNodeId) || sourceNodeId;
      const targetVar = nodeVariables.get(targetNodeId) || targetNodeId;
      const sourceStreamVar = streamVariables.get(sourceNodeId);
      if (!sourceStreamVar && !sourceCapableNodeIds.has(sourceNodeId)) continue;

      const targetStreamVar = pythonVariableName(`${targetVar}_out`, `stream_${index + 1}`, usedVariables);
      const sourceExpr = streamExpressionForNode(sourceVar, sourceStreamVar, String(source.port || "out"));
      const targetExpr = targetExpressionForEdge(targetVar, edge);
      lines.push(`${targetStreamVar} = ${sourceExpr} | ${targetExpr}`);
      streamVariables.set(targetNodeId, targetStreamVar);
      lastStreamExpression = targetStreamVar;
      emittedEdges.add(index);
      progressed = true;
    }

    if (!progressed) {
      return {
        ok: false,
        message: "Current graph cannot be converted to Python automatically; it may contain a cycle or a node with missing upstream input.",
      };
    }
  }

  for (const node of nodes) {
    const nodeId = String(node.id || "").trim();
    if (streamVariables.has(nodeId) || edgeNodeIds.has(nodeId) || !sourceCapableNodeIds.has(nodeId)) continue;
    const nodeVar = nodeVariables.get(nodeId) || nodeId;
    const streamVar = pythonVariableName(`${nodeVar}_out`, "stream", usedVariables);
    lines.push(`${streamVar} = ${nodeVar}.as_stream()`);
    streamVariables.set(nodeId, streamVar);
    if (!lastStreamExpression) lastStreamExpression = streamVar;
  }

  if (!lastStreamExpression) {
    const rootNode = nodes[0];
    const rootNodeId = String(rootNode.id || "").trim();
    lastStreamExpression = streamVariables.get(rootNodeId) || nodeVariables.get(rootNodeId) || "";
  }

  if (!lastStreamExpression) return { ok: false, message: "Current graph cannot be converted to Python automatically." };

  lines.push(`PIPELINE = ${lastStreamExpression}`);
  return { ok: true, source: `${lines.join("\n")}\n` };
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

export function prettyOperatorDescription(
  operator: Pick<PipelineOperatorDefinition, "id" | "description"> | string,
): string {
  const operatorId = typeof operator === "string" ? operator : String(operator.id || "").trim();
  const fallbackDescription = typeof operator === "string" ? "" : String(operator.description || "").trim();
  if (!operatorId) return fallbackDescription;
  const localized = i18n.t(`core.ui.pipelines.operator_description.${operatorId}`, {}, "");
  if (localized) return localized;
  return fallbackDescription || prettyOperatorName(operatorId);
}

export function emptyGraph(): Record<string, unknown> {
  return { schema_version: 1, nodes: [], edges: [] };
}

export function defaultPipeline(name: string): Pipeline {
  return {
    name,
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

function isSinkOperator(definition: PipelineOperatorDefinition | null): boolean {
  return operatorCapabilities(definition).has("sink");
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
  let mainTailNodeId: string | null = null;
  if (nodes.length > 0) {
    const firstOperatorId = String(nodes[0].operator || "");
    const firstOperator = operatorsById[firstOperatorId] ?? null;
    if (!isGateControlOperator(firstOperator) && !isSinkOperator(firstOperator)) {
      mainTailNodeId = String(nodes[0].id || "").trim() || null;
    }
  }
  for (let index = 0; index < nodes.length - 1; index += 1) {
    const previousNode = nodes[index];
    const targetNode = nodes[index + 1];
    const previousOperatorId = String(previousNode.operator || "");
    const targetOperatorId = String(targetNode.operator || "");
    const previousOperator = operatorsById[previousOperatorId] ?? null;
    const targetOperator = operatorsById[targetOperatorId] ?? null;
    const upstreamNode = mainTailNodeId
      ? nodes.find((node) => String(node.id || "") === mainTailNodeId) ?? previousNode
      : previousNode;
    const upstreamOperatorId = String(upstreamNode.operator || "");
    const upstreamOperator = operatorsById[upstreamOperatorId] ?? null;
    const policy = edgePolicyFor(upstreamOperator, targetOperator);

    const isGateControl = isGateControlOperator(previousOperator);

    let targetPort = "in";
    let sourceNodeId: string | null = null;
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
      sourceNodeId = String(previousNode.id || "").trim() || null;
    } else if (isGateControl) {
      return {
        graph: null,
        error: "Gate control steps must be followed by a source operator in interactive mode.",
      };
    } else if (mainTailNodeId) {
      sourceNodeId = mainTailNodeId;
    } else if (!isSinkOperator(previousOperator)) {
      sourceNodeId = String(previousNode.id || "").trim() || null;
    }

    if (!sourceNodeId) {
      return {
        graph: null,
        error: `${prettyOperatorName(targetOperatorId)} must follow a source or processing step in interactive mode.`,
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
      from: { node: sourceNodeId, port: "out" },
      to: { node: targetNode.id, port: targetPort },
      maxsize: policy.maxsize,
      drop_policy: policy.drop_policy,
    });

    if (isSourceOperator(targetOperator) || !isSinkOperator(targetOperator)) {
      mainTailNodeId = String(targetNode.id || "").trim() || null;
    }
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

function interactiveWarning(key: string, fallback: string): string {
  return i18n.t(key, {}, fallback);
}

function readLinearNodeOrder(
  graphValue: unknown,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): { order: string[]; warning: string | null } {
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

  const nodeOrderIndex = new Map<string, number>();
  const sinkNodeIds = new Set<string>();
  for (let index = 0; index < rawNodes.length; index += 1) {
    const node = rawNodes[index];
    if (!isRecord(node)) continue;
    const nodeId = String(node.id || "").trim();
    if (!nodeId) continue;
    nodeOrderIndex.set(nodeId, index);
    const operatorId = String(node.operator || "").trim();
    if (isSinkOperator(operatorsById[operatorId] ?? null)) sinkNodeIds.add(nodeId);
  }

  const mainInDegree = new Map<string, number>();
  const mainOutEdges = new Map<string, string[]>();
  const sinkTargetsBySource = new Map<string, string[]>();
  for (const id of nodeIds) {
    if (!sinkNodeIds.has(id)) {
      mainInDegree.set(id, 0);
      mainOutEdges.set(id, []);
    }
    sinkTargetsBySource.set(id, []);
  }

  for (const edge of rawEdges) {
    if (!isRecord(edge)) continue;
    const from = isRecord(edge.from) ? String(edge.from.node || "").trim() : "";
    const to = isRecord(edge.to) ? String(edge.to.node || "").trim() : "";
    if (!from || !to) continue;
    if (!nodeOrderIndex.has(from) || !nodeOrderIndex.has(to)) continue;
    if (sinkNodeIds.has(from)) {
      return {
        order: nodeIds,
        warning: interactiveWarning(
          "core.ui.pipelines.editor.warning.non_linear_graph",
          "Graph is not compatible with interactive step ordering. Interactive mode loaded node list order and will rewrite edges.",
        ),
      };
    }
    if (sinkNodeIds.has(to)) {
      sinkTargetsBySource.set(from, [...(sinkTargetsBySource.get(from) ?? []), to]);
      continue;
    }
    mainOutEdges.set(from, [...(mainOutEdges.get(from) ?? []), to]);
    mainInDegree.set(to, (mainInDegree.get(to) ?? 0) + 1);
  }

  if ([...mainOutEdges.values()].some((targets) => targets.length > 1) || [...mainInDegree.values()].some((count) => count > 1)) {
    return {
      order: nodeIds,
      warning: interactiveWarning(
        "core.ui.pipelines.editor.warning.non_linear_graph",
        "Graph is not compatible with interactive step ordering. Interactive mode loaded node list order and will rewrite edges.",
      ),
    };
  }

  const mainNodeIds = nodeIds.filter((id) => !sinkNodeIds.has(id));
  if (mainNodeIds.length === 0) {
    return { order: nodeIds, warning: null };
  }

  const starts = mainNodeIds.filter((id) => (mainInDegree.get(id) ?? 0) === 0);
  if (starts.length !== 1) {
    return {
      order: nodeIds,
      warning: interactiveWarning(
        "core.ui.pipelines.editor.warning.multiple_starts",
        "Graph has multiple starts. Interactive mode loaded node list order and will rewrite edges.",
      ),
    };
  }

  const mainOrder: string[] = [];
  const visited = new Set<string>();
  let current: string | undefined = starts[0];
  while (current) {
    if (visited.has(current)) {
      return {
        order: nodeIds,
        warning: interactiveWarning(
          "core.ui.pipelines.editor.warning.cycle",
          "Graph contains a cycle. Interactive mode loaded node list order and will rewrite edges.",
        ),
      };
    }
    visited.add(current);
    mainOrder.push(current);
    const nextNodes: string[] = mainOutEdges.get(current) ?? [];
    current = nextNodes.length > 0 ? nextNodes[0] : undefined;
  }

  if (mainOrder.length !== mainNodeIds.length) {
    return {
      order: nodeIds,
      warning: interactiveWarning(
        "core.ui.pipelines.editor.warning.disconnected",
        "Graph has disconnected segments. Interactive mode loaded node list order and will rewrite edges.",
      ),
    };
  }

  const order: string[] = [];
  for (const nodeId of mainOrder) {
    order.push(nodeId);
    const sinkTargets = [...(sinkTargetsBySource.get(nodeId) ?? [])].sort(
      (left, right) => (nodeOrderIndex.get(left) ?? Number.MAX_SAFE_INTEGER) - (nodeOrderIndex.get(right) ?? Number.MAX_SAFE_INTEGER),
    );
    order.push(...sinkTargets);
  }

  if (order.length !== nodeIds.length) {
    return {
      order: nodeIds,
      warning: interactiveWarning(
        "core.ui.pipelines.editor.warning.disconnected",
        "Graph has disconnected segments. Interactive mode loaded node list order and will rewrite edges.",
      ),
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

  const { order, warning } = readLinearNodeOrder(graphValue, operatorsById);
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
      collapsed: true,
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
