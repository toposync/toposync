import type { Pipeline, PipelineAlert, PipelineOperatorDefinition } from "../../../util/api";
import { i18n } from "../../../util/i18n";

import {
  HUMANIZE_ACRONYMS,
  NODE_ID_RE,
  OPERATOR_FRIENDLY_NAMES,
  PIPELINE_OPERATOR_GROUP_ORDER,
  PIPELINE_OPERATOR_GROUPS,
  PIPELINE_OPERATOR_RECIPES,
  PIPELINE_OPERATOR_UX,
  PIPELINE_PRESET_OPERATOR_IDS,
} from "./constants";
import type { PipelineOperatorGroupId, PipelineOperatorLevel, PipelineOperatorUxMetadata } from "./constants";
import type { DragInsertPosition, InteractiveBuildResult, InteractiveFromGraphResult, InteractiveStep, PipelineCatalogItem } from "./types";

type TranslationFunction = typeof i18n.t;

const PIPELINE_OPERATOR_GROUP_RANK = new Map<PipelineOperatorGroupId, number>(
  PIPELINE_OPERATOR_GROUP_ORDER.map((groupId, index) => [groupId, index]),
);

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

export function textConfigValue(value: unknown, fallback = ""): string {
  if (value === undefined || value === null) return fallback;
  return String(value);
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

export type ResolvedPipelineOperatorUx = PipelineOperatorUxMetadata & {
  aliases: string[];
};

const DEFAULT_PIPELINE_OPERATOR_UX: ResolvedPipelineOperatorUx = {
  group: "extensions",
  level: "advanced",
  order: 1000,
  aliases: [],
};

function isPipelineOperatorGroupId(value: string): value is PipelineOperatorGroupId {
  return Object.prototype.hasOwnProperty.call(PIPELINE_OPERATOR_GROUPS, value);
}

function normalizePipelineOperatorLevel(value: unknown): PipelineOperatorLevel | null {
  const level = String(value || "").trim().toLowerCase();
  return level === "basic" || level === "advanced" ? level : null;
}

function normalizePipelineOperatorOrder(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const order = Number(value);
  return Number.isFinite(order) ? order : null;
}

function normalizePipelineOperatorAliases(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const alias = String(item || "").trim();
    if (!alias) continue;
    const key = alias.toLocaleLowerCase();
    if (seen.has(key)) continue;
    out.push(alias);
    seen.add(key);
  }
  return out;
}

function resolvePipelineOperatorCapabilityUx(operator: PipelineOperatorDefinition): ResolvedPipelineOperatorUx {
  const capabilities = operatorCapabilities(operator);

  if (capabilities.has("source")) {
    return { group: "input", level: "basic", order: 1000, aliases: [] };
  }
  if (capabilities.has("sink") || capabilities.has("origin_only")) {
    return { group: "output", level: "basic", order: 1000, aliases: [] };
  }
  if (capabilities.has("heavy_compute")) {
    return { group: "vision", level: "advanced", order: 1000, aliases: [] };
  }
  if (capabilities.has("rate_control")) {
    return { group: "rate", level: "advanced", order: 1000, aliases: [] };
  }
  if (capabilities.has("filter") || capabilities.has("gate_control")) {
    return { group: "rules", level: "advanced", order: 1000, aliases: [] };
  }
  if (capabilities.has("debug")) {
    return { group: "diagnostics", level: "advanced", order: 1000, aliases: [] };
  }

  return DEFAULT_PIPELINE_OPERATOR_UX;
}

export function resolvePipelineOperatorUx(operator: PipelineOperatorDefinition): ResolvedPipelineOperatorUx {
  const local = PIPELINE_OPERATOR_UX[operator.id as keyof typeof PIPELINE_OPERATOR_UX] as
    | PipelineOperatorUxMetadata
    | undefined;
  const capabilityFallback = resolvePipelineOperatorCapabilityUx(operator);
  const ui = operator.ui ?? null;
  const uiGroupValue = String(ui?.pipeline_group || "").trim().toLowerCase();
  const uiGroup = isPipelineOperatorGroupId(uiGroupValue) ? uiGroupValue : null;
  const uiLevel = normalizePipelineOperatorLevel(ui?.pipeline_level);
  const uiOrder = normalizePipelineOperatorOrder(ui?.pipeline_order);
  const uiAliases = normalizePipelineOperatorAliases(ui?.aliases);
  const localAliases = normalizePipelineOperatorAliases(local?.aliases);

  return {
    group: uiGroup ?? local?.group ?? capabilityFallback.group,
    level: uiLevel ?? local?.level ?? capabilityFallback.level,
    order: uiOrder ?? local?.order ?? capabilityFallback.order,
    aliases: [...uiAliases, ...localAliases],
  };
}

export function comparePipelineOperatorsByUx(
  left: PipelineOperatorDefinition,
  right: PipelineOperatorDefinition,
): number {
  const leftUx = resolvePipelineOperatorUx(left);
  const rightUx = resolvePipelineOperatorUx(right);
  const leftGroupRank = PIPELINE_OPERATOR_GROUP_RANK.get(leftUx.group) ?? Number.MAX_SAFE_INTEGER;
  const rightGroupRank = PIPELINE_OPERATOR_GROUP_RANK.get(rightUx.group) ?? Number.MAX_SAFE_INTEGER;

  if (leftGroupRank !== rightGroupRank) return leftGroupRank - rightGroupRank;
  if (leftUx.level !== rightUx.level) return leftUx.level === "basic" ? -1 : 1;
  if (leftUx.order !== rightUx.order) return leftUx.order - rightUx.order;

  const nameComparison = prettyOperatorName(left.id).localeCompare(prettyOperatorName(right.id));
  if (nameComparison !== 0) return nameComparison;
  return left.id.localeCompare(right.id);
}

export function sortPipelineOperatorsForToolbar(
  operators: PipelineOperatorDefinition[],
): PipelineOperatorDefinition[] {
  return [...operators].sort(comparePipelineOperatorsByUx);
}

function resolvePipelineCatalogItemUx(item: PipelineCatalogItem): ResolvedPipelineOperatorUx {
  if (item.kind === "recipe") {
    return {
      group: item.recipe.group,
      level: item.recipe.level,
      order: item.recipe.order,
      aliases: [],
    };
  }
  return resolvePipelineOperatorUx(item.operator);
}

function pipelineCatalogItemName(item: PipelineCatalogItem): string {
  if (item.kind === "recipe") {
    return i18n.t(item.recipe.labelKey, {}, item.recipe.fallbackLabel);
  }
  return prettyOperatorName(item.operator.id);
}

export function comparePipelineCatalogItemsByUx(
  left: PipelineCatalogItem,
  right: PipelineCatalogItem,
): number {
  const leftUx = resolvePipelineCatalogItemUx(left);
  const rightUx = resolvePipelineCatalogItemUx(right);
  const leftGroupRank = PIPELINE_OPERATOR_GROUP_RANK.get(leftUx.group) ?? Number.MAX_SAFE_INTEGER;
  const rightGroupRank = PIPELINE_OPERATOR_GROUP_RANK.get(rightUx.group) ?? Number.MAX_SAFE_INTEGER;

  if (leftGroupRank !== rightGroupRank) return leftGroupRank - rightGroupRank;
  if (leftUx.level !== rightUx.level) return leftUx.level === "basic" ? -1 : 1;
  if (leftUx.order !== rightUx.order) return leftUx.order - rightUx.order;

  const nameComparison = pipelineCatalogItemName(left).localeCompare(pipelineCatalogItemName(right));
  if (nameComparison !== 0) return nameComparison;
  return left.id.localeCompare(right.id);
}

export function buildPipelineCatalogItemsForToolbar(
  operatorsById: Record<string, PipelineOperatorDefinition>,
): PipelineCatalogItem[] {
  const operators = Object.values(operatorsById).map(
    (operator): PipelineCatalogItem => ({ kind: "operator", id: operator.id, operator }),
  );
  const recipes = PIPELINE_OPERATOR_RECIPES.filter((recipe) =>
    recipe.steps.every((step) => Boolean(operatorsById[step.operatorId])),
  ).map((recipe): PipelineCatalogItem => ({ kind: "recipe", id: recipe.id, recipe }));
  return [...recipes, ...operators].sort(comparePipelineCatalogItemsByUx);
}

function normalizeAlertParam(value: unknown): string | number | boolean {
  if (Array.isArray(value)) {
    return value
      .map((item) => String(item ?? "").trim())
      .filter(Boolean)
      .join(", ");
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : "";
  if (typeof value === "boolean") return value;
  if (isRecord(value)) {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return String(value ?? "").trim();
}

function alertDetails(alert: PipelineAlert): Record<string, unknown> {
  return isRecord(alert.details) ? alert.details : {};
}

function alertDetailString(details: Record<string, unknown>, key: string): string {
  return String(normalizeAlertParam(details[key]) ?? "").trim();
}

function localizedAlertParams(alert: PipelineAlert, t: TranslationFunction): Record<string, unknown> {
  const details = alertDetails(alert);
  const params: Record<string, unknown> = {
    node_id: alert.node_id ?? "",
    operator_id: alert.operator_id ?? "",
    operator_name: alert.operator_id ? prettyOperatorName(alert.operator_id) : "",
  };

  for (const [key, value] of Object.entries(details)) {
    params[key] = normalizeAlertParam(value);
  }

  const sourceOperatorId = alertDetailString(details, "source_operator_id");
  if (sourceOperatorId) {
    params.source_operator = prettyOperatorName(sourceOperatorId);
  }

  const task = alertDetailString(details, "task");
  if (task) {
    params.task = t(`core.ui.pipelines.alerts.vision.task.${task}`, {}, task);
  }

  if (alert.code === "camera_mapping_camera_not_in_composition") {
    const scopeKind = alertDetailString(details, "scope_kind") || "any";
    const composition = alertDetailString(details, "composition_label");
    const scopeKey = `core.ui.pipelines.alerts.camera_mapping_camera_not_in_composition.scope.${scopeKind}`;
    params.scope = t(
      scopeKey,
      { composition },
      t("core.ui.pipelines.alerts.camera_mapping_camera_not_in_composition.scope.any", {}, "in any composition available to Map position in space"),
    );
  }

  return params;
}

export function localizePipelineAlert(alert: PipelineAlert, t: TranslationFunction = i18n.t): PipelineAlert {
  const code = String(alert.code || "").trim();
  if (!code) return alert;

  const params = localizedAlertParams(alert, t);
  const message = t(`core.ui.pipelines.alerts.${code}.message`, params, "");
  const suggestion = t(`core.ui.pipelines.alerts.${code}.suggestion`, params, "");

  return {
    ...alert,
    message: message || alert.message,
    suggestion: suggestion || alert.suggestion,
  };
}

export function pipelineAlertSeverityLabel(
  severity: PipelineAlert["severity"],
  t: TranslationFunction = i18n.t,
): string {
  return t(`core.ui.pipelines.checks.severity.${severity}`, {}, severity);
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

  if (sourceCaps.has("source")) return { maxsize: 1, drop_policy: "latest_only" };
  if (targetCaps.has("sink") || targetCaps.has("origin_only")) {
    return { maxsize: 128, drop_policy: "drop_oldest" };
  }
  if (sourceCaps.has("split_stream")) {
    return { maxsize: 64, drop_policy: "keyed_latest_only" };
  }
  if (targetCaps.has("heavy_compute")) return { maxsize: 1, drop_policy: "latest_only" };
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
  const capabilities = operatorCapabilities(definition);
  return capabilities.has("gate_control") && !capabilities.has("source");
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
  baseGraph?: unknown,
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

  const graph: Record<string, unknown> = {
    schema_version: 1,
    nodes,
    edges,
  };
  if (isRecord(baseGraph) && isRecord(baseGraph.limits) && Object.keys(baseGraph.limits).length > 0) {
    graph.limits = { ...baseGraph.limits };
  }

  return {
    graph,
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
          "Graph is not compatible with interactive step ordering. Switch to JSON mode before saving to preserve links.",
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
        "Graph is not compatible with interactive step ordering. Switch to JSON mode before saving to preserve links.",
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
        "Graph has multiple starts. Switch to JSON mode before saving to preserve links.",
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
          "Graph contains a cycle. Switch to JSON mode before saving to preserve links.",
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
        "Graph has disconnected segments. Switch to JSON mode before saving to preserve links.",
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
        "Graph has disconnected segments. Switch to JSON mode before saving to preserve links.",
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
