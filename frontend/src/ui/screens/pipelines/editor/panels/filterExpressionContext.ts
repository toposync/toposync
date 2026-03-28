import type {
  PipelineOperatorDefinition,
  PipelineOperatorExpressionHint,
} from "../../../../../util/api";
import type { InteractiveStep } from "../../types";
import { buildGraphFromInteractiveSteps, isRecord, prettyOperatorName, safeJsonParse } from "../../utils";

export type FilterExpressionPathSuggestion = {
  path: string;
  detail: string;
  valueType: string;
  description: string;
  examples: string[];
  enumValues: string[];
};

export type FilterExpressionUpstreamContext = {
  payloadPathSuggestions: FilterExpressionPathSuggestion[];
  metadataPathSuggestions: FilterExpressionPathSuggestion[];
  artifactNames: string[];
};

function parseStepConfig(step: InteractiveStep): Record<string, unknown> {
  const parsed = safeJsonParse(step.configText || "{}");
  if (!parsed.ok || !isRecord(parsed.data)) return {};
  return parsed.data as Record<string, unknown>;
}

function readString(value: unknown): string {
  return String(value || "").trim();
}

function normalizeStringArray(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of values) {
    const text = readString(item);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function dedupeStrings(values: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const normalized = readString(value);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    out.push(normalized);
  }
  return out;
}

function pushPathSuggestions(
  target: Map<string, FilterExpressionPathSuggestion>,
  suggestions: FilterExpressionPathSuggestion[],
): void {
  for (const suggestion of suggestions) {
    const path = readString(suggestion.path);
    if (!path || target.has(path)) continue;
    target.set(path, suggestion);
  }
}

function expressionHintsForOperator(definition: PipelineOperatorDefinition | null): PipelineOperatorExpressionHint[] {
  return Array.isArray(definition?.expression_hints) ? definition?.expression_hints ?? [] : [];
}

function buildPathSuggestion(
  path: string,
  detail: string,
  hint?: PipelineOperatorExpressionHint | null,
): FilterExpressionPathSuggestion | null {
  const normalizedPath = readString(path);
  if (!normalizedPath) return null;
  return {
    path: normalizedPath,
    detail,
    valueType: readString(hint?.type),
    description: readString(hint?.description),
    examples: normalizeStringArray(hint?.examples),
    enumValues: normalizeStringArray(hint?.enum_values),
  };
}

function pathSuggestionsForOperator(
  operatorId: string,
  definition: PipelineOperatorDefinition | null,
  kind: "payload_path" | "metadata_path",
): FilterExpressionPathSuggestion[] {
  const detail = prettyOperatorName(operatorId);
  const out = new Map<string, FilterExpressionPathSuggestion>();

  for (const hint of expressionHintsForOperator(definition)) {
    if (hint?.kind !== kind) continue;
    const suggestion = buildPathSuggestion(hint.path ?? "", detail, hint);
    if (!suggestion) continue;
    out.set(suggestion.path, suggestion);
  }

  if (kind === "payload_path") {
    const producedKeys = Array.isArray(definition?.produces_payload_keys) ? definition?.produces_payload_keys ?? [] : [];
    for (const rawKey of producedKeys) {
      const key = readString(rawKey);
      if (!key) continue;
      const suggestion = buildPathSuggestion(`payload.${key}`, detail, null);
      if (!suggestion || out.has(suggestion.path)) continue;
      out.set(suggestion.path, suggestion);
    }
  }

  return [...out.values()];
}

function artifactNamesForOperator(
  definition: PipelineOperatorDefinition | null,
  config: Record<string, unknown>,
): string[] {
  const out: string[] = Array.isArray(definition?.produces_artifacts) ? [...(definition?.produces_artifacts ?? [])] : [];

  for (const hint of expressionHintsForOperator(definition)) {
    if (hint?.kind !== "artifact_name") continue;
    const value = readString(hint.value);
    if (value) out.push(value);
  }

  const outputArtifactName = readString((config as any).output_artifact_name);
  if (outputArtifactName) out.push(outputArtifactName);

  return dedupeStrings(out);
}

function fallbackLinearContext(
  steps: InteractiveStep[],
  index: number,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): FilterExpressionUpstreamContext {
  const payloadPaths = new Map<string, FilterExpressionPathSuggestion>();
  const metadataPaths = new Map<string, FilterExpressionPathSuggestion>();
  const artifactNames = new Set<string>();

  for (let currentIndex = 0; currentIndex < index; currentIndex += 1) {
    const step = steps[currentIndex];
    const definition = operatorsById[step.operatorId] ?? null;
    const config = parseStepConfig(step);
    pushPathSuggestions(payloadPaths, pathSuggestionsForOperator(step.operatorId, definition, "payload_path"));
    pushPathSuggestions(metadataPaths, pathSuggestionsForOperator(step.operatorId, definition, "metadata_path"));
    for (const artifactName of artifactNamesForOperator(definition, config)) {
      artifactNames.add(artifactName);
    }
  }

  return {
    payloadPathSuggestions: [...payloadPaths.values()],
    metadataPathSuggestions: [...metadataPaths.values()],
    artifactNames: [...artifactNames].sort(),
  };
}

export function buildFilterExpressionUpstreamContext(
  steps: InteractiveStep[],
  index: number,
  operatorsById: Record<string, PipelineOperatorDefinition>,
): FilterExpressionUpstreamContext {
  const targetStep = steps[index];
  if (!targetStep) {
    return { payloadPathSuggestions: [], metadataPathSuggestions: [], artifactNames: [] };
  }

  const built = buildGraphFromInteractiveSteps(steps, operatorsById);
  if (!built.graph || !isRecord(built.graph)) {
    return fallbackLinearContext(steps, index, operatorsById);
  }

  const rawNodes = Array.isArray((built.graph as any).nodes) ? ((built.graph as any).nodes as unknown[]) : [];
  const rawEdges = Array.isArray((built.graph as any).edges) ? ((built.graph as any).edges as unknown[]) : [];
  const incomingByNodeId = new Map<string, string[]>();

  for (const edgeRaw of rawEdges) {
    if (!isRecord(edgeRaw)) continue;
    const from = isRecord(edgeRaw.from) ? edgeRaw.from : null;
    const to = isRecord(edgeRaw.to) ? edgeRaw.to : null;
    const sourceNodeId = readString(from?.node);
    const targetNodeId = readString(to?.node);
    if (!sourceNodeId || !targetNodeId) continue;
    const items = incomingByNodeId.get(targetNodeId) ?? [];
    items.push(sourceNodeId);
    incomingByNodeId.set(targetNodeId, items);
  }

  const payloadPathsByNodeId = new Map<string, Map<string, FilterExpressionPathSuggestion>>();
  const metadataPathsByNodeId = new Map<string, Map<string, FilterExpressionPathSuggestion>>();
  const artifactNamesByNodeId = new Map<string, Set<string>>();

  for (const nodeRaw of rawNodes) {
    if (!isRecord(nodeRaw)) continue;
    const nodeId = readString(nodeRaw.id);
    const operatorId = readString(nodeRaw.operator);
    if (!nodeId || !operatorId) continue;

    const incomingNodeIds = incomingByNodeId.get(nodeId) ?? [];
    const upstreamPayloadPaths = new Map<string, FilterExpressionPathSuggestion>();
    const upstreamMetadataPaths = new Map<string, FilterExpressionPathSuggestion>();
    const upstreamArtifactNames = new Set<string>();

    for (const sourceNodeId of incomingNodeIds) {
      for (const item of payloadPathsByNodeId.get(sourceNodeId)?.values() ?? []) {
        if (!upstreamPayloadPaths.has(item.path)) upstreamPayloadPaths.set(item.path, item);
      }
      for (const item of metadataPathsByNodeId.get(sourceNodeId)?.values() ?? []) {
        if (!upstreamMetadataPaths.has(item.path)) upstreamMetadataPaths.set(item.path, item);
      }
      for (const artifactName of artifactNamesByNodeId.get(sourceNodeId) ?? []) {
        upstreamArtifactNames.add(artifactName);
      }
    }

    if (nodeId === targetStep.nodeId) {
      return {
        payloadPathSuggestions: [...upstreamPayloadPaths.values()],
        metadataPathSuggestions: [...upstreamMetadataPaths.values()],
        artifactNames: [...upstreamArtifactNames].sort(),
      };
    }

    const nextPayloadPaths = new Map(upstreamPayloadPaths);
    const nextMetadataPaths = new Map(upstreamMetadataPaths);
    const nextArtifactNames = new Set(upstreamArtifactNames);

    const step = steps.find((item) => item.nodeId === nodeId) ?? null;
    const config = step ? parseStepConfig(step) : {};
    const definition = operatorsById[operatorId] ?? null;

    pushPathSuggestions(nextPayloadPaths, pathSuggestionsForOperator(operatorId, definition, "payload_path"));
    pushPathSuggestions(nextMetadataPaths, pathSuggestionsForOperator(operatorId, definition, "metadata_path"));
    for (const artifactName of artifactNamesForOperator(definition, config)) {
      nextArtifactNames.add(artifactName);
    }

    payloadPathsByNodeId.set(nodeId, nextPayloadPaths);
    metadataPathsByNodeId.set(nodeId, nextMetadataPaths);
    artifactNamesByNodeId.set(nodeId, nextArtifactNames);
  }

  return fallbackLinearContext(steps, index, operatorsById);
}
