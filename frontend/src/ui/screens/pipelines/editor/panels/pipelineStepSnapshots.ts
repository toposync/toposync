export type PipelineStepSnapshotKey = {
  pipelineName: string;
  nodeId: string;
  sourceId: string;
  filename: "input.png" | "input.jpg";
};

function normalizePipelineName(value: string): string {
  const raw = String(value || "").trim();
  if (raw.endsWith("__processing") && raw.length > "__processing".length) {
    return raw.slice(0, -1 * "__processing".length);
  }
  return raw;
}

function safeComponent(value: string, fallback: string, maxLen: number): string {
  const raw = String(value || "").trim() || fallback;
  const cleaned = raw.replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "");
  const normalized = (cleaned || fallback).slice(0, maxLen);
  return normalized || fallback;
}

export function buildPipelineStepSnapshotRelPath(key: PipelineStepSnapshotKey): string {
  const pipelineSafe = safeComponent(normalizePipelineName(key.pipelineName), "pipeline", 80);
  const nodeSafe = safeComponent(key.nodeId, "node", 80);
  const sourceSafe = safeComponent(key.sourceId, "source", 120);
  const filenameSafe = safeComponent(key.filename, key.filename, 80);
  return ["pipeline_snapshots", "v1", pipelineSafe, nodeSafe, sourceSafe, filenameSafe].join("/");
}

export function buildPipelineStepSnapshotUrl(key: PipelineStepSnapshotKey, nonce?: number): string {
  const relPath = buildPipelineStepSnapshotRelPath(key);
  const query = nonce ? `?t=${encodeURIComponent(String(nonce))}` : "";
  return `/files/${relPath}${query}`;
}

