import {
  getPipelineStorage,
  type PipelineStorageLayer,
  type PipelineStorageSummary,
} from "../../../util/api";

const STORAGE_CACHE_TTL_MS = 8_000;
const GIB = 1024 ** 3;

const storageCache = new Map<string, { summary: PipelineStorageSummary; loadedAt: number }>();

export function bytesToGiBValue(bytes: number | null | undefined): number {
  const value = Number(bytes ?? 0);
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.round((value / GIB) * 100) / 100;
}

export function giBToBytes(value: number | null | undefined): number | null {
  const parsed = Number(value ?? 0);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return Math.max(1, Math.round(parsed * GIB));
}

export function formatStorageBytes(bytes: number | null | undefined): string {
  const value = Math.max(0, Number(bytes ?? 0));
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let current = value;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const digits = current >= 100 || index === 0 ? 0 : current >= 10 ? 1 : 2;
  return `${current.toFixed(digits)} ${units[index]}`;
}

export function formatStorageTime(timestampSeconds: number | null | undefined, locale?: string): string {
  const value = Number(timestampSeconds ?? 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  try {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: "short",
      timeStyle: "short",
    }).format(new Date(value * 1000));
  } catch {
    return new Date(value * 1000).toLocaleString();
  }
}

export async function loadCachedPipelineStorage(
  pipelineName: string,
  options?: { force?: boolean; signal?: AbortSignal },
): Promise<PipelineStorageSummary> {
  const name = String(pipelineName || "").trim();
  if (!name) throw new Error("Pipeline name is required");
  const cached = storageCache.get(name);
  if (!options?.force && cached && Date.now() - cached.loadedAt < STORAGE_CACHE_TTL_MS) {
    return cached.summary;
  }
  const summary = await getPipelineStorage(name, options?.signal);
  storageCache.set(name, { summary, loadedAt: Date.now() });
  return summary;
}

export function updatePipelineStorageCache(summary: PipelineStorageSummary): void {
  const name = String(summary.pipeline_name || "").trim();
  if (!name) return;
  storageCache.set(name, { summary, loadedAt: Date.now() });
}

export function findStorageLayerForNode(
  summary: PipelineStorageSummary | null,
  nodeId: string,
  layerLabel?: string,
): PipelineStorageLayer | null {
  if (!summary) return null;
  const normalizedNodeId = String(nodeId || "").trim();
  const normalizedLayerLabel = String(layerLabel || "").trim().toLowerCase();
  if (!normalizedNodeId) return null;
  const layers = Array.isArray(summary.layers) ? summary.layers : [];
  if (normalizedLayerLabel) {
    const exact = layers.find(
      (layer) =>
        String(layer.node_id || "").trim() === normalizedNodeId &&
        String(layer.layer_label || "").trim().toLowerCase() === normalizedLayerLabel,
    );
    if (exact) return exact;
  }
  return layers.find((layer) => String(layer.node_id || "").trim() === normalizedNodeId) ?? null;
}
