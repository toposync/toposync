import type { ProcessingServerStatus } from "../util/api";

export type ProcessingRuntimeNodeIssue = {
  pipelineName: string;
  nodeId: string;
  runtimeNodeId: string;
  errorCount: number;
  lastError: string;
  lastErrorAt: number | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function readNumber(value: unknown): number {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : 0;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => readString(item)).filter(Boolean);
}

function readOccurrences(value: unknown): Array<{ pipelineName: string; nodeId: string }> {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!isRecord(item)) return null;
      const pipelineName = readString(item.pipeline_name);
      const nodeId = readString(item.node_id);
      if (!pipelineName && !nodeId) return null;
      return { pipelineName, nodeId };
    })
    .filter(Boolean) as Array<{ pipelineName: string; nodeId: string }>;
}

function inferIsolatedOccurrence(runtimeNodeId: string): { pipelineName: string; nodeId: string } | null {
  const prefix = "isolated_";
  if (!runtimeNodeId.startsWith(prefix)) return null;
  const rest = runtimeNodeId.slice(prefix.length);
  const separator = rest.indexOf("__");
  if (separator <= 0) return null;
  return {
    pipelineName: rest.slice(0, separator),
    nodeId: rest.slice(separator + 2),
  };
}

function occurrenceLabelsForNode(
  runtimeNodeId: string,
  bundleSnapshot: Record<string, unknown>,
  pipelineNames: string[],
): Array<{ pipelineName: string; nodeId: string }> {
  const nodeOccurrences = isRecord(bundleSnapshot.node_occurrences) ? bundleSnapshot.node_occurrences : {};
  const directOccurrences = readOccurrences(nodeOccurrences[runtimeNodeId]);
  if (directOccurrences.length) return directOccurrences;

  const sharedNodes = isRecord(bundleSnapshot.shared_nodes) ? bundleSnapshot.shared_nodes : {};
  const sharedOccurrences = readOccurrences(sharedNodes[runtimeNodeId]);
  if (sharedOccurrences.length) return sharedOccurrences;

  const isolated = inferIsolatedOccurrence(runtimeNodeId);
  if (isolated) return [isolated];

  if (pipelineNames.length === 1) return [{ pipelineName: pipelineNames[0], nodeId: runtimeNodeId }];
  return [{ pipelineName: "", nodeId: runtimeNodeId }];
}

export function extractProcessingRuntimeNodeIssues(status: ProcessingServerStatus | Record<string, unknown> | null | undefined): ProcessingRuntimeNodeIssue[] {
  const rootCandidate: unknown =
    isRecord(status) && isRecord((status as ProcessingServerStatus).status) ? (status as ProcessingServerStatus).status : status;
  if (!isRecord(rootCandidate)) return [];
  const root = rootCandidate;

  const bundleSnapshot = isRecord(root.runtime) ? root.runtime : {};
  const runtimeSnapshot = isRecord(bundleSnapshot.runtime) ? bundleSnapshot.runtime : bundleSnapshot;
  const nodeMetrics = isRecord(runtimeSnapshot.nodes) ? runtimeSnapshot.nodes : {};
  const pipelineNames = readStringArray(root.pipelines).concat(readStringArray(bundleSnapshot.pipelines)).filter((value, index, list) => list.indexOf(value) === index);
  const issues: ProcessingRuntimeNodeIssue[] = [];

  for (const [runtimeNodeId, rawMetrics] of Object.entries(nodeMetrics)) {
    if (!isRecord(rawMetrics)) continue;
    const errorCount = Math.max(0, Math.trunc(readNumber(rawMetrics.error_count)));
    const lastError = readString(rawMetrics.last_error);
    if (errorCount <= 0 && !lastError) continue;
    const lastErrorAtRaw = readNumber(rawMetrics.last_error_at);
    const lastErrorAt = lastErrorAtRaw > 0 ? lastErrorAtRaw : null;
    const occurrences = occurrenceLabelsForNode(runtimeNodeId, bundleSnapshot, pipelineNames);
    for (const occurrence of occurrences) {
      issues.push({
        pipelineName: occurrence.pipelineName,
        nodeId: occurrence.nodeId || runtimeNodeId,
        runtimeNodeId,
        errorCount,
        lastError,
        lastErrorAt,
      });
    }
  }

  return issues.sort((a, b) => (b.lastErrorAt || 0) - (a.lastErrorAt || 0) || b.errorCount - a.errorCount || a.nodeId.localeCompare(b.nodeId));
}

export function filterProcessingRuntimeIssuesForPipeline(
  issues: ProcessingRuntimeNodeIssue[],
  pipelineName: string,
): ProcessingRuntimeNodeIssue[] {
  const target = readString(pipelineName);
  if (!target) return [];
  return issues.filter((issue) => !issue.pipelineName || issue.pipelineName === target);
}
